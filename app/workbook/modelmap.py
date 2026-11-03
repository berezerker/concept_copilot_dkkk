"""Полная карта модели и предпроверка: классы строк, драйверы метрик по всей книге,
слои листов, аудит формул. Работает на индексе (без пересчёта), поэтому быстро и для любых моделей.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from .index import WorkbookIndex, col_letter, REF_RE, STR_RE

ERR_RE = re.compile(r"^#(REF!|DIV/0!|N/A|VALUE!|NAME\?|NUM!|NULL!)")
LINK_RE = re.compile(r"^=\s*(?:'[^']+'|[\w\.А-Яа-яЁё]+)?!?\$?[A-Z]{1,3}\$?\d+\s*$")
EXT_RE = re.compile(r"\[\d+\]")


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def row_class(idx: WorkbookIndex, sheet: str, row: int, forecast_start=None) -> str:
    """input — числа без формул; link — прогноз из простых ссылок (=Лист!A1); calc — расчёт;
    mixed — числа и формулы вперемешку в прогнозе; text — без чисел."""
    sh = idx.sheets[sheet]
    ri = sh.rows.get(row)
    if not ri:
        return "empty"
    ym = sh.years_for(row)
    cols = [c for c, y in ym.items() if forecast_start is None or y >= forecast_start] if ym else list(ri.values)
    f = [c for c in cols if c in ri.formulas]
    n = [c for c in cols if c not in ri.formulas and _num(ri.values.get(c))]
    if not f and not n:
        if not ym and any(_num(v) for v in ri.values.values()):
            return "input" if not ri.formulas else "calc"
        return "text"
    if f and n:
        return "mixed"
    if n:
        return "input"
    if all(LINK_RE.match(ri.formulas[c]) for c in f):
        return "link"
    return "calc"


def upstream_inputs(idx: WorkbookIndex, sheet: str, row: int, forecast_start=None, max_nodes=60000):
    """Все строки-допущения (input/mixed), от которых по графу зависит строка: по всей книге."""
    seen, stack, out = {(sheet, row)}, [(sheet, row, 0)], []
    while stack and len(seen) < max_nodes:
        s, r, d = stack.pop()
        for p in idx.precedents.get((s, r), ()):
            if p in seen:
                continue
            seen.add(p)
            if p[0] not in idx.sheets:
                continue
            shp = idx.sheets[p[0]]
            if p[1] in shp.headers or p[1] == shp.header_row:      # строка-заголовок с годами — не допущение
                continue
            cls = row_class(idx, p[0], p[1], forecast_start)
            if cls in ("input", "mixed"):
                ri = idx.sheets[p[0]].rows.get(p[1])
                if ri is not None:
                    out.append({"sheet": p[0], "row": p[1], "label": ri.label, "unit": ri.unit, "class": cls,
                                "depth": d + 1, "fanout": len(idx.dependents.get(p, ()))})
            stack.append((p[0], p[1], d + 1))
    return out, len(seen)


def sheet_layers(idx: WorkbookIndex):
    """Слои листов: 0 — источники, дальше по длиннейшему пути (циклы между листами разрываются)."""
    names = [n for n, s in idx.sheets.items() if s.role != "separator" and (s.rows or s.n_formulas)]
    succ = defaultdict(set)
    for (a, b), n in idx.sheet_edges.items():
        if a in idx.sheets and b in idx.sheets and a != b:
            succ[a].add(b)
    layer, state = {}, {}

    def visit(n):          # длиннейший путь до n от источников
        if state.get(n) == 1:
            return 0       # цикл
        if n in layer:
            return layer[n]
        state[n] = 1
        preds = [a for a in names if n in succ[a]]
        layer[n] = 0 if not preds else 1 + max(visit(p) for p in preds)
        state[n] = 2
        return layer[n]

    for n in names:
        visit(n)
    return layer


def build_map(idx: WorkbookIndex, card: dict, forecast_start=None):
    cls_count = defaultdict(Counter)
    for sh in idx.sheets.values():
        for r in sh.rows:
            cls_count[sh.name][row_class(idx, sh.name, r, forecast_start)] += 1
    layers = sheet_layers(idx)
    sheets = []
    for name, s in idx.sheets.items():
        if name not in layers:
            continue
        c = cls_count[name]
        sheets.append({"sheet": name, "role": s.role, "layer": layers[name], "formulas": s.n_formulas,
                       "inputs": c["input"] + c["mixed"], "calc": c["calc"], "links": c["link"]})
    edges = [{"from": a, "to": b, "refs": n} for (a, b), n in sorted(idx.sheet_edges.items(), key=lambda x: -x[1])
             if a in layers and b in layers]
    metrics = []
    for m in card.get("key_metrics", [])[:9]:
        c = m["candidates"][0]
        ups, visited = upstream_inputs(idx, c["sheet"], c["row"], forecast_start)
        by_sheet = Counter(u["sheet"] for u in ups)
        by_role = Counter(idx.sheets[u["sheet"]].role for u in ups)
        n = len(ups)
        outside = sum(v for k, v in by_role.items() if k != "inputs")
        metrics.append({"metric": m["metric"], "ref": f"{c['sheet']}!{c['row']}", "label": c["label"],
                        "upstream_inputs": n, "rows_visited": visited,
                        "outside_inputs_share": round(outside / n, 3) if n else None,
                        "by_sheet": dict(by_sheet.most_common(12))})
    return {"sheets": sheets, "edges": edges[:80], "metrics": metrics,
            "classes_total": dict(sum((Counter(v) for v in cls_count.values()), Counter()))}


# ───────────────────────── предпроверка ─────────────────────────
_R1C1_RE = re.compile(
    r"(?:(?:'((?:[^']|'')+)'|([A-Za-zА-Яа-яЁё_][\w\.А-Яа-яЁё]*))!)?"
    r"(?<![A-Za-z0-9_\.])(\$?)([A-Z]{1,3})(\$?)([0-9]{1,7})(?![\w(])")


def _to_r1c1(formula, row, col):
    from openpyxl.utils import column_index_from_string as ci

    def rep(m):
        sh = m.group(1) or m.group(2)
        cabs, cl, rabs, rw = m.group(3), m.group(4), m.group(5), int(m.group(6))
        c = ci(cl)
        rr = f"R{rw}" if rabs else f"R[{rw - row}]"
        cc = f"C{c}" if cabs else f"C[{c - col}]"
        return (f"{sh}!" if sh else "") + rr + cc
    return _R1C1_RE.sub(rep, STR_RE.sub('""', formula))


def audit(idx: WorkbookIndex, forecast_start=None, limit=25):
    findings = []
    # 1) ошибки в значениях
    errs = defaultdict(list)
    for sh in idx.sheets.values():
        for r, ri in sh.rows.items():
            for c, v in ri.values.items():
                if isinstance(v, str) and ERR_RE.match(v):
                    errs[(sh.name, v)].append(f"{col_letter(c)}{r}")
    for (s, e), cells in sorted(errs.items(), key=lambda x: -len(x[1]))[:limit]:
        dep = any(idx.dependents.get((s, int(re.sub(r'\D', '', c)))) for c in cells[:50])
        findings.append({"level": "critical" if dep else "warning", "type": "Ошибка в значениях",
                         "where": f"{s}!{cells[0]}" + (f" и ещё {len(cells) - 1}" if len(cells) > 1 else ""),
                         "text": f"{len(cells)} ячеек со значением {e}" + (" — от них зависят другие расчёты" if dep else "")})
    # 2) числа, вбитые в прогнозе посреди формул
    hard = []
    for sh in idx.sheets.values():
        if sh.role in ("inputs", "contents"):
            continue
        for r, ri in sh.rows.items():
            ym = sh.years_for(r)
            fc = [c for c, y in sorted(ym.items()) if forecast_start is None or y >= forecast_start]
            f = [c for c in fc if c in ri.formulas]
            n = [c for c in fc if c not in ri.formulas and _num(ri.values.get(c))]
            if r in sh.headers or r == sh.header_row:
                continue
            if len(f) >= 2 and n and ri.label and idx.dependents.get((sh.name, r)):
                hard.append((sh.name, r, ri.label, n, len(f)))
    for s, r, lab, n, nf in hard[:limit]:
        cells = ", ".join(f"{col_letter(c)}{r}" for c in n[:4])
        findings.append({"level": "warning", "type": "Число вместо формулы в прогнозе",
                         "where": f"{s}!{col_letter(n[0])}{r}",
                         "text": f"«{lab[:60]}»: {cells} — числа среди {nf} формул строки"})
    # 3) формула выбивается из ряда
    odd = []
    for sh in idx.sheets.values():
        for r, ri in sh.rows.items():
            ym = sh.years_for(r)
            fc = [c for c, y in sorted(ym.items()) if (forecast_start is None or y >= forecast_start) and c in ri.formulas]
            if len(fc) < 4:
                continue
            norm = {c: _to_r1c1(ri.formulas[c], r, c) for c in fc}
            top, cnt = Counter(norm.values()).most_common(1)[0]
            if cnt / len(fc) >= 0.6 and cnt < len(fc):
                bad = [c for c in fc if norm[c] != top]
                if len(bad) <= 2 and idx.dependents.get((sh.name, r)):
                    odd.append((sh.name, r, ri.label, bad, ri.formulas[bad[0]], ri.formulas[next(c for c in fc if norm[c] == top)]))
    for s, r, lab, bad, f_bad, f_ok in odd[:limit]:
        findings.append({"level": "warning", "type": "Формула отличается от соседних",
                         "where": f"{s}!{col_letter(bad[0])}{r}",
                         "text": f"«{lab[:50]}»: {f_bad[:70]} (в остальных годах — {f_ok[:70]})"})
    # 4) внешние ссылки
    ext = [(sh.name, r) for sh in idx.sheets.values() for r, ri in sh.rows.items()
           if any(EXT_RE.search(f) for f in ri.formulas.values())]
    if ext:
        findings.append({"level": "warning", "type": "Ссылки на внешние книги", "where": f"{ext[0][0]}!{ext[0][1]}",
                         "text": f"{len(ext)} строк ссылаются на другие файлы — при пересчёте значения не обновятся"})
    # 5) одна и та же кредитная метрика на разных итоговых листах — с разными значениями
    keys = {"DSCR": r"^dscr$", "ISCR": r"^(iscr|icr)$", "Net Debt / EBITDA": r"^(net debt|чистый долг) ?/ ?ebitda$",
            "Debt / EBITDA": r"^(debt|долг) ?/ ?ebitda$"}
    groups = defaultdict(list)
    for sh in idx.sheets.values():
        if sh.role not in ("dashboard", "outputs"):
            continue
        for r, ri in sh.rows.items():
            lab = re.sub(r"\s+", " ", (ri.label or "").lower().replace("ё", "е")).strip()
            for k, rx in keys.items():
                if re.match(rx, lab):
                    vals = {y: ri.values.get(c) for c, y in sh.years_for(r).items() if _num(ri.values.get(c))}
                    if len(vals) >= 3:
                        groups[k].append((sh.name, r, vals))
    for k, rows in groups.items():
        ref = rows[0]
        for b_ in rows[1:]:
            common = [y for y in ref[2] if y in b_[2] and (forecast_start is None or y >= forecast_start)]
            diff = [(y, ref[2][y], b_[2][y]) for y in common
                    if abs(ref[2][y] - b_[2][y]) > 0.02 * max(1e-9, abs(ref[2][y]), abs(b_[2][y]))]
            if common and len(diff) >= max(1, len(common) // 2):
                y, va, vb = diff[0]
                findings.append({"level": "warning", "type": "Одна метрика — разные значения",
                                 "where": f"{ref[0]}!{ref[1]} ↔ {b_[0]}!{b_[1]}",
                                 "text": f"{k} {y}: {va:.3g} против {vb:.3g} (расходится в {len(diff)} из {len(common)} лет прогноза) — разные определения метрики"})
    # 6) MIN/MAX по ряду, захватывающему факт
    for sh in idx.sheets.values():
        ym = sh.years
        if not ym or forecast_start is None:
            continue
        for r, ri in sh.rows.items():
            f = next(iter(ri.formulas.values()), "")
            m = re.search(r"\b(MIN|MAX)\(\$?([A-Z]{1,3})\$?\d+:\$?([A-Z]{1,3})\$?\d+\)", f)
            if m:
                from openpyxl.utils import column_index_from_string as ci
                y0 = ym.get(ci(m.group(2)))
                if y0 and y0 < forecast_start - 1 and ri.label:
                    findings.append({"level": "warning", "type": "Минимум/максимум по факту и прогнозу",
                                     "where": f"{sh.name}!{col_letter(next(iter(ri.formulas)))}{r}",
                                     "text": f"«{ri.label[:50]}» = {m.group(1)} по диапазону с {y0} года — включает фактические годы"})
    # сворачиваем однотипные замечания на одном листе: первые 3 подробно, остальные — одной строкой
    grouped, by = [], defaultdict(list)
    for f in findings:
        by[(f["type"], f["where"].split("!")[0])].append(f)
    for (typ, sheet), fs in by.items():
        if len(fs) <= 4:
            grouped += fs
        else:
            grouped += fs[:3]
            rest = [x["where"].split("!", 1)[1] if "!" in x["where"] else x["where"] for x in fs[3:]]
            grouped.append({"level": fs[0]["level"], "type": typ, "where": f"{sheet}: ещё {len(rest)}",
                            "text": "Однотипно: " + ", ".join(rest[:12]) + ("…" if len(rest) > 12 else "")})
    findings = grouped
    order = {"critical": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda x: order[x["level"]])
    summary = Counter()
    for f in findings:
        summary[f["type"]] += int(f["where"].split("ещё ")[1]) if "ещё " in f["where"] and ": ещё" in f["where"] else 1
    return {"findings": findings[:60], "summary": dict(summary), "total": len(findings),
            "iterative_calc": idx.iterate}


def explain_cell(idx: WorkbookIndex, sheet: str, row: int, col: int, read=None):
    """Из чего состоит значение ячейки: формула и значения её слагаемых (с подписями строк)."""
    sh = idx.sheets[sheet]
    ri = sh.rows.get(row)
    if not ri:
        raise KeyError(f"{sheet}!{row} пуста")
    f = ri.formulas.get(col)
    val = ri.values.get(col)
    out = {"cell": f"{sheet}!{col_letter(col)}{row}", "label": ri.label, "unit": ri.unit,
           "year": sh.years_for(row).get(col), "value": val, "formula": f, "parts": []}
    if not f:
        out["note"] = "Ячейка — введённое значение (допущение), формулы нет"
        return out
    refs = []
    for m in REF_RE.finditer(STR_RE.sub('""', f)):
        s2 = (m.group(1) or m.group(2) or sheet).replace("''", "'")
        if m.group(5):
            refs.append((s2, f"{m.group(3)}{m.group(4)}:{m.group(5)}{m.group(6)}", int(m.group(4)), None))
        else:
            refs.append((s2, f"{m.group(3)}{m.group(4)}", int(m.group(4)), m.group(3)))
    seen = set()
    for s2, addr, r2, cl in refs[:20]:
        if (s2, addr) in seen or s2 not in idx.sheets:
            continue
        seen.add((s2, addr))
        sh2 = idx.sheets[s2]
        ri2 = sh2.rows.get(r2)
        v = None
        if cl:
            from openpyxl.utils import column_index_from_string as ci
            v = ri2.values.get(ci(cl)) if ri2 else None
        out["parts"].append({"ref": f"{s2}!{addr}", "label": ri2.label if ri2 else "", "unit": ri2.unit if ri2 else "",
                             "value": v, "is_input": bool(ri2) and (ci(cl) if cl else 0) not in ri2.formulas if cl else None})
    return out
