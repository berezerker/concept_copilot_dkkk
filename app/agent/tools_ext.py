"""Расширенные инструменты: карта модели, предпроверка, «из чего состоит число», автопоиск драйверов
по всей модели, ковенанты, запас прочности, вероятность пробоя (Монте-Карло)."""
from __future__ import annotations

import random
import statistics

from ..workbook.card import numeric_years
from ..workbook.index import col_index
from ..workbook.periods import norm_period
from ..workbook.transaction import RestorationError
from ..workbook.modelmap import audit, build_map, explain_cell, is_scalar_row, upstream_inputs
from ..workbook import semantics as sm
from .tools import (METRIC_SCHEMA, SHOCK_SCHEMA, _build_overrides, _metric_refs, _read_metrics, norm, rnd)


def dlabel(s, d):
    """Человеческое имя драйвера/цели шока."""
    if d.get("name"):
        return d["name"]
    if d.get("concept") and not d.get("row"):
        from ..workbook import catalog as cg
        try:
            return cg.resolve(s, d["concept"])["name"]
        except ValueError:
            return str(d["concept"])
    if d.get("factor") or d.get("sub"):
        if d.get("sub"):
            ind = (s.sem or {}).get("industry", "generic")
            return sm.INDUSTRIES.get(ind, sm.INDUSTRIES["generic"])[1].get(d["sub"], d["sub"])
        return sm.FACTORS.get(d["factor"], (d["factor"],))[0]
    if d.get("match"):
        return f"все строки «{d['match']}»"
    if d.get("range"):
        return d["range"]
    if d.get("sheet") and d.get("row"):
        return sm.name(s, d["sheet"], int(d["row"]))
    return f"{d.get('sheet')}!{d.get('cell')}"

OPS = {">=": lambda v, t: v >= t, "<=": lambda v, t: v <= t, ">": lambda v, t: v > t, "<": lambda v, t: v < t}


def _agg(series: dict, how="min", year=None, min_year=None):
    nums = {y: v for y, v in series.items() if isinstance(v, (int, float)) and (min_year is None or y >= min_year)}
    if not nums:
        return None, None
    if how == "year":
        y = norm_period(year)
        k = next((k for k in nums if k == y), None)
        return (nums[k], k) if k is not None else (None, y)
    if how == "max":
        y = max(nums, key=nums.get)
    elif how == "avg":
        return sum(nums.values()) / len(nums), None
    else:
        y = min(nums, key=nums.get)
    return nums[y], y


def _fy(s, a):
    """Агрегаты (min/max/avg) считаем по прогнозным годам, если годы не заданы явно."""
    return None if a.get("years") else s.forecast_start


def _run(s, shocks, mrefs):
    reads = [r for m in mrefs for r in m["refs"]]
    ov, desc, notes, unreach = _build_overrides(s, shocks, {(m["sheet"], m["row"]) for m in mrefs})
    vals, _ = s.engine.scenario(ov, reads, reads)
    return _read_metrics(vals, mrefs), desc, notes


def _base_series(s, mrefs):
    reads = [r for m in mrefs for r in m["refs"]]
    return _read_metrics(s.engine.read(reads), mrefs)


# ───────────────────────── карта и предпроверка ─────────────────────────
def t_model_map(s, a, emit):
    m = build_map(s.idx, s.card, s.forecast_start)
    from collections import Counter
    for x in m["metrics"]:
        sh_, r_ = x["ref"].split("!")
        ups, _ = upstream_inputs(s.idx, sh_, int(r_), s.forecast_start)
        c = Counter(sm.info(s, u["sheet"], u["row"])["factor_name"] for u in ups if u["fanout"])
        x["by_factor"] = dict(c.most_common(8))
    emit("map", m)
    return {"sheets": [{k: x[k] for k in ("sheet", "role", "layer", "inputs", "calc")} for x in m["sheets"]],
            "flows_top": m["edges"][:15], "metrics_drivers": [{k: v for k, v in x.items() if k != "by_sheet"} for x in m["metrics"]],
            "row_classes": m["classes_total"],
            "alt_sets": s.idx.alt_sets, "warnings": s.idx.warnings,
            "note": "Роли листов определены по структуре книги (не по названиям): inputs — листы-источники допущений, "
                    "calc — расчёты, report — итоговые. outside_inputs_share — доля допущений, влияющих на метрику, которые "
                    "лежат НЕ на листах-источниках (вбиты в расчётные листы). Шоковать можно любые: row/cell/range/match/factor/concept."}


def t_audit_model(s, a, emit):
    res = audit(s.idx, s.forecast_start)
    emit("audit", res)
    return res


def t_explain_cell(s, a, emit):
    sh = s.idx.sheet(a["sheet"])
    if a.get("cell"):
        import re
        m = re.match(r"^\$?([A-Z]{1,3})\$?(\d+)$", str(a["cell"]).upper())
        if not m:
            raise ValueError("cell в виде K34")
        col, row = col_index(m.group(1)), int(m.group(2))
    else:
        row = int(a["row"])
        y = norm_period(a.get("year") or s.forecast_start)
        col = next((c for c, yy in sorted(sh.years_for(row).items()) if yy == y), None)
        if col is None:
            raise ValueError(f"Период {y} не найден в заголовке строки {sh.name}!{row}")
    res = explain_cell(s.idx, sh.name, row, col)
    res["name"] = sm.name(s, sh.name, row)
    for p in res["parts"]:
        if "!" in p["ref"]:
            ps_, a_ = p["ref"].rsplit("!", 1)
            import re as _re
            mm_ = _re.match(r"^[A-Z]{1,3}(\d+)", a_)
            if mm_ and ps_ in s.idx.sheets:
                p["name"] = sm.name(s, ps_, int(mm_.group(1)))
    # второй уровень для крупнейших слагаемых-формул
    if a.get("depth", 1) and int(a.get("depth", 1)) >= 2:
        for p in res["parts"][:6]:
            ref = p["ref"]
            if ":" in ref:
                continue
            ps, addr = ref.rsplit("!", 1)
            import re
            mm = re.match(r"^([A-Z]{1,3})(\d+)$", addr)
            if mm and ps in s.idx.sheets:
                try:
                    sub = explain_cell(s.idx, ps, int(mm.group(2)), col_index(mm.group(1)))
                    p["formula"] = sub["formula"]
                    p["parts"] = sub["parts"][:8]
                except KeyError:
                    pass
    return res


# ───────────────────────── драйверы по всей модели ─────────────────────────
def t_find_drivers(s, a, emit):
    s.require_engine()
    metric = a["metric"]
    how, year = a.get("aggregate", "min"), a.get("year")
    shock = float(a.get("shock_pct", 10))
    top = min(int(a.get("top", 10) or 10), 20)
    maxc = min(int(a.get("max_candidates", 60) or 60), 120)
    mrefs = _metric_refs(s, [metric], a.get("years"))
    m = mrefs[0]
    ups, visited = upstream_inputs(s.idx, m["sheet"], m["row"], s.forecast_start)
    excl = {norm(x) for x in (a.get("exclude_sheets") or [])}
    cands, seen_lab = [], {}
    for u in sorted(ups, key=lambda u: (-u["fanout"], u["depth"])):
        if norm(u["sheet"]) in excl or u["fanout"] == 0:
            continue
        ys = [y for y in numeric_years(s.idx, u["sheet"], u["row"]) if not s.forecast_start or y >= s.forecast_start]
        shx = s.idx.sheets[u["sheet"]]
        if not ys and not is_scalar_row(shx, shx.rows[u["row"]]):      # ни ряда в прогнозе, ни параметра одной ячейкой
            continue
        key = (norm(u["label"]), u["unit"])
        if key in seen_lab and u["label"]:        # одна и та же строка в Inputs / Inputs_C / Inputs_O
            continue
        seen_lab[key] = u
        cands.append(u)
        if len(cands) >= maxc:
            break
    base = _base_series(s, mrefs)[0]
    b0, by = _agg(base, how, year, _fy(s, a))
    res, zero = [], 0
    for i, u in enumerate(cands):
        emit("progress", {"text": f"Драйвер {i + 1}/{len(cands)}: {u['sheet']}!{u['row']} «{u['label'][:40]}»"})
        try:
            ser, _, _ = _run(s, [{"sheet": u["sheet"], "row": u["row"], "op": "pct", "value": -shock}], mrefs)
        except RestorationError:
            raise
        except Exception:  # строка без лет и т.п.
            continue
        v, vy = _agg(ser[0], how, year, _fy(s, a))
        if v is None or b0 is None:
            continue
        d = v - b0
        if abs(d) < 1e-9 * max(1, abs(b0)):
            zero += 1
            continue
        hi = sm.info(s, u["sheet"], u["row"])
        res.append({"sheet": u["sheet"], "row": u["row"], "label": hi["name"][:70], "factor": hi["factor_name"], "unit": u["unit"],
                    "role": s.idx.sheets[u["sheet"]].role, "row_class": u["class"], "delta": rnd(d), "value": rnd(v),
                    "delta_pct": rnd(d / b0 * 100 if b0 else 0, 3)})
    res.sort(key=lambda x: -abs(x["delta"]))
    res = res[:top]
    outside = sum(1 for r in res if r["role"] != "inputs")
    payload = {"metric": f"{m['sheet']}!{m['row']} {m['name']}", "aggregate": how, "base": rnd(b0), "base_year": by,
               "shock_pct": -shock, "drivers": res}
    emit("tornado", payload)
    return dict(payload, candidates_tested=len(cands), upstream_inputs_total=len(ups), no_effect=zero,
                top_outside_inputs=outside,
                note="Шок −X% к каждому допущению по отдельности (прогнозные годы); delta — изменение метрики. "
                     "no_effect — допущения, не влияющие в текущем сценарии (например, невыбранные наборы Inputs).")


# ───────────────────────── ковенанты и запас прочности ─────────────────────────
def t_covenant_check(s, a, emit):
    rules = a["rules"][:10]
    shocks = a.get("shocks") or []
    if shocks:
        s.require_engine()
    mrefs = _metric_refs(s, [{"sheet": r["sheet"], "row": r["row"], "name": r.get("name")} for r in rules], a.get("years"))
    if shocks:
        series, desc, notes = _run(s, shocks, mrefs)
    else:
        series = _base_series(s, mrefs) if s.engine_open else [numeric_years(s.idx, m["sheet"], m["row"]) for m in mrefs]
        desc, notes = [], []
    out = []
    for r, m, ser in zip(rules, mrefs, series):
        op, thr = r.get("op", ">="), float(r["threshold"])
        years = {}
        worst = None
        for y, v in ser.items():
            if not isinstance(v, (int, float)) or (not a.get("years") and s.forecast_start and y < s.forecast_start):
                continue
            ok = OPS[op](v, thr)
            head = (v - thr) / abs(thr) if op in (">=", ">") else (thr - v) / abs(thr)
            years[str(y)] = {"value": rnd(v), "ok": ok, "headroom_pct": rnd(head * 100, 3)}
            if worst is None or head < worst[1]:
                worst = (y, head, v)
        breaches = [y for y, x in years.items() if not x["ok"]]
        out.append({"rule": f"{r.get('name') or m['name']} {op} {thr:g}", "ref": f"{m['sheet']}!{m['row']}",
                    "years": years, "breach_years": breaches,
                    "worst": {"year": worst[0], "value": rnd(worst[2]), "headroom_pct": rnd(worst[1] * 100, 3)} if worst else None})
    payload = {"scenario": "; ".join(desc) if desc else "База", "rules": out, "notes": notes}
    emit("covenants", payload)
    return payload


def _seek(s, shock, mrefs, how, year, target, lo, hi, tol=0.002, max_iter=16, min_year=None):
    def f(x):
        ser, _, _ = _run(s, [dict(shock, value=x)], mrefs)
        return _agg(ser[0], how, year, min_year)[0]
    flo, fhi = f(lo), f(hi)
    if flo is None or fhi is None or (flo - target) * (fhi - target) > 0:
        return None, flo, fhi
    x = None
    for _ in range(max_iter):
        x = (lo + hi) / 2
        fx = f(x)
        if abs(fx - target) <= tol * max(1, abs(target)):
            return x, flo, fhi
        if (flo - target) * (fx - target) <= 0:
            hi, fhi = x, fx
        else:
            lo, flo = x, fx
    return x, flo, fhi


def t_headroom(s, a, emit):
    """Запас прочности: на сколько % может измениться каждый драйвер до пробоя ковенанта."""
    s.require_engine()
    rule = a["rule"]
    op, thr = rule.get("op", ">="), float(rule["threshold"])
    how = a.get("aggregate") or ("min" if op in (">=", ">") else "max")
    mrefs = _metric_refs(s, [{"sheet": rule["sheet"], "row": rule["row"], "name": rule.get("name")}], a.get("years"))
    base = _base_series(s, mrefs)[0]
    b0, by = _agg(base, how, a.get("year"), _fy(s, a))
    lo_b, hi_b = float(a.get("lower", -60)), float(a.get("upper", 60))
    rows = []
    for i, d in enumerate(a["drivers"][:8]):
        drv = dict(d, op="pct")
        label = dlabel(s, d)
        emit("progress", {"text": f"Запас по драйверу {i + 1}/{len(a['drivers'][:8])}: {label}"})
        best = None
        for lo, hi in ((lo_b, 0.0), (0.0, hi_b)):
            try:
                x, flo, fhi = _seek(s, drv, mrefs, how, a.get("year"), thr, lo, hi, min_year=_fy(s, a))
            except RestorationError:
                raise
            except Exception as e:
                best = {"driver": label, "error": str(e)[:200]}
                break
            if x is not None and (best is None or abs(x) < abs(best["change_pct"])):
                best = {"driver": label, "change_pct": rnd(x, 3)}
        if best is None:
            best = {"driver": label, "change_pct": None,
                    "note": f"Пробой не достигается при изменении от {lo_b:g}% до {hi_b:g}%"}
        rows.append(best)
    payload = {"rule": f"{rule.get('name') or mrefs[0]['name']} {op} {thr:g}", "ref": f"{mrefs[0]['sheet']}!{mrefs[0]['row']}",
               "aggregate": how, "base": rnd(b0), "base_year": by,
               "already_breached": (b0 is not None and not OPS[op](b0, thr)), "drivers": rows}
    emit("headroom", payload)
    return payload


# ───────────────────────── Монте-Карло ─────────────────────────
def t_monte_carlo(s, a, emit):
    s.require_engine()
    n = max(20, min(int(a.get("n", 100) or 100), 400))
    rnd_ = random.Random(int(a.get("seed", 42)))
    drivers = a["drivers"][:6]
    metric = a["metric"]
    how, year = a.get("aggregate", "min"), a.get("year")
    op = a.get("covenant_op") or a.get("op") or ">="
    thr = a.get("threshold")
    mrefs = _metric_refs(s, [metric], a.get("years"))
    base = _base_series(s, mrefs)[0]
    b0, _ = _agg(base, how, year, _fy(s, a))
    vals = []
    for i in range(n):
        shocks = []
        for d in drivers:
            if "sd_pct" in d:
                x = rnd_.gauss(float(d.get("mean_pct", 0)), float(d["sd_pct"]))
            else:
                x = rnd_.uniform(float(d.get("min_pct", -10)), float(d.get("max_pct", 10)))
            sd = {k: v for k, v in d.items() if k not in ("sd_pct", "mean_pct", "min_pct", "max_pct", "name")}
            shocks.append(dict(sd, op="pct", value=x))
        ser, _, _ = _run(s, shocks, mrefs)
        v, _ = _agg(ser[0], how, year, _fy(s, a))
        if v is not None:
            vals.append(v)
        if (i + 1) % 10 == 0:
            emit("progress", {"text": f"Монте-Карло: {i + 1}/{n} прогонов"})
    vals.sort()
    q = lambda p: vals[min(len(vals) - 1, int(p * len(vals)))]
    breach = below = None
    if thr is not None:
        breach = sum(1 for v in vals if not OPS[op](v, float(thr))) / len(vals)
        below = sum(1 for v in vals if v < float(thr)) / len(vals)
    payload = {"metric": f"{mrefs[0]['sheet']}!{mrefs[0]['row']} {mrefs[0]['name']}", "aggregate": how, "runs": len(vals),
               "base": rnd(b0), "mean": rnd(statistics.fmean(vals)), "p5": rnd(q(0.05)), "p50": rnd(q(0.5)),
               "p95": rnd(q(0.95)), "min": rnd(vals[0]), "max": rnd(vals[-1]),
               "threshold": thr, "covenant": f"{op} {thr}" if thr is not None else None, "op": op,
               "breach_probability": rnd(breach, 3) if breach is not None else None,
               "share_below_threshold": rnd(below, 3) if below is not None else None,
               "share_above_threshold": rnd(1 - below, 3) if below is not None else None,
               "values": [rnd(v, 4) for v in vals]}
    emit("histogram", payload)
    return {k: v for k, v in payload.items() if k != "values"}


# ───────────────────────── схемы ─────────────────────────
RULE_SCHEMA = {"type": "object", "description": "Ковенант: метрика (concept из справочника либо sheet+row), знак и порог",
               "properties": {
    "concept": {"type": "string", "description": "Показатель из справочника (dscr, net_debt_ebitda, icr…) вместо sheet+row"},
    "sheet": {"type": "string"}, "row": {"type": "integer"}, "name": {"type": "string"},
    "op": {"type": "string", "enum": [">=", "<=", ">", "<"]}, "threshold": {"type": "number"}},
    "required": ["op", "threshold"]}
DRIVER_SCHEMA = {"type": "object", "properties": {
    k: v for k, v in SHOCK_SCHEMA["properties"].items() if k not in ("op", "value")} | {"name": {"type": "string"}}}
MC_DRIVER_SCHEMA = {"type": "object", "properties": DRIVER_SCHEMA["properties"] | {
    "sd_pct": {"type": "number", "description": "Стандартное отклонение шока, % (нормальное распределение)"},
    "mean_pct": {"type": "number"}, "min_pct": {"type": "number"}, "max_pct": {"type": "number"}}}

TOOLS_EXT = [
    {"name": "model_map", "fn": t_model_map,
     "description": "Полная карта модели: слои листов (источники → расчёт → итоги), потоки между листами, классы строк, и для каждой ключевой метрики — сколько допущений на неё влияет и какая их доля лежит ВНЕ листов Inputs.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "audit_model", "fn": t_audit_model,
     "description": "Предпроверка модели: ошибки (#REF!, #DIV/0!), числа вместо формул в прогнозе, формулы, выбивающиеся из ряда, внешние ссылки, одна метрика с разными значениями на разных листах, MIN/MAX по факту+прогнозу. С адресами ячеек.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "explain_cell", "fn": t_explain_cell,
     "description": "Из чего состоит значение: формула ячейки и значения её слагаемых с подписями (depth=2 — на уровень глубже). Ячейка задаётся sheet+row+year или sheet+cell.",
     "parameters": {"type": "object", "properties": {
         "sheet": {"type": "string"}, "row": {"type": "integer"}, "year": {"type": "string", "description": "Год или период (2026, 2026Q1)"},
         "cell": {"type": "string"}, "depth": {"type": "integer"}}, "required": ["sheet"]}},
    {"name": "find_drivers", "fn": t_find_drivers,
     "description": "Автопоиск ключевых драйверов метрики по ВСЕЙ модели (не только Inputs): берёт все допущения выше по графу, шокирует каждое на −shock_pct% и ранжирует по влиянию (торнадо).",
     "parameters": {"type": "object", "properties": {
         "metric": METRIC_SCHEMA, "aggregate": {"type": "string", "enum": ["min", "max", "year", "avg"]},
         "year": {"type": "string", "description": "Год или период (2026, 2026Q1)"}, "years": {"type": "array", "items": {"type": "string"}, "description": "Годы ('2026') или периоды ('2026Q1')"},
         "shock_pct": {"type": "number", "description": "Величина шока, % (по умолчанию 10)"},
         "top": {"type": "integer"}, "max_candidates": {"type": "integer", "description": "Сколько допущений проверить (по умолчанию 60, до 120)"},
         "exclude_sheets": {"type": "array", "items": {"type": "string"}}}, "required": ["metric"]}},
    {"name": "covenant_check", "fn": t_covenant_check,
     "description": "Проверка ковенантов/ОКС по годам: выполнение, запас в %, годы пробоя. Можно передать shocks — тогда проверка в стресс-сценарии.",
     "parameters": {"type": "object", "properties": {
         "rules": {"type": "array", "items": RULE_SCHEMA}, "years": {"type": "array", "items": {"type": "string"}, "description": "Годы ('2026') или периоды ('2026Q1')"},
         "shocks": {"type": "array", "items": SHOCK_SCHEMA}}, "required": ["rules"]}},
    {"name": "headroom", "fn": t_headroom,
     "description": "Запас прочности: для каждого драйвера находит, на сколько % он может измениться (вниз или вверх) до пробоя ковенанта. Драйвер задаётся как шок без value: row / cell / range / match.",
     "parameters": {"type": "object", "properties": {
         "rule": RULE_SCHEMA, "drivers": {"type": "array", "items": DRIVER_SCHEMA},
         "aggregate": {"type": "string", "enum": ["min", "max", "year"]}, "year": {"type": "string", "description": "Год или период (2026, 2026Q1)"},
         "years": {"type": "array", "items": {"type": "string"}, "description": "Годы ('2026') или периоды ('2026Q1')"},
         "lower": {"type": "number"}, "upper": {"type": "number"}}, "required": ["rule", "drivers"]}},
    {"name": "monte_carlo", "fn": t_monte_carlo,
     "description": "Вероятность пробоя: n случайных сценариев (шоки драйверов из нормального sd_pct или равномерного min_pct..max_pct распределения), распределение метрики (p5/p50/p95) и доля прогонов с нарушением порога.",
     "parameters": {"type": "object", "properties": {
         "drivers": {"type": "array", "items": MC_DRIVER_SCHEMA}, "metric": METRIC_SCHEMA,
         "aggregate": {"type": "string", "enum": ["min", "max", "year", "avg"]}, "year": {"type": "string", "description": "Год или период (2026, 2026Q1)"},
         "years": {"type": "array", "items": {"type": "string"}, "description": "Годы ('2026') или периоды ('2026Q1')"},
         "covenant_op": {"type": "string", "enum": [">=", "<=", ">", "<"],
                         "description": "Условие ВЫПОЛНЕНИЯ ковенанта: для ISCR/DSCR — '>=', для Net Debt/EBITDA — '<='. Пробой = прогоны, где условие не выполнено."},
         "threshold": {"type": "number", "description": "Порог ковенанта"},
         "n": {"type": "integer", "description": "Число прогонов (по умолчанию 100, до 400)"}}, "required": ["drivers", "metric"]}},
]
