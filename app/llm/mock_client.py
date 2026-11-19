"""Офлайн-«модель» без сети: детерминированный сценарий вызова тех же инструментов.

Нужна, чтобы проверить harness, движок и интерфейс без ключей к LLM. Понимает простые
запросы вида «стресс: Urals −10% и −20%, что с ND/EBITDA и DSCR». Это не LLM: сложные
вопросы она не разбирает.
"""
from __future__ import annotations

import json
import re
import uuid

from .base import LLMClient, LLMResponse, ToolCall

DRIVER_WORDS = [("urals", "Urals"), ("brent", "Brent"), ("нефт", "Urals"), ("курс", "курс USD/RUB"),
                ("ставк", "ставка"), ("кс", "КС"), ("киум", "КИУМ"), ("тариф", "тариф"),
                ("добыч", "добыча"), ("выручк", "выручка"), ("цен", "цена"), ("инфляц", "инфляция")]
METRIC_WORDS = [("dscr", "DSCR"), ("iscr", "ISCR"), ("icr", "ISCR"), ("net debt", "Net Debt / EBITDA"),
                ("nd/ebitda", "Net Debt / EBITDA"), ("долг", "Net Debt / EBITDA"), ("ebitda", "EBITDA"),
                ("выручк", "Выручка"), ("прибыл", "Чистая прибыль")]


def _call(name, args):
    return LLMResponse(tool_calls=[ToolCall(id="mk_" + uuid.uuid4().hex[:8], name=name, arguments=args)])


class MockClient(LLMClient):
    name = "mock"
    model = "offline-scripted"

    def describe(self):
        return "Офлайн (без LLM, сценарий)"

    def chat(self, messages, tools, tool_choice="auto"):
        # текущий ход = сообщения после последнего user
        last_user = max(i for i, m in enumerate(messages) if m["role"] == "user")
        q = messages[last_user]["content"]
        ql = q.lower()
        turn = messages[last_user + 1:]
        results = [(m.get("name"), json.loads(m["content"])) for m in turn if m["role"] == "tool"]
        done = [n for n, _ in results]

        driver_q = next((v for k, v in DRIVER_WORDS if k in ql), "цена")
        metric_qs = [v for k, v in METRIC_WORDS if k in ql] or ["Net Debt / EBITDA", "EBITDA"]
        metric_qs = list(dict.fromkeys(metric_qs))[:2]
        shocks = [float(x.replace(",", ".").replace("−", "-")) for x in re.findall(r"([−\-+]?\d+(?:[.,]\d+)?)\s*%", q)]
        shocks = shocks or [-10.0, -20.0]
        shocks = [s if s <= 0 or "+" in q or "рост" in ql else -abs(s) for s in shocks][:4]

        has_plan = any(t["name"] == "create_plan" for t in tools)
        if isinstance(tool_choice, dict) or (has_plan and "create_plan" not in done):
            return _call("create_plan", {"goal": f"Стресс «{driver_q}» и влияние на {', '.join(metric_qs)}",
                                         "steps": [f"Найти строку-драйвер «{driver_q}»",
                                                   f"Найти метрики: {', '.join(metric_qs)}",
                                                   "Проверить, что драйвер влияет на метрики",
                                                   "Прогнать стресс-сценарии", "Сформулировать вывод"]})
        if "search_rows" not in done:
            return _call("search_rows", {"query": driver_q, "limit": 8})
        searches = [r for n, r in results if n == "search_rows"]
        if len(searches) < 1 + len(metric_qs):
            return _call("search_rows", {"query": metric_qs[len(searches) - 1], "limit": 5})

        def pick(rows, need_deps=False, kinds=None):
            # без опоры на названия листов: драйвер — допущение, от которого что-то зависит; метрика — первая найденная
            rows = [r for r in rows if (r.get("values") or r.get("cells")) and not r.get("inactive_set")
                    and (not need_deps or r.get("dependents", 0) > 0)]
            for r in rows:
                if not kinds or r.get("kind") in kinds:
                    return r
            return rows[0] if rows else None

        drv = pick(searches[0]["rows"], need_deps=True, kinds=("input", "mixed"))
        mets = [pick([r for r in s["rows"] if r.get("values")]) for s in searches[1:]]
        mets = [m for m in mets if m]
        if not drv or not mets:
            return LLMResponse(content="Не нашёл подходящих строк для драйвера или метрик. "
                                       "Уточните названия показателей так, как они подписаны в модели.")
        if "trace_row" not in done:
            return _call("trace_row", {"sheet": drv["sheet"], "row": drv["row"], "direction": "dependents", "depth": 2})
        if "run_stress" not in done:
            return _call("run_stress", {
                "scenarios": [{"name": f"{driver_q} {s:+g}%", "shocks": [
                    {"sheet": drv["sheet"], "row": drv["row"], "op": "pct", "value": s}]} for s in shocks],
                "metrics": [{"sheet": m["sheet"], "row": m["row"], "name": m["label"][:40]} for m in mets]})
        if has_plan and done.count("update_plan") < 1:
            return _call("update_plan", {"step": 4, "status": "done", "note": "Сценарии рассчитаны"})

        stress = next(r for n, r in results if n == "run_stress")
        if "error" in stress:
            return LLMResponse(content=f"Стресс не выполнен: {stress['error']}")
        lines = [f"**Драйвер:** {drv['sheet']}!{drv['row']} «{drv['label']}». Шоки: "
                 + ", ".join(f"{s:+g}%" for s in shocks) + " к прогнозным годам.", ""]
        for m in stress["metrics"]:
            years = list(m["table"]["База"].keys())
            lines.append(f"**{m['metric']}**")
            lines.append("| Сценарий | " + " | ".join(years) + " |")
            lines.append("|---|" + "---|" * len(years))
            for name, row in m["table"].items():
                lines.append(f"| {name} | " + " | ".join(str(row.get(y, "")) for y in years) + " |")
            lines.append("")
        if stress.get("notes"):
            lines.append("_Оговорки: " + "; ".join(stress["notes"]) + "._")
        lines.append("_Ответ собран офлайн-сценарием без LLM: выводы и интерпретацию даст подключённая модель._")
        return LLMResponse(content="\n".join(lines))
