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
            if session.plan:
                for st in session.plan["steps"]:
                    if st["status"] in ("pending", "in_progress"):
                        st["status"] = "done"
                emit("plan", session.plan)
            emit("answer", {"text": answer, "seconds": round(time.time() - t0, 1)})
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
            try:
                if tool is None:
                    raise ValueError(f"Нет инструмента {c.name}")
                if "_raw" in c.arguments:
                    raise ValueError("Аргументы не в формате JSON")
                from ..logbus import wait_lock
                with wait_lock(session.lock, emit, session.id):
                    result = tool["fn"](session, c.arguments, emit)
            except Exception as e:
                from ..logbus import Cancelled
                if isinstance(e, Cancelled):
                    raise
                ok = False
                result = {"error": f"{type(e).__name__}: {e}"}
                if not isinstance(e, (ValueError, KeyError)):
                    result["trace"] = traceback.format_exc(limit=2)[-600:]
            ui_result = result if len(json.dumps(result, ensure_ascii=False, default=str)) <= 20000 else _short(result, 20000)
            emit("tool_end", {"id": c.id, "name": c.name, "ok": ok, "seconds": round(time.time() - t1, 1),
                              "result": ui_result})
            turn.append({"role": "tool", "tool_call_id": c.id, "name": c.name,
                         "content": _short(result, client.max_tool_chars)})

    emit("answer", {"text": "Достигнут лимит шагов агента. Сузьте задачу или разбейте её на части."})
