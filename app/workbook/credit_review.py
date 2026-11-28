"""Проверка модели по профилям и подтверждённым условиям сделки."""
from collections import Counter, deque
from math import isfinite
import re

from . import cell_trace as cells
from . import credit_review_catalog as catalog
from .concepts import BY_ID
from .periods import FREQ_N, Period, norm_period, year_of


FIELDS = {key: (name, re.compile(pattern, re.I)) for key, (name, pattern) in catalog.FIELDS.items()}
EXTERNAL_FORMULA = re.compile(r"\[(?:\d+|[^\]]+\.xls[xmb]?)\][^!]*!", re.I)


def _rows(s):
    """Признаки должны происходить из числовых строк книги."""
    cache_key = (s.idx.file_hash, s.forecast_start, tuple(sorted(s.idx.inactive)))
    cached = s.credit_review_rows
    if cached and cached[0] == cache_key:
        return cached[1]
    matches = {key: [] for key in FIELDS}
    for name, sheet in s.idx.sheets.items():
        if sheet.role in ("contents", "separator"):
            continue
        for row, info in sheet.rows.items():
            if not info.label or row in sheet.headers:
                continue
            label = f"{info.label} {info.unit}".lower().replace("ё", "е")
            keys = [key for key, (_, rx) in FIELDS.items() if rx.search(label)]
            if not keys:
                continue
            numeric = {col for col, value in info.values.items()
                       if col > info.label_col and cells.numeric(value)}
            formulas = {col for col in info.formulas if col > info.label_col}
            if not numeric and not formulas:
                continue
            years = sheet.years_for(row)
            forecast = {col: period for col, period in years.items() if col in numeric | formulas
                        and (s.forecast_start is None or period >= s.forecast_start)}
            inputs = [{"ref": cells.ref_for((name, row, col)), "period": str(period)}
                      for col, period in sorted(forecast.items()) if col in numeric and col not in formulas]
            data = {
                "sheet": name, "row": row, "ref": f"{name}!{row}", "label": info.label, "unit": info.unit,
                "active": name not in s.idx.inactive, "forecast": bool(forecast),
                "periods_known": bool(years), "inputs": inputs[:5],
            }
            for key in keys:
                matches[key].append(data)
    s.credit_review_rows = (cache_key, matches)
    return matches


def _upstream(s, target):
    """Покрытие графа строк. Точный путь по ячейкам проверяется отдельно."""
    seen = set()
    pending = deque([target])
    while pending and len(seen) < 10000:
        node = pending.popleft()
        if node in seen or node[0] in s.idx.inactive:
            continue
        seen.add(node)
        pending.extend(parent for parent in s.idx.precedents.get(node, ()) if parent not in seen)
    sheets = {sheet for sheet, _ in seen if sheet in s.idx.sheets}
    return seen, bool(pending) or any(s.idx.sheets[sheet].dynamic for sheet in sheets)


def _metric_row(metric):
    binding = metric["bindings"][0]
    return binding["sheet"], binding["row"]


def _unavailable_sources(s, metric):
    sheet, row = _metric_row(metric)
    info = s.idx.row(sheet, row)
    if s.forecast_start is None:
        return []
    span = [(col, period) for col, period in s.idx.sheet(sheet).years_for(row).items()
            if period >= s.forecast_start and (col in info.formulas or cells.numeric(info.values.get(col)))]
    constants = [col for col, _ in span if col not in info.formulas]
    external = [col for col, _ in span if EXTERNAL_FORMULA.search(info.formulas.get(col) or "")]
    if not external and (not span or len(constants) != len(span)):
        return []
    return [{"ref": cells.ref_for((sheet, row, col)), "period": str(period),
             "kind": "external" if col in external else "constant"}
            for col, period in span if col in external or col in constants][:4]


def _scope(s, matches):
    selected = s.credit_review_profiles
    profiles = []
    for key, (name, group, features) in catalog.PROFILES.items():
        found = [[row for row in matches[feature] if row["active"] and row["forecast"]] for feature in features]
        has_profile_evidence = (s.forecast_start is not None and all(found)
                                and len({(r["sheet"], r["row"]) for rows in found for r in rows}) >= len(features))
        included = key in selected if selected is not None else has_profile_evidence
        profiles.append({
            "id": key, "name": name, "group": group, "included": included,
            "status": "confirmed" if included and selected is not None else "suggested" if included else "available",
            "evidence": [rows[0] for rows in found if rows],
        })
    return profiles


def _observations(s):
    measured = {}
    for run in reversed(tuple(s.verified_scenarios.values())):
        trials = run["individual"] if len(run["inputs"]) > 1 else [run["combined"]]
        for trial in trials:
            if not trial["transaction"].get("restored") or len(trial["inputs"]) != 1:
                continue
            source = trial["inputs"][0]["ref"]
            input_row = cells.cell(s.idx, source)[:2]
            for item in trial["metrics"]:
                target_row = cells.cell(s.idx, item["ref"])[:2]
                measured.setdefault((input_row, target_row), {
                    "plan_id": run["plan_id"], "source": source, "target": item["ref"], "delta": item["delta"],
                })
    return measured


def _check_rows(s, matches, profiles, metrics, incomplete):
    included = {profile["id"] for profile in profiles if profile["included"]}
    upstream, sources = {}, {}
    measured = _observations(s)
    checks = []
    for check_id, (profile, title, fields, targets, source) in catalog.CHECKS.items():
        if profile is not None and profile not in included:
            continue
        goals = [metrics[target] for target in targets if target in metrics]
        parents = set()
        for target in goals:
            key = _metric_row(target)
            if key not in upstream:
                upstream[key] = _upstream(s, key)
                sources[key] = _unavailable_sources(s, target)
            parents.update(upstream[key][0])
        inputs = []
        for field in fields:
            rows = matches[field]
            candidates = sorted(rows, key=lambda row: (
                not row["active"], not row["forecast"], (row["sheet"], row["row"]) not in parents,
            ))[:4]
            evidence = []
            for row in candidates:
                key = (row["sheet"], row["row"])
                evidence.append({
                    **row, "linked": key in parents and row["active"],
                    "measured": [measured[(key, _metric_row(target))] for target in goals
                                 if (key, _metric_row(target)) in measured],
                })
            inputs.append({"name": FIELDS[field][0], "evidence": evidence})
        usable = [[item for item in field["evidence"] if item["active"] and item["forecast"]] for field in inputs]
        is_conditional = check_id in catalog.CONDITIONAL_CHECKS
        decision = s.credit_review_decisions.get(check_id)
        map_incomplete = incomplete or any(upstream[_metric_row(goal)][1] for goal in goals)
        missing_sources = [item for goal in goals for item in sources[_metric_row(goal)]]
        if decision and decision["applies"] is False:
            status = "not_applicable"
        elif all(usable):
            status = "mapped" if all(any(r["linked"] for r in rows) for rows in usable) else "found"
        elif any(field["evidence"] and all(not row["active"] for row in field["evidence"]) for field in inputs):
            status = "inactive_only"
        elif is_conditional and not decision:
            status = "confirm_applicability"
        elif missing_sources:
            status = "source_detail_unavailable"
        elif map_incomplete or s.forecast_start is None:
            status = "limited"
        else:
            status = "review"
        source_name, source_url = catalog.SOURCES[source]
        checks.append({"id": check_id, "profile": profile, "title": title, "status": status, "fields": inputs,
                       "conditional": is_conditional, "decision": decision,
                       "source_gaps": missing_sources,
                       "targets": [{"id": m["id"], "name": m["name"], "periods": m["periods"]} for m in goals],
                       "source": {"name": source_name, "url": source_url}})
    return checks


def _period_cells(s, sheet, row, forecast=True):
    info = s.idx.row(sheet, row)
    values = {}
    duplicates = set()
    for col, period in s.idx.sheet(sheet).years_for(row).items():
        if forecast and (s.forecast_start is None or period < s.forecast_start):
            continue
        value = info.values.get(col)
        if not cells.numeric(value) and col not in info.formulas:
            continue
        key = str(period)
        if key in values:
            duplicates.add(key)
        values[key] = {"ref": cells.ref_for((sheet, row, col)), "period": key,
                       "value": value if cells.numeric(value) else None}
    def position(key):
        p = norm_period(key)
        return year_of(p), p.sub / FREQ_N[p.freq] if isinstance(p, Period) else 1, key

    return {key: values[key] for key in sorted(values, key=position) if key not in duplicates}


def _baseline(s, metrics):
    out = []
    for key in ("cash", "dscr", "net_debt_ebitda"):
        metric = metrics.get(key)
        if not metric:
            continue
        sheet, row = _metric_row(metric)
        values = [v for v in _period_cells(s, sheet, row).values() if v["value"] is not None]
        period = (max if key == "net_debt_ebitda" else min)(values, key=lambda item: item["value"], default=None)
        if period:
            out.append({"id": metric["id"], "name": metric["name"], "unit": metric["location"]["unit"],
                        "binding_confirmed": metric["binding_confirmed"], **period})
    return out


def _reconciliation_templates(s, metrics):
    templates = []
    for key, (name, concepts, _) in catalog.RECONCILIATIONS.items():
        sources = [_period_cells(s, *_metric_row(metrics[c])) if c in metrics else {} for c in concepts]
        periods = {period: [source[period] for source in sources] for period in sources[0]
                   if all(period in source for source in sources[1:])}
        templates.append({"id": key, "name": name, "labels": [BY_ID[c]["name"] for c in concepts],
                          "periods": periods})
    return templates


def inspect(s):
    """Читает индекс, карту и журнал. Пересчёт не запускается."""
    manifest = s.manifest or s.refresh_manifest()
    metrics = {m["catalog_id"]: m for m in manifest["metrics"]}
    rows = _rows(s)
    profiles = _scope(s, rows)
    incomplete = any(ex["code"] == "incomplete_static_map" for ex in manifest["limitations"])
    checks = _check_rows(s, rows, profiles, metrics, incomplete)
    covenants = []
    for metric in metrics.values():
        if metric["metric_kind"] != "ratio":
            continue
        bindings = []
        for row in metric["bindings"]:
            periods = list(_period_cells(s, row["sheet"], row["row"], forecast=False))
            if periods:
                bindings.append({"sheet": row["sheet"], "row": row["row"], "label": row["label"],
                                 "unit": row["unit"], "periods": periods, "ref": f"{row['sheet']}!{row['row']}"})
        if bindings:
            covenants.append({"id": metric["catalog_id"], "name": metric["name"], "bindings": bindings})
    return {"version": catalog.VERSION, "model": manifest["model"], "readiness": manifest["readiness"],
            "scenario_sets": manifest["scenario_sets"], "profiles": profiles, "checks": checks,
            "summary": dict(Counter(check["status"] for check in checks)),
            "baseline": _baseline(s, metrics), "covenants": covenants,
            "reconciliations": _reconciliation_templates(s, metrics),
            "confirmed_covenant": s.credit_covenant}


def set_profiles(s, ids):
    if ids is not None and any(key not in catalog.PROFILES for key in ids):
        raise ValueError("Выберите профили из справочника проверок")
    s.credit_review_profiles = set(ids) if ids is not None else None


def set_applicability(s, check_id, applies, source):
    if check_id not in catalog.CHECKS:
        raise ValueError("Проверка не найдена в справочнике")
    if applies is None:
        s.credit_review_decisions.pop(check_id, None)
    elif not source.strip():
        raise ValueError("Укажите источник решения о применимости")
    else:
        s.credit_review_decisions[check_id] = {"applies": applies, "source": source.strip()}


def confirm_covenant(s, rule):
    """Сохраняет явно выбранное определение показателя, порог и область проверки."""
    manifest = s.manifest or s.refresh_manifest()
    metric = next((m for m in manifest["metrics"] if m["catalog_id"] == rule["metric"]
                   and m["metric_kind"] == "ratio"), None)
    if not metric or not any((b["sheet"], b["row"]) == (rule["sheet"], rule["row"]) for b in metric["bindings"]):
        raise ValueError("Выберите строку показателя из паспорта модели")
    if not rule["source"].strip() or not isfinite(rule["threshold"]) or rule["op"] not in (">=", "<="):
        raise ValueError("Укажите сравнение, конечный числовой порог и источник правила")
    periods = set(rule["periods"])
    values = _period_cells(s, rule["sheet"], rule["row"], forecast=False)
    if not periods or not periods.issubset(values):
        raise ValueError("Выберите доступные периоды проверки по определению из договора")
    if len({getattr(norm_period(key), "freq", "Y") for key in periods}) != 1:
        raise ValueError("Выберите периоды одной частоты, указанной в условиях ковенанта")
    s.credit_covenant = {**rule, "name": metric["name"], "source": rule["source"].strip(),
                         "periods": [key for key in values if key in periods],
                         "targets": [values[key]["ref"] for key in values if key in periods]}
    return s.credit_covenant


def covenant_result(s, plan_id=None):
    """Сравнение только по явно заданному источнику и проверенным значениям."""
    rule = s.credit_covenant
    if not rule:
        return {"status": "confirmation_required"}
    manifest = s.manifest or s.refresh_manifest()
    metric = next((m for m in manifest["metrics"] if m["catalog_id"] == rule["metric"]), None)
    if not metric or not any((b["sheet"], b["row"]) == (rule["sheet"], rule["row"]) for b in metric["bindings"]):
        raise ValueError("Привязка показателя изменилась. Подтвердите определение ковенанта заново.")
    refs = rule["targets"]
    run = s.verified_scenarios.get(plan_id) if plan_id else None
    if plan_id and (not run or not run["rollback_verified"]):
        raise ValueError("Подтверждённый расчёт для этой книги отсутствует.")
    if run:
        plan_values = {m["target"]: m["before"] for m in run["metrics"]}
        calculated = {v["ref"]: v["after"] for v in run["combined"]["metrics"]}
        if any(ref not in plan_values or ref not in calculated for ref in refs):
            raise ValueError("Сценарий не охватывает все периоды и ячейки подтверждённого ковенанта.")
        base = [plan_values[ref] for ref in refs]
        stressed = [calculated[ref] for ref in refs]
    else:
        s.require_engine()
        if s.engine.name != "excel":
            raise ValueError("Для проверки базы требуется Microsoft Excel.")
        base = [block[0][0] for block in s.engine.read(refs)]
        stressed = [None] * len(base)
    if len(base) != len(refs) or any(not cells.numeric(value) for value in base):
        raise ValueError("Для выбранных периодов Excel не вернул числовую базу.")
    direction = 1 if rule["op"] == ">=" else -1
    series = []
    for period, ref, before, after in zip(rule["periods"], refs, base, stressed):
        gap = direction * ((after if after is not None else before) - rule["threshold"])
        series.append({"period": period, "ref": ref, "before": before, "after": after, "headroom": gap})
    breached = next((item for item in series if item["headroom"] < 0), None)
    return {"status": "breached" if breached else "met", "first_breach": breached,
            "minimum_headroom": min(series, key=lambda item: item["headroom"]), "series": series,
            "rule": rule, "plan_id": plan_id}


def reconcile(s, args):
    """Независимая сверка арифметики выбранного определения по живым ячейкам."""
    definition = catalog.RECONCILIATIONS.get(args.get("definition"))
    refs = args.get("refs") or []
    period = norm_period(args.get("period"))
    sign, tolerance = args.get("sign"), args.get("tolerance")
    source = str(args.get("source") or "").strip()
    if not definition or len(refs) != 3 or len(set(refs)) != 3 or period is None:
        raise ValueError("Укажите определение, точный период и три различные ячейки.")
    if sign not in (-1, 1) or not cells.numeric(tolerance) or tolerance < 0 or not source:
        raise ValueError("Укажите знак третьей статьи, допуск и источник определения.")
    if args.get("comparable_basis") is not True:
        raise ValueError("Подтвердите сопоставимость ячеек по валюте, масштабу и периметру.")
    nodes = [cells.cell(s.idx, ref) for ref in refs]
    if len(set(nodes)) != 3:
        raise ValueError("Для сверки нужны три различные ячейки.")
    if any(node[0] in s.idx.inactive or str(cells.period_at(s.idx, node)) != str(period) for node in nodes):
        raise ValueError("Все ячейки должны относиться к выбранному периоду активной модели.")
    s.require_engine()
    if s.engine.name != "excel":
        raise ValueError("Для сверки требуется Microsoft Excel.")
    readings = [block[0][0] for block in s.engine.read([cells.ref_for(node) for node in nodes])]
    if len(readings) != 3 or any(not cells.numeric(number) for number in readings):
        raise ValueError("Excel не вернул три числовых значения.")
    actual, first, second = readings
    adjusted = second * sign
    if definition[2] == "ratio" and adjusted <= 0:
        raise ValueError("После учёта знака обслуживание долга должно быть положительным.")
    calculated = first + adjusted if definition[2] == "sum" else first / adjusted
    delta = actual - calculated
    if not cells.numeric(calculated) or not cells.numeric(delta):
        raise ValueError("Сверка дала неконечное значение.")
    return {"status": "matched" if abs(delta) <= tolerance else "difference",
            "definition": definition[0], "period": str(period), "source": source, "sign": sign,
            "tolerance": tolerance, "actual": actual, "calculated": calculated, "difference": delta,
            "evidence": [{"ref": cells.ref_for(node), "value": value,
                          "label": cells.row_info(s.idx, node).label, "unit": cells.row_info(s.idx, node).unit}
                         for node, value in zip(nodes, readings)]}
