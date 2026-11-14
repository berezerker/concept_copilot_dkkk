"""Структура книги без опоры на названия листов.

Что определяется и по каким признакам:
• роль листа — по тому, что на нём лежит и куда идут ссылки:
    inputs   — почти одни введённые числа, на которые ссылаются другие листы; своих расчётов нет;
    report   — «сток»: лист только читает другие (на него никто не ссылается), либо название подсказывает
               отчёт/свод и обратных ссылок заметно меньше, чем входящих;
    calc     — всё остальное (расчёты);
    contents — нет чисел, участвующих в расчётах; separator — пустой лист-разделитель.
  Название листа даёт только подсказку (topic) и решает спорные случаи, но не определяет роль.
• наборы листов-близнецов (альтернативные сценарии допущений) — листы-источники с одинаковой раскладкой подписей
  по строкам; какой набор выбран, проверяется пробным пересчётом при открытии модели (catalog.probe_alt_sets).
• ограничения: листы с периодами по вертикали, формулы с INDIRECT/OFFSET (связи не видны в тексте формулы).
"""
from __future__ import annotations

import datetime
from collections import Counter, defaultdict

from .periods import ordinal, parse_period, year_of

TWIN_MIN_ROWS, TWIN_SIM = 15, 0.8


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def guess_forecast_start(idx):
    """Граница факт/прогноз по умолчанию — текущий календарный год в пределах горизонта модели.
    По формулам её надёжно не определить (факт тоже часто считается формулами); можно переопределить."""
    years = sorted({year_of(y) for s in idx.sheets.values() for y in s.years.values()})
    if not years:
        return None
    cur = datetime.date.today().year
    return min(max(cur, years[0]), years[-1])


def _vertical(rows: dict, cached: dict) -> bool:
    """Периоды идут сверху вниз в одном из первых столбцов: ≥ 6 подряд и каждый следующий — следующий период
    (просто столбец с годами ввода объектов таким не считается)."""
    for c in range(1, 7):
        run, prev = 0, None
        for r in sorted(rows):
            v = cached.get(r, {}).get(c, rows[r].get(c))
            p = parse_period(v)
            if p is None or isinstance(p, tuple):
                run, prev = 0, None
                continue
            o = ordinal(p)
            run = run + 1 if prev and o[1] == prev[1] and o[0] == prev[0] + 1 else 1
            prev = o
            if run >= 6:
                return True
    return False


def _sheet_stats(idx, fs):
    from .modelmap import row_class
    stats = {}
    for name, sh in idx.sheets.items():
        cls = Counter()
        n_in = 0
        for r in sh.rows:
            k = row_class(idx, name, r, fs)
            cls[k] += 1
            if k in ("input", "mixed") and idx.dependents.get((name, r)):
                n_in += 1
        stats[name] = {"input_rows": cls["input"] + cls["mixed"], "driver_rows": n_in, "calc_rows": cls["calc"] + cls["link"],
                       "in_refs": sum(v for (a, b), v in idx.sheet_edges.items() if b == name and a != name),
                       "out_refs": sum(v for (a, b), v in idx.sheet_edges.items() if a == name and b != name)}
    return stats


def _role(sh, st):
    if not sh.rows and not sh.n_formulas:
        return "separator"
    if st["driver_rows"] == 0 and st["calc_rows"] == 0:
        return "contents"                       # нет чисел, участвующих в расчётах
    # листы-источники: введённые числа, на которые ссылаются; собственных расчётов почти нет
    if st["driver_rows"] >= 3 and st["calc_rows"] <= 0.15 * st["driver_rows"] and st["out_refs"] > 0:
        return "inputs"
    if st["calc_rows"] >= 3 and st["in_refs"] > 0:
        if st["out_refs"] == 0:
            return "report"                     # чистый сток: только читает
        if sh.topic == "report" and st["out_refs"] <= 0.5 * st["in_refs"]:
            return "report"                     # название подсказывает отчёт, и по ссылкам лист в основном читает
    return "calc"


def _twin_groups(idx):
    """Листы с одинаковой раскладкой: одни и те же подписи в одних и тех же строках."""
    sig = {n: {r: ri.label for r, ri in sh.rows.items() if ri.label} for n, sh in idx.sheets.items()}
    names = [n for n, s in sig.items() if len(s) >= TWIN_MIN_ROWS]
    parent = {n: n for n in names}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(names):
        for b in names[i + 1:]:
            sa, sb = sig[a], sig[b]
            if abs(len(sa) - len(sb)) > 0.25 * max(len(sa), len(sb)):
                continue
            same = sum(1 for r, lab in sa.items() if sb.get(r) == lab)
            if same / max(len(sa), len(sb)) >= TWIN_SIM:
                parent[find(a)] = find(b)
    groups = defaultdict(list)
    for n in names:
        groups[find(n)].append(n)
    return [g for g in groups.values() if len(g) >= 2]


def analyze(idx, raw=None, cached=None):
    """Заполняет в индексе: роли листов, ориентацию, наборы сценариев, предупреждения."""
    fs = guess_forecast_start(idx)
    stats = _sheet_stats(idx, fs)
    warnings = []
    for name, sh in idx.sheets.items():
        sh.stats = stats[name]
        sh.role = _role(sh, stats[name])
        if not sh.years and raw is not None and _vertical(raw.get(name, {}), (cached or {}).get(name, {})):
            sh.orientation = "v"
            warnings.append(f"Лист «{name}»: периоды идут по строкам (сверху вниз). Такие листы участвуют в пересчёте, "
                            "но строки на них агент не разбирает по годам — шок задаётся диапазоном ячеек.")
    dyn = sum(s.dynamic for s in idx.sheets.values())
    if dyn:
        top = sorted(((s.dynamic, n) for n, s in idx.sheets.items() if s.dynamic), reverse=True)[:4]
        warnings.append(f"{dyn} формул с INDIRECT/OFFSET или ссылками на столбцы целиком ("
                        + ", ".join(f"{n}: {k}" for k, n in top) + "). Их связи не читаются из текста формулы: "
                        "карта зависимостей может быть неполной, проверка «шок дойдёт до метрики» — занижать связи. "
                        "Сам пересчёт в Excel от этого не страдает.")
    # Наборы сценариев: листы-источники с одинаковой раскладкой. Какой из них выбран, по формулам надёжно
    # не определить (VLOOKUP/CHOOSE/переключатели), поэтому предварительно берём первый по порядку листов,
    # а при открытии модели в движке проверяем пересчётом (catalog.probe_alt_sets) и исправляем.
    idx.alt_sets, idx.inactive = [], set()
    for g in _twin_groups(idx):
        order = [n for n in idx.sheets if n in g]
        if not all(idx.sheets[n].role == "inputs" for n in order):
            continue                           # одинаковые по форме расчётные листы (например, проекты) — не альтернативы
        idx.alt_sets.append({"sheets": order, "active": [order[0]], "detected": False})
        idx.inactive |= set(order[1:])
    idx.warnings = warnings
    idx.forecast_guess = fs
    return idx
