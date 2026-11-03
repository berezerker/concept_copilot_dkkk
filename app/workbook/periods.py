"""Периоды в шапке модели: годы, полугодия, кварталы, месяцы — числом, датой или текстом.

Год хранится как обычный int (как раньше). Подгодовой период — объект Period: это строка-подпись
(«2026Q1», «2026H2», «2026-03»), которая сравнивается с int по году. Поэтому фильтры «годы 2026–2028»
и «прогноз с 2026» работают одинаково для годовых, квартальных и месячных моделей.
"""
from __future__ import annotations

import datetime as dt
import re

YEAR_MIN, YEAR_MAX = 1990, 2070
FREQ_N = {"H": 2, "Q": 4, "M": 12}


class Period(str):
    """Подгодовой период. str-подпись для JSON/вывода; сравнение — по (год, номер внутри года)."""

    def __new__(cls, year, sub, freq):
        label = f"{year}{freq}{sub}" if freq in ("H", "Q") else f"{year}-{sub:02d}"
        obj = super().__new__(cls, label)
        obj.year, obj.sub, obj.freq = int(year), int(sub), freq
        return obj

    def _key(self):
        return (self.year, self.sub / FREQ_N[self.freq])

    @staticmethod
    def _k(o):
        if isinstance(o, Period):
            return o._key()
        if isinstance(o, (int, float)) and not isinstance(o, bool):
            return None
        return NotImplemented

    def __eq__(self, o):
        if isinstance(o, Period):
            return self._key() == o._key()
        if isinstance(o, int) and not isinstance(o, bool):
            return self.year == o
        return str.__eq__(self, o)

    def __ne__(self, o):
        r = self.__eq__(o)
        return r if r is NotImplemented else not r

    def __hash__(self):
        return hash(self.year)

    def __lt__(self, o):
        if isinstance(o, Period):
            return self._key() < o._key()
        if isinstance(o, (int, float)) and not isinstance(o, bool):
            return self.year < o
        return NotImplemented

    def __le__(self, o):
        if isinstance(o, Period):
            return self._key() <= o._key()
        if isinstance(o, (int, float)) and not isinstance(o, bool):
            return self.year <= o
        return NotImplemented

    def __gt__(self, o):
        if isinstance(o, Period):
            return self._key() > o._key()
        if isinstance(o, (int, float)) and not isinstance(o, bool):
            return self.year > o
        return NotImplemented

    def __ge__(self, o):
        if isinstance(o, Period):
            return self._key() >= o._key()
        if isinstance(o, (int, float)) and not isinstance(o, bool):
            return self.year >= o
        return NotImplemented

    def __sub__(self, o):          # Period - 1 → год - 1 (для «последний факт = прогноз − 1»)
        return self.year - o

    def __reduce__(self):
        return (Period, (self.year, self.sub, self.freq))


def year_of(p):
    return p.year if isinstance(p, Period) else int(p)


def ordinal(p):
    """Порядковый номер периода в своей частоте (для проверки «идут подряд»)."""
    if isinstance(p, Period):
        return p.year * FREQ_N[p.freq] + p.sub - 1, p.freq
    return int(p), "Y"


def _yy(s):
    y = int(s)
    if y < 100:
        y += 2000
    return y if YEAR_MIN <= y <= YEAR_MAX else None


MONTHS = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "мая": 5, "июн": 6, "июл": 7, "авг": 8, "сен": 9,
          "окт": 10, "ноя": 11, "дек": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_Y = r"((?:19|20)?\d\d)"
PATTERNS = [
    # годовые
    (re.compile(r"^\s*(?:FY|ФГ|ГОД)?\s*((?:19|20)\d\d)\s*(?:Г\.?|ГОД|[A-ZА-ЯЁ]{0,3}|\([A-ZА-ЯЁ]+\))?\s*$", re.I), "Y"),
    # кварталы: 1 кв. 2026 · 1кв26 · Q1 2026 · Q1'26 · 1Q26 · 2026Q1 · 2026 Q1 · I кв. 2026
    (re.compile(r"^\s*([1-4]|I{1,3}|IV)\s*(?:КВ\.?|КВАРТАЛ|Q)\s*[\'’\-/ ]?\s*" + _Y + r"\s*(?:Г\.?)?\s*[A-ZА-ЯЁ]{0,2}\s*$", re.I), "Q"),
    (re.compile(r"^\s*Q([1-4])\s*[\'’\-/ ]?\s*" + _Y + r"\s*[A-ZА-ЯЁ]{0,2}\s*$", re.I), "Q"),
    (re.compile(r"^\s*((?:19|20)\d\d)\s*[\-_ ]?\s*Q([1-4])\s*[A-ZА-ЯЁ]{0,2}\s*$", re.I), "Qr"),
    # полугодия: 1П2026 · 1 п/г 2026 · H1 2026 · 1H26 · 2026H1
    (re.compile(r"^\s*([12])\s*(?:П/?Г?\.?|ПОЛУГОДИЕ|H)\s*[\'’\-/ ]?\s*" + _Y + r"\s*[A-ZА-ЯЁ]{0,2}\s*$", re.I), "H"),
    (re.compile(r"^\s*H([12])\s*[\'’\-/ ]?\s*" + _Y + r"\s*$", re.I), "H"),
    (re.compile(r"^\s*((?:19|20)\d\d)\s*[\-_ ]?\s*H([12])\s*$", re.I), "Hr"),
    # месяцы: янв.26 · Jan-26 · январь 2026 · 03.2026 · 2026-03
    (re.compile(r"^\s*([A-Za-zА-Яа-яЁё]{3})[A-Za-zА-Яа-яЁё]*\.?\s*[\'’\-/ ]?\s*" + _Y + r"\s*$"), "Mn"),
    (re.compile(r"^\s*(0?[1-9]|1[0-2])[./](\d{4})\s*$"), "M"),
    (re.compile(r"^\s*(\d{4})-(0[1-9]|1[0-2])\s*$"), "Mr"),
]
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4}


def parse_period(v):
    """Значение ячейки шапки → int (год) или Period; иначе None."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        if float(v).is_integer() and YEAR_MIN <= int(v) <= YEAR_MAX:
            return int(v)
        return None
    if isinstance(v, (dt.datetime, dt.date)):
        if YEAR_MIN <= v.year <= YEAR_MAX:
            return ("DATE", v)                 # частоту определит строка целиком
        return None
    if not isinstance(v, str) or len(v) > 24:
        return None
    s = v.strip()
    for rx, kind in PATTERNS:
        m = rx.match(s)
        if not m:
            continue
        if kind == "Y":
            return int(m.group(1))
        if kind in ("Q", "H"):
            sub = ROMAN.get(m.group(1).upper(), None) or int(m.group(1))
            y = _yy(m.group(2))
            return Period(y, sub, kind) if y else None
        if kind in ("Qr", "Hr"):
            return Period(int(m.group(1)), int(m.group(2)), kind[0])
        if kind == "Mn":
            mon = MONTHS.get(m.group(1).lower()[:3])
            y = _yy(m.group(2))
            return Period(y, mon, "M") if mon and y else None
        if kind == "M":
            return Period(int(m.group(2)), int(m.group(1)), "M")
        if kind == "Mr":
            return Period(int(m.group(1)), int(m.group(2)), "M")
    return None


def resolve_dates(cells: dict):
    """Даты в строке шапки → годы / полугодия / кварталы / месяцы по шагу между датами."""
    dates = sorted((c, d) for c, d in cells.items())
    if len(dates) < 2:
        return {c: d.year for c, d in dates}
    steps = []
    for (_, a), (_, b) in zip(dates, dates[1:]):
        steps.append((b.year - a.year) * 12 + (b.month - a.month))
    step = max(set(steps), key=steps.count)
    out = {}
    for c, d in dates:
        if step >= 12:
            out[c] = d.year
        elif step == 6:
            out[c] = Period(d.year, 1 if d.month <= 6 else 2, "H")
        elif step == 3:
            out[c] = Period(d.year, (d.month - 1) // 3 + 1, "Q")
        else:
            out[c] = Period(d.year, d.month, "M")
    return out


def row_periods(raw_cells: dict, cached_cells: dict):
    """Строка-шапка периодов: {столбец: период} и число шагов «следующий период подряд»."""
    found, dates = {}, {}
    for c in set(raw_cells) | set(cached_cells):
        p = parse_period(cached_cells.get(c))
        if p is None:
            p = parse_period(raw_cells.get(c))
        if p is None:
            continue
        if isinstance(p, tuple):
            dates[c] = p[1]
        else:
            found[c] = p
    if dates:
        found.update(resolve_dates(dates))
    if len(found) < 4:
        return None, 0
    seq = [found[c] for c in sorted(found)]
    inc = 0
    for a, b in zip(seq, seq[1:]):
        oa, ob = ordinal(a), ordinal(b)
        if oa[1] == ob[1] and ob[0] == oa[0] + 1:
            inc += 1
        elif oa[1] != ob[1] and year_of(b) >= year_of(a):        # смешанная шапка: 2024, 2025, 1кв26…
            inc += 1
    return (found, inc) if inc >= 3 else (None, 0)


def norm_period(x):
    """Год или период из аргументов инструмента: 2026, '2026', '2026Q1', '1 кв. 2026', '2026-03' → int | Period."""
    if isinstance(x, Period) or (isinstance(x, int) and not isinstance(x, bool)):
        return x
    if isinstance(x, float) and x.is_integer():
        return int(x)
    p = parse_period(str(x))
    if isinstance(p, tuple) or p is None:
        raise ValueError(f"Не понимаю период «{x}»: укажите год (2026) или период (2026Q1, 2026H2, 2026-03)")
    return p


def norm_periods(xs):
    return [norm_period(x) for x in (xs or [])]
