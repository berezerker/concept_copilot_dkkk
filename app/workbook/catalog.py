"""Справочник модели: где в книге лежит каждый показатель и чем его можно шоковать.

Справочник отвечает на вопрос «хочу стресс по выручке — где она и как её изменить» для любой книги:
1. Кандидаты — строки, подпись которых похожа на показатель (лексикон concepts.py), по всей книге.
2. Выбор — по структурным признакам, не по названиям листов: строка «живая» (зависит от допущений), итоговая
   (у неё больше всего влияющих допущений среди кандидатов), стоит ближе к результатам (слой листа в графе).
3. Происхождение — если выбранная строка лишь ссылка (=Лист!A1), идём по цепочке до расчёта: шоковать надо строку,
   от которой зависят остальные, а не её копию в отчёте. Среди равных по значениям строк берётся самая влиятельная.
4. Способ шока — direct (подменить сам показатель: «выручка −10 % при прочих равных») или drivers (изменить
   допущения, из которых он считается, с подгонкой до заданного изменения). Коэффициенты напрямую не шокируются.
5. Чего не нашли по словам — сопоставляет LLM (по списку итоговых строк модели); пользователь может закрепить
   строку вручную (pins) — закрепление сильнее автоматики и переживает пересборку.
Плюс проверка пересчётом, какой из наборов-близнецов допущений выбран в модели (probe_alt_sets).
"""
from __future__ import annotations

import json
import math
import os
import statistics

from .card_utils import numeric_years, ref_for
from .concepts import BY_ID, CONCEPTS, GROUPS, HEADLINE, match_level, norm
from .index import WorkbookIndex
from .modelmap import is_scalar_row, row_class, sheet_layers, upstream_inputs

CATALOG_VERSION = 1
MAX_CANDIDATES = 6


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# ───────────────────────── кандидаты и выбор ─────────────────────────
def _candidates(idx: WorkbookIndex, concept: dict, fs):
    out = []
    for sh in idx.sheets.values():
        if not sh.years or sh.name in idx.inactive or sh.role in ("contents", "separator"):
            continue
        for ri in sh.rows.values():
            if not ri.label or ri.row in sh.headers:
                continue
            lvl = match_level(concept, ri.label)
            if not lvl:
                continue
            ny = numeric_years(idx, sh.name, ri.row)
            nf = [y for y in ny if fs is None or y >= fs]
            if len(nf) < 2:
                continue
            out.append({"sheet": sh.name, "row": ri.row, "label": ri.label, "unit": ri.unit, "level": lvl, "n": len(nf),
                        "pre": {3: 6.0, 2.5: 5.0, 2: 3.5, 1: 1.5}[lvl] + min(len(nf), 10) / 5 - len(ri.label) / 60})
    out.sort(key=lambda c: -c["pre"])
    return out[:MAX_CANDIDATES]


def _proportional(a: dict, b: dict, fs):
    """Ряды совпадают с точностью до постоянного множителя (одна величина в разных единицах)."""
    ys = [y for y in a if y in b and (fs is None or y >= fs) and abs(a[y]) > 1e-12 and abs(b[y]) > 1e-12]
    if len(ys) < 3:
        return False
    ratios = [b[y] / a[y] for y in ys]
    m = statistics.median(ratios)
    return m != 0 and all(abs(r - m) <= 0.005 * abs(m) for r in ratios)


def _downstream(idx, node, limit=30000):
    seen, stack = {node}, [node]
    while stack and len(seen) < limit:
        n = stack.pop()
        for d in idx.dependents.get(n, ()):
            if d not in seen:
                seen.add(d)
                stack.append(d)
    seen.discard(node)
    return seen


def _is_multiplier(idx, node):
    """Строка — множитель единиц или знака: все её числа одинаковы и равны ±1 или степени десяти (1000, 0.001…).
    Экономические параметры (ставка 0,15; курс 90) множителями не считаются."""
    sh = idx.sheets[node[0]]
    ri = sh.rows.get(node[1])
    if not ri:
        return False
    vals = [v for c, v in ri.values.items() if _num(v) and c > (ri.label_col or 0)]
    if not vals:
        return False
    v0 = vals[0]
    if any(abs(v - v0) > 1e-12 for v in vals) or v0 == 0:
        return False
    lg = math.log10(abs(v0))
    return abs(lg - round(lg)) < 1e-9


def origin_of(idx, sheet, row, fs=None, max_hops=12):
    """Откуда на самом деле берётся строка. Идём вверх, пока строка — «сквозная»: её значения пропорциональны
    единственному содержательному источнику (ссылка =Лист!A1, перевод единиц =X*k/1000, смена знака; прочие
    источники — только множители единиц). Останавливаемся на первой строке, которая считает что-то из нескольких
    рядов, или на допущении."""
    cur = (sheet, row)
    for _ in range(max_hops):
        ps = [p for p in idx.precedents.get(cur, ()) if p[0] in idx.sheets and p != cur
              and p[1] not in idx.sheets[p[0]].headers and idx.sheets[p[0]].rows.get(p[1]) is not None]
        main = [p for p in ps if not _is_multiplier(idx, p)] if len(ps) > 1 else ps
        if len(main) != 1:
            break
        a, b = numeric_years(idx, cur[0], cur[1]), numeric_years(idx, main[0][0], main[0][1])
        if not _proportional(a, b, fs):
            break
        cur = main[0]
    return cur


def _describe(idx, sheet, row):
    ri = idx.sheets[sheet].rows.get(row)
    return {"sheet": sheet, "row": row, "label": (ri.label if ri else "")[:120], "unit": ri.unit if ri else ""}


def display_unit(kind, raw_unit):
    """Денежная/физическая подпись строки не может быть единицей коэффициента."""
    if kind != "ratio":
        return raw_unit
    if raw_unit.strip().casefold() in {"%", "x", "х", "×", "раз", "раза", "крат"}:
        return raw_unit
    return ""


def _finalize(idx, concept, cands, fs, layers, how, upcache):
    """Из кандидатов — запись справочника: основная строка, происхождение, способ шока, уверенность."""
    max_layer = max(layers.values()) if layers else 0

    def n_inputs(s, r):
        k = (s, r)
        if k not in upcache:
            upcache[k] = len(upstream_inputs(idx, s, r, fs, max_nodes=20000)[0])
        return upcache[k]

    for c in cands:
        c["cls"] = row_class(idx, c["sheet"], c["row"], fs)
        c["inputs"] = n_inputs(c["sheet"], c["row"])
        c["live"] = c["inputs"] > 0 or (c["cls"] in ("input", "mixed") and bool(idx.dependents.get((c["sheet"], c["row"]))))
    top = max((c["inputs"] for c in cands), default=0) or 1
    for c in cands:
        sh = idx.sheets[c["sheet"]]
        c["score"] = (c["pre"] + (3.0 if c["live"] else -4.0) + 2.0 * c["inputs"] / top
                      + (layers.get(c["sheet"], 0) / max_layer if max_layer else 0) + (0.5 if sh.role == "report" else 0))
    cands.sort(key=lambda c: -c["score"])
    p = cands[0]
    # происхождение: по цепочке ссылок; среди строк с теми же значениями — самая влиятельная
    pser = numeric_years(idx, p["sheet"], p["row"])
    pool = {origin_of(idx, p["sheet"], p["row"], fs)}
    for c in cands[1:]:
        if _proportional(pser, numeric_years(idx, c["sheet"], c["row"]), fs):
            pool.add(origin_of(idx, c["sheet"], c["row"], fs))
    best, best_n = None, -1
    for node in pool:
        n = len(_downstream(idx, node, 4000))
        if n > best_n:
            best, best_n = node, n
    shock = "drivers"                           # уточняется в _second_pass, когда известны остальные показатели
    conf = {3: 0.9, 2.5: 0.85, 2: 0.7, 1: 0.45}[p["level"]] if how == "lexicon" else 0.8
    if not p["live"]:
        conf -= 0.35
    return {"id": concept["id"], "name": concept["name"], "group": concept["group"], "kind": concept["kind"],
            "primary": _describe(idx, p["sheet"], p["row"]), "origin": _describe(idx, best[0], best[1]),
            "shock": shock, "downstream": best_n, "inputs": p["inputs"], "live": p["live"],
            "alternatives": [_describe(idx, c["sheet"], c["row"]) | {"inputs": c["inputs"], "live": c["live"]} for c in cands[1:4]],
            "match": how if how != "lexicon" else {3: "exact", 2.5: "exact", 2: "prefix", 1: "contains"}[p["level"]],
            "confidence": round(max(0.05, conf), 2), "via": concept.get("via")}


RESULTS = ["cash", "debt", "net_debt", "net_income", "net_debt_ebitda", "dscr", "icr"]     # итоги: показываем, до каких дойдёт шок
MONEY = ("cash", "net_debt", "debt")                                                       # денежная позиция заёмщика


def _second_pass(idx, cat):
    """То, что видно только на всём справочнике: до каких итогов дойдёт прямая подмена показателя (и значит, можно ли
    шоковать его напрямую) и не похожа ли найденная строка на сегмент, а не на итог по компании."""
    nodes = {}
    for cid, e in cat["concepts"].items():
        nodes[cid] = {(e["primary"]["sheet"], e["primary"]["row"]), (e["origin"]["sheet"], e["origin"]["row"])}
    live_inputs = sorted(e["inputs"] for e in cat["concepts"].values() if e["live"] and e["inputs"] > 0)
    med = statistics.median(live_inputs) if live_inputs else 0
    for cid, e in cat["concepts"].items():
        down = _downstream(idx, (e["origin"]["sheet"], e["origin"]["row"]))
        targets = [r for r in RESULTS if r in nodes and r != cid]
        reach = [r for r in targets if nodes[r] & down]
        e["reaches"] = reach
        e["downstream"] = len(down)
        money = [r for r in MONEY if r in nodes and r != cid]
        if e["kind"] == "ratio":
            e["shock"] = "drivers"              # коэффициент нельзя задать: меняются показатели, из которых он считается
        elif money and any(r in reach for r in money):
            e["shock"] = "direct"               # подмена показателя доходит до денежной позиции — модель считает ОТ него
        elif not money and down:
            e["shock"] = "direct"
        else:
            e["shock"] = "drivers"              # справочная строка (например, EBITDA «для сведения»): от неё считаются
                                                # только коэффициенты, деньги — мимо; честнее менять её допущения
        if e["match"] not in ("pinned",) and med and 0 < e["inputs"] < 0.1 * med and not e.get("_seg"):
            e["confidence"] = round(max(0.05, e["confidence"] - 0.4), 2)
            e["note"] = "зависит от малого числа допущений — возможно, это показатель сегмента, а не компании"
            e["_seg"] = True
    return cat


def build_catalog(idx: WorkbookIndex, forecast_start=None, pins: dict | None = None):
    """Детерминированный справочник (без LLM и без пересчёта)."""
    fs = forecast_start
    layers = sheet_layers(idx)
    upcache, concepts, missing = {}, {}, []
    for c in CONCEPTS:
        cands = _candidates(idx, c, fs)
        if not cands:
            missing.append(c["id"])
            continue
        concepts[c["id"]] = _finalize(idx, c, cands, fs, layers, "lexicon", upcache)
    cat = {"version": CATALOG_VERSION, "file_hash": idx.file_hash, "concepts": concepts, "missing": missing,
           "alt_sets": [dict(a) for a in idx.alt_sets], "warnings": list(idx.warnings), "llm": None, "pins": {}}
    for cid, pin in (pins or {}).items():
        try:
            apply_pin(idx, cat, cid, pin["sheet"], int(pin["row"]), fs, pin.get("name"), save=False)
        except Exception as e:                      # закрепление устарело (строка исчезла) — сообщаем, не падаем
            cat["warnings"].append(f"Закрепление «{cid}» → {pin.get('sheet')}!{pin.get('row')} не применено: {e}")
    return _second_pass(idx, cat)


def apply_pin(idx, cat, cid, sheet, row, fs=None, name=None, save=True):
    """Закрепить показатель за строкой (решение пользователя сильнее автоматики)."""
    sh = idx.sheet(sheet)
    ri = sh.rows.get(int(row))
    if not ri:
        raise ValueError(f"Строка {sh.name}!{row} пуста")
    if not numeric_years(idx, sh.name, ri.row) and not is_scalar_row(sh, ri):
        raise ValueError(f"В строке {sh.name}!{row} нет чисел")
    concept = BY_ID.get(cid) or {"id": cid, "name": name or ri.label[:60] or cid, "group": "custom", "kind": "flow", "via": None}
    cand = {"sheet": sh.name, "row": ri.row, "label": ri.label, "unit": ri.unit, "level": 3, "n": 0, "pre": 6.0}
    cat["concepts"][cid] = _finalize(idx, concept, [cand], fs, sheet_layers(idx), "pinned", {})
    cat["concepts"][cid]["confidence"] = 1.0
    if cid in cat["missing"]:
        cat["missing"].remove(cid)
    cat["pins"][cid] = {"sheet": sh.name, "row": ri.row, **({"name": concept["name"]} if cid not in BY_ID else {})}
    if save:
        _second_pass(idx, cat)
    return cat["concepts"][cid]


# ───────────────────────── LLM: сопоставление ненайденного ─────────────────────────
MAP_SYSTEM = """Ты — кредитный аналитик. Тебе дают список показателей, которые не удалось найти в финансовой модели по
названию, и список итоговых строк модели (подпись, лист, блок, единицы, несколько значений). Для каждого показателя
выбери строку, которая им является (итоговое значение по компании/проекту, а не сегмент и не производная величина),
или "none", если такой строки нет. Не угадывай: при сомнении ставь "none" или низкую уверенность.
Ответ — только вызовом функции map_concepts."""


def _pool(idx, cat, fs, limit=220):
    """Строки, среди которых LLM ищет показатели: расчётные итоги на листах ближе к результатам."""
    layers = sheet_layers(idx)
    rows, seen = [], set()
    order = sorted((s for s in idx.sheets.values() if s.years and s.name not in idx.inactive and s.role not in ("inputs", "contents", "separator")),
                   key=lambda s: (-(s.role == "report"), -layers.get(s.name, 0)))
    for sh in order:
        for ri in sh.rows.values():
            lab = norm(ri.label)
            if not lab or len(lab) > 100 or lab in seen or ri.row in sh.headers:
                continue
            if row_class(idx, sh.name, ri.row, fs) not in ("calc", "link", "mixed"):
                continue
            ny = numeric_years(idx, sh.name, ri.row)
            vals = [(y, v) for y, v in sorted(ny.items()) if fs is None or y >= fs][:3]
            if len(vals) < 2:
                continue
            seen.add(lab)
            rows.append({"id": f"{sh.name}!{ri.row}", "label": ri.label[:100], "unit": ri.unit, "sheet": sh.name,
                         "values": {str(y): (round(v, 3) if abs(v) < 1e5 else round(v)) for y, v in vals}})
            if len(rows) >= limit:
                return rows
    return rows


def llm_map(idx, cat, client, fs=None, emit=None, threshold=0.6):
    """Показатели без уверенного совпадения сопоставляет LLM; результат проходит ту же структурную проверку."""
    if client is None or getattr(client, "name", "") == "mock":
        return 0
    todo = [c for c in CONCEPTS if c["id"] not in cat["pins"]
            and (c["id"] in cat["missing"] or cat["concepts"].get(c["id"], {}).get("confidence", 1) < threshold)]
    if not todo:
        cat["llm"] = {"model": client.describe(), "asked": 0, "mapped": 0}
        return 0
    pool = _pool(idx, cat, fs)
    if not pool:
        return 0
    if emit:
        emit("progress", {"text": f"Справочник показателей: сопоставляю {len(todo)} показателей со строками модели…"})
    spec = {"name": "map_concepts", "description": "Сопоставление показателей со строками модели",
            "parameters": {"type": "object", "properties": {"mappings": {"type": "array", "items": {"type": "object", "properties": {
                "concept": {"type": "string", "enum": [c["id"] for c in todo]},
                "row": {"type": "string", "description": "id строки из списка или none"},
                "confidence": {"type": "number"}}, "required": ["concept", "row"]}}}, "required": ["mappings"]}}
    user = {"concepts": [{"id": c["id"], "name": c["name"], "kind": c["kind"],
                          "current_guess": (cat["concepts"].get(c["id"]) or {}).get("primary", {}).get("label")} for c in todo],
            "rows": pool}
    mapped, errors = 0, []
    try:
        resp = client.chat([{"role": "system", "content": MAP_SYSTEM},
                            {"role": "user", "content": json.dumps(user, ensure_ascii=False)}], [spec], {"name": "map_concepts"})
        call = next((c for c in resp.tool_calls if c.name == "map_concepts"), None)
        ids = {r["id"] for r in pool}
        layers = sheet_layers(idx)
        for m in ((call.arguments if call else {}) or {}).get("mappings") or []:
            cid, rid = m.get("concept"), str(m.get("row") or "")
            if cid not in BY_ID or rid not in ids or float(m.get("confidence", 0.7) or 0) < 0.5:
                continue
            sheet, row = rid.rsplit("!", 1)
            ri = idx.sheets[sheet].rows[int(row)]
            cand = {"sheet": sheet, "row": int(row), "label": ri.label, "unit": ri.unit, "level": 2,
                    "n": 0, "pre": 5.0}
            entry = _finalize(idx, BY_ID[cid], [cand], fs, layers, "llm", {})
            old = cat["concepts"].get(cid)
            if old and old.get("live") and not entry["live"]:
                continue                                  # не меняем «живую» строку на статичную
            if old:
                entry["alternatives"] = [old["primary"] | {"inputs": old["inputs"], "live": old["live"]}] + old["alternatives"][:2]
            entry["confidence"] = round(min(0.85, float(m.get("confidence", 0.7) or 0.7)), 2)
            cat["concepts"][cid] = entry
            if cid in cat["missing"]:
                cat["missing"].remove(cid)
            mapped += 1
    except Exception as e:                                 # LLM недоступна — остаётся результат по лексикону
        errors.append(str(e)[:200])
    cat["llm"] = {"model": client.describe(), "asked": len(todo), "mapped": mapped, "errors": errors}
    _second_pass(idx, cat)
    return mapped


# ───────────────────────── какой набор допущений выбран ─────────────────────────
def probe_alt_sets(s, emit=None):
    """Проверка пересчётом: шокируем по очереди каждый лист-близнец и смотрим, меняются ли показатели.
    Набор, на шок которого модель реагирует, — выбранный. Если реагирует на несколько — это не альтернативы,
    а параллельные источники, все остаются активными. Результат кэшируется в справочнике."""
    idx, cat = s.idx, s.catalog
    todo = [a for a in cat["alt_sets"] if not a.get("detected")]
    if not todo or not s.engine_open:
        return False
    reads = []
    for cid in HEADLINE + [c for c in cat["concepts"] if c not in HEADLINE]:
        e = cat["concepts"].get(cid)
        if not e:
            continue
        sh = idx.sheets[e["primary"]["sheet"]]
        cols = [c for c, y in sorted(sh.years_for(e["primary"]["row"]).items()) if s.forecast_start is None or y >= s.forecast_start][:8]
        if cols:
            reads += ref_for(idx, sh.name, e["primary"]["row"], cols)
        if len(reads) >= 8:
            break
    if not reads:
        return False
    base = s.engine.read(reads)
    flat = lambda blocks: [v for b in blocks for line in b for v in line]        # noqa: E731
    fb = flat(base)

    def differs(x, y):                       # число изменилось или расчёт сломался (ошибка вместо числа) — это реакция
        if _num(x) and _num(y):
            return abs(x - y) > 1e-6 * max(abs(x), abs(y), 1e-9)
        return _num(x) != _num(y)

    def yearlike(vals):                      # строка-шкала периодов или индексов: её шок ломает поиск по шапке, а не экономику
        return all(float(v).is_integer() and 1900 <= v <= 2100 for v in vals)

    changed = False
    for a in todo:
        if emit:
            emit("progress", {"text": "Проверяю пересчётом, какой набор допущений выбран: " + ", ".join(a["sheets"])})
        share = {}
        for name in a["sheets"]:
            sh = idx.sheets[name]
            rows = [r for r, ri in sh.rows.items() if ri.label and idx.dependents.get((name, r))
                    and row_class(idx, name, r, s.forecast_start) == "input"]
            step = max(1, len(rows) // 60)
            ov = []
            for r in rows[::step][:60]:
                ri = sh.rows[r]
                cols = [c for c, y in sorted(sh.years_for(r).items()) if (s.forecast_start is None or y >= s.forecast_start)
                        and _num(ri.values.get(c)) and c not in ri.formulas]
                if not cols or yearlike([ri.values[c] for c in cols]):
                    continue
                for c0, c1 in _runs(cols):
                    ov.append({"ref": ref_for(idx, name, r, range(c0, c1 + 1))[0],
                               "values": [[ri.values[c] * 1.07 for c in range(c0, c1 + 1)]]})
            if not ov:
                continue
            got, _ = s.engine.scenario(ov, reads[:2], reads)
            fg = flat(got)
            share[name] = sum(differs(x, y) for x, y in zip(fb, fg)) / max(1, len(fb))
        # Набор выбран, если на его шок реагирует заметная доля показателей. Лист, который двигает лишь единичные
        # ячейки (например, таблицу сравнения сценариев в отчёте), выбранным не считается. Реагируют несколько
        # наборов сопоставимо — это не альтернативы, а параллельные источники: активны все.
        top = max(share.values(), default=0)
        react = [n for n in a["sheets"] if top > 0 and share.get(n, 0) >= max(0.5 * top, 1e-9)]
        a["reaction"] = {n: round(v, 3) for n, v in share.items()}
        if react:
            a["active"], a["detected"] = react, True
            changed = True
        else:
            a["note"] = "модель не реагирует ни на один набор — оставлен первый по порядку"
    if changed:
        apply_alt_sets(idx, cat)
    return changed


def _runs(cols):
    """Столбцы → непрерывные отрезки (c0, c1)."""
    out, cols = [], sorted(cols)
    for c in cols:
        if out and c == out[-1][1] + 1:
            out[-1][1] = c
        else:
            out.append([c, c])
    return [tuple(x) for x in out]


def apply_alt_sets(idx, cat):
    idx.alt_sets = [dict(a) for a in cat["alt_sets"]]
    idx.inactive = {n for a in idx.alt_sets for n in a["sheets"] if n not in a["active"]}


# ───────────────────────── кэш ─────────────────────────
def cache_path(cache_dir, fh):
    return os.path.join(cache_dir, f"{fh}.cat.v{CATALOG_VERSION}.json")


def pins_path(cache_dir, fh):
    return os.path.join(cache_dir, f"pins_{fh}.json")      # отдельным файлом: не стирается при пересборке кэша


def load_pins(cache_dir, fh):
    p = pins_path(cache_dir, fh) if cache_dir else None
    if p and os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_cached(cache_dir, fh):
    p = cache_path(cache_dir, fh) if cache_dir else None
    if p and os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return None


def save(cache_dir, cat):
    if not cache_dir:
        return
    os.makedirs(cache_dir, exist_ok=True)
    with open(cache_path(cache_dir, cat["file_hash"]), "w", encoding="utf-8") as f:
        json.dump(cat, f, ensure_ascii=False)
    with open(pins_path(cache_dir, cat["file_hash"]), "w", encoding="utf-8") as f:
        json.dump(cat.get("pins") or {}, f, ensure_ascii=False)


# ───────────────────────── доступ ─────────────────────────
def resolve(s, key: str):
    """Показатель по id или по названию («выручка», «Revenue», «чистый долг / EBITDA»)."""
    cat = getattr(s, "catalog", None) or {"concepts": {}, "missing": []}
    k = norm(str(key))
    if k in cat["concepts"]:
        return cat["concepts"][k]
    for e in cat["concepts"].values():
        if norm(e["name"]) == k:
            return e
    for c in CONCEPTS:                                    # по синонимам лексикона
        if match_level(c, str(key)) >= 2 and c["id"] in cat["concepts"]:
            return cat["concepts"][c["id"]]
    known = ", ".join(f"{e['id']} ({e['name']})" for e in cat["concepts"].values())
    miss = BY_ID.get(k) or next((c for c in CONCEPTS if match_level(c, str(key)) >= 2), None)
    if miss:
        raise ValueError(f"Показатель «{miss['name']}» в этой модели не найден. Найди строку через search_rows и закрепи её: "
                         f"catalog_set(concept=\"{miss['id']}\", sheet, row). В справочнике есть: {known}")
    raise ValueError(f"Показателя «{key}» нет в справочнике. Есть: {known}. Свой показатель можно добавить через catalog_set.")


def headline_metrics(cat):
    """Ключевые метрики карточки модели: сначала привычные девять, затем остальные найденные."""
    out = []
    for cid in HEADLINE + [c["id"] for c in CONCEPTS if c["id"] not in HEADLINE] + [c for c in cat["concepts"] if c not in BY_ID]:
        e = cat["concepts"].get(cid)
        if not e:
            continue
        cands = [dict(e["primary"], inputs=e["inputs"], **({} if e["live"] else {"static": True}))]
        for alt in e["alternatives"][:1]:
            cands.append(dict(alt, **({} if alt.get("live", True) else {"static": True})))
        out.append({"metric": e["name"], "concept": cid, "candidates": cands})
    return out


def text(cat, limit=30):
    """Справочник для системного промпта агента."""
    lines = []
    for g, gname in GROUPS.items():
        es = [e for e in cat["concepts"].values() if e["group"] == g]
        if not es:
            continue
        parts = []
        for e in es:
            how = {"direct": "шок напрямую", "drivers": "шок через допущения"}[e["shock"]]
            where = f"{e['primary']['sheet']}!{e['primary']['row']}"
            if (e["origin"]["sheet"], e["origin"]["row"]) != (e["primary"]["sheet"], e["primary"]["row"]):
                where += f" (считается в {e['origin']['sheet']}!{e['origin']['row']})"
            flag = "" if e["live"] else ", НЕ зависит от допущений"
            sure = "" if e["confidence"] >= 0.6 else ", совпадение неуверенное — проверь"
            parts.append(f"{e['id']} «{e['name']}» → {where} [{how}{flag}{sure}]")
        lines.append(f"{gname}: " + "; ".join(parts[:limit]))
    if cat.get("missing"):
        lines.append("Не найдены: " + ", ".join(BY_ID[m]["name"] for m in cat["missing"] if m in BY_ID)
                     + " (ищи через search_rows; найденную строку можно закрепить: catalog_set).")
    for a in cat.get("alt_sets") or []:
        lines.append("Наборы допущений (листы-близнецы): " + ", ".join(a["sheets"]) + " — выбран " + ", ".join(a["active"])
                     + (" (проверено пересчётом)" if a.get("detected") else " (предположительно, не проверено пересчётом)")
                     + ". Допущения на невыбранных листах на результат не влияют.")
    for w in (cat.get("warnings") or [])[:4]:
        lines.append("Ограничение: " + w)
    return "\n".join(lines)
