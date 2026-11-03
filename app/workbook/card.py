"""Экспресс-анализ: детерминированная «карточка модели» — каркас, понятный агенту.

Не требует LLM: роли листов, заголовки лет, граница факт/прогноз, кандидаты в ключевые
метрики и драйверы, переключатели сценариев, крупнейшие потоки между листами.
"""
from __future__ import annotations

import re
from collections import Counter

from .index import WorkbookIndex, col_letter

KEY_METRICS = [
    ("Выручка", ["выручка", "revenue", "sales", "доход от реализации"]),
    ("EBITDA", ["ebitda"]),
    ("Чистая прибыль", ["чистая прибыль", "net profit", "net income"]),
    ("Долг", ["долг", "debt"]),
    ("Чистый долг / EBITDA", ["net debt / ebitda", "net debt/ebitda", "чистый долг / ebitda", "netdebt/ebitda"]),
    ("DSCR", ["dscr", "покрытия долга"]),
    ("ISCR / ICR", ["iscr", "icr", "покрытия процент"]),
    ("CAPEX", ["capex", "капитальн"]),
    ("Денежные средства", ["денежные средства", "cash"]),
]
DRIVER_HINTS = ["urals", "brent", "цена", "price", "курс", "usd", "ставка", "ключевая", "mosprime", "кс ",
                "инфляц", "cpi", "добыча", "production", "объем", "объём", "киум", "тариф", "маржа", "crack", "крэк"]
SWITCH_RE = re.compile(r"сценари|scenario|переключат|стресс", re.I)
PRIORITY_ROLES = {"dashboard": 3, "outputs": 2, "is": 1, "cf": 1, "bs": 1, "debt": 1}


def _norm(s: str) -> str:
    return (s or "").lower().replace("ё", "е")


def numeric_years(idx: WorkbookIndex, sheet: str, row: int):
    sh = idx.sheet(sheet)
    ri = sh.rows.get(row)
    if not ri:
        return {}
    return {y: ri.values.get(c) for c, y in sorted(sh.years_for(row).items())
            if isinstance(ri.values.get(c), (int, float)) and not isinstance(ri.values.get(c), bool)}


def guess_forecast_start(idx: WorkbookIndex):
    """Надёжно отличить факт от прогноза по формулам нельзя (факт тоже часто считается формулами),
    поэтому по умолчанию — текущий календарный год в пределах горизонта модели.
    Агент и пользователь могут переопределить его параметром years / forecast_start."""
    import datetime
    from .periods import year_of
    years = sorted({year_of(y) for s in idx.sheets.values() if s.role in ("dashboard", "outputs", "is", "calc") for y in s.years.values()})
    if not years:
        return None
    cur = datetime.date.today().year
    return min(max(cur, years[0]), years[-1])


def find_metric_rows(idx: WorkbookIndex, synonyms, limit=3):
    out = []
    for sh in idx.sheets.values():
        if not sh.years or sh.role in ("inputs", "contents"):
            continue
        for ri in sh.rows.values():
            lab = _norm(ri.label)
            if not lab or len(lab) > 90:
                continue
            hit = next((s for s in synonyms if s in lab), None)
            if not hit:
                continue
            n = len(numeric_years(idx, sh.name, ri.row))
            if n < 3:
                continue
            score = PRIORITY_ROLES.get(sh.role, 0) * 2 + min(n, 10) / 5 + (3 if lab.startswith(hit) else 0) - len(lab) / 60
            out.append((score, sh.name, ri.row, ri.label, ri.unit))
    out.sort(key=lambda x: -x[0])
    return out[:limit]


def build_card(idx: WorkbookIndex) -> dict:
    roles = Counter(s.role for s in idx.sheets.values())
    fs = guess_forecast_start(idx)
    sheets = []
    for s in idx.sheets.values():
        yrs = (min(s.years.values()), max(s.years.values())) if s.years else None
        sheets.append({"name": s.name, "role": s.role, "formulas": s.n_formulas, "numbers": s.n_numbers,
                       "rows": len(s.rows), "years": yrs, "header_row": s.header_row})
    metrics = []
    for title, syn in KEY_METRICS:
        rows = find_metric_rows(idx, syn, limit=4)
        if rows:
            # «Живая» строка — та, что по графу зависит от допущений. Витрины со вставленными значениями
            # (пересчёт их не меняет) уходят в конец: иначе стрессы и факторы дают нулевую чувствительность.
            from .modelmap import upstream_inputs
            cands = []
            for i, r in enumerate(rows):
                try:
                    n_in = len(upstream_inputs(idx, r[1], r[2], fs, max_nodes=20000)[0])
                except Exception:
                    n_in = -1
                cands.append((0 if n_in != 0 else 1, i, {"sheet": r[1], "row": r[2], "label": r[3], "unit": r[4],
                                                       "inputs": n_in, **({"static": True} if n_in == 0 else {})}))
            cands.sort(key=lambda x: (x[0], x[1]))
            metrics.append({"metric": title, "candidates": [c[2] for c in cands[:2]]})
    switches = []
    for s in idx.sheets.values():
        if s.role not in ("dashboard", "assumptions", "calc"):
            continue
        for ri in s.rows.values():
            if ri.label and SWITCH_RE.search(ri.label) and len(switches) < 12:
                switches.append({"sheet": s.name, "row": ri.row, "label": ri.label})
    drivers, seen_lab = [], set()
    for s in idx.sheets.values():
        if s.role not in ("dashboard", "assumptions") or not s.years:
            continue
        for ri in s.rows.values():
            lab = _norm(ri.label)
            if lab and any(h in lab for h in DRIVER_HINTS) and len(numeric_years(idx, s.name, ri.row)) >= 3 \
                    and idx.dependents.get((s.name, ri.row)) and lab not in seen_lab:   # без ссылок — не драйвер; без дублей
                seen_lab.add(lab)
                drivers.append({"sheet": s.name, "row": ri.row, "label": ri.label, "unit": ri.unit})
    standard = any(s.role == "outputs" and s.name.lower() == "outputs" for s in idx.sheets.values()) and \
        any(s.role == "inputs" for s in idx.sheets.values())
    edges = sorted(idx.sheet_edges.items(), key=lambda x: -x[1])[:12]
    return {
        "file": idx.path.split("/")[-1].split("\\")[-1],
        "sheets_total": len(idx.sheets), "formulas_total": idx.n_formulas,
        "iterative_calc": idx.iterate,
        "template": "есть листы Inputs/Outputs шаблона АС CF" if standard else "нестандартная структура (нет листов Inputs/Outputs)",
        "roles": dict(roles), "forecast_start": fs,
        "sheets": sheets, "key_metrics": metrics, "drivers": drivers[:25], "switches": switches,
        "sheet_flows": [{"from": a, "to": b, "refs": n} for (a, b), n in edges],
    }


def card_text(card: dict) -> str:
    """Короткое текстовое представление карточки для системного промпта."""
    lines = [f"Файл: {card['file']} · листов {card['sheets_total']} · формул {card['formulas_total']} · "
             f"{card['template']} · итеративный расчёт: {'да' if card['iterative_calc'] else 'нет'} · "
             f"прогноз по умолчанию с {card['forecast_start']} (оценка = текущий год; уточняй по данным)"]
    lines.append("Листы (роль, годы): " + "; ".join(
        f"{s['name']} [{s['role']}{', ' + str(s['years'][0]) + '–' + str(s['years'][1]) if s['years'] else ''}]"
        for s in card["sheets"] if s["role"] != "separator"))
    if card["key_metrics"]:
        lines.append("Ключевые метрики (кандидаты): " + "; ".join(
            f"{m['metric']} → " + ", ".join(f"{c['sheet']}!{c['row']} «{c['label'][:50]}»"
                                            + (" [не зависит от допущений — для стрессов не брать]" if c.get("static") else "")
                                            for c in m["candidates"])
            for m in card["key_metrics"]))
    if card["drivers"]:
        lines.append("Возможные драйверы: " + "; ".join(
            f"{d['sheet']}!{d['row']} «{d['label'][:45]}»" for d in card["drivers"][:15]))
    if card["switches"]:
        lines.append("Сценарии/стрессы: " + "; ".join(
            f"{d['sheet']}!{d['row']} «{d['label'][:45]}»" for d in card["switches"][:8]))
    lines.append("Потоки между листами: " + "; ".join(
        f"{f['from']}→{f['to']} ({f['refs']})" for f in card["sheet_flows"][:8]))
    return "\n".join(lines)


def ref_for(idx: WorkbookIndex, sheet: str, row: int, cols) -> list[str]:
    """Сгруппировать столбцы строки в непрерывные диапазоны 'Лист'!K5:P5."""
    q = "'" + sheet.replace("'", "''") + "'"
    cols = sorted(cols)
    groups, start, prev = [], None, None
    for c in cols:
        if start is None:
            start = prev = c
        elif c == prev + 1:
            prev = c
        else:
            groups.append((start, prev))
            start = prev = c
    if start is not None:
        groups.append((start, prev))
    return [f"{q}!{col_letter(a)}{row}:{col_letter(b)}{row}" for a, b in groups]
