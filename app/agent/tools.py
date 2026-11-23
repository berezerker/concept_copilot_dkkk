"""Инструменты агента. Каждый инструмент: JSON-схема для LLM + функция (session, args, emit) -> dict.

Результаты для LLM компактные: числа округлены, длинные списки обрезаны.
"""
from __future__ import annotations

import difflib
import math
import re

from ..workbook.card_utils import numeric_years, ref_for
from ..workbook.modelmap import is_scalar_row, row_class, scalar_cols
from ..workbook.periods import norm_period, norm_periods

MAX_SCENARIOS, MAX_METRICS, MAX_SHOCKS, MAX_ROWS = 12, 8, 10, 25
MAX_EXPANDED = 250


def rnd(v, sig=5):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return v
    if v == 0 or not math.isfinite(v):
        return v
    return round(v, max(0, sig - 1 - int(math.floor(math.log10(abs(v))))))


def norm(s):
    return (s or "").lower().replace("ё", "е")


SYNONYMS = [
    ["dscr", "покрытия долга", "debt service coverage"],
    ["iscr", "icr", "покрытия процент", "interest coverage"],
    ["выручк", "revenue", "sales", "реализац"],
    ["ebitda"],
    ["чистый долг", "net debt"],
    ["долг", "debt", "кредит", "заем", "займ"],
    ["ставк", "rate", "ключев", "кс ", "mosprime", "libor", "процент"],
    ["курс", "usd/rub", "exchange", "валют"],
    ["нефть", "urals", "brent", "crude", "oil"],
    ["цен", "price"],
    ["тариф", "tariff"],
    ["капекс", "capex", "капитальн", "инвестиц"],
    ["добыч", "production", "выпуск", "output"],
    ["объем", "объём", "volume", "продаж", "sales"],
    ["инфляц", "cpi", "ppi", "индекс цен"],
    ["прибыл", "profit", "income"],
    ["денежн", "cash"],
    ["дивиденд", "dividend"],
    ["налог", "tax", "ндпи", "met", "акциз", "пошлин", "duty"],
    ["опекс", "opex", "операционные расходы", "operating cost", "себестоим"],
    ["киум", "загрузк", "utilization"],
    ["сценари", "scenario", "стресс", "stress"],
]


def expand(query):
    q = norm(query)
    toks = [t for t in re.split(r"[\s,;/()]+", q) if t]
    groups = []
    for t in toks:
        g = {t}
        for syn in SYNONYMS:
            if any(t.startswith(s.strip()) or s.strip().startswith(t) for s in syn if len(t) >= 3):
                g |= {s.strip() for s in syn}
        groups.append(g)
    return q, groups


def row_kind(idx, sheet, row, years=None):
    sh = idx.sheet(sheet)
    ri = sh.rows.get(row)
    if not ri:
        return "empty"
    cols = [c for c, y in sh.years_for(row).items() if (years is None or y in years)]
    nf = sum(1 for c in cols if c in ri.formulas)
    nc = sum(1 for c in cols if c not in ri.formulas and isinstance(ri.values.get(c), (int, float)))
    if nf and nc:
        return "mixed"
    return "formula" if nf else ("input" if nc else "text")


def _scalar_cells(sh, ri, only_inputs=False):
    """Числовые ячейки одиночного параметра: {адрес: значение}."""
    from ..workbook.index import col_letter
    out = {}
    for c in scalar_cols(sh, ri):
        v = ri.values.get(c)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and not (only_inputs and c in ri.formulas):
            out[f"{col_letter(c)}{ri.row}"] = rnd(v)
    return dict(list(out.items())[:12])


def years_slice(d: dict, years=None, start=None, max_n=14):
    items = sorted(d.items())
    if years:
        items = [(y, v) for y, v in items if y in set(years)]
    elif start:
        items = [(y, v) for y, v in items if y >= start - 1]
    return {str(y): rnd(v) for y, v in items[:max_n]}


# ───────────────────────── планирование ─────────────────────────
def t_create_plan(s, a, emit):
    steps = [str(x) for x in (a.get("steps") or [])][:10]
    if not steps:
        raise ValueError("Нужен непустой список шагов")
    s.plan = {"goal": a.get("goal", ""), "steps": [{"title": t, "status": "pending", "note": ""} for t in steps]}
    emit("plan", s.plan)
    return {"ok": True, "steps": len(steps), "hint": "Выполняй шаги по порядку и отмечай их через update_plan."}


def t_update_plan(s, a, emit):
    if not s.plan:
        raise ValueError("План ещё не создан")
    i = int(a.get("step", 0)) - 1
    if not 0 <= i < len(s.plan["steps"]):
        raise ValueError(f"Шаг должен быть от 1 до {len(s.plan['steps'])}")
    st = s.plan["steps"][i]
    status = a.get("status", "done")
    if status not in ("in_progress", "done", "skipped"):
        raise ValueError("status: in_progress, done или skipped")
    if status == "skipped" and not a.get("note"):
        raise ValueError("Для пропущенного шага нужна причина в note")
    st["status"] = status
    st["note"] = a.get("note", "")[:300]
    if a.get("new_steps"):
        for t in a["new_steps"][:5]:
            s.plan["steps"].append({"title": str(t), "status": "pending", "note": ""})
    emit("plan", s.plan)
    return {"ok": True}


# ───────────────────────── обзор модели ─────────────────────────
def t_get_model_card(s, a, emit):
    c = dict(s.card)
    c["sheets"] = [x for x in c["sheets"] if x["role"] != "separator"]
    c["engine"] = s.engine.name if s.engine_open else f"нет ({s.engine_error})"
    c["forecast_start"] = s.forecast_start
    return c


def t_get_model_manifest(s, a, emit):
    """Единый паспорт модели: готовность, показатели, параметры, зоны, связи и ограничения."""
    from ..workbook import manifest as mf
    manifest = s.refresh_manifest() or mf.build(s)
    section = a.get("section") or "summary"
    query = norm(a.get("query") or "")
    limit = max(1, min(int(a.get("limit", 40) or 40), 200))
    if section == "summary":
        payload = mf.summary(manifest)
    elif section == "metrics":
        rows = manifest["metrics"]
        if query:
            rows = [x for x in rows if query in norm(x["name"]) or query in norm(x["catalog_id"])
                    or query in norm(x["location"]["label"])]
        payload = {"readiness": manifest["readiness"], "metrics": rows[:limit], "found": len(rows)}
    elif section == "parameters":
        rows = manifest["parameters"]
        if query:
            rows = [x for x in rows if any(query in norm(v) for v in
                    (x["name"], x["location"]["label"], x["location"]["block"], x["factor"]["name"],
                     x["location"]["ref"]))]
        payload = {"readiness": manifest["readiness"], "parameters": rows[:limit], "found": len(rows)}
    elif section == "zones":
        payload = {"zones": manifest["zones"][:limit], "edges": manifest["edges"][:limit],
                   "found": len(manifest["zones"]), "readiness": manifest["readiness"]}
    elif section == "scenarios":
        payload = {"scenario_sets": manifest["scenario_sets"], "switches": manifest["switches"],
                   "readiness": manifest["readiness"]}
    elif section == "limitations":
        payload = {"limitations": manifest["limitations"], "readiness": manifest["readiness"]}
    elif section == "full":
        payload = dict(manifest)
        payload["parameters"] = payload["parameters"][:limit]
        payload["parameters_total"] = len(manifest["parameters"])
        payload["edges"] = payload["edges"][:limit]
    else:
        raise ValueError("section: summary, metrics, parameters, zones, scenarios, limitations или full")
    emit("manifest", payload)
    return payload


def t_resolve_target(s, a, emit):
    """Слова пользователя → однозначная сущность manifest либо список кандидатов для уточнения."""
    from ..workbook import manifest as mf
    if getattr(s, "manifest", None) is None:
        s.refresh_manifest()
    return mf.resolve(s, a["query"], a.get("kind", "auto"), a.get("intent", "read"),
                      int(a.get("limit", 8) or 8))


def t_list_sheets(s, a, emit):
    out = []
    for sh in s.idx.sheets.values():
        if sh.role == "separator":
            continue
        labels = [ri.label for ri in sh.rows.values() if ri.label and len(numeric_years(s.idx, sh.name, ri.row)) >= 3][:8]
        out.append({"sheet": sh.name, "role": sh.role, "formulas": sh.n_formulas, "rows": len(sh.rows),
                    "years": [min(sh.years.values()), max(sh.years.values())] if sh.years else None,
                    "sample_rows": labels})
    return {"sheets": out}


def t_search_rows(s, a, emit):
    query = a.get("query", "")
    limit = min(int(a.get("limit", 10) or 10), MAX_ROWS)
    only_sheet = a.get("sheet")
    only_inputs = bool(a.get("only_inputs"))
    q, groups = expand(query)
    hits = []
    for sh in s.idx.sheets.values():
        if only_sheet and norm(sh.name) != norm(only_sheet):
            continue
        if sh.role in ("contents", "separator"):
            continue
        for ri in sh.rows.values():
            lab = norm(ri.label)
            if not lab or ri.row in sh.headers:
                continue
            matched = sum(1 for g in groups if any(t in lab for t in g))
            if groups and matched == 0:
                continue
            nums = numeric_years(s.idx, sh.name, ri.row)
            cells = None
            if len(nums) < 2:
                if not is_scalar_row(sh, ri):
                    continue
                cells = _scalar_cells(sh, ri)               # одиночный параметр: «Ставка налога | 20%»
                if not cells:
                    continue
                kind = {"input": "input", "mixed": "input"}.get(row_class(s.idx, sh.name, ri.row, s.forecast_start), "formula")
            else:
                kind = row_kind(s.idx, sh.name, ri.row, {y for y in sh.years_for(ri.row).values() if s.forecast_start and y >= s.forecast_start} or None)
            if only_inputs and kind not in ("input", "mixed"):
                continue
            deps = len(s.idx.dependents.get((sh.name, ri.row), ()))
            score = matched * 3 + difflib.SequenceMatcher(None, q, lab[:80]).ratio() * 2 - len(lab) / 200
            score += 0.6 if deps else -0.6                  # строка, от которой что-то зависит, полезнее справочной
            if sh.name in s.idx.inactive:
                score -= 2                                  # невыбранный набор сценария
            if norm(sh.name) in q:
                score += 1
            hits.append((score, sh.name, ri.row, ri.label, ri.unit, kind, nums, cells))
    hits.sort(key=lambda x: -x[0])
    out = []
    for sc, sh, r, lab, unit, kind, nums, cells in hits[:limit]:
        from ..workbook.semantics import info as _hinfo
        hi = _hinfo(s, sh, r)
        row = {"sheet": sh, "row": r, "label": lab[:120], "name": hi["name"], "factor": hi["factor_name"],
               "meaning": hi["desc"] or None, "unit": unit, "kind": kind,
               "dependents": len(s.idx.dependents.get((sh, r), ())),
               "values": years_slice(nums, start=s.forecast_start, max_n=6)}
        if cells:
            row["cells"] = cells
            row["scalar"] = True
        if sh in s.idx.inactive:
            row["inactive_set"] = True
        out.append(row)
    return {"query": query, "found": len(hits), "rows": out,
            "note": "kind: input — числа (можно шоковать), formula — расчёт, mixed — факт числами, прогноз формулами; "
                    "dependents — сколько строк напрямую ссылаются на эту (0 = строка ни на что не влияет, шоковать бесполезно); "
                    "scalar — параметр одной ячейкой (шок по sheet+row меняет её); inactive_set — лист невыбранного сценария"}


def t_read_rows(s, a, emit):
    sheet = a["sheet"]
    rows = [int(r) for r in (a.get("rows") or [])][:MAX_ROWS]
    years = a.get("years")
    sh = s.idx.sheet(sheet)
    out = []
    for r in rows:
        ri = sh.rows.get(r)
        if not ri:
            out.append({"row": r, "error": "пустая строка"})
            continue
        nums = numeric_years(s.idx, sh.name, r)
        sample_f = next((ri.formulas[c] for c, y in sorted(sh.years_for(r).items())
                         if c in ri.formulas and (not s.forecast_start or y >= s.forecast_start)), None)
        cells = None
        if not nums:   # строка без лет — одиночные параметры: показываем ячейки
            from ..workbook.index import col_letter
            cells = {f"{col_letter(c)}{r}": rnd(v) for c, v in sorted(ri.values.items())
                     if isinstance(v, (int, float)) and not isinstance(v, bool)}
            cells = dict(list(cells.items())[:12]) or None
        from ..workbook.semantics import info as _hinfo
        hi = _hinfo(s, sh.name, r)
        out.append({"row": r, "label": ri.label, "name": hi["name"], "factor": hi["factor_name"], "meaning": hi["desc"] or None,
                    "unit": ri.unit, "kind": row_kind(s.idx, sh.name, r),
                    "dependents": len(s.idx.dependents.get((sh.name, r), ())), "cells": cells,
                    "values": years_slice(nums, years=years, max_n=25),
                    "formula_example": sample_f[:200] if sample_f else None})
    return {"sheet": sh.name, "rows": out}


def t_trace_row(s, a, emit):
    sheet, row = a["sheet"], int(a["row"])
    direction = a.get("direction", "precedents")
    depth = max(1, min(int(a.get("depth", 1) or 1), 3))
    sh = s.idx.sheet(sheet)
    graph = s.idx.precedents if direction == "precedents" else s.idx.dependents
    seen, frontier, levels = {(sh.name, row)}, [(sh.name, row)], []
    for d in range(depth):
        nxt = []
        for node in frontier:
            for p in sorted(graph.get(node, ())):
                if p in seen:
                    continue
                seen.add(p)
                nxt.append(p)
        lvl = []
        for (ps, pr) in nxt:
            pinfo = s.idx.sheets.get(ps)
            ri = pinfo.rows.get(pr) if pinfo else None
            if ri and ri.label:
                from ..workbook.semantics import info as _hinfo
                hi = _hinfo(s, ps, pr)
                lvl.append({"name": hi["name"], "factor": hi["factor_name"], "ref": f"{ps}!{pr}", "kind": row_kind(s.idx, ps, pr)})
        levels.append({"depth": d + 1, "count": len(nxt), "rows": lvl[:30]})
        frontier = nxt[:200]
    by_sheet = {}
    for lv in levels:
        for x in lv["rows"]:
            by_sheet[x["factor"]] = by_sheet.get(x["factor"], 0) + 1
    from ..workbook.semantics import name as _hname
    return {"start": f"«{_hname(s, sh.name, row)}» ({sh.name}!{row})",
            "direction": direction, "levels": levels, "by_factor": by_sheet}


# ───────────────────────── стресс и подбор ─────────────────────────
def reachable(idx, start, targets, limit=200000):
    """Дойдёт ли изменение строки start до строк targets по графу зависимостей."""
    targets = set(targets)
    seen, stack = {start}, [start]
    found = set()
    while stack and len(seen) < limit:
        n = stack.pop()
        for d in idx.dependents.get(n, ()):
            if d in seen:
                continue
            seen.add(d)
            if d in targets:
                found.add(d)
                if found == targets:
                    return found
            stack.append(d)
    return found


def _sources_hint(idx, sheet, row, n=6):
    out = []
    for ps, pr in sorted(idx.precedents.get((sheet, row), ()))[:40]:
        sh = idx.sheets.get(ps)
        ri = sh.rows.get(pr) if sh else None
        if ri and ri.label and idx.dependents.get((ps, pr)):
            out.append(f"{ps}!{pr} «{ri.label[:50]}»")
        if len(out) >= n:
            break
    return out



def _shock_cols(s, sheet, row, years):
    sh = s.idx.sheet(sheet)
    if row in sh.headers or row == sh.header_row:
        raise ValueError(f"{sh.name}!{row} — строка-заголовок с годами, её шоковать нельзя")
    ri = sh.rows.get(row)
    if not ri:
        raise ValueError(f"Строка {sheet}!{row} пуста")
    ym = sh.years_for(row)
    if is_scalar_row(sh, ri):
        # одиночный параметр (ставка, коэффициент, цена одной ячейкой): шок меняет введённые числа строки;
        # если в строке только формула — подменяем её значение
        sc = scalar_cols(sh, ri)
        typed = [c for c in sc if c not in ri.formulas]
        cols = {c: None for c in (typed or sc) if isinstance(ri.values.get(c), (int, float)) and not isinstance(ri.values.get(c), bool)}
        if not cols:
            raise ValueError(f"В строке {sh.name}!{row} нет числового значения")
        return sh, ri, dict(sorted(cols.items()))
    if not ym:
        raise ValueError(f"На листе «{sh.name}» нет шапки периодов, и строка {row} не похожа на параметр — задай шок через cell или range")
    if years:
        sel = set(norm_periods(years))
        cols = {c: y for c, y in ym.items() if y in sel}
    else:
        cols = {c: y for c, y in ym.items() if s.forecast_start is None or y >= s.forecast_start}
    cols = {c: y for c, y in cols.items() if c in ri.formulas or isinstance(ri.values.get(c), (int, float))}
    if not cols:
        raise ValueError(f"В строке {sh.name}!{row} нет числовых значений за указанные годы")
    return sh, ri, dict(sorted(cols.items()))


def _apply_op(v, op, value):
    if not isinstance(v, (int, float)):
        v = 0.0
    if op == "pct":
        return v * (1 + value / 100.0)
    if op == "add":
        return v + value
    if op == "mul":
        return v * value
    if op == "set":
        return value
    if op == "shift":
        raise ValueError("op=shift применяется только к строке по годам (sheet+row или match), не к cell/range")
    raise ValueError(f"Неизвестная операция {op}: используй pct, add, mul или set")


def _match_rows(s, query, sheet=None, include_formulas=False, max_rows=20):
    """Строки по смыслу подписи по всей книге (или на листе) — цели шока «по смыслу»."""
    # для шоков — строго слова запроса (без синонимов), чтобы «тариф» не задевал «цены»
    toks = [t[:max(4, len(t) - 2)] for t in re.split(r"[\s,;/()]+", norm(query)) if len(t) >= 3]
    def group(t):
        g = {t}
        for syn in SYNONYMS:
            if any(t.startswith(x.strip()[:4]) and len(x.strip()) >= 3 for x in syn):
                g |= {x.strip() for x in syn if len(x.strip()) >= 3}
        return re.compile(r"(?<![а-яa-z0-9])(" + "|".join(re.escape(x) for x in g) + ")")
    pats = [group(t) for t in toks]
    out = []
    for sh in s.idx.sheets.values():
        if sheet and norm(sh.name) != norm(sheet):
            continue
        if sh.role in ("contents", "separator"):
            continue
        if sh.name in s.idx.inactive and not sheet:           # невыбранные наборы сценариев на результат не влияют
            continue
        for r, ri in sh.rows.items():
            if r in sh.headers or r == sh.header_row:
                continue
            lab = norm(ri.label)
            if not lab or not pats or not all(p.search(lab) for p in pats):
                continue
            if not s.idx.dependents.get((sh.name, r)):
                continue
            kind = row_kind(s.idx, sh.name, r)
            scalar = is_scalar_row(sh, ri)
            if scalar:
                kind = "input" if row_class(s.idx, sh.name, r, s.forecast_start) == "input" else "formula"
            if kind in ("input", "mixed") or (include_formulas and kind == "formula"):
                ys = [y for y in numeric_years(s.idx, sh.name, r) if not s.forecast_start or y >= s.forecast_start]
                if ys or scalar:
                    out.append((sh.name, r))
    return out[:max_rows], len(out)


def _verify_direct(s, e, notes):
    """Способ шока direct выбран по графу формул, а граф может обещать лишнее (переключатели, INDEX/MATCH).
    Один раз на показатель проверяем пересчётом: подменяем показатель на −10 % и смотрим, сдвинулись ли итоги,
    до которых он должен дойти. Не сдвинулись — строка справочная, шокируем через допущения. Итог запоминается."""
    from ..workbook import catalog as cg
    if e.get("direct_verified") is not None or not s.engine_open:
        return "direct" if e.get("direct_verified", True) else "drivers"
    targets = [r for r in (e.get("reaches") or []) + ["ebitda", "net_income"] if r in s.catalog["concepts"] and r != e["id"]]
    targets = list(dict.fromkeys(targets))[:6]
    if not targets:
        return "direct"
    try:
        mref = _metric_refs(s, [{"concept": r} for r in targets], None)
        reads = [r for m in mref for r in m["refs"]]
        base = _read_metrics(s.engine.read(reads), mref)
        ov, _, _, _ = _build_overrides(s, [{"sheet": e["origin"]["sheet"], "row": e["origin"]["row"], "op": "pct", "value": -10, "_quiet": True}], None)
        got = _read_metrics(s.engine.scenario(ov, reads, reads)[0], mref)
        moved = [t_ for t_, b, g in zip(targets, base, got)
                 if any(b.get(y) is not None and g.get(y) is not None and abs(g[y] - b[y]) > 1e-6 * max(abs(b[y]), 1e-9) for y in b)]
    except Exception:
        return "direct"                       # проверить не удалось — остаёмся при выводе по графу
    e["direct_verified"] = bool(moved)
    e["reaches"] = moved
    if not moved:
        e["shock"] = "drivers"
        e["note"] = "прямая подмена строки не меняет итоги (проверено пересчётом) — шок выполняется через допущения"
        notes.append(f"«{e['name']}»: подмена строки {e['origin']['sheet']}!{e['origin']['row']} не меняет итоговые показатели "
                     "(проверено пересчётом) — шок выполнен через допущения, из которых показатель считается")
    cg.save(s.cache_dir, s.catalog)
    s.refresh_manifest()
    return "direct" if moved else "drivers"


def _concept_shock(s, sh_, notes):
    """Шок по показателю справочника («выручка −10 %»): напрямую или через допущения."""
    from ..workbook import catalog as cg, semantics as sm
    from ..workbook.modelmap import upstream_inputs
    e = cg.resolve(s, sh_["concept"])
    mode = sh_.get("mode") or e["shock"]
    if mode == "direct" and not sh_.get("mode") and e["kind"] != "ratio":
        mode = _verify_direct(s, e, notes)
    op, value = sh_.get("op", "pct"), float(sh_.get("value", 0))
    o = e["origin"]
    keep = {k: v for k, v in sh_.items() if k in ("op", "value", "years")}
    if mode == "direct":
        if e["kind"] == "ratio":
            raise ValueError(f"«{e['name']}» — коэффициент, его нельзя задать напрямую: шокируй показатели, из которых он "
                             "считается (mode=drivers или отдельные показатели/факторы).")
        reach = [cg.BY_ID[r]["name"] for r in e.get("reaches") or [] if r in cg.BY_ID]
        notes.append(f"Показатель «{e['name']}» изменён напрямую в строке {o['sheet']}!{o['row']} «{o['label'][:40]}» (формула на время сценария "
                     "заменена значениями). Пересчитано всё, что считается ОТ этого показателя"
                     + (f" (в т.ч. {', '.join(reach[:5])})" if reach else "")
                     + "; то, что считается от его составляющих напрямую (например, затраты от объёма), не меняется.")
        return [dict(keep, sheet=o["sheet"], row=o["row"])]
    # ── через допущения: меняем строки-допущения, из которых считается показатель ──
    if op != "pct":
        raise ValueError("Шок показателя через допущения (mode=drivers) задаётся в процентах (op=pct)")
    via = sh_.get("via") or e.get("via") or sorted(sm.MARKET)
    ups, _ = upstream_inputs(s.idx, o["sheet"], o["row"], s.forecast_start)
    ups = [u for u in ups if u["fanout"]]
    rows = [u for u in ups if sm.info(s, u["sheet"], u["row"])["factor"] in via]
    if not rows:
        rows = ups
        notes.append(f"«{e['name']}»: допущений факторов {', '.join(via)} выше показателя нет — изменены все его допущения")
    if not rows:
        raise ValueError(f"У показателя «{e['name']}» ({o['sheet']}!{o['row']}) нет допущений выше по расчёту: шокируй его напрямую (mode=direct)")
    rows = sorted(rows, key=lambda u: -u["fanout"])[:200]
    mk = lambda v: [dict({k: x for k, x in keep.items() if k != "value"}, value=v, sheet=u["sheet"], row=u["row"], _quiet=True, _inputs_only=True) for u in rows]  # noqa: E731
    pct = value
    if sh_.get("calibrate", True) and s.engine_open and e["kind"] != "ratio" and value:
        # подгонка: на сколько изменить допущения, чтобы сам показатель изменился на заданный процент
        ckey = ("_calib", e["id"], tuple(sorted(via)), tuple(sh_.get("years") or ()))
        ratio = s.base_cache.get(ckey)
        if ratio is None:
            mref = _metric_refs(s, [{"sheet": o["sheet"], "row": o["row"]}], sh_.get("years"))
            reads = [r for m in mref for r in m["refs"]]
            base = _read_metrics(s.engine.read(reads), mref)[0]
            ov, _, _, _ = _build_overrides(s, mk(value), None)
            got = _read_metrics(s.engine.scenario(ov, reads, reads)[0], mref)[0]
            fs_ = s.forecast_start                             # сравниваем только периоды, в которых был шок
            ch = [(got[y] - b) / abs(b) for y, b in base.items() if b not in (None, 0) and got.get(y) is not None
                  and abs(b) > 1e-12 and (sh_.get("years") or fs_ is None or y >= fs_)]
            ratio = (sum(ch) / len(ch) * 100 / value) if ch else 0.0
            s.base_cache[ckey] = ratio
        if abs(ratio) < 1e-6:
            raise ValueError(f"Допущения ({', '.join(via)}) не меняют «{e['name']}»: выбери другие факторы (via) или mode=direct")
        pct = max(-95.0, min(500.0, value / ratio))
        notes.append(f"«{e['name']}» {value:+g}% получен через допущения ({', '.join(via)}; {len(rows)} строк): они изменены на "
                     f"{pct:+.3g}% — подобрано пробным пересчётом, в среднем за период; точное изменение показателя по годам смотри в таблице")
    else:
        notes.append(f"«{e['name']}»: на {value:+g}% изменены его допущения ({', '.join(via)}; {len(rows)} строк) — сам показатель "
                     "изменится на другую величину")
    return mk(pct)


def _expand_shocks(s, shocks, notes):
    """concept / factor / match → набор строк; остальные шоки — как есть."""
    import math
    from ..workbook import manifest as mf
    prepared = []
    # Проверяем все именованные цели до пробных пересчётов concept/drivers.
    for source in shocks:
        sh_ = dict(source)
        if sh_.get("op", "pct") not in ("pct", "add", "mul", "set", "shift") or not math.isfinite(float(sh_.get("value", 0))):
            raise ValueError("Шок должен иметь допустимую операцию и конечное числовое значение")
        if sh_.get("target"):
            if any(sh_.get(k) is not None for k in ("sheet", "row", "cell", "range", "concept", "factor", "sub", "match")):
                raise ValueError("target нельзя смешивать с другими способами выбора цели")
            resolved = mf.resolve(s, sh_.pop("target"), intent="shock")
            if resolved["status"] != "resolved":
                raise ValueError("Цель шока " + resolved["status"] + ": " + "; ".join(
                    f"{c['id']} ({c['name']})" for c in resolved["candidates"]))
            selected = resolved["selected"]
            if selected["kind"] == "metric":
                sh_["concept"] = selected["catalog_id"]
            else:
                sh_.update(sheet=selected["location"]["sheet"], row=selected["location"]["row"], _inputs_only=True)
            notes.extend(resolved["warnings"])
        prepared.append(sh_)
    out = []
    for sh_ in prepared:
        if sh_.get("concept") and not sh_.get("row"):
            out += _concept_shock(s, sh_, notes)
        elif sh_.get("factor") or sh_.get("sub"):                     # экономический фактор из смыслового слоя
            from ..workbook import semantics as sm
            rows = sm.factor_rows(s, sh_.get("factor"), sh_.get("sub"))
            what = sh_.get("sub") or sh_.get("factor")
            if not rows:
                raise ValueError(f"Для фактора «{what}» нет строк-допущений в модели. Факторы модели: "
                                 + ", ".join(f"{x['id']} ({x['rows']})" for x in sm.summary(s.sem)["factors"]))
            rows = sorted(rows, key=lambda p: -p["fanout"])
            lim = int(sh_.get("max_rows", 200) or 200)
            if len(rows) > lim:
                notes.append(f"Фактор «{what}»: {len(rows)} строк, шок применён к {lim} самым влиятельным")
            for p in rows[:lim]:
                d = {k: v for k, v in sh_.items() if k not in ("factor", "sub", "max_rows", "include_formulas", "match")}
                d.update(sheet=p["sheet"], row=p["row"], _quiet=True, _inputs_only=True)
                out.append(d)
        elif sh_.get("match"):
            rows, total = _match_rows(s, sh_["match"], sh_.get("sheet"), bool(sh_.get("include_formulas")),
                                      int(sh_.get("max_rows", 20) or 20))
            if not rows:
                raise ValueError(f"По запросу «{sh_['match']}» не найдено строк-допущений"
                                 + (f" на листе {sh_.get('sheet')}" if sh_.get("sheet") else "")
                                 + ". Попробуй другую формулировку или include_formulas=true.")
            if total > len(rows):
                notes.append(f"«{sh_['match']}»: найдено {total} строк, шок применён к первым {len(rows)} (max_rows)")
            for (sh, r) in rows:
                d = {k: v for k, v in sh_.items() if k not in ("match", "max_rows", "include_formulas")}
                d.update(sheet=sh, row=r)
                out.append(d)
        else:
            out.append(sh_)
    return out


def _build_overrides(s, shocks, metric_nodes=None):
    """shocks -> overrides для движка + описание. Проверяет, что шок дойдёт до метрик.
    Виды целей шока: row (строка по годам) · cell (ячейка) · range (любой диапазон любого листа) ·
    match (все строки-допущения по смыслу подписи)."""
    overrides, desc, notes, unreach, pending = [], [], [], [], []
    shocks = _expand_shocks(s, shocks, notes)
    for sh_ in shocks[:MAX_EXPANDED]:
        op, value = sh_.get("op", "pct"), float(sh_.get("value", 0))
        if sh_.get("range"):                                  # произвольный диапазон
            rng = str(sh_["range"]).replace("$", "")
            if "!" in rng:
                sname, addr = rng.rsplit("!", 1)
                sname = sname.strip("'")
            else:
                sname, addr = sh_["sheet"], rng
            shi = s.idx.sheet(sname)
            m = re.match(r"^([A-Z]{1,3})(\d+):([A-Z]{1,3})(\d+)$", addr.upper())
            if not m:
                raise ValueError(f"Неверный диапазон {addr}: нужен вид K10:P25")
            r1, r2 = sorted((int(m.group(2)), int(m.group(4))))
            if (r2 - r1 + 1) * 40 > 20000:
                raise ValueError("Слишком большой диапазон (больше ~500 строк)")
            q = "'" + shi.name.replace("'", "''") + "'"
            ref = f"{q}!{m.group(1)}{r1}:{m.group(3)}{r2}"
            cur = s.engine.read([ref])[0]
            newv = [[_apply_op(v, op, value) if isinstance(v, (int, float)) and not isinstance(v, bool) else v
                     for v in line] for line in cur]
            overrides.append({"ref": ref, "values": newv})
            if metric_nodes and not any(reachable(s.idx, (shi.name, rr), metric_nodes) for rr in range(r1, r2 + 1)):
                unreach.append(f"Диапазон {shi.name}!{addr} по графу не связан с метриками.")
            nf = sum(1 for rr in range(r1, r2 + 1) for c in (shi.rows.get(rr).formulas if shi.rows.get(rr) else {}))
            if nf:
                notes.append(f"{shi.name}!{addr}: формулы в диапазоне временно заменены значениями")
            desc.append(f"{shi.name}!{addr} (диапазон, {r2 - r1 + 1} строк) {op} {value:g}")
            continue
        if sh_.get("cell"):                                   # одиночная ячейка: параметр/переключатель
            shi = s.idx.sheet(sh_["sheet"])
            addr = str(sh_["cell"]).replace("$", "").upper()
            m = re.match(r"^([A-Z]{1,3})(\d+)$", addr)
            if not m:
                raise ValueError(f"Неверный адрес ячейки {addr}")
            node = (shi.name, int(m.group(2)))
            if metric_nodes and not reachable(s.idx, node, metric_nodes):
                unreach.append(f"Ячейка {shi.name}!{addr} по графу зависимостей не связана с выбранными метриками. "
                               "Проверь, что это нужный параметр (read_rows / trace_row).")
            q = "'" + shi.name.replace("'", "''") + "'"
            ref = f"{q}!{addr}"
            cur = s.engine.read([ref])[0][0][0]
            overrides.append({"ref": ref, "values": [[_apply_op(cur, op, value)]]})
            desc.append(f"{shi.name}!{addr} {op} {value:g} (было {rnd(cur)})")
            continue
        if not sh_.get("sheet") or sh_.get("row") in (None, ""):
            raise ValueError("Шок должен иметь sheet+row, cell, range или match")
        sheet, row = sh_["sheet"], int(sh_["row"])
        try:
            sh, ri, cols = _shock_cols(s, sheet, row, sh_.get("years"))
            if sh_.get("_inputs_only"):
                # Факторы и параметры меняют только введённые числа. Формулы в
                # смешанной строке должны продолжать рассчитываться из этих чисел.
                cols = {c: y for c, y in cols.items() if c not in ri.formulas}
                if not cols:
                    raise ValueError(f"{sh.name}!{row}: в выбранном периоде нет введённых чисел")
        except ValueError as e:
            if not sh_.get("_quiet"):
                raise
            notes.append(f"Пропущена строка фактора: {e}")
            continue
        if metric_nodes and not sh_.get("_quiet"):
            hit = reachable(s.idx, (sh.name, row), metric_nodes) | ({(sh.name, row)} & set(metric_nodes))
            if not hit:
                hint = _sources_hint(s.idx, sh.name, row)
                unreach.append(
                    f"Шок {sh.name}!{row} «{ri.label[:40]}» по графу зависимостей не доходит ни до одной метрики "
                    f"(у строки {len(s.idx.dependents.get((sh.name, row), ()))} прямых зависимых). "
                    + (f"Источники этой строки, которые стоит шоковать: {'; '.join(hint)}." if hint else
                       "Найди другую строку-драйвер через search_rows / trace_row(precedents)."))
            if len(hit) < len(metric_nodes):
                miss = [f"{a}!{b}" for a, b in metric_nodes if (a, b) not in hit]
                notes.append(f"Шок {sh.name}!{row} не влияет на {', '.join(miss)} по графу зависимостей")
        refs = ref_for(s.idx, sh.name, row, cols.keys())
        pending.append((sh, ri, row, cols, refs, op, value))       # значения читаем одним запросом после цикла
    # ── одно чтение из Excel для всех строк (раньше — по запросу на строку: на Mac ~0,05–0,1 с каждый) ──
    vc = s.base_cache.setdefault("_vals", {})
    need = []
    for sh, ri, row, cols, refs, op, value in pending:
        if any(c in ri.formulas for c in cols) or tuple(refs) not in vc:
            need += refs
    got = dict(zip(need, s.engine.read(need))) if need else {}
    for sh, ri, row, cols, refs, op, value in pending:
        if any(c in ri.formulas for c in cols):
            base = [got[r] for r in refs]
        else:                                   # чистые допущения после прогонов восстанавливаются — кэшируем
            key = tuple(refs)
            if key not in vc:
                vc[key] = [got[r] for r in refs]
            base = vc[key]
        flat = [v for block in base for line in block for v in line]
        if op == "shift" and all(y is None for y in cols.values()):
            raise ValueError(f"{sh.name}!{row} — одиночный параметр, сдвигать во времени нечего")
        if op == "shift":                                    # сдвиг ряда во времени (задержка запуска/CAPEX)
            n = int(round(value))
            nums = [v if isinstance(v, (int, float)) else 0.0 for v in flat]
            if n > 0:
                new = [nums[0]] * min(n, len(nums)) + nums[:-n] if n < len(nums) else [nums[0]] * len(nums)
            elif n < 0:
                new = nums[-n:] + [nums[-1]] * min(-n, len(nums)) if -n < len(nums) else [nums[-1]] * len(nums)
            else:
                new = nums
        else:
            new = [_apply_op(v, op, value) for v in flat]
        k = 0
        for ref, block in zip(refs, base):
            n = len(block[0])
            overrides.append({"ref": ref, "values": [new[k:k + n]]})
            k += n
        if any(c in ri.formulas for c in cols):
            notes.append(f"{sh.name}!{row}: формулы в шокируемых ячейках временно заменены значениями")
        what = f"сдвиг на {int(round(value)):+d} пер." if op == "shift" else (f"{value:+g}%" if op == "pct" else f"{op} {value:g}")
        from ..workbook.semantics import name as _hname
        ys_ = [y for y in cols.values() if y is not None]
        span = f"за {min(ys_)}–{max(ys_)}" if ys_ else "(параметр)"
        desc.append(f"«{_hname(s, sh.name, row)[:60]}» {what} {span} ({sh.name}!{row})")
    if len(desc) > 8:
        desc = desc[:6] + [f"… и ещё {len(desc) - 6} строк"]
    return _merge_overrides(overrides), desc, notes, unreach


_ROW_REF = re.compile(r"^(.*)!([A-Z]{1,3})(\d+):([A-Z]{1,3})(\d+)$")


def _merge_overrides(overrides):
    """Соседние строки с одинаковыми столбцами — одним блоком: в Excel это одна запись вместо десятков."""
    parsed, other = [], []
    for o in overrides:
        m = _ROW_REF.match(o["ref"])
        if m and m.group(3) == m.group(5) and len(o["values"]) == 1:
            parsed.append((m.group(1), m.group(2), m.group(4), int(m.group(3)), o["values"][0]))
        else:
            other.append(o)
    parsed.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
    out, cur = [], None
    for sh, c1, c2, r, vals in parsed:
        if cur and cur["sh"] == sh and cur["c1"] == c1 and cur["c2"] == c2 and r == cur["r2"] + 1:
            cur["rows"].append(vals)
            cur["r2"] = r
            continue
        if cur:
            out.append(cur)
        cur = {"sh": sh, "c1": c1, "c2": c2, "r1": r, "r2": r, "rows": [vals]}
    if cur:
        out.append(cur)
    merged = [{"ref": f"{b['sh']}!{b['c1']}{b['r1']}:{b['c2']}{b['r2']}", "values": b["rows"]} for b in out]
    return merged + other


def _metric_refs(s, metrics, years):
    out = []
    for m in metrics[:MAX_METRICS]:
        if m.get("concept") and not m.get("sheet"):           # показатель по справочнику
            from ..workbook import catalog as cg
            e = cg.resolve(s, m["concept"])
            m = dict(m, sheet=e["primary"]["sheet"], row=e["primary"]["row"], name=m.get("name") or e["name"])
        sh = s.idx.sheet(m["sheet"])
        row = int(m["row"])
        ri = sh.rows.get(row)
        if not ri:
            raise ValueError(f"Метрика {m['sheet']}!{row} — пустая строка")
        cols = sh.year_cols(norm_periods(years), row) if years else {
            c: y for c, y in sh.years_for(row).items() if s.forecast_start is None or y >= s.forecast_start - 1}
        cols = {c: y for c, y in sorted(cols.items())
                if isinstance(ri.values.get(c), (int, float)) and not isinstance(ri.values.get(c), bool)}
        cols = dict(list(cols.items())[:40])
        if not cols:
            raise ValueError(f"У метрики {sh.name}!{row} нет значений за выбранные годы")
        refs = ref_for(s.idx, sh.name, row, cols.keys())
        out.append({"sheet": sh.name, "row": row, "name": m.get("name") or ri.label[:60], "unit": ri.unit,
                    "years": list(cols.values()), "refs": refs})
    return out


def _flatten(blocks):
    return [v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
            for b in blocks for line in b for v in line]


def _read_metrics(values, mrefs):
    res, k = [], 0
    for m in mrefs:
        n = len(m["refs"])
        flat = _flatten(values[k:k + n])
        k += n
        res.append(dict(zip(m["years"], flat)))
    return res


def _base(s, mrefs):
    refs = [r for m in mrefs for r in m["refs"]]
    key = tuple(refs)
    if key not in s.base_cache:
        s.base_cache[key] = _read_metrics(s.engine.read(refs), mrefs)
    return s.base_cache[key]


def t_run_stress(s, a, emit):
    s.require_engine()
    scenarios = (a.get("scenarios") or [])[:MAX_SCENARIOS]
    metrics = a.get("metrics") or []
    if not scenarios or not metrics:
        raise ValueError("Нужны scenarios и metrics")
    mrefs = _metric_refs(s, metrics, a.get("years"))
    reads = [r for m in mrefs for r in m["refs"]]
    base = _base(s, mrefs)
    results, notes_all = [], []
    for i, sc in enumerate(scenarios):
        emit("progress", {"text": f"Сценарий {i + 1}/{len(scenarios)}: {sc.get('name', '')}"})
        overrides, desc, notes, unreach = _build_overrides(s, sc.get("shocks") or [], {(m["sheet"], m["row"]) for m in mrefs})
        notes_all += notes
        vals, iters = s.engine.scenario(overrides, reads, reads)
        unchanged = _flatten(vals) == [v for m_ in _base(s, mrefs) for v in m_.values()]
        if unchanged and unreach:
            raise ValueError(f"Сценарий «{sc.get('name')}»: метрики не изменились. " + " ".join(unreach))
        if unchanged:
            notes_all.append(f"Сценарий «{sc.get('name')}»: метрики не изменились — проверь драйвер, единицы и годы шока")
        elif unreach:
            notes_all.append("Граф зависимостей не увидел связь шока с метрикой (INDIRECT/OFFSET/имена), "
                             "но пересчёт показал влияние — результату пересчёта можно доверять")
        results.append({"name": sc.get("name") or f"Сценарий {i + 1}", "shocks": desc, "iters": iters,
                        "values": _read_metrics(vals, mrefs)})
    # ответ для LLM
    out_metrics = []
    for j, m in enumerate(mrefs):
        rows = {"База": {str(y): rnd(v) for y, v in base[j].items()}}
        summ = []
        for r in results:
            vals = r["values"][j]
            rows[r["name"]] = {str(y): rnd(v) for y, v in vals.items()}
            nums = [(y, v) for y, v in vals.items() if v is not None]
            if nums:
                ymin = min(nums, key=lambda x: x[1])
                summ.append({"scenario": r["name"], "min": [ymin[0], rnd(ymin[1])],
                             "max_abs_delta": rnd(max((abs(v - (base[j].get(y) or 0)) for y, v in nums), default=0))})
        out_metrics.append({"metric": f"{m['sheet']}!{m['row']} {m['name']}", "unit": m["unit"],
                            "table": rows, "summary": summ})
    chart = {"metrics": [{"name": m["name"], "ref": f"{m['sheet']}!{m['row']}", "unit": m["unit"],
                          "years": m["years"],
                          "series": [{"name": "База", "values": [base[j].get(y) for y in m["years"]]}] +
                                    [{"name": r["name"], "values": [r["values"][j].get(y) for y in m["years"]]}
                                     for r in results]} for j, m in enumerate(mrefs)],
             "scenarios": [{"name": r["name"], "shocks": r["shocks"]} for r in results]}
    emit("chart", chart)
    s.results.append(chart)
    return {"metrics": out_metrics,
            "scenarios": [{"name": r["name"], "shocks": r["shocks"], "recalc_iterations": r["iters"]} for r in results],
            "notes": sorted(set(notes_all)),
            "model_restored": True}


def t_goal_seek(s, a, emit):
    s.require_engine()
    shock = a["shock"]
    metric = a["metric"]
    agg = a.get("aggregate", "min")
    target = float(a["target"])
    lo, hi = float(a["lower"]), float(a["upper"])
    years = a.get("years")
    mrefs = _metric_refs(s, [metric], years)
    reads = [r for m in mrefs for r in m["refs"]]

    def f(x):
        ov, desc, _, _ = _build_overrides(s, [dict(shock, value=x)], {(m["sheet"], m["row"]) for m in mrefs})
        vals, _ = s.engine.scenario(ov, reads, reads)
        series = _read_metrics(vals, mrefs)[0]
        nums = {y: v for y, v in series.items() if v is not None and (years or not s.forecast_start or y >= s.forecast_start)}
        if agg == "year":
            y = norm_period(a.get("year"))
            m = next((v for k, v in nums.items() if k == y), None)
        elif agg == "max":
            m = max(nums.values())
        else:
            m = min(nums.values())
        return m, series

    trace = []
    flo, _ = f(lo)
    fhi, _ = f(hi)
    trace += [{"x": rnd(lo), "metric": rnd(flo)}, {"x": rnd(hi), "metric": rnd(fhi)}]
    emit("progress", {"text": f"Границы: {lo:g} → {rnd(flo)}, {hi:g} → {rnd(fhi)}"})
    if flo == fhi:
        return {"converged": False, "trace": trace,
                "message": "Метрика не меняется при изменении драйвера — драйвер не влияет на метрику. "
                           "Найди другой драйвер (trace_row precedents/dependents)."}
    if (flo - target) * (fhi - target) > 0:
        return {"converged": False, "trace": trace,
                "message": "Цель не достигается внутри границ — расширь lower/upper или проверь направление."}
    best = None
    for it in range(int(a.get("max_iter", 14))):
        mid = (lo + hi) / 2
        fm, series = f(mid)
        trace.append({"x": rnd(mid), "metric": rnd(fm)})
        emit("progress", {"text": f"Итерация {it + 1}: значение {rnd(mid)} → метрика {rnd(fm)}"})
        best = (mid, fm, series)
        if abs(fm - target) <= float(a.get("tolerance", 0.005)) * max(1, abs(target)):
            break
        if (flo - target) * (fm - target) <= 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    x, fx, series = best
    return {"converged": True, "value": rnd(x), "metric_value": rnd(fx), "aggregate": agg,
            "metric_by_year": {str(y): rnd(v) for y, v in series.items()}, "iterations": trace,
            "shock": {k: v for k, v in shock.items()}, "model_restored": True}


# ───────────────────────── схемы для LLM ─────────────────────────
SHOCK_SCHEMA = {
    "type": "object",
    "properties": {
        "target": {"type": "string", "description": "id сущности из resolve_target (metric:… / parameter:…) или однозначное название. Неоднозначная цель блокирует шок до записи."},
        "sheet": {"type": "string", "description": "Лист строки-драйвера"},
        "row": {"type": "integer", "description": "Номер строки-драйвера (ряд по годам)"},
        "cell": {"type": "string", "description": "Вместо row: адрес одной ячейки (например E87) — для одиночных параметров, стресс-надстроек и переключателей сценариев"},
        "range": {"type": "string", "description": "Вместо row: любой диапазон любого листа, например 'Opex!K10:P25' — шок ко всем числам диапазона"},
        "match": {"type": "string", "description": "Вместо row: смысловой запрос — шок ко ВСЕМ строкам-допущениям с такой подписью по всей модели (например 'тариф' или 'Urals'); sheet сужает поиск"},
        "factor": {"type": "string", "description": "Вместо row: экономический фактор из смыслового слоя (price_sales, volume, fx, rate, opex, capex, tax, debt, …) — шок ко всем его строкам-допущениям сразу. Список факторов — в карточке модели"},
        "sub": {"type": "string", "description": "Вместо factor: отраслевой подфактор (например crude_price, power_price, capacity)"},
        "concept": {"type": "string", "description": "Вместо row: показатель из справочника модели (revenue, ebitda, capex, opex, debt, … или его название) — агент сам знает, где он лежит. Список — в справочнике (get_catalog)"},
        "mode": {"type": "string", "enum": ["direct", "drivers"], "description": "Для concept: direct — подменить сам показатель (всё, что считается от него, пересчитается); drivers — изменить допущения, из которых он считается, так, чтобы показатель изменился на value %. По умолчанию — как указано в справочнике"},
        "via": {"type": "array", "items": {"type": "string"}, "description": "Для concept+drivers: через какие факторы менять показатель (например ['price_sales'] — только ценами)"},
        "include_formulas": {"type": "boolean", "description": "Для match: шоковать и расчётные строки"},
        "max_rows": {"type": "integer", "description": "Для match: максимум строк (по умолчанию 20)"},
        "op": {"type": "string", "enum": ["pct", "add", "mul", "set", "shift"],
               "description": "pct — изменение в %, add — прибавить, mul — умножить, set — задать значение, "
                              "shift — сдвинуть ряд по времени на value периодов (1 = задержка на год/период: запуск, ввод мощностей, CAPEX)"},
        "value": {"type": "number", "description": "Величина шока (для pct: -10 = минус 10%)"},
        "years": {"type": "array", "items": {"type": "string"}, "description": "Годы ('2026') или периоды ('2026Q1', '2026H2', '2026-03') шока; по умолчанию весь прогноз. Год в квартальной/месячной модели = все его периоды"},
    },
    "required": ["op", "value"],
}
METRIC_SCHEMA = {
    "type": "object",
    "description": "Метрика: либо concept (показатель из справочника: dscr, net_debt_ebitda, revenue…), либо sheet+row",
    "properties": {"concept": {"type": "string", "description": "id или название показателя из справочника модели"},
                   "sheet": {"type": "string"}, "row": {"type": "integer"},
                   "name": {"type": "string", "description": "Короткое имя метрики"}},
}


# ───────────────────────── справочник модели ─────────────────────────
def resolve_concepts(s, args, _key=None):
    """Всюду, где инструмент ждёт метрику или ковенант (sheet+row), можно передать concept — подставляем строку
    из справочника. Цели шоков (shocks/drivers) не трогаем: их разбирает _expand_shocks (там важен способ шока)."""
    if isinstance(args, list):
        return [resolve_concepts(s, x, _key) for x in args]
    if not isinstance(args, dict):
        return args
    if _key in ("shocks", "shock", "drivers", "driver", "x", "y", "scenarios", "fields"):
        return {k: (resolve_concepts(s, v, k) if k in ("metrics", "metric", "rules", "rule") else v) for k, v in args.items()}
    out = {k: resolve_concepts(s, v, k) for k, v in args.items()}
    if out.get("concept") and not out.get("sheet") and out.get("row") in (None, "") and _key is not None:
        from ..workbook import catalog as cg
        e = cg.resolve(s, out["concept"])
        out.update(sheet=e["primary"]["sheet"], row=e["primary"]["row"])
        out.setdefault("name", e["name"])
    return out


def t_get_catalog(s, a, emit):
    """Справочник модели: показатели (где лежат и как шокируются), факторы и параметры-допущения."""
    from ..workbook import catalog as cg, semantics as sm
    cat = s.catalog
    q = norm(a.get("query") or "")
    group, factor = a.get("group"), a.get("factor")
    concepts = []
    for e in cat["concepts"].values():
        if group and e["group"] != group:
            continue
        if q and q not in norm(e["name"]) and q not in e["id"] and q not in norm(e["primary"]["label"]):
            continue
        p, o = e["primary"], e["origin"]
        concepts.append({"id": e["id"], "name": e["name"], "group": cg.GROUPS.get(e["group"], e["group"]), "kind": e["kind"],
                         "where": f"{p['sheet']}!{p['row']}", "label": p["label"][:70], "unit": p["unit"],
                         "computed_in": f"{o['sheet']}!{o['row']}" if (o["sheet"], o["row"]) != (p["sheet"], p["row"]) else None,
                         "shock": e["shock"], "direct_shock_reaches": [cg.BY_ID[r]["name"] for r in e.get("reaches", []) if r in cg.BY_ID],
                         "depends_on_inputs": e["inputs"], "found_by": e["match"], "confidence": e["confidence"],
                         "note": e.get("note") or (None if e["live"] else "не зависит от допущений — значения вставлены"),
                         "alternatives": [f"{x['sheet']}!{x['row']} «{x['label'][:40]}»" for x in e["alternatives"][:3]] or None})
    params = []
    if q or factor or a.get("params"):
        for key, p in (s.sem or {}).get("rows", {}).items():
            if p["kind"] not in ("input", "mixed"):
                continue
            if factor and p["factor"] != factor and p.get("sub") != factor:
                continue
            if q and q not in norm(p["name"]) and q not in norm(p["label"]):
                continue
            params.append({"ref": key, "name": p["name"], "factor": p["factor"], "sub": p.get("sub") or None, "unit": p["unit"],
                           "scalar": p.get("scalar") or None, "values": p["values"], "influences": len(p["metrics"]), "fanout": p["fanout"]})
        params.sort(key=lambda x: (-x["influences"], -x["fanout"]))
    su = sm.summary(s.sem) if s.sem else {"factors": [], "subfactors": []}
    payload = {"concepts": concepts, "missing": [cg.BY_ID[m]["name"] for m in cat["missing"] if m in cg.BY_ID],
               "factors": su["factors"], "subfactors": su["subfactors"],
               "parameters": params[:int(a.get("limit", 40) or 40)], "parameters_found": len(params),
               "alt_sets": cat.get("alt_sets"), "switches": s.card.get("switches"), "warnings": cat.get("warnings"),
               "how_found": cat.get("llm") or "лексикон + структура (без LLM)"}
    emit("catalog", payload)
    payload["note"] = ("Шок по показателю: {concept: id, op: pct, value}. shock=direct — показатель подменяется напрямую; "
                       "drivers — меняются его допущения (с подгонкой). Метрики и ковенанты тоже можно задавать через concept. "
                       "Параметры (parameters) показываются по query или factor. Если показатель найден неверно — catalog_set.")
    return payload


def t_find_parameters(s, a, emit):
    """Реестр параметров модели: найти введённые числа (цены, курсы, коэффициенты, ставки…) по смыслу запроса."""
    from ..workbook import params as pr
    limit = int(a.get("limit", 20) or 20)
    hits, total = pr.search(s, a.get("query") or "", a.get("factor"), a.get("sheet"), limit)
    out = [{"ref": h["ref"], "sheet": h["sheet"], "row": h["row"], "name": h["name"], "label": h["label"][:90], "block": h["block"][:90] or None,
            "unit": h["unit"] or None, "factor": h["factor"], "sub": h["sub"], "scalar": h["scalar"] or None, "values": h["values"],
            "influences": h["influences"] or None, "fanout": h["fanout"], "meaning": h["desc"] or None} for h in hits]
    payload = {"query": a.get("query") or "", "found": total, "parameters": out, "overview": pr.overview(s)}
    emit("params", payload)
    res = {"found": total, "parameters": out,
           "note": "Шок параметра: {sheet, row, op, value} в run_stress (scalar — параметр одной ячейкой, шок меняет её). "
                   "influences — на какие ключевые показатели параметр влияет по формулам (пусто — до них не доходит). "
                   "Несколько похожих строк (по сегментам, продуктам) — уточни у пользователя или шокируй все и скажи об этом."}
    if not out:
        res["overview"] = pr.overview(s)
        res["note"] = ("По запросу ничего не найдено. Попробуй другие слова (в том числе по-английски, как в подписях модели), "
                       "фильтр factor или sheet; overview показывает, какие листы и блоки параметров есть. "
                       "Подписи без пояснений (коды, сокращения) находятся лучше после разбора модели LLM.")
    elif not a.get("query"):
        res["overview"] = pr.overview(s)
    return res


def t_catalog_set(s, a, emit):
    """Закрепить показатель за строкой модели (исправить справочник или добавить свой показатель)."""
    from ..workbook import catalog as cg
    cid = norm(str(a["concept"])).replace(" ", "_")
    known = next((c["id"] for c in cg.CONCEPTS if c["id"] == cid or norm(c["name"]) == norm(str(a["concept"]))), None)
    cid = known or cid
    e = cg.apply_pin(s.idx, s.catalog, cid, a["sheet"], int(a["row"]), s.forecast_start, a.get("name") or (None if known else str(a["concept"])))
    cg.save(s.cache_dir, s.catalog)
    s.refresh_card()
    s.refresh_manifest()
    return {"ok": True, "concept": e["id"], "name": e["name"], "where": f"{e['primary']['sheet']}!{e['primary']['row']}",
            "computed_in": f"{e['origin']['sheet']}!{e['origin']['row']}", "shock": e["shock"],
            "direct_shock_reaches": e.get("reaches"), "note": "Закрепление сохранено для этой модели и применится при следующих загрузках"}

TOOLS = [
    {"name": "create_plan", "fn": t_create_plan,
     "description": "Создать план решения задачи (3–7 шагов). Вызывай первым, до любых других инструментов.",
     "parameters": {"type": "object", "properties": {
         "goal": {"type": "string", "description": "Цель в одной фразе"},
         "steps": {"type": "array", "items": {"type": "string"}, "description": "Шаги плана"}},
         "required": ["goal", "steps"]}},
    {"name": "update_plan", "fn": t_update_plan,
     "description": "Отметить статус шага плана (номер с 1). Можно добавить новые шаги, если план пришлось расширить.",
     "parameters": {"type": "object", "properties": {
         "step": {"type": "integer"}, "status": {"type": "string", "enum": ["in_progress", "done", "skipped"]},
         "note": {"type": "string", "description": "Короткий итог шага"},
         "new_steps": {"type": "array", "items": {"type": "string"}}}, "required": ["step", "status"]}},
    {"name": "get_model_manifest", "fn": t_get_model_manifest,
     "description": "Единый паспорт загруженной модели и её готовность: найденные показатели, изменяемые параметры, "
                    "зоны и связи, активные наборы сценариев, уверенность, доказательства и ограничения. Используй summary "
                    "для знакомства с незнакомой моделью, остальные sections — для детализации по надобности.",
     "parameters": {"type": "object", "properties": {
         "section": {"type": "string", "enum": ["summary", "metrics", "parameters", "zones", "scenarios", "limitations", "full"]},
         "query": {"type": "string", "description": "Фильтр по названию/подписи/блоку"},
         "limit": {"type": "integer", "description": "До 200 записей; full тоже ограничивает параметры и связи"}}}},
    {"name": "resolve_target", "fn": t_resolve_target,
     "description": "Разрешить слова пользователя в показатель или изменяемый параметр Model Manifest перед чтением или шоком. "
                    "Возвращает status resolved / ambiguous / not_found, кандидатов, уверенность и предупреждения. "
                    "При ambiguous не выбирай строку молча: уточни или исследуй кандидатов.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Показатель или параметр словами пользователя"},
         "kind": {"type": "string", "enum": ["auto", "metric", "parameter"]},
         "intent": {"type": "string", "enum": ["read", "shock"]},
         "limit": {"type": "integer"}}, "required": ["query"]}},
    {"name": "get_catalog", "fn": t_get_catalog,
     "description": "Справочник модели: какие показатели (выручка, EBITDA, CAPEX, долг, DSCR…) где лежат, где считаются и как их шоковать "
                    "(напрямую или через допущения), какие есть факторы и параметры-допущения (в т.ч. одиночные ячейки), наборы сценариев, "
                    "ограничения. query — поиск по названию; factor — параметры одного фактора.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string"}, "group": {"type": "string", "enum": ["pnl", "cf", "bs", "ratio", "custom"]},
         "factor": {"type": "string", "description": "Показать параметры-допущения этого фактора/подфактора"},
         "params": {"type": "boolean", "description": "Показать все параметры-допущения"}, "limit": {"type": "integer"}}}},
    {"name": "find_parameters", "fn": t_find_parameters,
     "description": "Реестр параметров ЭТОЙ модели: все введённые числа, от которых что-то зависит (цены, курсы, коэффициенты, "
                    "ставки, нормативы — на любом листе, рядом по годам или одной ячейкой). Ищет по смыслу запроса в названии, "
                    "подписи, блоке, листе и факторе («цена бензина», «коэффициент загрузки», «ставка по кредиту»). "
                    "ВСЕГДА вызывай перед шоком, если пользователь называет параметр словами. Без query — обзор реестра.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Что ищем, словами пользователя"},
         "factor": {"type": "string", "description": "Ограничить фактором или подфактором"},
         "sheet": {"type": "string", "description": "Ограничить листом"}, "limit": {"type": "integer"}}}},
    {"name": "catalog_set", "fn": t_catalog_set,
     "description": "Закрепить показатель справочника за строкой модели: исправить неверно найденный (concept = id из справочника) "
                    "или добавить свой («выручка сегмента А»). Строку сначала найди через search_rows. Закрепление сохраняется для модели.",
     "parameters": {"type": "object", "properties": {
         "concept": {"type": "string"}, "sheet": {"type": "string"}, "row": {"type": "integer"},
         "name": {"type": "string", "description": "Название для своего показателя"}}, "required": ["concept", "sheet", "row"]}},
    {"name": "get_model_card", "fn": t_get_model_card,
     "description": "Карточка модели (экспресс-анализ): роли листов, годы, кандидаты ключевых метрик и драйверов, переключатели сценариев, потоки между листами.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "list_sheets", "fn": t_list_sheets,
     "description": "Список листов с ролями, числом формул, годами и примерами строк.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "search_rows", "fn": t_search_rows,
     "description": "Найти строки модели по смыслу подписи (рус/англ, синонимы: DSCR, EBITDA, ставка, курс, Urals…). Возвращает лист, номер строки, тип (input/formula/mixed) и значения по годам.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string"}, "sheet": {"type": "string", "description": "Искать только на этом листе"},
         "only_inputs": {"type": "boolean", "description": "Только строки с числовыми допущениями"},
         "limit": {"type": "integer"}}, "required": ["query"]}},
    {"name": "read_rows", "fn": t_read_rows,
     "description": "Прочитать строки листа: подпись, единицы, значения по годам, пример формулы прогноза.",
     "parameters": {"type": "object", "properties": {
         "sheet": {"type": "string"}, "rows": {"type": "array", "items": {"type": "integer"}},
         "years": {"type": "array", "items": {"type": "string"}, "description": "Годы ('2026') или периоды ('2026Q1', '2026H2', '2026-03')"}}, "required": ["sheet", "rows"]}},
    {"name": "trace_row", "fn": t_trace_row,
     "description": "Проследить зависимости строки: precedents — откуда берётся значение (найти драйвер-источник), dependents — на что влияет (проверить, что шок дойдёт до метрики).",
     "parameters": {"type": "object", "properties": {
         "sheet": {"type": "string"}, "row": {"type": "integer"},
         "direction": {"type": "string", "enum": ["precedents", "dependents"]},
         "depth": {"type": "integer", "description": "1–3"}}, "required": ["sheet", "row", "direction"]}},
    {"name": "run_stress", "fn": t_run_stress,
     "description": "Прямой стресс-тест: применить шоки к строкам-драйверам в копии модели, пересчитать (с итерациями до сходимости) и вернуть метрики по годам для базы и каждого сценария. Модель после прогона восстанавливается.",
     "parameters": {"type": "object", "properties": {
         "scenarios": {"type": "array", "items": {"type": "object", "properties": {
             "name": {"type": "string"}, "shocks": {"type": "array", "items": SHOCK_SCHEMA}},
             "required": ["name", "shocks"]}},
         "metrics": {"type": "array", "items": METRIC_SCHEMA},
         "years": {"type": "array", "items": {"type": "string"}, "description": "Годы ('2026') или периоды ('2026Q1', '2026H2', '2026-03')"}},
         "required": ["scenarios", "metrics"]}},
    {"name": "goal_seek", "fn": t_goal_seek,
     "description": "Обратный подбор: найти величину шока драйвера, при которой метрика (min/max по годам или конкретный год) равна целевому значению. Бинарный поиск между lower и upper.",
     "parameters": {"type": "object", "properties": {
         "shock": {"type": "object", "properties": {
             "sheet": {"type": "string"}, "row": {"type": "integer"},
             "op": {"type": "string", "enum": ["pct", "add", "set"]},
             "years": {"type": "array", "items": {"type": "string"}, "description": "Годы ('2026') или периоды ('2026Q1', '2026H2', '2026-03')"}}, "required": ["sheet", "row", "op"]},
         "metric": METRIC_SCHEMA,
         "aggregate": {"type": "string", "enum": ["min", "max", "year"]},
         "year": {"type": "string", "description": "Год или период для aggregate=year"},
         "years": {"type": "array", "items": {"type": "string"}, "description": "Годы ('2026') или периоды ('2026Q1', '2026H2', '2026-03')"},
         "target": {"type": "number"}, "lower": {"type": "number"}, "upper": {"type": "number"}},
         "required": ["shock", "metric", "target", "lower", "upper"]}},
]
TOOL_MAP = {t["name"]: t for t in TOOLS}


def tool_specs():
    return [{"name": t["name"], "description": t["description"], "parameters": t["parameters"]} for t in TOOLS]
