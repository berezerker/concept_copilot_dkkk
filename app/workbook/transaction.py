"""Предпроверка точных диапазонов до первой записи в Excel."""
import math
import re

from openpyxl.utils.cell import range_boundaries


class RestorationError(RuntimeError):
    """Состояние книги не подтверждено; повтор операции запрещён до переоткрытия."""


def split_ref(ref):
    sheet, addr = ref.rsplit("!", 1)
    if sheet.startswith("'") and sheet.endswith("'"):
        sheet = sheet[1:-1].replace("''", "'")
    return sheet, addr.replace("$", "")


def validate_overrides(overrides):
    seen = []
    cells = 0
    for item in overrides:
        ref = item.get("ref", "")
        if "!" not in ref:
            raise ValueError(f"Нужен точный адрес лист!диапазон: {ref}")
        sheet, addr = split_ref(ref)
        addr = addr.upper()
        if not sheet or not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]*(?::[A-Z]{1,3}[1-9][0-9]*)?", addr):
            raise ValueError(f"Неверный адрес: {ref}")
        c1, r1, c2, r2 = range_boundaries(addr)
        if c2 < c1 or r2 < r1 or c2 > 16384 or r2 > 1048576:
            raise ValueError(f"Диапазон вне границ Excel: {ref}")
        values = item.get("values")
        if not isinstance(values, list) or len(values) != r2 - r1 + 1 or any(
                not isinstance(row, list) or len(row) != c2 - c1 + 1 for row in values):
            raise ValueError(f"Размер значений не соответствует диапазону {ref}")
        for row in values:
            for value in row:
                if isinstance(value, float) and not math.isfinite(value):
                    raise ValueError(f"Неконечное число в {ref}")
                if isinstance(value, str) and value.startswith("="):
                    raise ValueError(f"Сценарий не может внедрять формулы: {ref}")
        for sh, a, b, c, d in seen:
            if sheet.casefold() == sh and c1 <= c and c2 >= a and r1 <= d and r2 >= b:
                raise ValueError(f"Пересекающиеся изменения: {ref}; объедините их в один шок")
        seen.append((sheet.casefold(), c1, r1, c2, r2))
        cells += (r2 - r1 + 1) * (c2 - c1 + 1)
        if cells > 100000:
            raise ValueError("Сценарий меняет больше 100000 ячеек; сузьте диапазоны")
    return {"status": "passed", "blocks": len(seen), "cells": cells}


def same_values(a, b, tol=1e-6):
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same_values(x, y, tol) for x, y in zip(a, b))
    if isinstance(a, (float, int)) and not isinstance(a, bool) and isinstance(b, (float, int)) and not isinstance(b, bool):
        return math.isfinite(a) and math.isfinite(b) and abs(a - b) <= tol * max(1.0, abs(b))
    return type(a) is type(b) and a == b
