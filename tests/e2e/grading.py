"""Оценка ответов: детерминированные проверки + LLM-as-judge.

Детерминированные метрики (без LLM, воспроизводимые):
  tools_ok        — вызваны ли нужные инструменты (каждая группа expect_tools: хотя бы один из списка)
  tool_errors     — сколько вызовов инструментов завершилось ошибкой
  grounding       — доля чисел в ответе, которые есть в результатах инструментов (защита от выдуманных цифр)
  cell_refs       — сколько адресов ячеек вида «Лист!123» в тексте ответа вне строки «Где в модели» (язык факторов)
  seconds / steps — время и число вызовов инструментов

LLM-as-judge (отдельная модель/вызов): 5 критериев по шкале 1–5 + общий вердикт, по вопросу, ответу и сжатой
трассе инструментов. Судья видит ТОЛЬКО то, что получил агент, поэтому оценивает обоснованность, а не «правду о мире».
"""
from __future__ import annotations

import json
import math
import re

NUM_RE = re.compile(r"(?<![\w.])[−\-+]?\d{1,3}(?:[  ]\d{3})+(?:[.,]\d+)?|(?<![\w.])[−\-+]?\d+(?:[.,]\d+)?")
CELL_RE = re.compile(r"[A-Za-zА-Яа-яЁё_][\wА-Яа-яЁё .&-]{0,30}![A-Z]{0,3}\d{1,5}\b")


def _to_float(tok):
    t = tok.replace(" ", "").replace(" ", "").replace("−", "-").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def numbers_in_text(text):
    out = []
    for m in NUM_RE.finditer(text or ""):
        v = _to_float(m.group(0))
        if v is not None:
            out.append(v)
    return out


def numbers_in_obj(obj, acc=None):
    acc = acc if acc is not None else []
    if isinstance(obj, bool):
        return acc
    if isinstance(obj, (int, float)):
        if math.isfinite(obj):
            acc.append(float(obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            numbers_in_obj(v, acc)
            if isinstance(k, str):
                acc += numbers_in_text(k)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            numbers_in_obj(v, acc)
    elif isinstance(obj, str):
        acc += numbers_in_text(obj)
    return acc


def _match(v, pool):
    for p in pool:
        for cand in (p, p * 100, p / 100):            # доля ↔ проценты
            if abs(v - cand) <= max(0.011, 0.012 * abs(cand)):   # округление при выводе
                return True
    return False


def grounding(answer, tool_results, question=""):
    """Доля «значимых» чисел ответа, которые встречаются в результатах инструментов (±1,2% или ±0,01)."""
    pool = numbers_in_obj(tool_results)
    qnums = set(numbers_in_text(question))
    nums = []
    for v in numbers_in_text(answer):
        if 1990 <= v <= 2070 and float(v).is_integer():      # годы
            continue
        if abs(v) <= 10 and float(v).is_integer():             # нумерация, «3 замечания», «1–3»
            continue
        if v in qnums or -v in qnums:                          # числа из вопроса (шоки, пороги)
            continue
        nums.append(v)
    if not nums:
        return {"score": None, "numbers": 0, "ungrounded": []}
    bad = [v for v in nums if not _match(v, pool) and not _match(-v, pool)]
    return {"score": round(1 - len(bad) / len(nums), 3), "numbers": len(nums), "ungrounded": bad[:12]}


def cell_refs(answer):
    lines = [l for l in (answer or "").split("\n")
             if not re.search(r"где в модели|ячейк|адрес", l, re.I) and not l.lstrip().startswith("|")]   # таблицы-справки допустимы
    text = "\n".join(lines)
    text = re.sub(r"`[^`]*`", "", text)                   # код-блоки с адресами допустимы
    return len(CELL_RE.findall(text))


def tools_check(called, expect):
    miss = [g for g in (expect or []) if not any(t in called for t in g)]
    return {"ok": not miss, "missing": miss}


def deterministic(case, run):
    called = [t["name"] for t in run["tools"]]
    res = {"seconds": run["seconds"], "steps": len(called), "tool_errors": sum(1 for t in run["tools"] if not t["ok"]),
           "error": run.get("error")}
    if case.get("expect_tools"):
        res["tools"] = tools_check(called, case["expect_tools"])
    if run.get("answer"):
        res["grounding"] = grounding(run["answer"], [t["result"] for t in run["tools"]], case.get("query", ""))
        res["cell_refs"] = cell_refs(run["answer"])
        res["answer_chars"] = len(run["answer"])
    lim = case.get("max_seconds")
    res["too_slow"] = bool(lim and run["seconds"] > lim)
    return res


# ───────────────────────── LLM-as-judge ─────────────────────────
JUDGE_SYSTEM = """Ты — строгий старший кредитный аналитик и проверяющий качество ИИ-агента для анализа финансовых моделей
заёмщиков (CashFlow). Тебе дают вопрос сотрудника, ответ агента и сжатую трассу инструментов (что агент реально
посчитал в модели). Трасса может быть обрезана: не штрафуй за факт, если он правдоподобно мог прийти из обрезанной части,
а отметь это в bad. Оцени ответ ТОЛЬКО по этим данным, по шкале 1–5 (5 — отлично):
• correctness — цифры и утверждения ответа подтверждаются результатами инструментов; нет выдумок и противоречий;
• completeness — ответ закрывает все части вопроса (все метрики, сценарии, годы, что просили);
• credit_usefulness — полезно ли кредитчику: вывод, риски, что проверить; ясная интерпретация, а не пересказ таблиц;
• business_language — понятный язык факторов и показателей, адреса ячеек только для ссылок, без жаргона;
• honesty — оговорки и ограничения указаны там, где данных не хватает или были ошибки инструментов.
Дай короткие комментарии: что хорошо, что плохо, и конкретную рекомендацию по улучшению агента.
pass = true, если все критерии ≥ 3 и correctness ≥ 4. Ответь ТОЛЬКО вызовом функции grade."""

GRADE_SPEC = {"name": "grade", "description": "Оценка ответа агента", "parameters": {"type": "object", "properties": {
    "correctness": {"type": "integer"}, "completeness": {"type": "integer"}, "credit_usefulness": {"type": "integer"},
    "business_language": {"type": "integer"}, "honesty": {"type": "integer"},
    "pass": {"type": "boolean"}, "good": {"type": "string"}, "bad": {"type": "string"},
    "fix": {"type": "string", "description": "Что поменять в агенте (промпт, инструмент, логика)"}},
    "required": ["correctness", "completeness", "credit_usefulness", "business_language", "honesty", "pass"]}}
CRITERIA = ["correctness", "completeness", "credit_usefulness", "business_language", "honesty"]


def _trace(run, limit=60000):
    parts = []
    for t in run["tools"]:
        if t["name"] in ("create_plan", "update_plan"):
            continue
        r = json.dumps(t["result"], ensure_ascii=False, default=str)
        parts.append(f"### {t['name']} ({'ok' if t['ok'] else 'ОШИБКА'}) args={json.dumps(t['args'], ensure_ascii=False)[:300]}\n{r[:8000]}")
    s = "\n\n".join(parts)
    return s[:limit] + ("\n…[трасса обрезана]" if len(s) > limit else "")


def judge(client, case, run):
    if client is None or getattr(client, "name", "") == "mock" or not run.get("answer"):
        return None
    q = case.get("query") or case.get("news") or case.get("id")
    user = (f"ВОПРОС / ЗАДАЧА:\n{q}\n\nОСОБЫЙ ФОКУС ПРОВЕРКИ: {case.get('rubric', '—')}\n\n"
            f"ОТВЕТ АГЕНТА:\n{run['answer'][:12000]}\n\nТРАССА ИНСТРУМЕНТОВ:\n{_trace(run)}")
    try:
        r = client.chat([{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": user}],
                        [GRADE_SPEC], {"name": "grade"})
        call = next((c for c in r.tool_calls if c.name == "grade"), None)
        g = dict(call.arguments) if call else {}
    except Exception as e:  # судья недоступен — тест не валится, просто без оценки
        return {"error": str(e)[:300]}
    vals = [g.get(k) for k in CRITERIA if isinstance(g.get(k), (int, float))]
    g["score"] = round(sum(vals) / len(vals), 2) if vals else None
    return g


def verdict(det, jd):
    """Итог кейса: FAIL при ошибке/нет нужных инструментов/выдуманных цифрах/провале судьи; WARN — мягкие проблемы."""
    reasons = []
    if det.get("error"):
        reasons.append(("FAIL", f"ошибка: {det['error'][:120]}"))
    if det.get("tools") and not det["tools"]["ok"]:
        reasons.append(("FAIL", f"не вызваны инструменты: {det['tools']['missing']}"))
    g = (det.get("grounding") or {}).get("score")
    if g is not None and g < 0.7:
        reasons.append(("FAIL", f"обоснованность чисел {g:.0%}"))
    elif g is not None and g < 0.9:
        reasons.append(("WARN", f"обоснованность чисел {g:.0%}"))
    if det.get("tool_errors"):
        reasons.append(("WARN", f"ошибок инструментов: {det['tool_errors']}"))
    if det.get("cell_refs", 0) > 3:
        reasons.append(("WARN", f"адресов ячеек в тексте: {det['cell_refs']}"))
    if det.get("too_slow"):
        reasons.append(("WARN", f"дольше лимита: {det['seconds']} с"))
    if jd and not jd.get("error"):
        if jd.get("pass") is False:
            reasons.append(("FAIL", f"судья: {str(jd.get('bad', ''))[:140]}"))
    status = "FAIL" if any(r[0] == "FAIL" for r in reasons) else ("WARN" if reasons else "PASS")
    return status, [r[1] for r in reasons]
