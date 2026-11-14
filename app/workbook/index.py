"""Индекс Excel-модели: листы, строки с подписями, заголовки лет, граф зависимостей строк.

Строится один раз при загрузке (openpyxl, два прохода: формулы и кэшированные значения)
и кэшируется на диск по хэшу файла. Всё, что агент «знает» о модели без пересчёта, берётся отсюда.
"""
from __future__ import annotations

import hashlib
import os
import pickle
import re
import time
import warnings
from collections import defaultdict
from dataclasses import dataclass, field

import openpyxl
from openpyxl.utils import get_column_letter, column_index_from_string

warnings.filterwarnings("ignore", module="openpyxl")

INDEX_VERSION = 8
YEAR_MIN, YEAR_MAX = 1990, 2070
LABEL_COLS = 10            # подписи ищем в первых N столбцах (или до первого столбца с годами)
MAX_RANGE_ROWS = 300       # диапазоны длиннее не разворачиваем в граф строк

NAME_COL_RE = re.compile(r"^\s*(наименование|показатель|статья|name|item|indicator name)\b", re.I)
UNIT_RE = re.compile(r"(млн|млрд|тыс|руб|₽|\$|usd|eur|%|п\.п\.|раз|x$|^х$|тонн|ton|bbl|барр|гвт|квт|шт|ед\.)", re.I)
YEAR_RE = re.compile(r"^\s*((?:19|20)\d\d)\s*[A-Za-zА-Яа-яЁё]{0,3}\s*$")

# Ссылки в формулах: [‘Лист’!]A1[:B2]. Строки в кавычках вырезаем заранее.
REF_RE = re.compile(
    r"(?:(?:'((?:[^']|'')+)'|([A-Za-zА-Яа-яЁё_][\w\.А-Яа-яЁё]*))!)?"
    r"(?<![A-Za-z0-9_\.])\$?([A-Z]{1,3})\$?([0-9]{1,7})(?::\$?([A-Z]{1,3})\$?([0-9]{1,7}))?(?![\w(])"
)
STR_RE = re.compile(r'"[^"]*"')
# Ссылки на строки целиком: 5:5, $5:$12 (итоги по всей строке)
ROWREF_RE = re.compile(r"(?:(?:'((?:[^']|'')+)'|([A-Za-zА-Яа-яЁё_][\w\.А-Яа-яЁё]*))!)?(?<![\w\.:$])\$?(\d{1,7}):\$?(\d{1,7})(?![\w:.])")
# Функции, из-за которых связи нельзя прочитать из текста формулы (адрес вычисляется на лету)
DYNAMIC_RE = re.compile(r"\b(INDIRECT|OFFSET)\s*\(", re.I)
COLREF_RE = re.compile(r"(?<![\w$])\$?[A-Z]{1,3}:\$?[A-Z]{1,3}(?![\w(])")
IDENT_RE = re.compile(r"(?<![\w\.!'$])([A-Za-zА-Яа-яЁё_\\][\w\.А-Яа-яЁё]*)(?![\w\.(!'])")


@dataclass
class RowInfo:
    row: int
    label: str = ""
    unit: str = ""
    values: dict = field(default_factory=dict)     # col_idx -> cached value (число/строка)
    formulas: dict = field(default_factory=dict)   # col_idx -> формула
    label_col: int = 0                             # столбец подписи (числа левее — нумерация, не параметры)


@dataclass
class SheetInfo:
    name: str
    max_row: int = 0
    max_col: int = 0
    n_formulas: int = 0
    n_numbers: int = 0
    header_row: int | None = None
    years: dict = field(default_factory=dict)       # col_idx -> year
    rows: dict = field(default_factory=dict)        # row -> RowInfo
    role: str = ""                                  # структурная роль: inputs | calc | report | contents | separator
    headers: dict = field(default_factory=dict)     # row заголовка -> {col: year} (блоков может быть несколько)
    topic: str = ""                                 # подсказка из названия листа (is, bs, cf, debt, dashboard…) — только для показа
    orientation: str = "h"                          # h — периоды по столбцам; v — по строкам (не анализируется)
    dynamic: int = 0                                # формул с INDIRECT/OFFSET/ссылками на столбцы целиком
    stats: dict = field(default_factory=dict)       # счётчики, по которым определена роль

    def years_for(self, row: int) -> dict:
        """Годы по столбцам для строки: ближайший заголовок лет выше строки (или основной)."""
        above = [h for h in self.headers if h <= row]
        if above:
            return self.headers[max(above)]
        return self.years

    def year_cols(self, years=None, row=None):
        """Столбцы для списка лет (или все) с учётом блока, в котором стоит строка."""
        ym = self.years_for(row) if row else self.years
        if not years:
            return dict(sorted(ym.items()))
        ys = set(years)
        return {c: y for c, y in sorted(ym.items()) if y in ys}

    def col_of_year(self, year):
        for c, y in self.years.items():
            if y == year:
                return c
        return None


@dataclass
class WorkbookIndex:
    path: str
    file_hash: str
    sheets: dict                     # name -> SheetInfo
    precedents: dict                 # (sheet,row) -> set((sheet,row))
    dependents: dict                 # (sheet,row) -> set((sheet,row))
    sheet_edges: dict                # (src_sheet, dst_sheet) -> count
    iterate: bool = False
    build_seconds: float = 0.0
    names: dict = field(default_factory=dict)       # именованные диапазоны: имя -> [(лист, строка1, строка2)]
    alt_sets: list = field(default_factory=list)    # наборы листов-близнецов (альтернативные сценарии допущений)
    inactive: set = field(default_factory=set)      # листы-близнецы, не выбранные текущим сценарием
    warnings: list = field(default_factory=list)    # что в книге агент видит неполно

    # ── удобные методы ──
    def sheet(self, name: str) -> SheetInfo:
        if name in self.sheets:
            return self.sheets[name]
        low = {k.lower(): k for k in self.sheets}
        if name.lower() in low:
            return self.sheets[low[name.lower()]]
        raise KeyError(f"Лист «{name}» не найден. Есть: {', '.join(self.sheets)}")

    def row(self, sheet: str, row: int) -> RowInfo:
        sh = self.sheet(sheet)
        if row not in sh.rows:
            raise KeyError(f"Строка {row} на листе «{sh.name}» пуста или не найдена")
        return sh.rows[row]

    @property
    def n_formulas(self):
        return sum(s.n_formulas for s in self.sheets.values())


def _file_hash(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _as_year(v):
    """Похоже ли значение на период шапки (год, квартал, месяц…) — для отсева из подписей строк."""
    from .periods import parse_period
    return parse_period(v)


def _row_years(raw_cells: dict, cached_cells: dict):
    from .periods import row_periods
    return row_periods(raw_cells, cached_cells)


def _detect_headers(raw_rows: dict, cached_rows: dict):
    """Все строки-заголовки с годами (блоки листа) и основной заголовок."""
    headers, best = {}, (0, None)
    for r in sorted(raw_rows):
        yrs, inc = _row_years(raw_rows[r], cached_rows.get(r, {}))
        if yrs:
            headers[r] = yrs
            if inc > best[0]:
                best = (inc, r)
    main = best[1]
    return main, (headers.get(main) or {}), headers


def sheet_topic(name: str) -> str:
    """Подсказка из названия листа. На логику не влияет: роль листа определяется структурой (structure.py),
    название — только слабая подсказка при равенстве и подпись в интерфейсе."""
    n = name.lower()
    rules = [
        (("input", "исходн", "вводн", "параметр", "допущ", "assumption", "предпос", "driver"), "inputs"),
        (("output", "summary", "итог", "свод", "результ", "dashboard", "дашборд", "kpi", "кпки", "report", "отчет", "отчёт"), "report"),
        (("contents", "context", "оглав", "содерж", "readme", "cover", "титул"), "contents"),
        (("is", "p&l", "pl", "pnl", "опу", "опиу"), "is"), (("bs", "баланс"), "bs"), (("cf", "ддс", "cash flow"), "cf"),
        (("debt", "долг", "кредит", "loan"), "debt"),
    ]
    for keys, topic in rules:
        for k in keys:
            if (len(k) <= 3 and (n == k or n.startswith(k + " ") or n.startswith(k + "_"))) or (len(k) > 3 and k in n):
                return topic
    return ""


def _defined_names(wb):
    """Именованные диапазоны книги: имя (в нижнем регистре) -> [(лист, строка1, строка2)]."""
    out = {}
    try:
        items = list(wb.defined_names.items())
    except Exception:
        try:
            items = [(d.name, d) for d in wb.defined_names.definedName]
        except Exception:
            items = []
    for nm, dn in items:
        dests = []
        try:
            for sheet, coord in dn.destinations:
                m = re.match(r"^\$?[A-Z]{1,3}\$?(\d+)(?::\$?[A-Z]{1,3}\$?(\d+))?$", (coord or "").replace("$", ""))
                if sheet and m:
                    r1 = int(m.group(1))
                    r2 = int(m.group(2)) if m.group(2) else r1
                    dests.append((sheet, min(r1, r2), max(r1, r2)))
        except Exception:
            pass
        if dests and not str(nm).startswith("_xlnm"):
            out[str(nm).lower()] = dests
    return out


def _parse_refs(formula: str, own_sheet: str, names: dict | None = None):
    f = STR_RE.sub('""', formula)
    for m in REF_RE.finditer(f):
        sh = (m.group(1) or m.group(2) or own_sheet).replace("''", "'")
        r1 = int(m.group(4))
        r2 = int(m.group(6)) if m.group(6) else r1
        yield sh, min(r1, r2), max(r1, r2)
    if ":" in f:
        for m in ROWREF_RE.finditer(f):                       # ссылки на строки целиком
            sh = (m.group(1) or m.group(2) or own_sheet).replace("''", "'")
            r1, r2 = int(m.group(3)), int(m.group(4))
            yield sh, min(r1, r2), max(r1, r2)
    if names:                                                 # именованные диапазоны: =Выручка*СтавкаНалога
        for m in IDENT_RE.finditer(f):
            for sh, r1, r2 in names.get(m.group(1).lower(), ()):
                yield sh, r1, r2


def build_index(path: str, cache_dir: str | None = None, progress=None) -> WorkbookIndex:
    t0 = time.time()
    fh = _file_hash(path)
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        cpath = os.path.join(cache_dir, f"{fh}.v{INDEX_VERSION}.pkl")
        if os.path.exists(cpath):
            with open(cpath, "rb") as f:
                idx = pickle.load(f)
            idx.path = path
            return idx

    def say(msg):
        if progress:
            progress(msg)

    say("Читаю формулы…")
    wb_f = openpyxl.load_workbook(path, read_only=True, data_only=False)
    raw = {}
    for ws in wb_f.worksheets:
        rows = {}
        try:
            for row in ws.iter_rows():
                for c in row:
                    v = getattr(c, "value", None)
                    if v is None:
                        continue
                    rows.setdefault(c.row, {})[c.column] = v
        except Exception:  # битые data validation и т.п. — пропускаем лист частично
            pass
        raw[ws.title] = rows
    iterate = False
    try:
        iterate = bool(wb_f.calculation.iterate)
    except Exception:
        pass
    defnames = _defined_names(wb_f)
    wb_f.close()

    say("Читаю значения…")
    wb_v = openpyxl.load_workbook(path, read_only=True, data_only=True)
    cached = {}
    for ws in wb_v.worksheets:
        rows = {}
        try:
            for row in ws.iter_rows():
                for c in row:
                    v = getattr(c, "value", None)
                    if v is None:
                        continue
                    rows.setdefault(c.row, {})[c.column] = v
        except Exception:
            pass
        cached[ws.title] = rows
    wb_v.close()

    say("Строю карту листов и граф зависимостей…")
    sheets = {}
    precedents = defaultdict(set)
    sheet_edges = defaultdict(int)
    for name, rows in raw.items():
        si = SheetInfo(name=name, role="calc", topic=sheet_topic(name))
        crow = cached.get(name, {})
        si.header_row, si.years, si.headers = _detect_headers(rows, crow)
        first_year_col = min(si.years) if si.years else LABEL_COLS + 1
        label_limit = min(first_year_col - 1, LABEL_COLS) if si.years else LABEL_COLS
        name_col = None
        for hr in sorted(rows)[:8]:
            for c, v in rows[hr].items():
                txt = crow.get(hr, {}).get(c, v)
                if c <= label_limit and isinstance(txt, str) and NAME_COL_RE.match(txt):
                    name_col = c
                    break
            if name_col:
                break
        for r, cells in rows.items():
            ri = RowInfo(row=r)
            labels = []
            seen_value = False
            for c in sorted(cells):
                v = cells[c]
                si.max_col = max(si.max_col, c)
                is_f = isinstance(v, str) and v.startswith("=")
                if is_f:
                    si.n_formulas += 1
                    ri.formulas[c] = v
                    if DYNAMIC_RE.search(v) or COLREF_RE.search(STR_RE.sub('""', v)):
                        si.dynamic += 1
                    for sh, r1, r2 in _parse_refs(v, name, defnames):
                        if r2 - r1 > MAX_RANGE_ROWS:
                            r2 = r1 + MAX_RANGE_ROWS
                        for rr in range(r1, r2 + 1):
                            if (sh, rr) != (name, r):
                                precedents[(name, r)].add((sh, rr))
                        if sh != name:
                            sheet_edges[(sh, name)] += 1
                elif isinstance(v, (int, float)) and not isinstance(v, bool):
                    si.n_numbers += 1
                cv = crow.get(r, {}).get(c, None if is_f else v)
                if cv is not None:
                    ri.values[c] = cv
                if not si.years and (is_f or (isinstance(cv, (int, float)) and not isinstance(cv, bool))) and labels:
                    seen_value = True                       # лист без шапки лет: текст правее значения — комментарий, не подпись
                if c <= label_limit and not seen_value:
                    txt = cv if isinstance(cv, str) else (v if isinstance(v, str) and not is_f else None)
                    if isinstance(txt, str) and txt.strip() and not _as_year(txt):
                        labels.append(txt.strip())
                        if not ri.label_col:
                            ri.label_col = c
            units = [l for l in labels if len(l) <= 22 and UNIT_RE.search(l)]
            names = [l for l in labels if l not in units and not l.isdigit()]
            nm = crow.get(r, {}).get(name_col) if name_col else None
            if isinstance(nm, str) and nm.strip():
                ri.label = nm.strip()[:160]
            else:
                ri.label = " · ".join(names[:3])[:160]
            ri.unit = units[0] if units else ""
            si.max_row = max(si.max_row, r)
            si.rows[r] = ri
        sheets[name] = si

    dependents = defaultdict(set)
    for k, ps in precedents.items():
        for p in ps:
            dependents[p].add(k)

    idx = WorkbookIndex(path=path, file_hash=fh, sheets=sheets, precedents=dict(precedents),
                        dependents=dict(dependents), sheet_edges=dict(sheet_edges), iterate=iterate, names=defnames)
    say("Определяю структуру книги: роли листов, наборы сценариев…")
    from .structure import analyze
    analyze(idx, raw, cached)
    idx.build_seconds = round(time.time() - t0, 1)
    if cache_dir:
        with open(cpath, "wb") as f:
            pickle.dump(idx, f)
    return idx


# ── вспомогательное для инструментов ──
def col_letter(c: int) -> str:
    return get_column_letter(c)


def col_index(letter: str) -> int:
    return column_index_from_string(letter)
