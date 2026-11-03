"""Анализ в экономических терминах: факторы вместо ячеек.

  factor_drivers       — торнадо по факторам: все строки фактора шокируются вместе, доля каждого фактора
                         в чувствительности метрики и эластичность;
  dependency_analysis  — «от чего на самом деле зависит» метрика: концентрация риска, сцепки факторов
                         (цена считается из курса, долг валютный…), скрытые допущения вне Inputs, слабые
                         факторы и смысловая карта «фактор → метрика».
"""
from __future__ import annotations

from collections import Counter, defaultdict

from ..workbook import semantics as sm
from ..workbook.card import numeric_years
from ..workbook.modelmap import upstream_inputs  # noqa: F401
from .tools import METRIC_SCHEMA, _metric_refs, rnd
from .tools_ext import RULE_SCHEMA, _agg, _base_series, _fy, _run

EXPECTED = {  # ожидаемые связи — показываем как «механику» модели, а не как замечание
    ("revenue", "price_sales"): "выручка определяется ценами реализации",
    ("revenue", "volume"): "выручка определяется объёмами",
    ("revenue", "tariff"): "выручка определяется тарифами",
    ("opex", "volume"): "переменные расходы зависят от объёмов",
    ("opex", "price_inputs"): "расходы зависят от цен сырья и топлива",
    ("tax", "price_sales"): "налоги и пошлины зависят от цен (НДПИ, пошлины, демпфер)",
}
NOTABLE = {   # (фактор строки, фактор-источник) → пояснение для кредитного аналитика
    ("price_sales", "fx"): "цены реализации пересчитываются через курс — выручка в рублях зависит от валюты",
    ("price_sales", "price_sales"): "цены одних продуктов привязаны к другим котировкам (например, нефтепродукты — к нефти)",
    ("revenue", "fx"): "часть выручки валютная",
    ("volume", "price_sales"): "объёмы зависят от цен — проверьте, что это задумано",
    ("debt", "fx"): "долг или его обслуживание валютные",
    ("debt", "rate"): "обслуживание долга плавающее — зависит от ставки",
    ("opex", "fx"): "часть расходов валютная",
    ("opex", "inflation"): "расходы индексируются на инфляцию",
    ("revenue", "inflation"): "выручка индексируется на инфляцию — рост номинальный, без отдельных допущений по ценам и объёмам",
    ("price_sales", "inflation"): "цены индексируются на инфляцию, а не задаются рынком",
    ("capex", "fx"): "часть CAPEX валютная",
    ("tax", "price_sales"): "налоги/пошлины зависят от цен (НДПИ, пошлины, демпфер)",
    ("tax", "fx"): "налоги/пошлины зависят от курса",
}


def _groups(s, metric_ref, level="factor"):
    """Строки-допущения выше метрики, сгруппированные по фактору (или подфактору)."""
    sh, r = metric_ref
    ups, _ = upstream_inputs(s.idx, sh, r, s.forecast_start)
    prim = sm._primary_inputs(s.idx)
    groups = defaultdict(list)
    for u in ups:
        if not u["fanout"]:
            continue
        if s.idx.sheets[u["sheet"]].role == "inputs" and u["sheet"] != prim:
            continue
        ys = [y for y in numeric_years(s.idx, u["sheet"], u["row"]) if not s.forecast_start or y >= s.forecast_start]
        if not ys:
            continue
        hi = sm.info(s, u["sheet"], u["row"])
        key = (hi["factor"], hi["sub"]) if level == "sub" and hi["sub"] else (hi["factor"], "")
        groups[key].append(dict(u, name=hi["name"], factor=hi["factor"], sub=hi["sub"]))
    out = {}
    for k, rows in groups.items():
        ins = [x for x in rows if x["class"] == "input"] or rows
        out[k] = sorted(ins, key=lambda x: -x["fanout"])
    return out


def _gname(s, k):
    f, sub = k
    if sub:
        ind = (s.sem or {}).get("industry", "generic")
        return sm.INDUSTRIES.get(ind, sm.INDUSTRIES["generic"])[1].get(sub, sub)
    return sm.FACTORS[f][0]


def _union_groups(s, mrefs, level):
    groups = {}
    for m in mrefs:
        for k, rows in _groups(s, (m["sheet"], m["row"]), level).items():
            g = groups.setdefault(k, {})
            for x in rows:
                g[(x["sheet"], x["row"])] = x
    return {k: sorted(v.values(), key=lambda x: -x["fanout"]) for k, v in groups.items()}


def scan(s, metrics, hows, shock=10.0, level="factor", years=None, emit=None, concepts=False, max_rows=200):
    """Один пересчёт на фактор для ВСЕХ метрик сразу (+ чтение строк-понятий для сцепок).
    Если concepts=True, дополнительно шокируются отраслевые подфакторы — чтобы видеть привязки внутри фактора."""
    from .tools import _build_overrides, _read_metrics
    mrefs = _metric_refs(s, metrics, years)
    watch = [r for m in mrefs for r in m["refs"]]
    base = _base_series(s, mrefs)
    mn = None if years else s.forecast_start
    groups = _union_groups(s, mrefs, level)
    runs = [(k, "main") for k in sorted(groups, key=lambda k: -len(groups[k]))]
    layout, cbase, crow = [], {}, []
    if concepts:
        crow = concept_rows(s)
        layout = _probe_layout(s, crow) if crow else []
        if layout:
            cbase = _extract(layout, s.engine.read([L["ref"] for L in layout]))
        subs = _union_groups(s, mrefs, "sub")
        split = defaultdict(list)
        for k in subs:
            if k[1]:
                split[k[0]].append(k)
        for f, ks in split.items():
            if f in ("other", "scenario"):
                continue
            for k in ks:
                groups[k] = subs[k]
                runs.append((k, "sub"))
    creads = [L["ref"] for L in layout]
    per_metric = [[] for _ in mrefs]
    effects = defaultdict(dict)
    for i, (k, kind) in enumerate(runs):
        rows = groups[k][:max_rows]
        if emit:
            emit("progress", {"text": f"Пересчёт {i + 1}/{len(runs)}: {_gname(s, k)} ({len(rows)} строк) −{shock:g}%"})
        shocks = [{"sheet": x["sheet"], "row": x["row"], "op": "pct", "value": -shock, "_quiet": True} for x in rows]
        try:
            ov, _, _, _ = _build_overrides(s, shocks)
            vals, _ = s.engine.scenario(ov, watch, watch + creads)
        except Exception as e:
            from ..logbus import Cancelled
            if isinstance(e, Cancelled):
                raise
            if kind == "main":
                for j in range(len(mrefs)):
                    per_metric[j].append({"id": k[1] or k[0], "factor": k[0], "name": _gname(s, k), "rows": len(rows),
                                          "error": str(e)[:160]})
            continue
        ser = _read_metrics(vals[:len(watch)], mrefs)
        if creads:
            new = _extract(layout, vals[len(watch):])
            for key, b in cbase.items():
                r = _rel(b, new.get(key, []))
                if r > 1e-6:
                    effects[key][k] = r
        if kind != "main":
            continue
        for j, m in enumerate(mrefs):
            b0, _ = _agg(base[j], hows[j], None, mn)
            v, _ = _agg(ser[j], hows[j], None, mn)
            if v is None or b0 is None:
                continue
            d = v - b0
            per_metric[j].append({"id": k[1] or k[0], "factor": k[0], "sub": k[1], "name": _gname(s, k), "rows": len(groups[k]),
                                  "delta": rnd(d), "delta_pct": rnd(d / abs(b0) * 100 if b0 else 0, 3), "value": rnd(v, 4),
                                  "elasticity": rnd((d / abs(b0) * 100 if b0 else 0) / shock, 3),
                                  "outside_inputs": sum(1 for x in groups[k] if s.idx.sheets[x["sheet"]].role != "inputs"),
                                  "examples": [x["name"][:60] for x in rows[:3]],
                                  "refs": [f"{x['sheet']}!{x['row']}" for x in rows[:6]]})
    tornados = []
    for j, m in enumerate(mrefs):
        res = per_metric[j]
        tot = sum(abs(x.get("delta") or 0) for x in res) or 1
        for x in res:
            x["share"] = rnd(abs(x.get("delta") or 0) / tot, 3)
        res.sort(key=lambda x: -abs(x.get("delta") or 0))
        b0, by = _agg(base[j], hows[j], None, mn)
        tornados.append({"metric": m["name"], "ref": f"{m['sheet']}!{m['row']}", "aggregate": hows[j], "base": rnd(b0, 4),
                         "base_period": str(by), "shock_pct": -shock, "factors": res})
    return {"tornados": tornados, "effects": effects, "groups": groups, "concepts": crow, "runs": len(runs)}


def factor_tornado(s, metric, how="min", year=None, shock=10.0, level="factor", years=None, emit=None, max_rows=200):
    return scan(s, [metric], [how], shock, level, years, emit, concepts=False, max_rows=max_rows)["tornados"][0]


def t_factor_drivers(s, a, emit):
    s.require_engine()
    p = factor_tornado(s, a["metric"], a.get("aggregate", "min"), a.get("year"), float(a.get("shock_pct", 10)),
                       a.get("level", "factor"), a.get("years"), emit)
    emit("factors", p)
    return dict(p, note="Все строки-допущения фактора одновременно −shock_pct%; share — доля фактора в суммарной "
                        "чувствительности; elasticity — % изменения метрики на 1% изменения фактора.")


# ───────────────────────── сцепки факторов по пересчёту ─────────────────────────
def concept_rows(s, limit=150):
    """Расчётные строки-«понятия» между допущениями и метриками: цены, выручка, объёмы, расходы, налоги, долг."""
    rows = [p for p in (s.sem or {}).get("rows", {}).values()
            if p["kind"] in ("calc", "mixed", "metric") and p["factor"] in sm.MARKET | {"debt", "tax"}]
    rows.sort(key=lambda p: (p["kind"] != "metric", -p["fanout"]))
    return rows[:limit]


def _probe_layout(s, rows):
    """Чтение строк блоками: один прямоугольный диапазон на лист (быстро и для Excel, и для LibreOffice)."""
    from ..workbook.index import col_letter
    by = defaultdict(list)
    for p in rows:
        shx = s.idx.sheets[p["sheet"]]
        cols = [c for c, y in shx.years_for(p["row"]).items() if not s.forecast_start or y >= s.forecast_start]
        if cols:
            by[p["sheet"]].append((p["row"], sorted(cols)[:30]))
    layout = []
    for sheet, items in by.items():
        r1, r2 = min(r for r, _ in items), max(r for r, _ in items)
        c1, c2 = min(c[0] for _, c in items), max(c[-1] for _, c in items)
        if (r2 - r1 + 1) * (c2 - c1 + 1) > 60000:          # слишком разреженно — читаем построчно
            for r, cols in items:
                q = "'" + sheet.replace("'", "''") + "'"
                layout.append({"ref": f"{q}!{col_letter(cols[0])}{r}:{col_letter(cols[-1])}{r}", "r1": r, "c1": cols[0],
                               "rows": [(r, cols)]})
            continue
        q = "'" + sheet.replace("'", "''") + "'"
        layout.append({"ref": f"{q}!{col_letter(c1)}{r1}:{col_letter(c2)}{r2}", "r1": r1, "c1": c1, "rows": items,
                       "sheet": sheet})
    for L in layout:
        L.setdefault("sheet", L["ref"].split("!")[0].strip("'").replace("''", "'"))
    return layout


def _extract(layout, blocks):
    out = {}
    for L, block in zip(layout, blocks):
        for r, cols in L["rows"]:
            line = block[r - L["r1"]] if r - L["r1"] < len(block) else []
            vals = []
            for c in cols:
                j = c - L["c1"]
                v = line[j] if 0 <= j < len(line) else None
                vals.append(v if isinstance(v, (int, float)) and not isinstance(v, bool) else None)
            out[f"{L['sheet']}!{r}"] = vals
    return out


def _rel(base, new):
    num = sum(abs((n or 0) - (b or 0)) for b, n in zip(base, new) if b is not None or n is not None)
    den = sum(abs(b) for b in base if b is not None)
    return num / den if den > 1e-12 else 0.0


def _path(s, sources, target, max_nodes=120000):
    """Кратчайшая цепочка формул от строк фактора до показателя (без ограничения глубины)."""
    from collections import deque
    srcs = [(x["sheet"], x["row"]) for x in sources]
    prev = {n: None for n in srcs}
    dq = deque(srcs)
    while dq and len(prev) < max_nodes:
        n = dq.popleft()
        if n == target:
            break
        for d in s.idx.dependents.get(n, ()):
            if d not in prev:
                prev[d] = n
                dq.append(d)
    if target not in prev:
        return None
    chain, n = [], target
    while n is not None:
        chain.append(n)
        n = prev[n]
    chain.reverse()
    names, last = [], None
    for (sh, r) in chain:
        ri = s.idx.sheets[sh].rows.get(r)
        if not ri or not ri.label:
            continue
        nm = sm.name(s, sh, r)[:50]
        if nm != last:
            names.append(nm)
            last = nm
    if len(names) > 6:
        names = names[:2] + ["…"] + names[-3:]
    return {"steps": len(chain) - 1, "chain": names}


def couplings_from(s, sc, shock=10.0, limit=10):
    """Сцепки по результатам scan(..., concepts=True): какие факторы реально двигают строки-понятия."""
    concepts, effects, groups = sc["concepts"], sc["effects"], sc["groups"]
    out = []
    for p in concepts:
        key = f"{p['sheet']}!{p['row']}"
        eff = effects.get(key)
        if not eff:
            continue
        own = (p["factor"], p.get("sub") or "")
        main = {k: r for k, r in eff.items() if not k[1]}           # факторы целиком
        subs = {k: r for k, r in eff.items() if k[1]}              # отраслевые подфакторы
        tot = sum(main.values()) or 1
        foreign = []
        for k, r in sorted(main.items(), key=lambda kv: -kv[1]):
            sh = r / tot
            if k[0] == p["factor"] or k[0] in ("other", "scenario") or sh < 0.15 or r < 0.002:
                continue
            foreign.append((k, sh, r))
        if own[1]:                                                  # привязка внутри фактора: дизель ← нефть
            stot = sum(r for k, r in subs.items() if k[0] == p["factor"]) or 1
            for k, r in sorted(subs.items(), key=lambda kv: -kv[1]):
                if k[0] == p["factor"] and k != own and r >= 0.002 and r / stot >= 0.3:
                    foreign.append((k, r / stot, r))
        if p["factor"] == "debt":       # долг-итоги (револьвер, потребность) зависят от всего денежного потока —
            foreign = [x for x in foreign if x[1] >= 0.4 and x[0][0] in ("fx", "rate")]   # оставляем только явную валюту/ставку
        if not foreign or p["kind"] == "metric":
            continue
        notes, kinds = [], []
        for k, _, _ in foreign:
            pair = (p["factor"], k[0])
            if pair in NOTABLE:
                notes.append(NOTABLE[pair]); kinds.append("attention")
            elif pair in EXPECTED:
                notes.append(EXPECTED[pair]); kinds.append("expected")
            elif k[0] == p["factor"] and k[1]:
                notes.append(f"привязан к подфактору «{_gname(s, k)}»"); kinds.append("attention")
            else:
                notes.append("зависит от другого фактора — проверьте, что так задумано"); kinds.append("attention")
        out.append({"name": p["name"], "ref": key, "factor": sm.FACTORS[p["factor"]][0], "factor_id": p["factor"],
                    "src": tuple(k for k, _, _ in foreign[:2]), "groups": groups, "p": p,
                    "from": [f"{_gname(s, k)} ({sh * 100:.0f}%)" for k, sh, _ in foreign[:3]],
                    "sensitivity_pct": rnd(sum(r for _, _, r in foreign) * 100 / shock, 3),
                    "formula": p.get("formula_words"), "notes": notes,
                    "note": next((n for n, k_ in zip(notes, kinds) if k_ == "attention"), notes[0]),
                    "kind": "attention" if "attention" in kinds else "expected", "fanout": p["fanout"]})
    # схлопываем однотипные строки («Выручка внешняя: A, B, C… индексируются на инфляцию»)
    merged = {}
    for c in out:
        key = (c["src"], c["note"], c["factor_id"])
        if key in merged:
            merged[key]["members"].append(c["name"])
            continue
        c["members"] = [c["name"]]
        merged[key] = c
    out = list(merged.values())
    for c in out:
        k0 = c["src"][0]
        path = _path(s, list(c.pop("groups")[k0]), (c["p"]["sheet"], c["p"]["row"])) if c["src"] else None
        c.pop("p"); c.pop("src")
        c["chain"], c["steps"] = (path["chain"], path["steps"]) if path else (None, None)
        if len(c["members"]) > 1:
            c["name"] = f"{c['members'][0]} и ещё {len(c['members']) - 1}"
            c["members"] = c["members"][:12]
    prio = {f: i for i, f in enumerate(["price_sales", "revenue", "volume", "tariff", "price_inputs", "opex", "capex", "tax", "debt"])}
    out.sort(key=lambda x: (x["kind"] != "attention", prio.get(x["factor_id"], 9), -len(x["members"]), -(x["sensitivity_pct"] or 0)))
    return out[:limit]


def t_dependency_analysis(s, a, emit):
    s.require_engine()
    metrics = a.get("metrics") or []
    rules = a.get("rules") or []
    if not metrics and rules:
        metrics = [{"sheet": r["sheet"], "row": r["row"], "name": r.get("name"), "_op": r.get("op")} for r in rules]
    if not metrics:
        km = {m["metric"]: m["candidates"][0] for m in s.card["key_metrics"]}
        for nm in ("Чистый долг / EBITDA", "DSCR", "ISCR / ICR", "EBITDA"):
            if nm in km:
                metrics.append({"sheet": km[nm]["sheet"], "row": km[nm]["row"], "name": nm})
            if len(metrics) >= 2:
                break
    metrics = metrics[:3]
    shock = float(a.get("shock_pct", 10))
    hows = [a.get("aggregate") or ("max" if (m.get("_op") in ("<=", "<") or "долг" in (m.get("name") or "").lower()) else "min")
            for m in metrics]
    clean = [{k: v for k, v in m.items() if not k.startswith("_")} for m in metrics]
    import json as _json
    ckey = _json.dumps([[m["sheet"], int(m["row"]), h] for m, h in zip(clean, hows)] + [shock, a.get("years"),
                       bool(a.get("couplings", True))], ensure_ascii=False)
    cache = s.sem.setdefault("deps_cache", {}) if s.sem is not None else {}
    if ckey in cache and not a.get("force"):
        res = cache[ckey]
        emit("progress", {"text": "Результат из кэша модели (пересчёт не нужен; force=true — пересчитать)"})
        for t in res["_tornados_full"]:
            emit("factors", t)
        emit("semmap", res["_semmap"])
        emit("deps", {k: res[k] for k in ("findings", "couplings", "industry")} | {"cached": True})
        return {k: v for k, v in res.items() if not k.startswith("_")} | {"cached": True}
    sc = scan(s, clean, hows, shock, "factor", a.get("years"), emit, concepts=bool(a.get("couplings", True)))
    tornados = sc["tornados"]
    for t in tornados:
        emit("factors", t)
    cp = couplings_from(s, sc, shock) if a.get("couplings", True) else []
    findings = []
    for t in tornados:
        allf = [x for x in t["factors"] if x.get("delta")]
        fs = [x for x in allf if x["factor"] in sm.MARKET]
        struct = [x for x in allf if x["factor"] not in sm.MARKET and x["share"] >= 0.2]
        for x in struct[:2]:
            why = {"tax": "налоговый режим (НДПИ, пошлины, налог на прибыль) — риск изменения законодательства",
                   "debt": "условия долга (объём, график, ставка) — это параметры структуры сделки",
                   "wc": "оборотный капитал — проверьте допущения об оборачиваемости"}.get(
                x["factor"], "структурный фактор, а не рыночный")
            findings.append(f"{t['metric']}: заметное влияние «{x['name']}» ({x['share'] * 100:.0f}% чувствительности) — {why}.")
        if not fs:
            continue
        top = fs[0]
        mshare = sum(x["share"] for x in fs) or 1
        dirw = "растёт" if top["delta"] > 0 else "снижается"
        findings.append(f"{t['metric']}: главный рыночный фактор — «{top['name']}» ({top['share'] / mshare * 100:.0f}% рыночной чувствительности): "
                        f"при его снижении на {shock:g}% метрика {dirw} на {abs(top['delta_pct']):.1f}% "
                        f"(до {top['value']} против {t['base']} в базе).")
        if top["share"] / mshare >= 0.5:
            findings.append(f"Концентрация риска: устойчивость {t['metric']} держится на одном факторе — «{top['name']}».")
        if len(fs) > 1 and fs[1]["share"] / mshare >= 0.15:
            findings.append(f"Второй по силе рыночный фактор для {t['metric']} — «{fs[1]['name']}» ({fs[1]['share'] / mshare * 100:.0f}%).")
        weak = [x for x in t["factors"] if x.get("delta") is not None and x["share"] < 0.02 and x["rows"] >= 3
                and x["factor"] in ("rate", "debt", "fx", "opex", "capex")]
        if weak:
            findings.append(f"Почти не влияют на {t['metric']}: " + ", ".join(f"«{x['name']}»" for x in weak[:4])
                            + " — проверьте, что это соответствует структуре бизнеса и долга.")
        hidden = [x for x in fs[:4] if x.get("outside_inputs")]
        for x in hidden[:2]:
            part = "все" if x["outside_inputs"] >= x["rows"] else f"{x['outside_inputs']} из {x['rows']}"
            findings.append(f"Допущения фактора «{x['name']}» заданы вне листов Inputs ({part} строк) — "
                            "их легко пропустить при проверке и сверке со сценарием Аналитического хаба.")
    for c in [x for x in cp if x["kind"] == "attention"][:5]:
        chain = f" Цепочка: {' → '.join(c['chain'])}." if c.get("chain") else ""
        findings.append(f"«{c['name']}» ({c['factor'].lower()}) зависит от: {', '.join(c['from'])} — {c['note']}.{chain}")
    links = []
    for t in tornados:
        for x in t["factors"]:
            if x.get("delta"):
                links.append({"factor": x["name"], "factor_id": x["factor"], "market": x["factor"] in sm.MARKET,
                              "metric": t["metric"], "share": x["share"],
                              "sign": 1 if x["delta"] > 0 else -1, "delta_pct": x["delta_pct"]})
    semmap = {"metrics": [t["metric"] for t in tornados], "links": links, "shock_pct": -shock}
    emit("semmap", semmap)
    findings = list(dict.fromkeys(findings))
    payload = {"findings": findings, "couplings": cp, "industry": sm.summary(s.sem)["industry_name"]}
    emit("deps", payload)
    result = {"tornados": [{"metric": t["metric"], "base": t["base"], "factors": [
        dict({k: x.get(k) for k in ("name", "share", "delta", "delta_pct", "elasticity", "examples", "rows", "outside_inputs")},
             market=x.get("factor") in sm.MARKET)
        for x in t["factors"][:8]]} for t in tornados], **payload,
        "note": "Пиши выводы через названия факторов и строк (name), адреса — только в скобках для проверки.",
        "recalcs": sc["runs"]}
    if s.sem is not None:
        cache[ckey] = dict(result, _tornados_full=tornados, _semmap=semmap)
        try:
            sm.save(s.cache_dir, s.sem)
        except Exception:
            pass
    return result


TOOLS_SEMANTIC = [
    {"name": "factor_drivers", "fn": t_factor_drivers,
     "description": "Торнадо по ЭКОНОМИЧЕСКИМ ФАКТОРАМ (цены реализации, объёмы, курс, ставки, OPEX, CAPEX, налоги, долг…, "
                    "или отраслевые подфакторы при level=sub): все строки-допущения фактора шокируются вместе на −shock_pct%; "
                    "доля фактора в чувствительности, эластичность и примеры строк с понятными названиями.",
     "parameters": {"type": "object", "properties": {
         "metric": METRIC_SCHEMA, "aggregate": {"type": "string", "enum": ["min", "max", "year", "avg"]},
         "year": {"type": "string"}, "years": {"type": "array", "items": {"type": "string"}},
         "shock_pct": {"type": "number"}, "level": {"type": "string", "enum": ["factor", "sub"]}}, "required": ["metric"]}},
    {"name": "dependency_analysis", "fn": t_dependency_analysis,
     "description": "Полный анализ зависимостей метрик в экономических терминах: доли факторов в чувствительности, "
                    "концентрация риска, сцепки факторов (цена считается из курса, валютный долг, привязка цен "
                    "нефтепродуктов к нефти…), слабые и скрытые (вне Inputs) факторы, смысловая карта «фактор → метрика». "
                    "Используй для вопросов «от чего зависит», «что определяет устойчивость», и в конце сложного анализа.",
     "parameters": {"type": "object", "properties": {
         "metrics": {"type": "array", "items": METRIC_SCHEMA}, "rules": {"type": "array", "items": RULE_SCHEMA},
         "shock_pct": {"type": "number"}, "years": {"type": "array", "items": {"type": "string"}},
         "couplings": {"type": "boolean", "description": "Искать сцепки факторов пересчётом (по умолчанию да)"},
         "force": {"type": "boolean", "description": "Пересчитать, даже если результат есть в кэше модели"}}}},
]
