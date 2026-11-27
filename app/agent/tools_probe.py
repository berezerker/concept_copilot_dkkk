"""Проверка конкретного допущения и результата в рабочей копии Excel."""
from ..workbook import cell_trace as cells
from ..workbook import manifest, user_overrides
from ..workbook.periods import norm_period


def _target(s, args):
    ref = args.get("target")
    metric = args.get("metric") or args.get("concept")
    requested_period = args.get("period")
    row_ref = cells.row_reference(s.idx, ref) if ref else None
    node = cells.cell(s.idx, ref) if ref and not row_ref else None
    if node is None and (not requested_period or not (row_ref or metric)):
        raise ValueError("Укажите адрес A1 либо точный period вместе с показателем или строкой из паспорта")
    period = norm_period(requested_period) if requested_period else None
    if node is not None and period is not None and cells.period_at(s.idx, node) != period:
        raise ValueError(f"{ref}: адрес результата не соответствует периоду {requested_period}")

    location = node[:2] if node else row_ref
    entry = None
    if metric:
        resolved = manifest.resolve(s, metric, kind="metric")
        if resolved["status"] != "resolved":
            refs = ", ".join(m["id"] for m in resolved["candidates"])
            raise ValueError(f"Уточните показатель: {resolved['status']} ({refs})")
        entry = s.catalog["concepts"][resolved["selected"]["catalog_id"]]
        bound = (entry["primary"]["sheet"], entry["primary"]["row"])
        if location and location != bound:
            raise ValueError(f"{ref}: адрес не соответствует привязке показателя {metric} ({bound[0]}!{bound[1]})")
        location = bound
    return node or cells.cell_for_period(s.idx, location, period), entry


def describe_target(s, target, binding=None):
    dst = cells.ref_for(target)
    row = cells.row_info(s.idx, target)
    period = cells.period_at(s.idx, target)
    from ..workbook.catalog import display_unit
    matches = [entry for entry in s.catalog["concepts"].values()
               if (entry["primary"]["sheet"], entry["primary"]["row"]) == target[:2]]
    kind = "ratio" if any(entry["kind"] == "ratio" for entry in matches) else None
    result = {"file_hash": s.idx.file_hash, "target": dst, "target_label": row.label,
              "target_formula": row.formulas.get(target[2]), "period": str(period) if period is not None else None,
              "target_unit": display_unit(kind, row.unit),
              "published_binding": binding["id"] if binding else None, "method": "input_cell_pct"}
    if binding:
        result["binding_confirmed"] = binding["match"] == "pinned"
        result["binding_alternatives"] = [f"{x['sheet']}!{x['row']}" for x in binding.get("alternatives", [])]
    return result


def resolve_input(s, args):
    source = cells.cell(s.idx, args["source"])
    ri = cells.row_info(s.idx, source)
    if source[2] in ri.formulas or not cells.numeric(ri.values.get(source[2])):
        raise ValueError("Драйвер должен быть отдельной ячейкой с введённым числом")
    if source[0] in s.idx.inactive:
        raise ValueError("Драйвер находится в неактивном наборе допущений")
    pct = args.get("pct")
    if not cells.numeric(pct) or pct == 0:
        raise ValueError("Укажите ненулевое конечное изменение в процентах")
    source_period = cells.period_at(s.idx, source)
    if args.get("source_period") and norm_period(args["source_period"]) != source_period:
        raise ValueError("Выбранная ячейка не соответствует указанному периоду входа")
    return source, {"source": cells.ref_for(source), "source_label": ri.label, "source_unit": ri.unit, "requested_pct": pct,
                    "source_period": str(source_period) if source_period is not None else None}


def proposed_value(a, pct):
    if not cells.numeric(a) or a == 0:
        raise ValueError("Нет ненулевой числовой базы для процентного шока драйвера")
    proposed = a * (1 + pct / 100)
    if not cells.numeric(proposed) or proposed == a:
        raise ValueError("Шок не задаёт нового конечного числового значения")
    return proposed


def require_excel(s):
    s.require_engine()
    if s.engine.name != "excel":
        raise ValueError("Для проверяемого сценария требуется Excel")


def inspect_cell_stress(s, args, emit):
    """Без записи: адрес результата, цепочка и кандидаты либо точный план воздействия."""
    target, binding = _target(s, args)
    result = describe_target(s, target, binding)
    if not args.get("source"):
        requested_period = args.get("source_period")
        source_period = norm_period(requested_period) if requested_period else None
        query = args.get("query") or ""
        binding = user_overrides.parameter_binding(s, query)
        source_row = None
        if binding:
            selected = manifest.resolve(s, binding["parameter_id"], kind="parameter")["selected"]
            if not selected or selected["id"] != binding["parameter_id"] or not selected["active"]:
                raise ValueError("Закреплённый параметр недоступен. Проверьте привязку в обзоре модели.")
            loc = selected["location"]
            source_row = (loc["sheet"], loc["row"])
            result["user_override"] = binding
        result["trace"] = cells.trace(s.idx, target, query="" if binding else query, source_period=source_period,
                                      source_row=source_row)
        return result
    source, detail = resolve_input(s, args)
    if source == target:
        raise ValueError("Драйвер должен отличаться от результата")
    require_excel(s)
    a, b = (block[0][0] for block in s.engine.read([detail["source"], result["target"]]))
    if not cells.numeric(b):
        raise ValueError("Нет числовой базы для результата")
    result.update(detail, source_before=a, source_proposed=proposed_value(a, detail["requested_pct"]),
                  target_before=b, trace=cells.trace(s.idx, target, source))
    return result


def run_cell_stress(s, args, emit):
    from ..workbook.transaction import same_values

    if args.get("file_hash") != s.idx.file_hash:
        raise ValueError("Книга изменилась. Проверьте план сценария заново.")
    plan = inspect_cell_stress(s, args, emit)
    if ("source" not in plan or not cells.numeric(args.get("expected_input"))
            or args["expected_input"] != plan["source_before"]):
        raise ValueError("Допущение изменилось. Проверьте план сценария заново.")
    src, dst = plan["source"], plan["target"]
    refs = [src, dst]
    values, iters = s.engine.scenario(
        [{"ref": src, "values": [[plan["source_proposed"]]],
          "expected_input": [[plan["source_before"]]]}], refs, refs)
    transaction = s.engine.last_transaction
    if transaction.get("status") != "verified" or not transaction.get("restored"):
        raise RuntimeError("Восстановление сценария не подтверждено")
    if not same_values(s.engine.read(refs), [[[plan["source_before"]]], [[plan["target_before"]]]]):
        raise RuntimeError("База между подготовкой и откатом изменилась. Повторите проверку плана.")
    after_input, after_target = (block[0][0] for block in values)
    plan.update(source_after=after_input, achieved_pct=(after_input / plan["source_before"] - 1) * 100,
                target_after=after_target, target_delta=after_target - plan["target_before"],
                rollback_verified=True, iterations=iters)
    emit("cell_stress", plan)
    return plan


_TARGET = {
    "target": {"type": "string", "description": "Точный адрес результата: лист!A1"},
    "metric": {"type": "string", "description": "ID показателя паспорта вместо target"},
    "period": {"type": "string", "description": "Точный период при выборе показателя"},
    "source": {"type": "string", "description": "Точный адрес ячейки с введённым числом"},
    "pct": {"type": "number", "description": "Относительное изменение драйвера в процентах"},
    "query": {"type": "string", "description": "Фильтр кандидатов по фрагменту подписи"},
    "source_period": {"type": "string", "description": "Фильтр кандидатов по точному периоду входа"},
}
TOOLS_PROBE = [
    {"name": "inspect_cell_stress", "fn": inspect_cell_stress,
     "description": "Показать явные цепочки формул до ячейки результата. Без source возвращает кандидатов-допущения, "
                    "с source и pct — план изменения одного конкретного входа и его базу. Excel не меняется.",
     "parameters": {"type": "object", "properties": _TARGET}},
    {"name": "run_cell_stress", "fn": run_cell_stress,
     "description": "Выполнить проверенный план одного входа из inspect_cell_stress с точными source, target, pct, "
                    "file_hash и expected_input=source_before. Вернуть фактический шок, результат и подтверждение отката.",
     "parameters": {"type": "object", "properties": {**_TARGET, "file_hash": {"type": "string"},
                                                      "expected_input": {"type": "number"}},
                    "required": ["target", "source", "pct", "file_hash", "expected_input"]}},
]
