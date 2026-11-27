"""Harness: цикл «план → инструменты → наблюдение → ответ» с событиями для интерфейса.

События (emit(type, data)):
  status      — текстовый статус
  plan        — план {goal, steps:[{title,status,note}]}
  think       — текст модели, сопровождающий вызов инструментов
  tool_start  — {id, name, args}
  tool_end    — {id, name, ok, result(усечённый), seconds}
  progress    — промежуточный прогресс внутри инструмента
  chart       — данные для графиков стресс-теста
  answer      — финальный ответ (markdown)
  error       — ошибка
"""
from __future__ import annotations

import hashlib
import json
import time
import traceback

from ..llm.base import LLMError
from .prompts import system_prompt
from .registry import TOOL_MAP, tool_specs

MAX_STEPS = 32


def _short(obj, limit):
    s = json.dumps(obj, ensure_ascii=False, default=str)
    if len(s) <= limit:
        return s
    return s[:limit] + f'… [обрезано: {len(s) - limit} символов; уточни запрос/сузь выборку]'


def run_turn(session, client, user_text: str, emit, plan_mode: bool = True, max_steps: int = MAX_STEPS):
    session.plan = None
    session.client = client                      # для инструментов с собственным вызовом LLM (отраслевой эксперт)
    turn = [{"role": "user", "content": user_text}]
    system = {"role": "system", "content": system_prompt(session)}
    force_plan = plan_mode
    failures = {}
    observations = {}
    completion_retry = False
    session.evidence = []
    specs = tool_specs()
    if not plan_mode:
        specs = [t for t in specs if t["name"] not in ("create_plan", "update_plan")]
    emit("status", {"text": f"Модель: {client.describe()}"})

    for step in range(max_steps):
        msgs = [system] + session.history + turn
        try:
            t0 = time.time()
            resp = client.chat(msgs, specs, {"name": "create_plan"} if force_plan else "auto")
        except LLMError as e:
            emit("error", {"text": str(e)})
            return
        except Exception as e:  # noqa
            from ..logbus import Cancelled
            if isinstance(e, Cancelled):
                raise
            emit("error", {"text": f"{type(e).__name__}: {e}"})
            return
        force_plan = False

        if not resp.tool_calls:
            answer = resp.content.strip() or "Модель вернула пустой ответ."
            steps = (session.plan or {}).get("steps", [])
            pending = [i + 1 for i, st in enumerate(steps) if st["status"] not in ("done", "skipped")]
            needs_evidence = plan_mode and not any(st["status"] == "done" and st.get("evidence") for st in steps)
            if (pending or failures or needs_evidence) and not completion_retry:
                completion_retry = True
                turn.append({"role": "assistant", "content": answer})
                turn.append({"role": "user", "content":
                    "Проверка выполнения: незавершённые шаги " + str(pending) +
                    "; ошибки инструментов: " + json.dumps(failures, ensure_ascii=False) +
                    (". Нет завершённого шага с результатом инструмента" if needs_evidence else "") +
                    ". Исправь ошибки и повтори нужные инструменты. Обнови план только по фактическим результатам; "
                    "невыполнимые шаги пропусти с причиной. Не объявляй расчёт успешным без подтверждения."})
                continue
            complete = not failures and not pending and not needs_evidence
            if failures:
                answer = "Задача не завершена: остались ошибки инструментов.\n\n" + "\n".join(
                    f"- {failure['tool']}: {failure['error']}" for failure in failures.values())
            elif not complete:
                answer = "Задача выполнена частично: завершение всех шагов не подтверждено.\n\n" + answer
            if session.plan:
                emit("plan", session.plan)
            emit("answer", {"text": answer, "complete": complete, "seconds": round(time.time() - t0, 1)})
            session.history += [{"role": "user", "content": user_text},
                                {"role": "assistant", "content": answer}]
            session.history = session.history[-12:]          # прошлые ходы — без служебного трафика
            return

        calls = resp.tool_calls if client.parallel_tools else resp.tool_calls[:1]
        if resp.content.strip():
            emit("think", {"text": resp.content.strip()})
        turn.append({"role": "assistant", "content": resp.content or "",
                     "tool_calls": [{"id": c.id, "type": "function",
                                     "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)}}
                                    for c in calls],
                     "_functions_state_id": resp.extra.get("functions_state_id")})
        for c in calls:
            emit("tool_start", {"id": c.id, "name": c.name, "args": c.arguments})
            t1 = time.time()
            tool = TOOL_MAP.get(c.name)
            ok = True
            key = json.dumps(c.arguments, sort_keys=True, ensure_ascii=False)
            retry_of = None
            execution = None
            try:
                if tool is None:
                    raise ValueError(f"Нет инструмента {c.name}")
                if not isinstance(c.arguments, dict) or "_raw" in c.arguments:
                    raise ValueError("Аргументы должны быть JSON-объектом")
                args = {k: v for k, v in c.arguments.items() if k != "retry_of"}
                key = json.dumps(args, sort_keys=True, ensure_ascii=False)
                retry_of = c.arguments.get("retry_of")
                if retry_of and (retry_of not in failures or failures[retry_of]["tool"] != c.name):
                    raise ValueError("retry_of должен ссылаться на ошибочный вызов этого же инструмента")
                from ..logbus import wait_lock
                with wait_lock(session.lock, emit, session.id):
                    engine = getattr(session, "engine", None)
                    seq = getattr(engine, "n_scen", 0)
                    try:
                        result = tool["fn"](session, args, emit)
                    finally:
                        receipts = [r for r in getattr(engine, "transactions", []) if r["sequence"] > seq]
                        if receipts:
                            execution = {"scenarios": getattr(engine, "n_scen", 0) - seq,
                                         "recent_transactions": receipts[-5:]}
                    if isinstance(result, dict) and (result.get("error") or result.get("ok") is False):
                        ok = False
                    if execution and getattr(engine, "_tainted", False):
                        raise RuntimeError("Excel не восстановлен; результат инструмента нельзя использовать")
            except Exception as e:
                from ..logbus import Cancelled
                if isinstance(e, Cancelled):
                    raise
                ok = False
                result = {"error": f"{type(e).__name__}: {e}"}
                if not isinstance(e, (ValueError, KeyError)):
                    result["trace"] = traceback.format_exc(limit=2)[-600:]
                if getattr(getattr(session, "engine", None), "_tainted", False):
                    from ..workbook import manifest as mf
                    session.refresh_manifest()
                    emit("manifest", mf.summary(session.manifest))
            if ok:
                failures = {fid: f for fid, f in failures.items() if not (
                    f["tool"] == c.name and (f["args"] == key or fid == retry_of))}
            else:
                failures[c.id] = {"tool": c.name, "args": key,
                                  "error": str(result.get("error") or "Инструмент не подтвердил выполнение")}
                result = dict(result, failed_call_id=c.id,
                              recovery="Повтори тот же вызов или передай retry_of с этим id при исправлении аргументов")
            evidence = {"id": c.id, "tool": c.name, "ok": ok}
            if execution:
                evidence["execution"] = execution
                if isinstance(result, dict):
                    result = dict(result, execution=execution)
            serialized = json.dumps(result, ensure_ascii=False, default=str)
            model_result = result
            if ok and not execution and tool.get("compact_repeated_read"):
                observation = (c.name, key)
                fingerprint = hashlib.sha256(serialized.encode("utf-8")).digest()
                previous = observations.get(observation)
                if previous and previous[0] == fingerprint:
                    evidence["same_as_call"] = previous[1]
                    model_result = {
                        "unchanged": True, "same_as_call": previous[1],
                        "next_action": "Результат этого чтения уже получен. Перейди к следующему шагу или измени запрос, "
                                       "если нужны другие данные.",
                    }
                else:
                    observations[observation] = (fingerprint, c.id)
            session.evidence.append(evidence)
            ui_result = result if len(serialized) <= 20000 else _short(result, 20000)
            emit("tool_end", {"id": c.id, "name": c.name, "ok": ok, "seconds": round(time.time() - t1, 1),
                              "result": ui_result})
            turn.append({"role": "tool", "tool_call_id": c.id, "name": c.name,
                         "content": _short(model_result, client.max_tool_chars)})

    emit("answer", {"text": "Достигнут лимит шагов агента. Задача не завершена; оставшиеся шаги не отмечены выполненными.",
                    "complete": False})
