"""Реестр параметров модели — «что в этой книге можно менять».

В отличие от справочника показателей (catalog.py, типовые величины вроде выручки и DSCR), реестр собирается из самой
книги: это ВСЕ введённые числа, на которые ссылаются формулы, — цены, курсы, коэффициенты, ставки, нормативы,
на любом листе, рядом по годам или одной ячейкой. Заранее заданного списка нет.

Запись берётся из паспорта строк (semantics.py): адрес, подпись, заголовки блока над строкой, единицы, значения,
фактор, деловое название и описание (после разбора LLM), на какие показатели влияет.

Поиск — по словам запроса во всех текстовых полях записи сразу (название, подпись, блок, лист, фактор, описание),
с усечением окончаний и небольшим русско-английским словарём общих терминов. Он нужен, чтобы запрос
«цена на бензин» нашёл строку «Gasoline» в блоке «Prices».
"""
from __future__ import annotations

import re

from .semantics import FACTORS

_WORD = re.compile(r"[a-zа-я0-9]+")
_STOP = {"на", "по", "для", "от", "за", "и", "в", "с", "the", "of", "for", "to", "in", "and", "при", "из", "к", "у", "до"}
# Общие термины финансовых моделей: слова одной группы считаются одним и тем же. Это не привязка к конкретной модели.
_SYN = [
    ("цен", "цена", "price", "прайс"), ("курс", "fx", "usd", "eur", "валют", "exchange", "доллар", "евро"),
    ("ставк", "rate", "процент", "interest"), ("ключев", "key", "кс", "цб"), ("объем", "volume", "выпуск", "production", "output"),
    ("добыч", "production", "extraction"), ("налог", "tax", "ндпи", "акциз", "пошлин", "duty"), ("нефт", "oil", "urals", "brent", "crude"),
    ("бензин", "gasoline", "petrol"), ("дизел", "diesel", "дт"), ("мазут", "fuel", "fueloil"), ("газ", "gas"),
    ("инфляц", "cpi", "ppi", "inflation", "ипц", "ипп"), ("тариф", "tariff"), ("выручк", "revenue", "sales", "доход"),
    ("затрат", "расход", "cost", "opex", "expense"), ("капитал", "capex", "инвестиц", "капвлож"), ("зарплат", "фот", "payroll", "salary", "оплат"),
    ("долг", "debt", "кредит", "loan", "займ", "заем"), ("дивиденд", "dividend"), ("амортизац", "depreciation", "da"),
    ("маржа", "margin", "крэк", "crack", "спред", "spread"), ("коэффициент", "ratio", "coef", "норматив", "доля", "share"),
    ("мощност", "capacity", "киум"), ("электроэнерг", "ээ", "power", "electricity"), ("оборотн", "working", "wc", "запас", "дебитор", "кредитор"),
    ("скидк", "discount", "дисконт"), ("премия", "premium"), ("транспорт", "transport", "логист", "freight"),
]
_GROUP = {}
for _i, _g in enumerate(_SYN):
    for _w in _g:
        _GROUP.setdefault(_w, set()).add(_i)


def _stem(w: str) -> str:
    """Грубое усечение окончаний: «цены», «ценой», «цена» → «цен»; латиницу не трогаем короче 4 знаков."""
    if len(w) <= 3 or w.isdigit():
        return w
    if re.match(r"[а-я]", w):
        if len(w) <= 4:
            return w                                  # «долг», «курс», «цена» — короткие слова целиком
        return w[:min(len(w) - (2 if len(w) >= 6 else 1), 7)]
    return w[:max(4, len(w) - 1)] if w.endswith("s") else w


def _tokens(text: str):
    return [_stem(w) for w in _WORD.findall((text or "").lower().replace("ё", "е")) if w not in _STOP]


def _groups(tok: str):
    out = set()
    for w, gs in _GROUP.items():
        if tok.startswith(w) or (len(tok) >= 4 and w.startswith(tok)):
            out |= gs
    return out


def entries(s):
    """Все параметры модели: введённые числа, на которые ссылаются формулы (без невыбранных наборов сценариев)."""
    out = []
    for key, p in ((s.sem or {}).get("rows") or {}).items():
        if p["kind"] not in ("input", "mixed"):
            continue
        f = p.get("factor") or "other"
        out.append({"ref": key, "sheet": p["sheet"], "row": p["row"], "name": p.get("name") or p["label"], "label": p["label"],
                    "block": " / ".join(p.get("section") or []), "unit": p.get("unit") or "", "factor": f,
                    "factor_name": FACTORS.get(f, (f,))[0], "sub": p.get("sub") or None, "desc": p.get("desc") or "",
                    "scalar": bool(p.get("scalar")), "values": p.get("values") or {},
                    "influences": p.get("metrics") or [], "fanout": p.get("fanout", 0),
                    "mixed": p["kind"] == "mixed"})
    return out


_FIELDS = (("name", 3.0), ("label", 3.0), ("block", 2.0), ("factor_name", 1.5), ("sub", 1.5), ("desc", 1.0), ("sheet", 1.0), ("unit", 0.5))


def _index(s):
    """Токены записей считаем один раз на состояние паспорта (после разбора LLM паспорт меняется → пересчёт)."""
    sem = s.sem or {}
    stamp = (id(sem.get("rows")), len(sem.get("rows") or {}), bool(sem.get("llm")))
    cache = getattr(s, "_param_index", None)
    if cache and cache[0] == stamp:
        return cache[1]
    idx = []
    for e in entries(s):
        ft = {}
        for f, w in _FIELDS:
            toks = set(_tokens(str(e.get(f) or "")))
            ft[f] = (toks, set().union(*[_groups(t) for t in toks]) if toks else set())
        idx.append((e, ft))
    s._param_index = (stamp, idx)
    return idx


def search(s, query="", factor=None, sheet=None, limit=25):
    """Поиск параметров. Возвращает (найденные записи с баллом, сколько всего подошло).
    Запись подходит, если в ней нашлось не меньше половины слов запроса (точно, по началу слова или по синониму)."""
    q = _tokens(query)
    qg = [_groups(t) for t in q]
    # О каком факторе сам запрос («цена на бензин» → цены реализации): записи этого фактора поднимаем выше,
    # иначе «акциз на бензин» обгоняет цену бензина только потому, что в подписи есть слово «бензин».
    from .semantics import classify
    qf = classify(query, "", "", (s.sem or {}).get("industry") or "generic", "")[0] if q else None
    if qf == "other":
        qf = None
    hits = []
    for e, ft in _index(s):
        if factor and factor not in (e["factor"], e["sub"]):
            continue
        if sheet and e["sheet"].lower() != str(sheet).lower():
            continue
        if not q:
            hits.append((len(e["influences"]) * 10 + min(e["fanout"], 50) / 10, e, []))
            continue
        score, matched, why = 0.0, 0, []
        for t, g in zip(q, qg):
            best, bf = 0.0, None
            for f, w in _FIELDS:
                toks, groups = ft[f]
                if t in toks:
                    k = 1.0
                elif len(t) >= 3 and any(x.startswith(t) or (len(x) >= 3 and t.startswith(x)) for x in toks):
                    k = 0.8
                elif g and g & groups:
                    k = 0.6                                   # синоним: «цена» ↔ price
                else:
                    continue
                if k * w > best:
                    best, bf = k * w, f
            if best:
                matched += 1
                score += best
                why.append(bf)
        if matched * 2 < len(q):
            continue
        full = matched == len(q)
        score = score / len(q) + full * 1.5 + (0.4 if e["influences"] else -0.3) - len(e["label"]) / 300
        if qf and e["factor"] == qf:
            score += 1.2
        hits.append((score, e, full))
    if q and any(h[2] for h in hits):                      # есть записи со всеми словами запроса — частичные не показываем
        hits = [h for h in hits if h[2]]
    hits.sort(key=lambda x: -x[0])
    return [dict(e, score=round(sc, 2)) for sc, e, _ in hits[:limit]], len(hits)


def overview(s):
    """Сводка реестра: сколько параметров, по листам, факторам и блокам — для промпта и карточки."""
    es = entries(s)
    by_sheet, by_factor, blocks = {}, {}, {}
    for e in es:
        by_sheet[e["sheet"]] = by_sheet.get(e["sheet"], 0) + 1
        by_factor[e["factor_name"]] = by_factor.get(e["factor_name"], 0) + 1
        b = (e["block"].split(" / ")[0] if e["block"] else "")[:50]
        if b:
            blocks[(e["sheet"], b)] = blocks.get((e["sheet"], b), 0) + 1
    top = lambda d, n: sorted(d.items(), key=lambda x: -x[1])[:n]      # noqa: E731
    return {"total": len(es), "scalar": sum(e["scalar"] for e in es), "no_effect": sum(not e["influences"] for e in es),
            "sheets": top(by_sheet, 12), "factors": top(by_factor, 15),
            "blocks": [{"sheet": k[0], "block": k[1], "n": v} for k, v in top(blocks, 25)]}


def text(s):
    o = overview(s)
    if not o["total"]:
        return "Реестр параметров пуст."
    return (f"Параметров (введённых чисел, от которых что-то зависит): {o['total']}, из них одной ячейкой — {o['scalar']}. "
            "По листам: " + ", ".join(f"{k} {v}" for k, v in o["sheets"]) + ". "
            "Крупные блоки: " + "; ".join(f"{b['sheet']} › {b['block']} ({b['n']})" for b in o["blocks"][:12]) + ".")


def to_xlsx(s, path):
    """Реестр параметров — в Excel: аналитик видит всё, что можно менять, и может проверить разметку."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Параметры"
    head = ["Адрес", "Лист", "Строка", "Название", "Подпись в модели", "Блок", "Ед.", "Фактор", "Подфактор",
            "Одна ячейка", "Значения", "Влияет на показатели", "Ссылок на строку", "Описание"]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True)
    for e in sorted(entries(s), key=lambda e: (e["sheet"], e["row"])):
        ws.append([e["ref"], e["sheet"], e["row"], e["name"], e["label"], e["block"], e["unit"], e["factor_name"], e["sub"] or "",
                   "да" if e["scalar"] else "", "; ".join(f"{k}: {v}" for k, v in e["values"].items()),
                   ", ".join(e["influences"]), e["fanout"], e["desc"]])
    for col, w in zip("ABCDEFGHIJKLMN", (18, 16, 8, 38, 38, 30, 8, 20, 18, 10, 34, 34, 10, 50)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(path)
    return path
