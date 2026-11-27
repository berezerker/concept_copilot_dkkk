"""Мелкие функции чтения индекса, общие для карточки, справочника и инструментов."""
from __future__ import annotations

from .index import WorkbookIndex, col_letter


def numeric_years(idx: WorkbookIndex, sheet: str, row: int):
    sh = idx.sheet(sheet)
    ri = sh.rows.get(row)
    if not ri:
        return {}
    return {y: ri.values.get(c) for c, y in sorted(sh.years_for(row).items())
            if isinstance(ri.values.get(c), (int, float)) and not isinstance(ri.values.get(c), bool)}


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
