"""Инструменты кредитного анализа CashFlow (практики банков и проектного финансирования):

  lender_metrics  — CFADS, DSCR (min/avg), LLCR, PLCR, ND/EBITDA, ICR и долговая ёмкость при целевом DSCR/LLCR
                    (скульптинг графика) — по рядам модели, в базе или в стрессе;
  stress_pack     — стандартный банковский стресс-пакет: однофакторные шоки умеренной/жёсткой силы +
                    комбинированный downside, матрица «сценарий × ковенант»;
  safe_zone       — карта безопасной зоны: сетка двух драйверов, где ковенант выполняется/нарушается;
  reverse_stress  — обратный стресс-тест: минимальное совместное изменение драйверов, при котором
                    нарушается ковенант (с оценкой правдоподобия в σ);
  debt_capacity   — сколько ещё долга выдержит модель (пересчётом) при всех ковенантах.

Все расчёты — пересчётом копии модели (движок) или по рядам модели; числа LLM не придумывает.
"""
from __future__ import annotations

import math

from ..workbook.card import numeric_years
from ..workbook.periods import FREQ_N
from .tools import METRIC_SCHEMA, SHOCK_SCHEMA, _metric_refs, rnd
from .tools_ext import DRIVER_SCHEMA, OPS, RULE_SCHEMA, _agg, _base_series, _fy, _run, dlabel

DRIVER_KEYS = ("sheet", "row", "cell", "range", "match", "factor", "sub", "include_formulas", "max_rows", "years")


def _drv(d):
    return {k: v for k, v in d.items() if k in DRIVER_KEYS and v not in (None, "")}


def _label(s, d):
    return dlabel(s, d)


def _read(s, metrics, shocks=None, years=None):
    """Ряды метрик в базе (или в стрессе, если есть shocks)."""
    mrefs = _metric_refs(s, metrics, years)
    if shocks:
        s.require_engine()
        ser, desc, notes = _run(s, shocks, mrefs)
    elif s.engine_open:
        ser, desc, notes = _base_series(s, mrefs), [], []
    else:
        ser = []
        for m in mrefs:
            ny = numeric_years(s.idx, m["sheet"], m["row"])
            ser.append({y: ny.get(y) for y in m["years"]})
        desc, notes = [], ["движок пересчёта недоступен — значения из сохранённого файла"]
    return mrefs, ser, desc, notes


def _ppy(periods):
    p = next(iter(periods), None)
    return FREQ_N.get(getattr(p, "freq", None), 1)


def _rule_ok(v, rule):
    return v is not None and OPS[rule.get("op", ">=")](v, float(rule["threshold"]))


def _rule_agg(rule):
    return "min" if rule.get("op", ">=") in (">=", ">") else "max"


def _check_rules(s, rules, ser, years_given):
    """Агрегаты по прогнозу и выполнение каждого ковенанта."""
    out = []
    for r, x in zip(rules, ser):
        v, y = _agg(x, _rule_agg(r), None, None if years_given else s.forecast_start)
        out.append({"value": v, "year": y, "ok": _rule_ok(v, r)})
    return out


# ───────────────────────── lender_metrics ─────────────────────────
def t_lender_metrics(s, a, emit):
    rate = float(a.get("rate_pct", 12)) / 100
    t_dscr = float(a.get("target_dscr", 1.3))
    t_llcr = float(a.get("target_llcr", 1.4))
    lockup = a.get("lockup_dscr")
    specs = []                       # (тег, metric, sign, abs)

    def add(tag, m, sign=1, use_abs=False):
        if m:
            specs.append((tag, {"sheet": m["sheet"], "row": m["row"], "name": m.get("name") or tag}, sign, use_abs))

    if a.get("cfads"):
        add("cfads", a["cfads"])
    for p in a.get("cfads_parts") or []:
        add("cfads", p, float(p.get("sign", 1)), bool(p.get("abs", float(p.get("sign", 1)) < 0)))
    if a.get("debt_service"):
        add("ds", a["debt_service"], 1, True)
    for p in a.get("debt_service_parts") or []:
        add("ds", p, 1, True)
    add("debt", a.get("debt"), 1, True)
    add("cash", a.get("cash"), 1, False)
    add("ebitda", a.get("ebitda"), 1, False)
    add("interest", a.get("interest"), 1, True)
    tags = {t for t, *_ in specs}
    if "cfads" not in tags or "ds" not in tags:
        raise ValueError("Нужны cfads (или cfads_parts) и debt_service (или debt_service_parts)")
    mrefs, ser, desc, notes = _read(s, [m for _, m, _, _ in specs], a.get("shocks"), a.get("years"))
    agg = {}
    periods = set()
    for (tag, m, sign, use_abs), x in zip(specs, ser):
        d = agg.setdefault(tag, {})
        for y, v in x.items():
            if not isinstance(v, (int, float)):
                continue
            periods.add(y)
            d[y] = d.get(y, 0.0) + sign * (abs(v) if use_abs else v)
    allp = sorted(periods)
    fs = None if a.get("years") else s.forecast_start
    P = [y for y in allp if fs is None or y >= fs]
    if not P:
        raise ValueError("Нет прогнозных периодов в рядах")
    ppy = _ppy(P)
    r_p = (1 + rate) ** (1 / ppy) - 1
    cf = [agg["cfads"].get(y) for y in P]
    ds = [agg["ds"].get(y, 0.0) for y in P]
    debt = agg.get("debt", {})
    last_ds = max((i for i, v in enumerate(ds) if v and v > 1e-9), default=None)

    def pv(vals, i0, i1):
        return sum((vals[k] or 0) / (1 + r_p) ** (k - i0 + 1) for k in range(i0, i1 + 1))

    rows, dscrs = [], []
    for i, y in enumerate(P):
        prev = allp[allp.index(y) - 1] if allp.index(y) > 0 else None
        opening = debt.get(prev) if prev is not None and prev in debt else debt.get(y)
        dscr = cf[i] / ds[i] if cf[i] is not None and ds[i] and ds[i] > 1e-9 else None
        if dscr is not None:
            dscrs.append((y, dscr))
        llcr = pv(cf, i, last_ds) / opening if opening and last_ds is not None and i <= last_ds and opening > 1e-9 else None
        plcr = pv(cf, i, len(P) - 1) / opening if opening and opening > 1e-9 else None
        row = {"period": str(y), "cfads": rnd(cf[i]), "debt_service": rnd(ds[i]), "dscr": rnd(dscr, 4),
               "opening_debt": rnd(opening), "llcr": rnd(llcr, 4), "plcr": rnd(plcr, 4)}
        if "ebitda" in agg and debt.get(y) is not None and agg["ebitda"].get(y):
            row["nd_ebitda"] = rnd((debt[y] - agg.get("cash", {}).get(y, 0.0)) / (agg["ebitda"][y] * ppy), 4)
        if "ebitda" in agg and "interest" in agg and agg["interest"].get(y):
            row["icr"] = rnd(agg["ebitda"].get(y, 0) / agg["interest"][y], 4)
        rows.append(row)
    summary = {}
    if dscrs:
        ymin, vmin = min(dscrs, key=lambda x: x[1])
        summary.update(min_dscr=rnd(vmin, 4), min_dscr_period=str(ymin),
                       avg_dscr=rnd(sum(v for _, v in dscrs) / len(dscrs), 3),
                       dscr_below_target=[str(y) for y, v in dscrs if v < t_dscr])
        if lockup is not None:
            summary["lockup_periods"] = [str(y) for y, v in dscrs if v < float(lockup)]
    llcrs = [(r["period"], r["llcr"]) for r in rows if r["llcr"] is not None]
    if llcrs:
        p0, l0 = min(llcrs, key=lambda x: x[1])
        summary.update(min_llcr=l0, min_llcr_period=p0, llcr_first=llcrs[0][1])
    # долговая ёмкость: скульптинг графика под целевой DSCR и размер по LLCR
    cap = {}
    if last_ds is not None:
        sculpt = [max(0.0, (cf[k] or 0) / t_dscr) for k in range(0, last_ds + 1)]
        cap["sculpted_capacity"] = rnd(pv(sculpt, 0, last_ds))
        cap["llcr_capacity"] = rnd(pv(cf, 0, last_ds) / t_llcr)
        op0 = rows[0]["opening_debt"]
        if op0:
            cap["current_debt"] = op0
            cap["spare_vs_dscr_pct"] = rnd((cap["sculpted_capacity"] / op0 - 1) * 100, 3)
            cap["spare_vs_llcr_pct"] = rnd((cap["llcr_capacity"] / op0 - 1) * 100, 3)
        cap["sculpted_schedule"] = {str(P[k]): rnd(sculpt[k]) for k in range(len(sculpt))}
    if debt and P and debt.get(P[-1]) and rows[0]["opening_debt"] and debt[P[-1]] > 0.5 * rows[0]["opening_debt"]:
        notes.append("Долг почти не гасится в горизонте модели (balloon/рефинансирование): LLCR и PLCR занижены "
                     "по построению — опирайся на DSCR, ND/EBITDA и риск рефинансирования в конце горизонта")
    payload = {"scenario": "; ".join(desc) if desc else "База", "rate_pct": rate * 100, "periods_per_year": ppy,
               "targets": {"dscr": t_dscr, "llcr": t_llcr, "lockup_dscr": lockup},
               "inputs": [f"{t}: {m['sheet']}!{m['row']}" for t, m, _, _ in specs],
               "rows": rows, "summary": summary, "capacity": cap, "notes": notes,
               "method": "DSCR = CFADS / обслуживание долга; LLCR = PV(CFADS до последнего платежа) / долг на начало периода; "
                         "PLCR — то же до конца горизонта модели; ёмкость = PV(CFADS/целевой DSCR) — скульптинг графика; "
                         "дисконт по ставке rate_pct, поток в конце периода."}
    emit("lender", payload)
    return payload


# ───────────────────────── stress_pack ─────────────────────────
PACK = {   # категория: (умеренный, жёсткий, op, пояснение)
    "price":   (-10, -20, "pct", "цены реализации"),
    "volume":  (-10, -20, "pct", "объёмы/загрузка"),
    "revenue": (-10, -20, "pct", "выручка"),
    "opex":    (10, 20, "pct", "операционные расходы"),
    "capex":   (15, 30, "pct", "перерасход CAPEX"),
    "rate":    (20, 40, "pct", "процентная ставка (лучше задать add в единицах модели: +2 п.п.)"),
    "fx":      (-10, -20, "pct", "курс (знак — по экспозиции заёмщика)"),
    "delay":   (1, 2, "shift", "задержка запуска/ввода мощностей, периодов"),
    "other":   (-10, -20, "pct", "прочее"),
}


def t_stress_pack(s, a, emit):
    s.require_engine()
    metrics = a.get("metrics") or []
    rules = a.get("rules") or []
    if not metrics and not rules:
        raise ValueError("Нужны metrics или rules")
    if rules:
        metrics = [{"sheet": r["sheet"], "row": r["row"], "name": r.get("name")} for r in rules] + \
                  [m for m in metrics if (m["sheet"], int(m["row"])) not in {(r["sheet"], int(r["row"])) for r in rules}]
    sev = a.get("severity", "both")
    drivers = a["drivers"][:7]
    mrefs = _metric_refs(s, metrics, a.get("years"))
    base = _base_series(s, mrefs)
    years_given = bool(a.get("years"))

    def shock(d, level):
        cat = d.get("category", "other")
        m_, s_, op, _ = PACK.get(cat, PACK["other"])
        op = d.get("op") or op
        v = d.get("value") if level == "moderate" else d.get("severe_value")
        if v is None:
            v = m_ if level == "moderate" else s_
            if d.get("value") is not None and level == "severe":
                v = float(d["value"]) * 2 if op != "shift" else float(d["value"]) + 1
        return dict(_drv(d), op=op, value=float(v))

    levels = ["moderate", "severe"] if sev == "both" else [sev]
    scen = []
    for d in drivers:
        for lv in levels:
            scen.append((f"{_label(s, d)} · {'умеренный' if lv == 'moderate' else 'жёсткий'}", [shock(d, lv)], "single"))
    if a.get("combined", True) and len(drivers) > 1:
        for lv in levels:
            scen.append((f"Комбинированный · {'умеренный' if lv == 'moderate' else 'жёсткий'}",
                         [shock(d, lv) for d in drivers], "combined"))
    scen = scen[:18]

    def summarize(ser):
        cells = []
        for j, m in enumerate(mrefs):
            rule = rules[j] if j < len(rules) else None
            how = _rule_agg(rule) if rule else a.get("aggregate", "min")
            v, y = _agg(ser[j], how, None, None if years_given else s.forecast_start)
            b, _ = _agg(base[j], how, None, None if years_given else s.forecast_start)
            c = {"value": rnd(v, 4), "period": str(y) if y is not None else None,
                 "delta_pct": rnd((v - b) / abs(b) * 100, 3) if v is not None and b else None}
            if rule:
                c["ok"] = _rule_ok(v, rule)
            cells.append(c)
        return cells

    rows = [{"scenario": "База", "kind": "base", "shocks": [], "cells": summarize(base)}]
    series_all = []
    for i, (name, shocks, kind) in enumerate(scen):
        emit("progress", {"text": f"Стресс-пакет {i + 1}/{len(scen)}: {name}"})
        try:
            ser, desc, notes = _run(s, shocks, mrefs)
        except Exception as e:
            rows.append({"scenario": name, "kind": kind, "error": str(e)[:200]})
            continue
        rows.append({"scenario": name, "kind": kind, "shocks": desc, "cells": summarize(ser)})
        series_all.append((name, desc, ser))
    cols = []
    for j, m in enumerate(mrefs):
        rule = rules[j] if j < len(rules) else None
        cols.append({"metric": m["name"], "ref": f"{m['sheet']}!{m['row']}",
                     "rule": f"{rule.get('op', '>=')} {float(rule['threshold']):g}" if rule else None,
                     "aggregate": _rule_agg(rule) if rule else a.get("aggregate", "min")})
    breaches = [r["scenario"] for r in rows if any(c.get("ok") is False for c in r.get("cells", []))]
    payload = {"columns": cols, "rows": rows, "breach_scenarios": breaches,
               "note": "Ячейка — худшее значение метрики по прогнозу (min для ≥-ковенантов, max для ≤) и период; "
                       "delta_pct — к базе. Умеренный/жёсткий — стандартные величины шоков пакета, если не заданы явно."}
    emit("matrix", payload)
    s.results.append({"metrics": [{"name": m["name"], "ref": f"{m['sheet']}!{m['row']}", "unit": m["unit"], "years": m["years"],
                                   "series": [{"name": "База", "values": [base[j].get(y) for y in m["years"]]}] +
                                             [{"name": n, "values": [ser[j].get(y) for y in m["years"]]} for n, _, ser in series_all]}
                                  for j, m in enumerate(mrefs)],
                      "scenarios": [{"name": n, "shocks": d} for n, d, _ in series_all]})
    return payload


# ───────────────────────── safe_zone ─────────────────────────
def t_safe_zone(s, a, emit):
    s.require_engine()
    rule = a["rule"]
    xd, yd = a["x"], a["y"]
    xs = [float(v) for v in (a.get("x_values") or [-30, -20, -10, 0, 10])][:8]
    ys = [float(v) for v in (a.get("y_values") or [-30, -20, -10, 0, 10])][:8]
    mrefs = _metric_refs(s, [{"sheet": rule["sheet"], "row": rule["row"], "name": rule.get("name")}], a.get("years"))
    how = a.get("aggregate") or _rule_agg(rule)
    mn = None if a.get("years") else s.forecast_start
    b0, by = _agg(_base_series(s, mrefs)[0], how, a.get("year"), mn)
    grid, k = [], 0
    for yv in ys:
        line = []
        for xv in xs:
            k += 1
            emit("progress", {"text": f"Карта безопасной зоны: {k}/{len(xs) * len(ys)}"})
            shocks = []
            if xv:
                shocks.append(dict(_drv(xd), op=xd.get("op", "pct"), value=xv))
            if yv:
                shocks.append(dict(_drv(yd), op=yd.get("op", "pct"), value=yv))
            if shocks:
                ser, _, _ = _run(s, shocks, mrefs)
                v, _ = _agg(ser[0], how, a.get("year"), mn)
            else:
                v = b0
            line.append({"value": rnd(v, 4), "ok": _rule_ok(v, rule)})
        grid.append(line)
    # граница: для каждого x — самый сильный шок y, при котором ковенант ещё выполняется
    edge = []
    for i, xv in enumerate(xs):
        okys = [ys[j] for j in range(len(ys)) if grid[j][i]["ok"]]
        edge.append({"x": xv, "y_ok_range": [min(okys), max(okys)] if okys else None})
    share = sum(c["ok"] for line in grid for c in line) / (len(xs) * len(ys))
    payload = {"rule": f"{rule.get('name') or mrefs[0]['name']} {rule.get('op', '>=')} {float(rule['threshold']):g}",
               "ref": f"{mrefs[0]['sheet']}!{mrefs[0]['row']}", "aggregate": how, "base": rnd(b0, 4), "base_period": str(by),
               "op": rule.get("op", ">="), "threshold": float(rule["threshold"]),
               "x": {"name": _label(s, xd), "op": xd.get("op", "pct"), "values": xs},
               "y": {"name": _label(s, yd), "op": yd.get("op", "pct"), "values": ys},
               "grid": grid, "safe_share": rnd(share, 3), "edge": edge,
               "note": "grid[i][j] — значение метрики при шоке y=y_values[i] и x=x_values[j]; ok — ковенант выполняется."}
    emit("safezone", payload)
    return payload


# ───────────────────────── reverse_stress ─────────────────────────
def t_reverse_stress(s, a, emit):
    s.require_engine()
    rules = a["rules"][:4]
    drivers = a["drivers"][:6]
    mrefs = _metric_refs(s, [{"sheet": r["sheet"], "row": r["row"], "name": r.get("name")} for r in rules], a.get("years"))
    yg = bool(a.get("years"))
    w = []
    for d in drivers:
        sd = d.get("sd_pct")
        direction = -1 if float(d.get("direction", -1)) < 0 else 1
        if d.get("weight") is not None:
            w.append(float(d["weight"]))
        elif sd:
            w.append(direction * float(sd))
        else:
            w.append(direction * 10.0)
    base_chk = _check_rules(s, rules, _base_series(s, mrefs), yg)
    if not all(c["ok"] for c in base_chk):
        return {"already_breached": True, "base": [{"rule": f"{r.get('name') or m['name']} {r.get('op', '>=')} {r['threshold']}",
                                                    "value": rnd(c["value"], 4), "period": str(c["year"])}
                                                   for r, m, c in zip(rules, mrefs, base_chk)],
                "message": "Ковенант нарушен уже в базовом сценарии — обратный стресс не нужен."}
    k_max = float(a.get("max_multiple", 6))       # во сколько «единиц» (весов) можно уйти

    def f(k):
        shocks = [dict(_drv(d), op="pct", value=k * wi) for d, wi in zip(drivers, w)]
        ser, _, _ = _run(s, shocks, mrefs)
        chk = _check_rules(s, rules, ser, yg)
        return all(c["ok"] for c in chk), chk

    ok_hi, chk_hi = f(k_max)
    if ok_hi:
        return {"breach_found": False, "max_multiple": k_max,
                "shocks_tested": {(_label(s, d)): rnd(k_max * wi, 3) for d, wi in zip(drivers, w)},
                "message": "Даже при максимальном совместном шоке ковенанты выполняются — запас очень большой "
                           "(или драйверы слабо влияют). Увеличь max_multiple или добавь драйверы."}
    lo, hi, chk_at = 0.0, k_max, chk_hi
    for i in range(int(a.get("max_iter", 12))):
        mid = (lo + hi) / 2
        emit("progress", {"text": f"Обратный стресс: итерация {i + 1}, множитель {mid:.2f}"})
        ok, chk = f(mid)
        if ok:
            lo = mid
        else:
            hi, chk_at = mid, chk
    k = hi
    shocks = [{"driver": _label(s, d), "change_pct": rnd(k * wi, 3),
               "sigma": rnd(abs(k * wi) / float(d["sd_pct"]), 3) if d.get("sd_pct") else None} for d, wi in zip(drivers, w)]
    sig = [x["sigma"] for x in shocks if x["sigma"] is not None]
    dist = math.sqrt(sum(x * x for x in sig)) if len(sig) == len(shocks) else None
    binding = [f"{r.get('name') or m['name']} {r.get('op', '>=')} {float(r['threshold']):g}: {rnd(c['value'], 4)} ({c['year']})"
               for r, m, c in zip(rules, mrefs, chk_at) if not c["ok"]]
    payload = {"breach_found": True, "multiple": rnd(k, 3), "shocks": shocks, "binding": binding,
               "sigma_distance": rnd(dist, 3) if dist is not None else None,
               "plausibility": (None if dist is None else "высокая (в пределах 1σ)" if dist <= 1 else
                                "умеренная (1–2σ)" if dist <= 2 else "низкая (2–3σ)" if dist <= 3 else "очень низкая (>3σ)"),
               "note": "Минимальное совместное изменение драйверов в заданных пропорциях, при котором нарушается хотя бы один "
                       "ковенант. sigma — величина шока в стандартных отклонениях (если задан sd_pct)."}
    emit("reverse", payload)
    return payload


# ───────────────────────── debt_capacity ─────────────────────────
def t_debt_capacity(s, a, emit):
    s.require_engine()
    rules = a["rules"][:4]
    d = a["driver"]
    mrefs = _metric_refs(s, [{"sheet": r["sheet"], "row": r["row"], "name": r.get("name")} for r in rules], a.get("years"))
    yg = bool(a.get("years"))
    hi0 = float(a.get("upper", 200))

    def f(x):
        if not x:
            ser = _base_series(s, mrefs)
        else:
            ser, _, _ = _run(s, [dict(_drv(d), op="pct", value=x)], mrefs)
        chk = _check_rules(s, rules, ser, yg)
        return all(c["ok"] for c in chk), chk

    ok0, chk0 = f(0)
    base = [{"rule": f"{r.get('name') or m['name']} {r.get('op', '>=')} {float(r['threshold']):g}", "value": rnd(c["value"], 4),
             "period": str(c["year"]), "ok": c["ok"]} for r, m, c in zip(rules, mrefs, chk0)]
    lo, hi = (0.0, hi0) if ok0 else (-90.0, 0.0)
    okh, chkh = f(hi) if ok0 else f(lo)
    if all(abs((c["value"] or 0) - (b["value"] or 0)) < 1e-9 for c, b in zip(chkh, chk0)):
        raise ValueError(f"Драйвер {_label(s, d)} не влияет на метрики ковенантов — выбери строку/ячейку суммы долга, "
                         "выборок или лимита, от которой зависят долг и обслуживание (trace_row dependents)")
    if ok0 and okh:
        return {"driver": _label(s, d), "base": base, "max_change_pct": f">{hi0:g}",
                "message": f"Ковенанты выполняются даже при +{hi0:g}% к драйверу долга"}
    if not ok0 and not okh:
        return {"driver": _label(s, d), "base": base,
                "message": "Ковенанты нарушены в базе и не восстанавливаются даже при снижении долга на 90%"}
    for i in range(int(a.get("max_iter", 12))):
        mid = (lo + hi) / 2
        emit("progress", {"text": f"Долговая ёмкость: итерация {i + 1}, изменение долга {mid:+.1f}%"})
        ok, _ = f(mid)
        if ok:
            lo = mid
        else:
            hi = mid
    x = lo
    _, chk = f(x)
    _, chk_b = f(hi)
    binding = [b["rule"] for b, c in zip(base, chk_b) if not c["ok"]]
    payload = {"driver": _label(s, d), "base": base, "max_change_pct": rnd(x, 3),
               "binding_covenant": binding,
               "at_capacity": [{"rule": b["rule"], "value": rnd(c["value"], 4), "period": str(c["year"])} for b, c in zip(base, chk)],
               "note": ("Максимальное увеличение драйвера долга (в %), при котором все ковенанты ещё выполняются." if ok0 else
                        "В базе ковенанты нарушены: показано, на сколько нужно снизить долг, чтобы их выполнить.")}
    emit("capacity", payload)
    return payload


# ───────────────────────── схемы ─────────────────────────
PART_SCHEMA = {"type": "object", "properties": {
    "sheet": {"type": "string"}, "row": {"type": "integer"}, "name": {"type": "string"},
    "sign": {"type": "number", "description": "+1 прибавить, −1 вычесть"},
    "abs": {"type": "boolean", "description": "Брать модуль значения (для расходов, которые в модели могут быть с минусом)"}},
    "required": ["sheet", "row"]}
PACK_DRIVER_SCHEMA = {"type": "object", "properties": DRIVER_SCHEMA["properties"] | {
    "category": {"type": "string", "enum": list(PACK), "description": "Тип драйвера — задаёт стандартные шоки пакета"},
    "op": {"type": "string", "enum": ["pct", "add", "set", "shift"]},
    "value": {"type": "number", "description": "Умеренный шок (переопределяет стандартный)"},
    "severe_value": {"type": "number", "description": "Жёсткий шок"}}, "required": ["category"]}
AXIS_SCHEMA = {"type": "object", "properties": DRIVER_SCHEMA["properties"] | {
    "op": {"type": "string", "enum": ["pct", "add", "shift"]}}}
REV_DRIVER_SCHEMA = {"type": "object", "properties": DRIVER_SCHEMA["properties"] | {
    "direction": {"type": "number", "description": "−1 — неблагоприятно падение (цена, объём), +1 — рост (ставка, OPEX)"},
    "sd_pct": {"type": "number", "description": "Типичная волатильность драйвера за год, % (для оценки правдоподобия)"},
    "weight": {"type": "number", "description": "Явный вес в % на единицу множителя (со знаком), вместо direction/sd_pct"}}}
YEARS = {"type": "array", "items": {"type": "string"}, "description": "Годы ('2026') или периоды ('2026Q1'); по умолчанию прогноз"}

TOOLS_CREDIT = [
    {"name": "lender_metrics", "fn": t_lender_metrics,
     "description": "Метрики кредитора по рядам модели: CFADS, DSCR по периодам (min/avg, период минимума, lock-up), LLCR, PLCR, "
                    "ND/EBITDA и ICR (если даны долг, деньги, EBITDA, проценты) и долговая ёмкость: скульптинг графика под целевой "
                    "DSCR и размер долга по целевому LLCR против текущего долга. Можно в стрессе (shocks). CFADS задай строкой cfads "
                    "или собери из cfads_parts (EBITDA +1, налоги −1, поддерживающий CAPEX −1, изменение ОК +1).",
     "parameters": {"type": "object", "properties": {
         "cfads": METRIC_SCHEMA, "cfads_parts": {"type": "array", "items": PART_SCHEMA},
         "debt_service": METRIC_SCHEMA, "debt_service_parts": {"type": "array", "items": PART_SCHEMA,
                                                               "description": "Например, проценты и погашение тела долга"},
         "debt": METRIC_SCHEMA, "cash": METRIC_SCHEMA, "ebitda": METRIC_SCHEMA, "interest": METRIC_SCHEMA,
         "rate_pct": {"type": "number", "description": "Ставка дисконтирования для LLCR/ёмкости, % годовых (≈ ставка долга)"},
         "target_dscr": {"type": "number"}, "target_llcr": {"type": "number"}, "lockup_dscr": {"type": "number"},
         "shocks": {"type": "array", "items": SHOCK_SCHEMA}, "years": YEARS}}},
    {"name": "stress_pack", "fn": t_stress_pack,
     "description": "Стандартный банковский стресс-пакет: для каждого драйвера — умеренный и жёсткий однофакторный шок "
                    "(цена/объём −10/−20%, OPEX +10/+20%, CAPEX +15/+30%, ставка, курс, задержка запуска на 1–2 периода) "
                    "и комбинированный downside. Возвращает матрицу «сценарий × метрика» с худшим значением, периодом и "
                    "выполнением ковенантов. Используй для комплексной оценки устойчивости.",
     "parameters": {"type": "object", "properties": {
         "drivers": {"type": "array", "items": PACK_DRIVER_SCHEMA}, "rules": {"type": "array", "items": RULE_SCHEMA},
         "metrics": {"type": "array", "items": METRIC_SCHEMA},
         "severity": {"type": "string", "enum": ["moderate", "severe", "both"]},
         "combined": {"type": "boolean"}, "years": YEARS}, "required": ["drivers"]}},
    {"name": "safe_zone", "fn": t_safe_zone,
     "description": "Карта безопасной зоны: сетка значений двух драйверов (шоки в % по осям x и y), для каждой точки — "
                    "значение метрики и выполнение ковенанта. Показывает, при каких сочетаниях (например, цена × курс) "
                    "ковенант нарушается и где модель сейчас.",
     "parameters": {"type": "object", "properties": {
         "rule": RULE_SCHEMA, "x": AXIS_SCHEMA, "y": AXIS_SCHEMA,
         "x_values": {"type": "array", "items": {"type": "number"}, "description": "Шоки по оси x (до 8), по умолчанию −30…+10"},
         "y_values": {"type": "array", "items": {"type": "number"}},
         "aggregate": {"type": "string", "enum": ["min", "max", "year"]}, "year": {"type": "string"}, "years": YEARS},
         "required": ["rule", "x", "y"]}},
    {"name": "reverse_stress", "fn": t_reverse_stress,
     "description": "Обратный стресс-тест: находит минимальное СОВМЕСТНОЕ неблагоприятное изменение нескольких драйверов "
                    "(в пропорциях их волатильности sd_pct или весов), при котором нарушается хотя бы один ковенант; "
                    "возвращает точку пробоя, связывающий ковенант и правдоподобие в σ.",
     "parameters": {"type": "object", "properties": {
         "rules": {"type": "array", "items": RULE_SCHEMA}, "drivers": {"type": "array", "items": REV_DRIVER_SCHEMA},
         "max_multiple": {"type": "number", "description": "Максимальный множитель весов (по умолчанию 6)"}, "years": YEARS},
         "required": ["rules", "drivers"]}},
    {"name": "debt_capacity", "fn": t_debt_capacity,
     "description": "Долговая ёмкость пересчётом: на сколько % можно увеличить драйвер долга (сумма кредита/выборки/лимит — "
                    "строка или ячейка) так, чтобы ВСЕ ковенанты выполнялись; какой ковенант ограничивает. Если в базе "
                    "ковенанты нарушены — на сколько надо снизить долг.",
     "parameters": {"type": "object", "properties": {
         "driver": DRIVER_SCHEMA, "rules": {"type": "array", "items": RULE_SCHEMA},
         "upper": {"type": "number", "description": "Верхняя граница поиска, % (по умолчанию 200)"}, "years": YEARS},
         "required": ["driver", "rules"]}},
]
