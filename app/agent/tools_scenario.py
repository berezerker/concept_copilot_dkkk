"""Разрешение целей, подготовка и выполнение проверяемого Excel-сценария."""
from copy import deepcopy
from uuid import uuid4

from ..workbook import cell_trace as cells
from ..workbook.transaction import same_values, validate_overrides
from .tools_probe import _target, describe_target, proposed_value, require_excel, resolve_input


LIMIT = 8


def _baseline(s, refs):
    values = s.engine.read(refs)
    if len(values) != len(refs) or any(len(b) != 1 or len(b[0]) != 1 or
                                       not cells.numeric(b[0][0]) for b in values):
        raise ValueError("Для всех входов и результатов нужны конечные числовые значения Excel")
    return dict(zip(refs, values))


def prepare_excel_scenario(s, args, emit):
    """Разрешает весь сценарий и возвращает проверенный план. Записей в книгу нет."""
    shocks, metrics = args.get("shocks"), args.get("metrics")
    if not all(isinstance(items, list) and 1 <= len(items) <= LIMIT and
               all(isinstance(item, dict) for item in items) for items in (shocks, metrics)):
        raise ValueError(f"Нужно от 1 до {LIMIT} входов и столько же контрольных ячеек")
    require_excel(s)
    resolved = [_target(s, item) for item in metrics]
    inputs = [resolve_input(s, shock) for shock in shocks]
    outputs = [describe_target(s, *item) for item in resolved]
    source_refs = [detail["source"] for _, detail in inputs]
    target_refs = [detail["target"] for detail in outputs]
    refs = source_refs + target_refs
    if len(set(refs)) != len(refs):
        raise ValueError("Адреса входов и результатов должны быть уникальны и не пересекаться")
    reason = str(args.get("experiment_reason") or "").strip()
    baseline = _baseline(s, refs)
    overrides, changes = [], []
    for node, detail in inputs:
        ref = detail["source"]
        before = baseline[ref][0][0]
        proposed = proposed_value(before, detail["requested_pct"])
        paths = [{"target": out["target"], **cells.trace(s.idx, target, node)}
                 for (target, _), out in zip(resolved, outputs)]
        if not reason and not any(path["status"] == "found" for path in paths):
            raise ValueError(f"{ref}: путь до выбранных результатов не найден. Для пробного пересчёта укажите его цель.")
        changes.append({**detail, "before": before, "proposed": proposed, "paths": paths})
        overrides.append({"ref": ref, "values": [[proposed]], "expected_input": baseline[ref]})
    validate_overrides(overrides)
    plan = {"plan_id": uuid4().hex, "file_hash": s.idx.file_hash, "phase": "preflight",
            "inputs": changes, "metrics": [{**out, "before": baseline[out["target"]][0][0]} for out in outputs],
            "experiment_reason": reason or None}
    plans = s.scenario_plans
    if len(plans) >= LIMIT:
        del plans[next(iter(plans))]
    plans[plan["plan_id"]] = (deepcopy(plan), deepcopy(metrics), refs, baseline, overrides)
    emit("progress", {"text": "Предпроверка Excel завершена. Адреса и исходные значения зафиксированы."})
    return plan


def execute_excel_scenario(s, args, emit):
    """Выполняет точные изменения из плана. Каждый расчёт завершается проверкой общей базы."""
    require_excel(s)
    saved = s.scenario_plans.get(args.get("plan_id"))
    if not saved:
        raise ValueError("План сценария отсутствует или устарел. Подготовьте его заново.")
    plan, selectors, refs, baseline, overrides = saved
    current_targets = [cells.ref_for(_target(s, item)[0]) for item in selectors]
    if plan["file_hash"] != s.idx.file_hash or current_targets != [m["target"] for m in plan["metrics"]]:
        raise ValueError("Книга или привязка изменились. Подготовьте сценарий заново.")
    for item in plan["inputs"]:
        resolve_input(s, {"source": item["source"], "pct": item["requested_pct"]})

    def recalc(changes):
        values, iterations = s.engine.scenario(changes, refs, refs, expected_watch=baseline)
        transaction = s.engine.transactions[-1]
        if transaction["status"] != "verified" or not transaction.get("restored"):
            raise RuntimeError("Восстановление сценария не подтверждено")
        actual = dict(zip(refs, values))
        applied = {ch["ref"]: ch["values"] for ch in changes}
        if any(not same_values(actual[ref], applied.get(ref, baseline[ref]), tol=0)
               for ref in refs[:len(overrides)]):
            raise RuntimeError("Фактические изменения входов отличаются от проверенного плана")
        return {
            "inputs": [{"ref": item["source"], "after": actual[item["source"]][0][0],
                        "achieved_pct": (actual[item["source"]][0][0] / item["before"] - 1) * 100}
                       for item in plan["inputs"] if item["source"] in applied],
            "metrics": [{"ref": m["target"], "after": actual[m["target"]][0][0],
                         "delta": actual[m["target"]][0][0] - m["before"]} for m in plan["metrics"]],
            "transaction": transaction, "iterations": iterations,
        }

    individual = []
    if args.get("compare_individual") and len(overrides) > 1:
        for change in overrides:
            emit("progress", {"text": f"Отдельный пересчёт Excel: {change['ref']}"})
            individual.append(recalc([change]))
    emit("progress", {"text": "Совместный пересчёт Excel и проверка отката…"})
    result = recalc(overrides)
    restored = _baseline(s, refs)
    if any(not same_values(restored[ref], baseline[ref], tol=0 if ref in refs[:len(overrides)] else 1e-6)
           for ref in refs):
        raise RuntimeError("База после завершения серии отличается от плана")
    s.scenario_plans.pop(plan["plan_id"], None)
    return {"plan_id": plan["plan_id"], "file_hash": plan["file_hash"], "phase": "verified",
            "rollback_verified": True, "combined": result, "individual": individual,
            "inputs": [{k: v for k, v in item.items() if k != "paths"} for item in plan["inputs"]],
            "metrics": [{k: v for k, v in item.items() if k != "target_formula"} for item in plan["metrics"]]}


TOOLS_SCENARIO = [
    {"name": "prepare_excel_scenario", "fn": prepare_excel_scenario,
     "description": "Разрешить и проверить до 8 точных входов и результатов. Возвращает plan_id, адреса, периоды, "
                    "значения до записи и пути по формулам. При неоднозначности уточни выбор. Книга не меняется.",
     "parameters": {"type": "object", "properties": {
         "shocks": {"type": "array", "items": {"type": "object", "properties": {
             "source": {"type": "string", "description": "Адрес введённого числа: лист!A1"},
             "source_period": {"type": "string", "description": "Точный период входа, если задан"},
             "pct": {"type": "number", "description": "Относительный шок в процентах, не процентных пунктах"},
         }, "required": ["source", "pct"]}},
         "metrics": {"type": "array", "items": {"type": "object", "properties": {
             "target": {"type": "string", "description": "Адрес результата: лист!A1"},
             "metric": {"type": "string", "description": "ID показателя паспорта вместо target"},
             "period": {"type": "string", "description": "Точный период при выборе metric"},
         }}},
         "experiment_reason": {"type": "string", "description": "Цель пробного пересчёта, если карта не показывает путь"},
     }, "required": ["shocks", "metrics"]}},
    {"name": "execute_excel_scenario", "fn": execute_excel_scenario,
     "description": "Выполнить проверенный план из prepare_excel_scenario и вернуть измеренный эффект. "
                    "Входы и база сверяются до записи, результат выдаётся после подтверждения отката.",
     "parameters": {"type": "object", "properties": {
         "plan_id": {"type": "string"},
         "compare_individual": {"type": "boolean", "description": "Сначала измерить эффект каждого входа отдельно"},
     }, "required": ["plan_id"]}},
]
