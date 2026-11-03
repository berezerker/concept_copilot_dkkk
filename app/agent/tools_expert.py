"""«Отраслевой эксперт»: отдельный вызов LLM в роли эксперта отрасли.

Получает новость/событие и список полей-допущений модели (подпись, единицы, текущие значения по периодам),
возвращает новые значения или изменения по каждому полю с обоснованием и уверенностью — и сразу
прогоняет их стресс-тестом на текущей модели (run_stress + covenant_check).
"""
from __future__ import annotations

import json
import re

from ..workbook.card import numeric_years
from .tools import METRIC_SCHEMA, rnd, t_run_stress
from .tools_ext import RULE_SCHEMA, t_covenant_check

EXPERT_SYSTEM = """Ты — отраслевой эксперт и аналитик рынков сырья, макроэкономики и отраслей (нефтегаз, металлургия,
энергетика, девелопмент, ритейл, АПК и др.). Тебе дают новость или событие и список допущений финансовой модели
заёмщика с текущими значениями по периодам. Оцени, как разумный консервативный эксперт, какими станут эти допущения
с учётом новости, и верни ответ ТОЛЬКО вызовом функции propose_assumptions.
Правила:
• Сохраняй единицы модели (если значение 0,82 — это доля, не проценты; курс в руб./$; цена в $/барр. и т. п.).
• Для каждого поля выбери: op=pct (изменение в % ко всем периодам), op=add (прибавить в единицах модели) или
  op=path (новые значения по конкретным периодам в values_by_period — если эффект меняется во времени, например
  шок в первый год и постепенное восстановление).
• Если новость на поле не влияет — op=pct, value=0 и так и напиши в rationale.
• rationale — 1–2 предложения: механизм влияния и откуда величина (аналогии, эластичности, консенсус). Не выдумывай
  точные цитаты и источники.
• confidence: low / medium / high.
• Дай короткое название сценария и 2–4 ключевых риска/допущения сценария."""

PROPOSE_SPEC = {
    "name": "propose_assumptions",
    "description": "Новые допущения модели с учётом новости",
    "parameters": {"type": "object", "properties": {
        "scenario_title": {"type": "string"},
        "summary": {"type": "string", "description": "Суть сценария в 1–2 предложениях"},
        "assumptions": {"type": "array", "items": {"type": "object", "properties": {
            "field_id": {"type": "integer"},
            "op": {"type": "string", "enum": ["pct", "add", "path"]},
            "value": {"type": "number", "description": "Для pct — изменение в %, для add — прибавка в единицах модели"},
            "values_by_period": {"type": "object", "description": "Для path: новые значения по периодам, например {'2026': 65.0, '2027': 70.0}"},
            "rationale": {"type": "string"},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]}},
            "required": ["field_id", "op", "rationale"]}},
        "key_risks": {"type": "array", "items": {"type": "string"}}},
        "required": ["scenario_title", "assumptions"]},
}

NEG = re.compile(r"паден|сниж|санкц|кризис|рецесс|обвал|ограничен|запрет|потол|дефицит спроса|спад|убыт|дешев", re.I)
POS = re.compile(r"рост|повыш|подорож|дефицит предлож|увелич|сокращен.{0,10}добыч|ослаблен.{0,6}рубл", re.I)


def _field_info(s, f, i):
    sh = s.idx.sheet(f["sheet"])
    if f.get("cell"):
        from ..workbook.index import col_index
        m = re.match(r"^\$?([A-Z]{1,3})\$?(\d+)$", str(f["cell"]).upper())
        if not m:
            raise ValueError(f"Неверная ячейка {f['cell']}")
        row = int(m.group(2))
        ri = sh.rows.get(row)
        v = ri.values.get(col_index(m.group(1))) if ri else None
        if s.engine_open:
            try:
                v = s.engine.read([f"'{sh.name}'!{m.group(1)}{row}"])[0][0][0]
            except Exception:
                pass
        return {"field_id": i, "ref": f"{sh.name}!{f['cell']}", "label": f.get("name") or (ri.label if ri else ""),
                "unit": ri.unit if ri else "", "current": rnd(v), "kind": "cell"}
    row = int(f["row"])
    ri = s.idx.row(sh.name, row)
    vals = {y: v for y, v in numeric_years(s.idx, sh.name, row).items() if not s.forecast_start or y >= s.forecast_start}
    from ..workbook import semantics as sm
    hi = sm.info(s, sh.name, row)
    return {"field_id": i, "ref": f"{sh.name}!{row}", "label": f.get("name") or hi["name"], "unit": ri.unit,
            "meaning": hi["desc"] or None, "factor": hi["factor_name"],
            "current": {str(y): rnd(v) for y, v in list(sorted(vals.items()))[:10]}, "kind": "row"}


def _offline(news, info):
    """Без LLM: простая эвристика по тону новости и процентам в тексте (для офлайн-проверки)."""
    pcts = [float(x.replace(",", ".")) for x in re.findall(r"(\d+(?:[.,]\d+)?)\s*%", news)]
    mag = pcts[0] if pcts else 10.0
    sign = -1 if NEG.search(news) and not POS.search(news) else (1 if POS.search(news) else -1)
    return {"scenario_title": "Сценарий по новости (офлайн-эвристика)",
            "summary": "Офлайн-режим без LLM: ко всем полям применён одинаковый шок по тону новости. "
                       "Подключите OpenAI или GigaChat, чтобы получить экспертную оценку по каждому полю.",
            "assumptions": [{"field_id": x["field_id"], "op": "pct", "value": sign * mag,
                             "rationale": "эвристика по тону новости", "confidence": "low"} for x in info],
            "key_risks": ["Оценка не экспертная — только для проверки работы"]}


def ask_expert(client, news, info, industry=None, emit=None):
    if client is None or getattr(client, "name", "") == "mock":
        return _offline(news, info)
    user = {"news": news, "industry": industry or "определи по полям", "fields": info}
    msgs = [{"role": "system", "content": EXPERT_SYSTEM},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False, default=str)}]
    if emit:
        emit("progress", {"text": f"Отраслевой эксперт ({client.describe()}) оценивает {len(info)} полей…"})
    resp = client.chat(msgs, [PROPOSE_SPEC], {"name": "propose_assumptions"})
    call = next((c for c in resp.tool_calls if c.name == "propose_assumptions"), None)
    if call is None or "_raw" in call.arguments:
        txt = resp.content or (call.arguments.get("_raw") if call else "")
        m = re.search(r"\{.*\}", txt or "", re.S)
        if not m:
            raise ValueError("Эксперт не вернул структурированный ответ")
        return json.loads(m.group(0))
    return call.arguments


def build_shocks(s, fields, info, prop):
    shocks, table, warn = [], [], []
    by_id = {x["field_id"]: x for x in info}
    for p in prop.get("assumptions") or []:
        fid = int(p.get("field_id", -1))
        if fid not in by_id:
            continue
        f, x = fields[fid], by_id[fid]
        op = p.get("op", "pct")
        target = {"sheet": f["sheet"], "cell": f["cell"]} if f.get("cell") else {"sheet": f["sheet"], "row": int(f["row"])}
        row = {"field": x["label"][:70], "ref": x["ref"], "unit": x["unit"], "op": op,
               "rationale": p.get("rationale", ""), "confidence": p.get("confidence", "")}
        if op == "path" and p.get("values_by_period"):
            path = {str(k): float(v) for k, v in p["values_by_period"].items()}
            row["new"] = path
            cur = x["current"] if isinstance(x["current"], dict) else {}
            for k, v in path.items():
                if x["kind"] == "cell":
                    shocks.append(dict(target, op="set", value=v))
                    break
                shocks.append(dict(target, op="set", value=v, years=[k]))
                c = cur.get(k)
                if isinstance(c, (int, float)) and c and abs(v / c) > 5:
                    warn.append(f"{x['ref']} {k}: {v} против {c} — проверь единицы")
        else:
            val = float(p.get("value") or 0)
            if op == "pct" and abs(val) > 80:
                warn.append(f"{x['ref']}: шок {val:+g}% ограничен до ±80%")
                val = max(-80.0, min(80.0, val))
            row["change"] = f"{val:+g}%" if op == "pct" else f"{val:+g} {x['unit']}".strip()
            if val:
                shocks.append(dict(target, op=op if op in ("pct", "add") else "pct", value=val))
        table.append(row)
    return shocks, table, warn


def t_industry_expert(s, a, emit):
    news = (a.get("news") or "").strip()
    fields = (a.get("fields") or [])[:15]
    if not news or not fields:
        raise ValueError("Нужны news (текст новости/события) и fields (поля-допущения: sheet+row или sheet+cell)")
    info = [_field_info(s, f, i) for i, f in enumerate(fields)]
    prop = ask_expert(getattr(s, "client", None), news, info, a.get("industry"), emit)
    shocks, table, warn = build_shocks(s, fields, info, prop)
    payload = {"title": prop.get("scenario_title", "Сценарий эксперта"), "summary": prop.get("summary", ""),
               "news": news[:600], "assumptions": table, "key_risks": prop.get("key_risks") or [], "warnings": warn}
    emit("expert", payload)
    out = dict(payload, shocks=shocks)
    metrics = a.get("metrics") or [{"sheet": r["sheet"], "row": r["row"], "name": r.get("name")} for r in (a.get("rules") or [])]
    if a.get("run", True) and metrics and shocks:
        s.require_engine()
        emit("progress", {"text": "Стресс-тест по сценарию эксперта…"})
        scen = [{"name": payload["title"][:60], "shocks": shocks}]
        if a.get("with_severe", True):
            sev = []
            for sh in shocks:
                if sh["op"] in ("pct", "add"):
                    sev.append(dict(sh, value=sh["value"] * 1.5))
                else:
                    sev.append(sh)
            scen.append({"name": "Эксперт ×1,5 (жёстче)", "shocks": sev})
        try:
            out["stress"] = t_run_stress(s, {"scenarios": scen, "metrics": metrics, "years": a.get("years")}, emit)
        except ValueError as e:                     # часть полей эксперта не влияет на метрики — не валим весь сценарий
            out["warnings"] = warn + [f"Стресс по сценарию: {e}"]
            return out
        if a.get("rules"):
            out["covenants"] = t_covenant_check(s, {"rules": a["rules"], "shocks": shocks, "years": a.get("years")}, emit)
    return out


EXPERT_TOOL = {
    "name": "industry_expert", "fn": t_industry_expert,
    "description": "Отраслевой эксперт: отдельная LLM-роль получает новость/событие и список полей-допущений модели "
                   "(с текущими значениями), предлагает новые значения по каждому полю с обоснованием и уверенностью — "
                   "и сразу прогоняет стресс-тест (база, сценарий эксперта, сценарий ×1,5) и проверку ковенантов. "
                   "Поля сначала найди через search_rows (строки-допущения с dependents > 0).",
    "parameters": {"type": "object", "properties": {
        "news": {"type": "string", "description": "Текст новости или описание события"},
        "fields": {"type": "array", "items": {"type": "object", "properties": {
            "sheet": {"type": "string"}, "row": {"type": "integer"}, "cell": {"type": "string"}, "name": {"type": "string"}},
            "required": ["sheet"]}, "description": "Поля-допущения (до 15)"},
        "metrics": {"type": "array", "items": METRIC_SCHEMA}, "rules": {"type": "array", "items": RULE_SCHEMA},
        "industry": {"type": "string"}, "run": {"type": "boolean"},
        "years": {"type": "array", "items": {"type": "string"}}},
        "required": ["news", "fields"]},
}
