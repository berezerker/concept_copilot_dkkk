"""Bounded tracing of explicit A1 references, independent of row/period layout."""
from collections import deque
from math import isfinite
import re

from openpyxl.formula import Tokenizer
from openpyxl.formula.tokenizer import TokenizerError
from openpyxl.utils.cell import get_column_letter, quote_sheetname, range_boundaries

from .transaction import split_ref


_CELL = r"\$?[A-Za-z]{1,3}\$?[1-9][0-9]{0,6}"
_RANGE = re.compile(rf"{_CELL}(?::{_CELL})?")
_DYNAMIC = {"INDIRECT", "OFFSET"}


def numeric(value):
    return type(value) in (float, int) and isfinite(value)


def address(idx, ref, own_sheet=None):
    if "!" in ref:
        sheet, coord = split_ref(ref)
    else:
        sheet, coord = own_sheet, ref
    if not sheet or not _RANGE.fullmatch(coord):
        raise ValueError(f"Нужен адрес A1 на листе этой книги: {ref}")
    sh = idx.sheet(sheet)
    c1, r1, c2, r2 = range_boundaries(coord.upper())
    if not (c1 <= c2 <= 16384 and r1 <= r2 <= 1048576):
        raise ValueError(f"Диапазон вне границ Excel: {ref}")
    return sh.name, (c1, r1, c2, r2)


def cell(idx, ref):
    sheet, (c, r, c2, r2) = address(idx, ref)
    if (c, r) != (c2, r2):
        raise ValueError("Нужен адрес ровно одной ячейки")
    node = (sheet, r, c)
    if row_info(idx, node) is None:
        raise ValueError(f"Строка ячейки {ref} не найдена в индексе")
    return node


def row_reference(idx, ref):
    """Разобрать строку в формате паспорта «лист!номер строки»."""
    if "!" not in ref:
        return None
    sheet, coord = split_ref(ref)
    if not re.fullmatch(r"[1-9][0-9]{0,6}", coord):
        return None
    sh = idx.sheet(sheet)
    row = int(coord)
    if row > 1048576 or row not in sh.rows:
        raise ValueError(f"Строка {ref} не найдена в индексе книги")
    return sh.name, row


def cell_for_period(idx, location, period, *, input_only=False):
    """Выбрать единственную подходящую ячейку строки за точный период."""
    sheet, row = location
    sh = idx.sheet(sheet)
    info = idx.row(sheet, row)
    matching = []
    for col, value in sh.years_for(row).items():
        if str(value) != str(period):
            continue
        if col in info.formulas:
            if not input_only:
                matching.append(col)
        elif numeric(info.values.get(col)):
            matching.append(col)
    if len(matching) != 1:
        kind = "с введённым числом" if input_only else "результата"
        raise ValueError(f"{sheet}!{row}: в периоде {period} нет единственной ячейки {kind}. "
                         "Укажите точный адрес A1.")
    return sh.name, row, matching[0]


def ref_for(node):
    sheet, row, col = node
    return f"{quote_sheetname(sheet)}!{get_column_letter(col)}{row}"


def row_info(idx, node):
    return idx.sheets[node[0]].rows.get(node[1])


def period_at(idx, node):
    return idx.sheets[node[0]].years_for(node[1]).get(node[2])


def trace(idx, target, source=None, limit=12000, candidates=12, query="", source_period=None, source_row=None):
    """BFS backwards from the exact target. Paths are static formula references."""
    parents = {target: None}
    queue = deque([target])
    issues = set()
    inputs = []
    label_match = re.compile(r"(?<!\w)" + re.escape(query), re.I) if query else None

    def path(node):
        refs = []
        while node is not None:
            refs.append(ref_for(node))
            node = parents[node]
        return refs

    while queue:
        node = queue.popleft()
        if node == source:
            return {"status": "found", "path": path(node), "limitations": sorted(issues)}
        row = row_info(idx, node)
        if not row:
            continue
        _, _, col = node
        formula = row.formulas.get(col)
        if not formula:
            value = row.values.get(col)
            header = node[1] in idx.sheets[node[0]].headers
            period = period_at(idx, node)
            if (source is None and node != target and numeric(value) and value != 0 and not header
                    and node[0] not in idx.inactive and (not label_match or label_match.search(row.label))
                    and (source_row is None or node[:2] == source_row)
                    and (source_period is None or period == source_period)):
                inputs.append({"ref": ref_for(node), "label": row.label, "path": path(node),
                               "period": str(period) if period is not None else None})
                if len(inputs) >= candidates:
                    return {"status": "sample", "candidates": inputs, "limitations": sorted(issues | {"candidate_limit"})}
            continue
        try:
            tokens = Tokenizer(formula).items
        except (TokenizerError, ValueError, IndexError):
            issues.add("unparsed_formula")
            continue
        for token in tokens:
            if token.type == "FUNC" and token.subtype == "OPEN" and token.value[:-1].upper().split(".")[-1] in _DYNAMIC:
                issues.add("dynamic_reference")
            if (token.type, token.subtype) != ("OPERAND", "RANGE"):
                continue
            try:
                sheet, (c1, r1, c2, r2) = address(idx, token.value, node[0])
            except (ValueError, KeyError):
                issues.add("unsupported_reference")
                continue
            if (r2 - r1 + 1) * (c2 - c1 + 1) > 2048:
                issues.add("range_limit")
                continue
            for r in range(r1, r2 + 1):
                for c in range(c1, c2 + 1):
                    child = (sheet, r, c)
                    if child in parents:
                        continue
                    if len(parents) >= limit:
                        issues.add("search_limit")
                        break
                    parents[child] = node
                    queue.append(child)
                else:
                    continue
                break
    if source is None:
        return {"status": "sample", "candidates": inputs, "limitations": sorted(issues)}
    return {"status": "not_found", "path": [], "limitations": sorted(issues)}
