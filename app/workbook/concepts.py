"""Лексикон показателей — единственное место, где перечислены «слова», по которым показатель узнаётся в модели.

Это словарь понятий кредитного анализа (выручка, EBITDA, долг, DSCR…), а не описание конкретного файла: в нём нет
названий листов, номеров строк и раскладки. Лексикон даёт только кандидатов по подписи строки; какая из строк-кандидатов
становится показателем, решают структурные признаки (catalog.py), а то, что не нашлось по словам, сопоставляет LLM.
Добавить показатель = добавить запись сюда; остальной код менять не нужно.

Поля: id · название · группа · вид (flow — поток за период, stock — остаток, ratio — коэффициент) ·
syn — фразы-синонимы (регулярные выражения по нормализованной подписи) · exclude — что точно не этот показатель ·
via — через какие факторы шоковать «снизу» (режим drivers); None — все рыночные факторы ·
good — up/down: рост показателя хорош или плох для кредитора.
"""
from __future__ import annotations

import re

# для потоков и остатков: признаки того, что строка — производная величина (доля, темп, коэффициент), а не сам показатель
DERIVED = (r"/|%|рентаб|марж|margin|коэфф|ratio|темп|growth|прирост|доля|share of|на акцию|per share|"
           r"изменени|change in|дней|days|оборачиваем|индекс|index|мультипликатор|multiple|\bк выручке|to revenue|"
           r"от выручки|of revenue|ставк|\brate\b|отклонен|variance|план[- ]факт")
RATIO_NOISE = r"ковенант|covenant|порог|threshold|лимит|limit|запас|headroom|целев|target|нарушен|breach|флаг|flag|\bтест|test"

CONCEPTS = [
    # ── отчёт о прибылях и убытках ──
    {"id": "revenue", "name": "Выручка", "group": "pnl", "kind": "flow", "good": "up",
     "syn": [r"выручка( от реализации| от продаж| нетто| всего| итого)?", r"(total |net )?revenues?", r"(net )?sales", r"turnover",
             r"доходы? от реализации", r"доходы от основной деятельности", r"итого выручка", r"выручка итого"],
     "exclude": r"себестоим|cost|прочи|other|отложен|deferred|налог|дебитор|аванс|receiv",
     "via": ["price_sales", "volume", "tariff", "revenue"]},
    {"id": "ebitda", "name": "EBITDA", "group": "pnl", "kind": "flow", "good": "up",
     "syn": [r"ebitda", r"ебитда", r"oibda"], "exclude": r"долг|debt|interest|процент", "via": None},
    {"id": "net_income", "name": "Чистая прибыль", "group": "pnl", "kind": "flow", "good": "up",
     "syn": [r"чистая прибыль( \(?убыток\)?)?", r"net (income|profit)( \(?loss\)?)?", r"прибыль за (период|год)",
             r"profit for the (year|period)"],
     "exclude": r"нераспредел|retained|накоплен", "via": None},
    {"id": "debt", "name": "Долг", "group": "bs", "kind": "stock", "good": "down",
     "syn": [r"долг", r"(общий |финансовый |совокупный )долг", r"(total |gross |financial )?debt", r"кредиты и займы",
             r"заемные средства", r"(loans and )?borrowings", r"итого долг", r"долг итого"],
     "exclude": r"чист|\bnet\b|обслуж|service|покрыт|coverage|погашен|repay|привлеч|выборк|drawdown|процент|interest|"
                r"стоимость|cost of|долгосроч|краткосроч|long.?term|short.?term|субордин|капитал|equity", "via": ["debt"]},
    {"id": "net_debt_ebitda", "name": "Чистый долг / EBITDA", "group": "ratio", "kind": "ratio", "good": "down",
     "syn": [r"(чистый долг|net ?debt|nd) ?/ ?ebitda", r"чистый долг к ebitda", r"net debt to ebitda", r"net leverage"],
     "exclude": RATIO_NOISE, "via": None},
    {"id": "dscr", "name": "DSCR", "group": "ratio", "kind": "ratio", "good": "up",
     "syn": [r"dscr", r"коэффициент покрытия( обслуживания)? долга", r"debt service coverage( ratio)?"],
     "exclude": RATIO_NOISE, "via": None},
    {"id": "icr", "name": "ISCR / ICR", "group": "ratio", "kind": "ratio", "good": "up",
     "syn": [r"iscr", r"icr", r"коэффициент покрытия процентов", r"покрытие процентов", r"interest coverage( ratio)?",
             r"ebitda? ?/ ?(проценты|interest)( expenses?)?"],
     "exclude": RATIO_NOISE, "via": None},
    {"id": "capex", "name": "CAPEX", "group": "cf", "kind": "flow", "good": "down",
     "syn": [r"capex", r"капитальные (вложения|затраты|расходы)", r"капвложения", r"(total )?capital expenditures?",
             r"инвестиции в основные средства", r"приобретение основных средств", r"purchase of (pp&e|property.*)"],
     "exclude": r"коэффициент|поддерж.{0,20}/|финансир", "via": ["capex"]},
    {"id": "cash", "name": "Денежные средства", "group": "bs", "kind": "stock", "good": "up",
     "syn": [r"денежные средства( и( их)? эквиваленты)?( и депозиты)?( на конец( периода| года)?)?",
             r"cash( and( cash)? equivalents)?( at( the)? end.*)?", r"остаток денежных средств( на конец.*)?",
             r"cash balance.*", r"closing cash( balance)?"],
     "exclude": r"поток|flow|начал|beginning|opening|минимальн|minimum|огранич|restricted", "via": None},
    # ── дальше — показатели, которых в прежнем наборе не было ──
    {"id": "cogs", "name": "Себестоимость", "group": "pnl", "kind": "flow", "good": "down",
     "syn": [r"себестоимость( продаж| реализации| реализованной продукции)?", r"cost of (sales|goods sold|revenue)", r"cogs"],
     "exclude": r"единиц|unit|1 т|за тонн", "via": ["opex", "price_inputs"]},
    {"id": "opex", "name": "Операционные расходы", "group": "pnl", "kind": "flow", "good": "down",
     "syn": [r"операционные (расходы|затраты)", r"opex", r"(total )?operating (expenses|costs)", r"итого (расходы|затраты)"],
     "exclude": r"capex|прочие|other|неоперац", "via": ["opex", "price_inputs"]},
    {"id": "ebit", "name": "Операционная прибыль (EBIT)", "group": "pnl", "kind": "flow", "good": "up",
     "syn": [r"ebit\b", r"операционная прибыль", r"прибыль от продаж", r"operating (profit|income)"],
     "exclude": r"до налог", "via": None},
    {"id": "da", "name": "Амортизация", "group": "pnl", "kind": "flow", "good": "neutral",
     "syn": [r"амортизация", r"износ и амортизация", r"depreciation( (and|&) amortization)?", r"d&a"],
     "exclude": r"накоплен|accumulated|налогов|tax", "via": ["capex"]},
    {"id": "interest", "name": "Процентные расходы", "group": "pnl", "kind": "flow", "good": "down",
     "syn": [r"проценты к уплате", r"процентные расходы", r"расходы по процентам", r"проценты уплаченные",
             r"interest (expenses?|paid)", r"уплата процентов"],
     "exclude": r"получ|доход|income|received|капитализ", "via": ["rate", "debt"]},
    {"id": "income_tax", "name": "Налог на прибыль", "group": "pnl", "kind": "flow", "good": "down",
     "syn": [r"налог на прибыль", r"(income|profit|corporate) tax( expense)?"],
     "exclude": r"отложен|deferred|до налог|before|после|after", "via": ["tax"]},
    {"id": "cfo", "name": "Операционный денежный поток", "group": "cf", "kind": "flow", "good": "up",
     "syn": [r"операционный денежный поток", r"(чистый )?денежный поток от операционной деятельности",
             r"чистые денежные средства от операционной деятельности", r"(net )?cash (flow )?from operat\w+( activities)?",
             r"operating cash flow", r"cfo", r"ocf"],
     "exclude": r"до изменен|before", "via": None},
    {"id": "fcf", "name": "Свободный денежный поток", "group": "cf", "kind": "flow", "good": "up",
     "syn": [r"свободный денежный поток", r"free cash flow( to (firm|equity))?", r"fcf[fe]?"], "exclude": r"yield", "via": None},
    {"id": "cfads", "name": "CFADS", "group": "cf", "kind": "flow", "good": "up",
     "syn": [r"cfads", r"денежный поток,? доступный для обслуживания долга", r"cash flow available for debt service",
             r"денежный поток для обслуживания долга"], "exclude": r"", "via": None},
    {"id": "debt_service", "name": "Обслуживание долга", "group": "cf", "kind": "flow", "good": "down",
     "syn": [r"обслуживание долга", r"(total )?debt service", r"платежи по долгу"],
     "exclude": r"coverage|покрыт|резерв|reserve|dsra|dscr", "via": ["rate", "debt"]},
    {"id": "dividends", "name": "Дивиденды", "group": "cf", "kind": "flow", "good": "neutral",
     "syn": [r"дивиденды( выплаченные| к выплате)?", r"выплата дивидендов", r"dividends( paid)?"],
     "exclude": r"получ|received|доход|income", "via": None},
    {"id": "net_debt", "name": "Чистый долг", "group": "bs", "kind": "stock", "good": "down",
     "syn": [r"чистый (финансовый )?долг", r"net (financial )?debt"], "exclude": r"ebitda", "via": ["debt"]},
    {"id": "equity", "name": "Собственный капитал", "group": "bs", "kind": "stock", "good": "up",
     "syn": [r"собственный капитал", r"капитал и резервы", r"(total |shareholders.? )?equity", r"итого капитал"],
     "exclude": r"уставн|share capital|добавочн|return|roe|вложени|contribution", "via": None},
    {"id": "assets", "name": "Активы", "group": "bs", "kind": "stock", "good": "neutral",
     "syn": [r"итого активы?", r"активы всего", r"total assets", r"валюта баланса"],
     "exclude": r"внеоборот|оборотн|current|чист|net", "via": None},
    {"id": "debt_ebitda", "name": "Долг / EBITDA", "group": "ratio", "kind": "ratio", "good": "down",
     "syn": [r"(долг|(total |gross )?debt) ?/ ?ebitda", r"долг к ebitda", r"debt to ebitda"],
     "exclude": RATIO_NOISE + r"|чист|\bnet\b", "via": None},
    {"id": "llcr", "name": "LLCR", "group": "ratio", "kind": "ratio", "good": "up",
     "syn": [r"llcr", r"loan life coverage( ratio)?"], "exclude": RATIO_NOISE, "via": None},
    {"id": "ebitda_margin", "name": "Рентабельность EBITDA", "group": "ratio", "kind": "ratio", "good": "up",
     "syn": [r"рентабельность( по)? ebitda", r"ebitda (margin|рентабельность)", r"маржа ebitda"], "exclude": RATIO_NOISE, "via": None},
]
BY_ID = {c["id"]: c for c in CONCEPTS}
HEADLINE = ["revenue", "ebitda", "net_income", "debt", "net_debt_ebitda", "dscr", "icr", "capex", "cash"]   # карточка модели
GROUPS = {"pnl": "Прибыли и убытки", "cf": "Денежные потоки", "bs": "Баланс и долг", "ratio": "Коэффициенты", "custom": "Заданные пользователем"}

_UNIT_TAIL = re.compile(r"[,;]\s*(млн|млрд|тыс|руб|rub|usd|eur|\$|₽|%|x\b|х\b|раз|mln|bn|k\b|000).*$")
_PAREN = re.compile(r"\s*[\(\[][^)\]]*[\)\]]")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower().replace("ё", "е")).strip()


def core(label: str) -> str:
    """Подпись без единиц и пояснений в скобках: «Выручка (нетто), млн руб.» → «выручка»."""
    t = _UNIT_TAIL.sub("", norm(label))
    t = _PAREN.sub("", t)
    return re.sub(r"[\s:.\-–—*]+$", "", t).strip()


_COMPILED = {}


def _rx(c):
    if c["id"] not in _COMPILED:
        alt = "|".join(f"(?:{s})" for s in c["syn"])
        exc = c.get("exclude") or ""
        if c["kind"] != "ratio":
            exc = (exc + "|" if exc else "") + DERIVED
        _COMPILED[c["id"]] = (re.compile(rf"^(?:{alt})$"), re.compile(rf"^(?:{alt})(?![a-zа-я0-9])"),
                              re.compile(rf"(?<![a-zа-я0-9])(?:{alt})(?![a-zа-я0-9])"), re.compile(exc) if exc else None)
    return _COMPILED[c["id"]]


def match_level(concept: dict, label: str):
    """Насколько подпись похожа на показатель: 3 — точное совпадение; 2.5 — точное, но с уточнением в скобках
    («Выручка (LTM)»); 2 — начинается с него; 1 — содержит; 0 — нет."""
    lab = norm(label)
    if not lab or len(lab) > 110:
        return 0
    full, prefix, anywhere, exc = _rx(concept)
    co = core(label)
    if exc is not None and exc.search(lab):
        return 0
    if full.match(co):
        plain = re.sub(r"[\s:.\-–—*]+$", "", _UNIT_TAIL.sub("", lab)).strip()
        return 3 if plain == co else 2.5
    if prefix.match(co):
        return 2
    return 1 if anywhere.search(lab) else 0
