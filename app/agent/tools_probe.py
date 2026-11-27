"""Проверка конкретного допущения и результата в рабочей копии Excel."""
from ..workbook import cell_trace as cells
from ..workbook.periods import norm_period


def _target(s, args):
    if args.get("target"):
        if args.get("metric") or args.get("period"):
            raise ValueError("Укажите адрес результата или показатель и период")
        return cells.cell(s.idx, args["target"]), None
    from ..workbook import manifest

    if not args.get("metric") or not args.get("period"):
        raise ValueError("Укажите адрес результата или показатель и точный период")
    resolved = manifest.resolve(s, args["metric"], kind="metric")
    if resolved["status"] != "resolved":
        refs = ", ".join(m["id"] for m in resolved["candidates"])
        raise ValueError(f"Уточните показатель: {resolved['status']} ({refs})")
    entry = s.catalog["concepts"][resolved["selected"]["catalog_id"]]
    sheet, row = (entry["primary"][k] for k in ("sheet", "row"))
    sh = s.idx.sheet(sheet)
    ri = s.idx.row(sheet, row)
    period = norm_period(args["period"])
    matching = [c for c, y in sh.years_for(row).items() if y == period and
                (c in ri.formulas or cells.numeric(ri.values.get(c)))]
    if len(matching) != 1:
        raise ValueError("Период не соответствует одной ячейке. Укажите точный адрес результата.")
    return (sheet, row, matching[0]), entry


def inspect_cell_stress(s, args, emit):
    """Без записи: адрес результата, цепочка и кандидаты либо точный план воздействия."""
    target, binding = _target(s, args)
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
              "published_binding": args.get("metric"), "method": "input_cell_pct"}
    if binding:
        result["binding_confirmed"] = binding["match"] == "pinned"
        result["binding_alternatives"] = [f"{x['sheet']}!{x['row']}" for x in binding.get("alternatives", [])]
    if not args.get("source"):
        requested_period = args.get("source_period")
        source_period = norm_period(requested_period) if requested_period else None
        result["trace"] = cells.trace(s.idx, target, query=args.get("query") or "", source_period=source_period)
        return result
    source = cells.cell(s.idx, args["source"])
    ri = cells.row_info(s.idx, source)
    if source == target or source[2] in ri.formulas or not cells.numeric(ri.values.get(source[2])):
        raise ValueError("Драйвер должен быть отдельной ячейкой с введённым числом")
    if source[0] in s.idx.inactive:
        raise ValueError("Драйвер находится в неактивном наборе допущений")
    pct = args.get("pct")
    if not cells.numeric(pct) or pct == 0:
        raise ValueError("Укажите ненулевое конечное изменение в процентах")
    src = cells.ref_for(source)
    source_period = cells.period_at(s.idx, source)
    if args.get("source_period") and norm_period(args["source_period"]) != source_period:
        raise ValueError("Выбранная ячейка не соответствует указанному периоду входа")
    s.require_engine()
    if s.engine.name != "excel":
        raise ValueError("Для проверяемого сценария требуется Excel")
    a, b = (block[0][0] for block in s.engine.read([src, dst]))
    if not (cells.numeric(a) and cells.numeric(b)) or a == 0:
        raise ValueError("Нет числовой базы для результата или процентного шока драйвера")
    proposed = a * (1 + pct / 100)
    if not cells.numeric(proposed) or proposed == a:
        raise ValueError("Шок не задаёт нового конечного числового значения")
    result.update(source=src, source_label=ri.label, source_before=a, source_proposed=proposed,
                  source_period=str(source_period) if source_period is not None else None,
                  target_before=b, requested_pct=pct, trace=cells.trace(s.idx, target, source))
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
