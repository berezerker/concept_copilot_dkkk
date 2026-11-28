"""Разрешение целей, подготовка и выполнение проверяемого Excel-сценария."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from ..workbook import cell_trace as cells
from ..workbook.transaction import same_values, validate_overrides
from .tools_probe import _target, describe_target, proposed_value, require_excel, resolve_input_selector


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
    resolved = []
    for i, item in enumerate(metrics, start=1):
        try:
            resolved.append(_target(s, item))
        except (KeyError, ValueError) as e:
            raise ValueError(f"Результат {i} (metrics) — {e}") from e
    inputs = [resolve_input_selector(s, shock) for shock in shocks]
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
    plans[plan["plan_id"]] = (deepcopy(plan), deepcopy(metrics), deepcopy(shocks), refs, baseline, overrides)
    emit("progress", {"text": "Предпроверка Excel завершена. Адреса и исходные значения зафиксированы."})
    return plan


def execute_excel_scenario(s, args, emit):
    """Выполняет точные изменения из плана. Каждый расчёт завершается проверкой общей базы."""
    require_excel(s)
    saved = s.scenario_plans.get(args.get("plan_id"))
    if not saved:
        raise ValueError("План сценария отсутствует или устарел. Подготовьте его заново.")
    plan, metric_selectors, input_selectors, refs, baseline, overrides = saved
    if plan["file_hash"] != s.idx.file_hash:
        raise ValueError("Книга изменилась. Подготовьте сценарий заново.")
    current_targets = [cells.ref_for(_target(s, item)[0]) for item in metric_selectors]
    current_sources = [resolve_input_selector(s, item)[1]["source"] for item in input_selectors]
    if (current_targets != [m["target"] for m in plan["metrics"]]
            or current_sources != [item["source"] for item in plan["inputs"]]):
        raise ValueError("Книга или привязка изменились. Подготовьте сценарий заново.")

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
    separate_trials = individual or ([result] if len(overrides) == 1 else [])
    zero_effect = [trial["inputs"][0]["ref"] for trial in separate_trials
                   if all(metric["delta"] == 0 for metric in trial["metrics"])]
    verified = {"plan_id": plan["plan_id"], "file_hash": plan["file_hash"], "phase": "verified",
                "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "rollback_verified": True}
    if zero_effect:
        verified.update(zero_effect_inputs=zero_effect,
                        note="Эти отдельные шоки не изменили выбранные результаты в Excel. "
                             "При неоднозначной привязке проверь другие подходящие входы.")
    verified.update(
        combined=result,
        individual=individual,
        inputs=[{k: v for k, v in item.items() if k != "paths"} for item in plan["inputs"]],
        metrics=[{k: v for k, v in item.items() if k != "target_formula"} for item in plan["metrics"]],
    )
    history = s.verified_scenarios
    if len(history) >= 20:
        del history[next(iter(history))]
    history[plan["plan_id"]] = deepcopy(verified)
    return verified


def get_verified_excel_scenario(s, args, emit):
    """История подтверждённых результатов. Наличие записи не подтверждает живую базу."""
    plan_id = args.get("plan_id")
    history = s.verified_scenarios
    if plan_id:
        result = history.get(plan_id)
        if not result:
            raise ValueError("Сценарий не найден в истории текущей загрузки")
        return {**deepcopy(result), "historical": True, "recalculated": False}
    return {"historical": True, "scenarios": [
        {"plan_id": key, "verified_at": item["verified_at"],
         "inputs": [v["source"] for v in item["inputs"]],
         "metrics": [v["target"] for v in item["metrics"]]}
        for key, item in reversed(history.items())
    ]}


def format_verified_excel_scenario(result):
    """Отчёт для пользователя из измеренных значений и подтверждённого отката."""
    def label(text, fallback):
        return " ".join(str(text or fallback).split()).replace("|", "¦")

    inputs = {item["source"]: item for item in result["inputs"]}
    metrics = {item["target"]: item for item in result["metrics"]}
    lines = [
        "### Результаты Excel",
        "",
        "| Показатель | Период | База | После совместного шока | Изменение |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for observed in result["combined"]["metrics"]:
        metric = metrics[observed["ref"]]
        name = label(metric["target_label"], observed["ref"])
        period = metric["period"] or "—"
        unit = label(metric["target_unit"], "")
        if unit:
            name = f"{name} ({unit})"
        lines.append(f"| {name} | {period} | {metric['before']:.10g} | "
                     f"{observed['after']:.10g} | {observed['delta']:+.10g} |")

    lines += ["", "| Допущение | Период | База | После шока | Фактический шок |", "| --- | --- | ---: | ---: | ---: |"]
    for observed in result["combined"]["inputs"]:
        item = inputs[observed["ref"]]
        name = label(item["source_label"], observed["ref"])
        lines.append(f"| {name} | {item['source_period'] or '—'} | {item['before']:.10g} | "
                     f"{observed['after']:.10g} | {observed['achieved_pct']:+.10g}% |")
    if result["individual"]:
        lines += ["", "| Допущение | Период входа | Показатель | Период результата | Изменение от отдельного шока |",
                  "| --- | --- | --- | --- | ---: |"]
        for trial in result["individual"]:
            ref = trial["inputs"][0]["ref"]
            item = inputs[ref]
            for observed in trial["metrics"]:
                metric = metrics[observed["ref"]]
                lines.append(f"| {label(item['source_label'], ref)} | {item['source_period'] or '—'} | "
                             f"{label(metric['target_label'], observed['ref'])} | {metric['period'] or '—'} | "
                             f"{observed['delta']:+.10g} |")
    refs = ", ".join(f"`{ref}`" if "`" not in ref else ref for ref in [*inputs, *metrics])
    lines += ["", "Откат Excel подтверждён.", "", f"Где в модели: {refs}."]
    return "\n".join(lines)


TOOLS_SCENARIO = [
    {"name": "prepare_excel_scenario", "fn": prepare_excel_scenario,
     "description": "Подготовить совместный шок до 8 входов. Принимает ячейки либо параметры из паспорта с периодами. "
                    "Возвращает plan_id, точные адреса, базу и пути формул. Книга не меняется.",
     "parameters": {"type": "object", "properties": {
         "shocks": {"type": "array", "items": {"type": "object", "properties": {
             "source": {"type": "string", "description": "Адрес лист!A1 либо строка лист!номер с source_period"},
             "parameter": {"type": "string", "description": "ID или имя параметра паспорта вместо source"},
             "source_period": {"type": "string", "description": "Точный период входа, обязателен с parameter"},
             "pct": {"type": "number", "description": "Относительный шок в процентах, не процентных пунктах"},
         }, "required": ["pct"]}},
         "metrics": {"type": "array", "description": "По одному объекту на каждый результат и период. "
                                                     'Например, {"metric":"metric:dscr","period":"2026"} '
                                                     'или {"target":"Лист!34","period":"2026"}.',
                     "items": {"type": "object", "properties": {
             "target": {"type": "string", "description": "Адрес лист!A1 либо строка лист!номер с period"},
             "metric": {"type": "string", "description": "ID показателя паспорта"},
             "concept": {"type": "string", "description": "Совместимый псевдоним для metric"},
             "period": {"type": "string", "description": "Точный период результата, например 2026"},
         }, "anyOf": [{"required": ["target"]}, {"required": ["metric", "period"]},
                      {"required": ["concept", "period"]}]}},
         "experiment_reason": {"type": "string", "description": "Цель пробного пересчёта, если карта не показывает путь"},
     }, "required": ["shocks", "metrics"]}},
    {"name": "execute_excel_scenario", "fn": execute_excel_scenario,
     "description": "Выполнить проверенный план из prepare_excel_scenario и вернуть измеренный эффект. "
                    "Входы и база сверяются до записи, результат выдаётся после подтверждения отката.",
     "parameters": {"type": "object", "properties": {
         "plan_id": {"type": "string"},
         "compare_individual": {"type": "boolean", "description": "Сначала измерить эффект каждого входа отдельно"},
     }, "required": ["plan_id"]}},
    {"name": "get_verified_excel_scenario", "fn": get_verified_excel_scenario,
     "description": "Найти подтверждённые результаты текущей загрузки без нового пересчёта. С plan_id читает "
                    "конкретный результат и измеренные эффекты. Это исторический снимок, он не подтверждает текущую базу.",
     "parameters": {"type": "object", "properties": {"plan_id": {"type": "string"}}}},
]
