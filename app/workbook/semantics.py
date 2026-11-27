"""Смысловой слой модели: «паспорт» строк и экономические факторы.

Для строк, от которых зависят ключевые метрики, собирается паспорт: подпись, единицы, заголовок блока над
строкой, роль листа, текущие значения и формула словами («= Объём добычи × Цена Urals / 1000»).
Каждая строка относится к фактору фиксированного кредитного справочника (верхний уровень одинаков для всех
отраслей) и к подфактору отраслевого пакета (нефтегаз, электроэнергетика, металлургия, девелопмент, ритейл, АПК).

Сначала — детерминированно (словари), затем, если подключена LLM, — обогащение пакетами: короткое
деловое название, описание, фактор/подфактор и направление «что хорошо для кредитора». Результат
кэшируется по хэшу файла, так что LLM вызывается один раз на модель.
"""
from __future__ import annotations

import json
import os
import re
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from .index import REF_RE, STR_RE, WorkbookIndex
from .modelmap import is_scalar_row, row_class, scalar_cols, upstream_inputs

SEM_VERSION = 9

# ───────────────────────── справочник ─────────────────────────
FACTORS = {   # id: (название, направление по умолчанию: up — рост хорош для кредитора, down — плох, neutral)
    "price_sales": ("Цены реализации продукции", "up"),
    "revenue": ("Выручка по сегментам и проектам", "up"),
    "volume": ("Объёмы производства и продаж", "up"),
    "tariff": ("Регулируемые тарифы", "up"),
    "price_inputs": ("Цены на сырьё, топливо и закупки", "down"),
    "fx": ("Курсы валют", "neutral"),
    "rate": ("Процентные ставки", "down"),
    "inflation": ("Инфляция и индексация", "neutral"),
    "opex": ("Операционные расходы", "down"),
    "capex": ("Капитальные вложения", "down"),
    "tax": ("Налоги, пошлины, акцизы", "down"),
    "wc": ("Оборотный капитал", "down"),
    "debt": ("Долг и график погашения", "down"),
    "scenario": ("Сценарные переключатели и надстройки", "neutral"),
    "other": ("Прочие допущения", "neutral"),
}
INDUSTRIES = {
    "oil_gas": ("Нефтегаз", {
        "crude_price": "Цена нефти (Urals, Brent)", "products_price": "Цены нефтепродуктов", "gas_price": "Цена газа",
        "oil_output": "Добыча нефти и газа", "refining": "Переработка и выпуск нефтепродуктов",
        "mineral_tax": "НДПИ, НДД", "export_duty": "Экспортные пошлины и демпфер", "excise": "Акцизы"},
        r"нефт|urals|brent|добыч|нпз|бензин|дизел|ндпи|мазут|нафт|баррел|барр|крекинг|газоконденс"),
    "power": ("Электроэнергетика", {
        "power_price": "Цена электроэнергии (РСВ)", "capacity": "Плата за мощность (КОМ, ДПМ)",
        "generation": "Выработка и КИУМ", "fuel": "Топливо (газ, уголь, уран)", "heat": "Тепловая энергия",
        "grid": "Сетевые тарифы"},
        r"э/э|электроэнерг|рсв|мощност|аэс|тэс|гэс|киум|выработ|уран|генерац|мвт|гвт|квт"),
    "metals": ("Металлургия и добыча", {
        "metal_price": "Цены металлов", "ore_price": "Цены руды и концентрата", "coal_coke": "Уголь и кокс",
        "metal_output": "Выпуск металла", "mining_output": "Добыча руды"},
        r"сталь|прокат|hrc|руда|металл|чугун|никель|медь|алюмин|золото|концентрат|окатыш|кокс"),
    "real_estate": ("Девелопмент", {
        "price_sqm": "Цена 1 м²", "sales_sqm": "Продажи, м²", "construction_cost": "Себестоимость строительства",
        "escrow": "Эскроу и проектное финансирование", "land": "Земля"},
        r"м2|м²|кв\.?\s?м|девелоп|эскроу|жил|застрой|квартир|ввод площад"),
    "retail": ("Ритейл", {
        "lfl": "Сопоставимые продажи (LFL)", "traffic": "Трафик", "avg_check": "Средний чек", "rent": "Аренда",
        "stores": "Число магазинов и площадь"},
        r"магазин|lfl|трафик|средний чек|торгов.{0,5}площад|ритейл|розниц"),
    "agro": ("АПК", {
        "crop_price": "Цены сельхозпродукции", "yield": "Урожайность", "area": "Посевные площади",
        "livestock": "Животноводство", "feed": "Корма и удобрения"},
        r"урожа|зерн|пшениц|посев|скот|свинин|птиц|молок|удобрен|подсолнеч|сахарн"),
    "generic": ("Прочие отрасли", {}, r"$^"),
}
SUB_RULES = {
    "crude_price": r"urals|brent|нефт.{0,15}(цен|марк)|цен.{0,15}нефт", "products_price": r"бензин|дизел|мазут|нафт|авиакеросин|битум|нефтепродукт",
    "gas_price": r"цен.{0,10}газ|газ.{0,10}цен|ttf|henry", "oil_output": r"добыч", "refining": r"переработ|нпз|выпуск",
    "mineral_tax": r"ндпи|ндд", "export_duty": r"пошлин|демпфер", "excise": r"акциз",
    "power_price": r"рсв|цен.{0,15}(э/э|электроэнерг)", "capacity": r"мощност|ком\b|дпм", "generation": r"выработ|киум|отпуск",
    "fuel": r"уран|уголь|топлив|газ", "heat": r"тепл", "grid": r"сет",
    "metal_price": r"hrc|прокат|сталь|металл|никел|мед|алюмин|золот|арматур", "ore_price": r"руд|концентрат|окатыш",
    "coal_coke": r"уголь|кокс", "metal_output": r"выплавк|выпуск", "mining_output": r"добыч",
    "price_sqm": r"цен.{0,10}(м2|м²|кв)", "sales_sqm": r"продаж.{0,10}(м2|м²|кв)", "construction_cost": r"строит|себестоим",
    "escrow": r"эскроу", "land": r"земл", "lfl": r"lfl|сопоставим", "traffic": r"трафик", "avg_check": r"чек", "rent": r"аренд",
    "stores": r"магазин|площад", "crop_price": r"цен", "yield": r"урожай", "area": r"площад|посев", "livestock": r"скот|свин|птиц|молок",
    "feed": r"корм|удобрен",
}
FACTOR_RULES = [   # порядок важен: первое совпадение
    ("scenario", r"сценари|переключат|флаг|стресс-надстройк|^стресс|scenario|switch"),
    ("tax", r"налог|ндпи|ндд\b|ндс|акциз|пошлин|взнос|демпфер|\btax|duty|excise"),
    ("inflation", r"инфляц|индекс.{0,6}цен|\bcpi\b|\bppi\b|индексац"),
    ("rate", r"ставк.{0,25}(процент|кредит|долг|займ|облигац|ключев|кс\b|рефин|дисконт)|ключев.{0,10}ставк|mosprime|ruonia|libor|sofr|\birs\b|"
             r"\bкс\b|policy rate|interest rate|процентн.{0,10}ставк|ставка.{0,5}(кс|ruonia|mosprime)"),
    ("capex", r"capex|капекс|капитальн|инвестиц|строительств|ввод основн"),
    ("wc", r"оборачиваем|дебитор|кредиторск.{0,5}задолж|запас|оборотн|\bдни\b|days|ндс к возмещ"),
    ("debt", r"процент\w* (к уплат|уплач|невыплач|начисл)|долг|кредит|займ|облигац|погашен|выборк|лизинг|debt|loan|bond|ковенант"),
    ("fx", r"курс|usd/rub|eur/rub|cny|юан|валют|exchange|\$/руб|руб/\$|inr/|cny/"),
    ("tariff", r"тариф|регулир"),
    ("price_inputs", r"(цен|стоимост|price).{0,20}(сырь|газ|уголь|уран|топлив|закуп|руд|кокс|корм|энерг)|закупочн"),
    ("opex", r"opex|опекс|расход|затрат|себестоим|амортиз|depreciat|зарплат|\bфот\b|персонал|ремонт|аренд|материал|услуг"),
    ("price_sales", r"(?<![а-я])цен|price|urals|brent|котировк|\bfob\b|\bcif\b|\bexw\b|\$/т|\$/бар|\$/барр|\$/унц|руб/кг|руб/т|маржа|crack|спред"),
    ("volume", r"\bмвт\b|\bгвт|output|throughput|объ[её]м|добыч|выработ|производств|выпуск|загрузк|киум|переработ|отгрузк|тонн|продаж.{0,6}(м2|тыс|т\b)|volume|production|мощност"),
    ("revenue", r"выручк|доход|revenue|sales|реализац|\bнии\b"),
]
MARKET = {"price_sales", "revenue", "volume", "tariff", "price_inputs", "fx", "rate", "inflation", "opex", "capex"}
DIRECTION = {"up": "рост хорош для кредитора", "down": "рост плох для кредитора", "neutral": "зависит от экспозиции"}


def _n(s):
    return (s or "").lower().replace("ё", "е")


def detect_industry(texts):
    joined = _n(" ".join(texts))
    scores = {k: len(re.findall(v[2], joined)) for k, v in INDUSTRIES.items() if k != "generic"}
    best = max(scores, key=scores.get) if scores else "generic"
    return (best if scores.get(best, 0) >= 5 else "generic"), scores


_NOISE = re.compile(r"(без|с|вкл\.?|включая)\s+(ндс|ндпи|акциз\w*)|london metals? exchange|\bexchange\b(?!\s+rate)")


def _match(text):
    return next((f for f, rx in FACTOR_RULES if re.search(rx, text)), None)


def classify(label, section="", unit="", industry="generic", sheet=""):
    """Фактор и подфактор: сначала по подписи и единицам, затем по блоку (родительской строке), затем по листу."""
    t = _NOISE.sub(" ", _n(f"{label} {unit}"))
    secs = section if isinstance(section, (list, tuple)) else [section]
    ts = _NOISE.sub(" ", _n(" ".join(secs)))
    factor = _match(t) or next((f for f in (_match(_NOISE.sub(" ", _n(x))) for x in secs) if f), None) \
        or _match(_n(sheet).replace("_", " ")) or "other"
    sub = ""
    pack = INDUSTRIES.get(industry, INDUSTRIES["generic"])[1]
    for sid in pack:
        rx = SUB_RULES.get(sid)
        if rx and re.search(rx, t + " " + ts + " " + _n(sheet)):
            sub = sid
            break
    return factor, sub


# ───────────────────────── паспорт строк ─────────────────────────
def _parent(idx: WorkbookIndex, sheet, row):
    """Строка-итог, в которую входит строка (SUM по диапазону с ней) — родитель в иерархии статей."""
    best = None
    for (s2, r2) in idx.dependents.get((sheet, row), ()):
        if s2 != sheet or r2 == row:
            continue
        ri = idx.sheets[s2].rows.get(r2)
        if not ri or not ri.label:
            continue
        f = next(iter(ri.formulas.values()), "")
        if re.search(r"\bSUM\(", f, re.I) and (best is None or abs(r2 - row) < abs(best - row)):
            best = r2
    return idx.sheets[sheet].rows[best].label[:70] if best else None


def _section(idx: WorkbookIndex, sheet, row, depth=2):
    """Блок строки: итог, в который она входит (SUM), и заголовки над ней (строки без чисел)."""
    sh = idx.sheets[sheet]
    ym = sh.years_for(row)
    out = []
    par = _parent(idx, sheet, row)
    if par:
        out.append(par)
    for r in range(row - 1, max(0, row - 12), -1):
        ri = sh.rows.get(r)
        if not ri or not ri.label or r in sh.headers:
            continue
        has_num = any(isinstance(ri.values.get(c), (int, float)) for c in ym)
        if not has_num and not ri.formulas and not ri.unit and ri.label[:70] not in out:
            out.append(ri.label[:70])
            if len(out) >= depth:
                break
    return out


def short_label(idx, sheet, row, n=40):
    sh = idx.sheets.get(sheet)
    ri = sh.rows.get(row) if sh else None
    return (ri.label[:n] if ri and ri.label else f"{sheet}!{row}")


def formula_words(idx: WorkbookIndex, sheet, row, forecast_start=None, names=None, limit=180):
    sh = idx.sheets[sheet]
    ri = sh.rows.get(row)
    if not ri or not ri.formulas:
        return None
    ym = sh.years_for(row)
    col = next((c for c, y in sorted(ym.items()) if c in ri.formulas and (forecast_start is None or y >= forecast_start)),
               next(iter(ri.formulas)))
    f = STR_RE.sub('""', ri.formulas[col])

    def rep(m):
        s2 = (m.group(1) or m.group(2) or sheet).replace("''", "'")
        r2 = int(m.group(4))
        key = f"{s2}!{r2}"
        nm = (names or {}).get(key) or short_label(idx, s2, r2, 32)
        if m.group(5):
            return f"[{nm}…]"
        return f"[{nm}]"
    txt = REF_RE.sub(rep, f)[1:]
    return txt[:limit] + ("…" if len(txt) > limit else "")


def build_passports(idx: WorkbookIndex, card: dict, forecast_start=None, max_calc=450):
    """Строки-допущения выше показателей справочника (без невыбранных наборов сценариев) + расчётные строки
    между допущениями и показателями. Одиночные параметры (ячейка без ряда по годам) входят наравне с рядами."""
    rows = {}
    metric_of = defaultdict(set)
    for m in card.get("key_metrics", []):
        for c in m["candidates"][:1]:
            ups, _ = upstream_inputs(idx, c["sheet"], c["row"], forecast_start)
            for u in ups:
                if u["fanout"] == 0:
                    continue
                key = (u["sheet"], u["row"])
                metric_of[key].add(m["metric"])
                rows.setdefault(key, {"kind": u["class"], "fanout": u["fanout"], "depth": u["depth"]})
            rows.setdefault((c["sheet"], c["row"]), {"kind": "metric", "fanout": 0, "depth": 0})
            metric_of[(c["sheet"], c["row"])].add(m["metric"])
            # расчётные строки между допущениями и метрикой (до 10 уровней) — для сцепок факторов и «формул словами»
            frontier, seen, depth = [(c["sheet"], c["row"])], {(c["sheet"], c["row"])}, 0
            while frontier and depth < 10:
                nxt = []
                for node in frontier:
                    for p in idx.precedents.get(node, ()):
                        if p in seen or p[0] not in idx.sheets:
                            continue
                        seen.add(p)
                        ri = idx.sheets[p[0]].rows.get(p[1])
                        if ri and ri.label and row_class(idx, p[0], p[1], forecast_start) == "calc":
                            rows.setdefault(p, {"kind": "calc", "fanout": len(idx.dependents.get(p, ())), "depth": depth + 1})
                            metric_of[p].add(m["metric"])
                            nxt.append(p)
                frontier = nxt[:250]
                depth += 1
    # Реестр параметров: ВСЕ введённые числа, на которые ссылаются формулы, — и те, что не доходят до ключевых
    # показателей (их тоже могут попросить изменить). У них пустой список metrics.
    inactive = getattr(idx, "inactive", None) or set()
    for sname, sh in idx.sheets.items():
        if sname in inactive or sh.role in ("contents", "separator"):
            continue
        for r, ri in sh.rows.items():
            if (sname, r) in rows or not ri.label or r in sh.headers or r == sh.header_row or not idx.dependents.get((sname, r)):
                continue
            cls = row_class(idx, sname, r, forecast_start)
            if cls in ("input", "mixed"):
                rows[(sname, r)] = {"kind": cls, "fanout": len(idx.dependents[(sname, r)]), "depth": 99}
    calc = [k for k, v in rows.items() if v["kind"] == "calc"]
    if len(calc) > max_calc:
        drop = sorted(calc, key=lambda k: (rows[k]["depth"], -rows[k]["fanout"]))[max_calc:]
        for k in drop:
            rows.pop(k, None)
    out = {}
    for (sheet, r), meta in rows.items():
        sh = idx.sheets[sheet]
        ri = sh.rows.get(r)
        if not ri:
            continue
        vals = [(y, ri.values[c]) for c, y in sorted(sh.years_for(r).items())
                if isinstance(ri.values.get(c), (int, float)) and not isinstance(ri.values.get(c), bool)
                and (forecast_start is None or y >= forecast_start)][:4]
        scalar = False
        if not vals and is_scalar_row(sh, ri):               # параметр одной ячейкой
            from .index import col_letter
            vals = [(f"{col_letter(c)}{r}", ri.values[c]) for c in scalar_cols(sh, ri)
                    if isinstance(ri.values.get(c), (int, float)) and not isinstance(ri.values.get(c), bool)][:3]
            scalar = True
        out[f"{sheet}!{r}"] = {"sheet": sheet, "row": r, "label": ri.label, "unit": ri.unit, "role": sh.role, "scalar": scalar,
                              "kind": meta["kind"], "fanout": meta["fanout"], "section": _section(idx, sheet, r),
                              "values": {str(y): (round(v, 4) if abs(v) < 1e6 else round(v)) for y, v in vals},
                              "metrics": sorted(metric_of[(sheet, r)])}
    return out


def build_semantics(idx: WorkbookIndex, card: dict, forecast_start=None):
    """Детерминированный смысловой слой (без LLM)."""
    rows = build_passports(idx, card, forecast_start)
    industry, scores = detect_industry([p["label"] + " " + " ".join(p["section"]) for p in rows.values()])
    for key, p in rows.items():
        f, sub = classify(p["label"], p["section"], p["unit"], industry, p["sheet"])
        p["factor"], p["sub"] = f, sub
        lab = (p["label"] or "").strip()
        sec = p["section"][0] if p["section"] else ""
        own = _match(_NOISE.sub(" ", _n(lab)))
        name = lab or sec or key
        if sec and sec != lab and (not own or len(lab) < 4):      # «Лимон» → «Выручка: Лимон»
            name = f"{sec}: {lab}" if lab else sec
        elif not sec and not own and p["role"] not in ("inputs",):
            name = f"{lab} ({p['sheet']})"
        p["name"] = name[:80]
        p["desc"] = ""
        p["direction"] = FACTORS[f][1]
        p["source"] = "rules"
    for key, p in rows.items():
        if p["kind"] in ("calc", "metric", "mixed"):
            p["formula_words"] = formula_words(idx, p["sheet"], p["row"], forecast_start,
                                               {k: v["name"] for k, v in rows.items()})
    return {"version": SEM_VERSION, "industry": industry, "industry_scores": scores, "rows": rows,
            "llm": None, "file_hash": idx.file_hash}


# ───────────────────────── обогащение LLM ─────────────────────────
SEM_SYSTEM = """Ты — кредитный аналитик и отраслевой эксперт. Тебе дают строки финансовой модели заёмщика (подпись, единицы,
лист, заголовки блоков над строкой, несколько значений прогноза, формулу словами). Для каждой строки верни через
функцию describe_rows:
• name — короткое понятное деловое название (до 60 знаков, по-русски), чтобы сотрудник понял, что это, без адреса
  ячейки. Кодовые имена активов/проектов (фрукты, города) сохраняй, но поясняй, что это: «Выручка проекта "Лимон"».
• desc — одно предложение: что это за показатель и как он влияет на денежный поток.
• factor — фактор из фиксированного справочника (enum). sub — подфактор отраслевого пакета (enum) или "none".
• direction — up, если рост показателя хорош для кредитора; down — если плох; neutral — неоднозначно.
Не выдумывай то, чего нет в данных; если смысл неясен — честно назови по подписи и блоку."""


def _spec(industry):
    pack = list(INDUSTRIES.get(industry, INDUSTRIES["generic"])[1]) + ["none"]
    return {"name": "describe_rows", "description": "Описания строк модели",
            "parameters": {"type": "object", "properties": {
                "industry": {"type": "string", "enum": list(INDUSTRIES), "description": "Отрасль заёмщика"},
                "rows": {"type": "array", "items": {"type": "object", "properties": {
                    "id": {"type": "string"}, "name": {"type": "string"}, "desc": {"type": "string"},
                    "factor": {"type": "string", "enum": list(FACTORS)},
                    "sub": {"type": "string", "enum": pack},
                    "direction": {"type": "string", "enum": ["up", "down", "neutral"]}},
                    "required": ["id", "name", "factor"]}}},
                "required": ["rows"]}}


def _prio(p):
    return ({"metric": 0, "calc": 1}.get(p["kind"], 2), -min(p["fanout"], 50), p["role"] == "inputs")


def enrich(sem: dict, client, emit=None, batch=50, max_rows=None, workers=6):
    """LLM-обогащение паспорта пакетами (параллельно). Возвращает число описанных строк."""
    if client is None or getattr(client, "name", "") == "mock":
        return 0
    sem.pop("deps_cache", None)          # факторы строк меняются — прежние результаты анализа устарели
    max_rows = max_rows or int(os.environ.get("CF_SEM_MAX_ROWS", "1500"))
    items = sorted(sem["rows"].items(), key=lambda kv: _prio(kv[1]))[:max_rows]
    industry = sem["industry"]
    head = (f"Отрасль по ключевым словам: {INDUSTRIES[industry][0]}. Справочник факторов: "
            + "; ".join(f"{k} — {v[0]}" for k, v in FACTORS.items())
            + ". Подфакторы отрасли: " + ("; ".join(f"{k} — {v}" for k, v in INDUSTRIES[industry][1].items()) or "нет"))
    batches = [items[i:i + batch] for i in range(0, len(items), batch)]
    done = [0]
    lock = threading.Lock()
    votes = Counter()

    def run(b):
        payload = [{"id": k, "label": p["label"], "unit": p["unit"], "sheet": p["sheet"], "sheet_role": p["role"],
                    "blocks": p["section"], "values": p["values"], "formula": p.get("formula_words"),
                    "rules_factor": p["factor"]} for k, p in b]
        msgs = [{"role": "system", "content": SEM_SYSTEM + "\n" + head},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
        resp = client.chat(msgs, [_spec(industry)], {"name": "describe_rows"})
        call = next((c for c in resp.tool_calls if c.name == "describe_rows"), None)
        res = (call.arguments if call else {}) or {}
        with lock:
            if res.get("industry"):
                votes[res["industry"]] += 1
            for x in res.get("rows") or []:
                p = sem["rows"].get(str(x.get("id")))
                if not p:
                    continue
                if x.get("name"):
                    p["name"] = str(x["name"])[:80]
                p["desc"] = str(x.get("desc") or "")[:220]
                if x.get("factor") in FACTORS:
                    p["factor"] = x["factor"]
                if x.get("sub") in INDUSTRIES[industry][1]:
                    p["sub"] = x["sub"]
                if x.get("direction") in DIRECTION:
                    p["direction"] = x["direction"]
                p["source"] = "llm"
            done[0] += len(b)
        if emit:
            emit("progress", {"text": f"Разбор смысла модели: {done[0]}/{len(items)} строк"})

    errors = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in [ex.submit(run, b) for b in batches]:
            try:
                fut.result()
            except Exception as e:  # пакет не описан — остаются правила
                errors.append(str(e)[:200])
    if votes:
        ind = votes.most_common(1)[0][0]
        if ind != sem["industry"] and ind in INDUSTRIES:
            sem["industry"] = ind
            for p in sem["rows"].values():                      # подфакторы под новую отрасль
                if p["source"] != "llm":
                    p["sub"] = classify(p["label"], p["section"], p["unit"], ind, p["sheet"])[1]
    names = {k: v["name"] for k, v in sem["rows"].items()}
    sem["llm"] = {"model": client.describe(), "rows": done[0], "errors": errors[:3]}
    sem["_names"] = names
    return done[0]


def refresh_formula_words(idx, sem, forecast_start=None):
    names = {k: v["name"] for k, v in sem["rows"].items()}
    for p in sem["rows"].values():
        if p["kind"] in ("calc", "metric", "mixed"):
            p["formula_words"] = formula_words(idx, p["sheet"], p["row"], forecast_start, names)


# ───────────────────────── кэш и доступ ─────────────────────────
def cache_path(cache_dir, file_hash):
    return os.path.join(cache_dir, f"{file_hash}.sem.v{SEM_VERSION}.json")


def load_cached(cache_dir, file_hash):
    p = cache_path(cache_dir, file_hash)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return None


def save(cache_dir, sem):
    os.makedirs(cache_dir, exist_ok=True)
    data = {k: v for k, v in sem.items() if not k.startswith("_")}
    with open(cache_path(cache_dir, sem["file_hash"]), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def info(s, sheet, row):
    """Смысл строки для вывода: название, фактор, подфактор, описание, адрес."""
    sem = getattr(s, "sem", None) or {}
    key = f"{sheet}!{row}"
    p = (sem.get("rows") or {}).get(key)
    if p:
        ind = sem.get("industry", "generic")
        return {"name": p["name"], "factor": p["factor"], "factor_name": FACTORS[p["factor"]][0],
                "sub": p.get("sub") or "", "sub_name": INDUSTRIES.get(ind, INDUSTRIES["generic"])[1].get(p.get("sub") or "", ""),
                "desc": p.get("desc") or "", "ref": key, "unit": p["unit"]}
    shx = s.idx.sheets.get(sheet)
    ri = shx.rows.get(row) if shx else None
    lab = ri.label if ri and ri.label else key
    f, sub = classify(lab, "", ri.unit if ri else "", sem.get("industry", "generic"), sheet)
    return {"name": lab[:80], "factor": f, "factor_name": FACTORS[f][0], "sub": sub, "sub_name": "", "desc": "",
            "ref": key, "unit": ri.unit if ri else ""}


def name(s, sheet, row):
    return info(s, sheet, row)["name"]


def factor_rows(s, factor=None, sub=None, metric_name=None, inputs_only=True):
    """Строки-допущения фактора (или подфактора), которые можно шоковать."""
    sem = getattr(s, "sem", None) or {}
    out = []
    for key, p in (sem.get("rows") or {}).items():
        if p["kind"] not in (("input", "mixed") if inputs_only else ("input", "mixed", "calc")):
            continue
        if factor and p["factor"] != factor:
            continue
        if sub and p.get("sub") != sub:
            continue
        if metric_name and metric_name not in p["metrics"]:
            continue
        out.append(p)
    inputs = [p for p in out if p["kind"] == "input"]
    return inputs or out


def summary(sem):
    rows = sem.get("rows") or {}
    ind = sem.get("industry", "generic")
    c = Counter(p["factor"] for p in rows.values() if p["kind"] in ("input", "mixed"))
    subs = Counter(p["sub"] for p in rows.values() if p["kind"] in ("input", "mixed") and p.get("sub"))
    return {"industry": ind, "industry_name": INDUSTRIES.get(ind, INDUSTRIES["generic"])[0],
            "rows": len(rows), "llm": sem.get("llm"),
            "factors": [{"id": k, "name": FACTORS[k][0], "rows": v} for k, v in c.most_common()],
            "subfactors": [{"id": k, "name": INDUSTRIES.get(ind, INDUSTRIES["generic"])[1].get(k, k), "rows": v} for k, v in subs.most_common()]}
