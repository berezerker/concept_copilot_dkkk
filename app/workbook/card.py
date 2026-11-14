"""Экспресс-анализ: детерминированная «карточка модели» — каркас, понятный агенту.

Не требует LLM: роли листов, заголовки лет, граница факт/прогноз, кандидаты в ключевые
метрики и драйверы, переключатели сценариев, крупнейшие потоки между листами.
"""
from __future__ import annotations

import re
from collections import Counter

from .card_utils import numeric_years, ref_for  # noqa: F401  (используются инструментами)
from .index import WorkbookIndex

SWITCH_RE = re.compile(r"сценари|scenario|переключат|стресс|stress|\bcase\b|вариант расч", re.I)
MARKET_FIRST = ("price_sales", "volume", "tariff", "fx", "rate", "inflation", "price_inputs", "revenue", "opex", "capex", "tax")


def _norm(s: str) -> str:
    return (s or "").lower().replace("ё", "е")


def guess_forecast_start(idx: WorkbookIndex):
    from .structure import guess_forecast_start as g
    return g(idx)


def find_drivers(idx: WorkbookIndex, catalog: dict, fs, limit=25):
    """Главные допущения модели — по структуре, а не по названиям листов: строки-допущения, от которых зависит
    больше всего показателей справочника; при равенстве — рыночные факторы и строки, на которые чаще ссылаются."""
    from .modelmap import upstream_inputs
    from .semantics import classify
    reach, meta = Counter(), {}
    for e in catalog["concepts"].values():
        if not e["live"]:
            continue
        ups, _ = upstream_inputs(idx, e["origin"]["sheet"], e["origin"]["row"], fs, max_nodes=20000)
        for u in ups:
            if not u["fanout"] or not u["label"] or SWITCH_RE.search(u["label"]):
                continue
            k = (u["sheet"], u["row"])
            reach[k] += 1
            meta[k] = u
    out, seen = [], set()

    def key(k):
        u = meta[k]
        f = classify(u["label"], "", u["unit"], "generic", u["sheet"])[0]
        return (-(f in MARKET_FIRST), -reach[k], -u["fanout"], u["depth"])

    for k in sorted(meta, key=key):
        u = meta[k]
        lab = _norm(u["label"])
        if lab in seen:
            continue
        seen.add(lab)
        out.append({"sheet": u["sheet"], "row": u["row"], "label": u["label"], "unit": u["unit"], "reach": reach[k]})
        if len(out) >= limit:
            break
    return out


def find_switches(idx: WorkbookIndex, limit=12):
    """Переключатели сценариев: подпись про сценарий/стресс + от строки что-то зависит (на любом листе)."""
    from .modelmap import row_class
    out = []
    for s in idx.sheets.values():
        if s.role in ("contents", "separator") or s.name in idx.inactive:
            continue
        for ri in s.rows.values():
            if ri.label and len(ri.label) < 80 and SWITCH_RE.search(ri.label) and idx.dependents.get((s.name, ri.row)) \
                    and row_class(idx, s.name, ri.row) in ("input", "mixed"):       # переключатель — введённое значение, не формула
                out.append({"sheet": s.name, "row": ri.row, "label": ri.label})
                if len(out) >= limit:
                    return out
    return out


def build_card(idx: WorkbookIndex, catalog: dict | None = None) -> dict:
    """Карточка модели. Ключевые метрики и драйверы берутся из справочника (catalog.py), роли листов — из структуры."""
    from . import catalog as cg
    fs = guess_forecast_start(idx)
    if catalog is None:
        catalog = cg.build_catalog(idx, fs)
    roles = Counter(s.role for s in idx.sheets.values())
    sheets = []
    for s in idx.sheets.values():
        yrs = (min(s.years.values()), max(s.years.values())) if s.years else None
        sheets.append({"name": s.name, "role": s.role, "topic": s.topic, "formulas": s.n_formulas, "numbers": s.n_numbers,
                       "rows": len(s.rows), "years": yrs, "header_row": s.header_row,
                       **({"inactive": True} if s.name in idx.inactive else {})})
    edges = sorted(idx.sheet_edges.items(), key=lambda x: -x[1])[:12]
    n_in = sum(1 for s in idx.sheets.values() if s.role == "inputs")
    return {
        "file": idx.path.split("/")[-1].split("\\")[-1],
        "sheets_total": len(idx.sheets), "formulas_total": idx.n_formulas,
        "iterative_calc": idx.iterate,
        "template": f"листов-источников допущений: {n_in}, отчётных: {roles.get('report', 0)}, расчётных: {roles.get('calc', 0)}",
        "roles": dict(roles), "forecast_start": fs,
        "sheets": sheets, "key_metrics": cg.headline_metrics(catalog),
        "drivers": find_drivers(idx, catalog, fs), "switches": find_switches(idx),
        "sheet_flows": [{"from": a, "to": b, "refs": n} for (a, b), n in edges],
        "alt_sets": [dict(a) for a in idx.alt_sets], "warnings": list(idx.warnings),
        "named_ranges": len(idx.names),
    }


def card_text(card: dict) -> str:
    """Короткое текстовое представление карточки для системного промпта."""
    lines = [f"Файл: {card['file']} · листов {card['sheets_total']} · формул {card['formulas_total']} · "
             f"{card['template']} · итеративный расчёт: {'да' if card['iterative_calc'] else 'нет'} · "
             f"прогноз по умолчанию с {card['forecast_start']} (оценка = текущий год; уточняй по данным)"]
    lines.append("Листы (роль, годы): " + "; ".join(
        f"{s['name']} [{s['role']}{', ' + str(s['years'][0]) + '–' + str(s['years'][1]) if s['years'] else ''}"
        f"{', не выбран' if s.get('inactive') else ''}]"
        for s in card["sheets"] if s["role"] != "separator")
        + ". Роли: inputs — источники допущений, calc — расчёты, report — итоговые/отчётные, contents — без расчётов.")
    if card["drivers"]:
        lines.append("Главные допущения (по числу зависящих от них показателей): " + "; ".join(
            f"{d['sheet']}!{d['row']} «{d['label'][:45]}»" for d in card["drivers"][:15]))
    if card["switches"]:
        lines.append("Сценарии/стрессы: " + "; ".join(
            f"{d['sheet']}!{d['row']} «{d['label'][:45]}»" for d in card["switches"][:8]))
    lines.append("Потоки между листами: " + "; ".join(
        f"{f['from']}→{f['to']} ({f['refs']})" for f in card["sheet_flows"][:8]))
    return "\n".join(lines)
