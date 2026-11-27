"""Свободный план и ошибки: harness не завершает шаги за модель."""
import json
import os
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.agent.harness import run_turn
from app.agent.kpki import run_kpki
from app.agent.tools import t_create_plan, t_update_plan
from app.agent.tools_credit import t_stress_pack
from app.llm.base import LLMClient, LLMResponse, ToolCall
from app.workbook.transaction import RestorationError


def call(name, **args):
    return LLMResponse(tool_calls=[ToolCall(name, name, args)])


class Script(LLMClient):
    def __init__(self, responses):
        self.responses = iter(responses)

    def chat(self, *args):
        return next(self.responses, LLMResponse(content="Всё готово"))


class Harness(unittest.TestCase):
    def run_script(self, responses, work=lambda *args: {"ok": True}, max_steps=10):
        s = SimpleNamespace(plan=None, history=[], id="harness-test", lock=threading.Lock())
        events = []
        tools = {"create_plan": {"fn": t_create_plan}, "update_plan": {"fn": t_update_plan}, "work": {"fn": work}}
        with patch("app.agent.harness.system_prompt", return_value="test"), patch("app.agent.harness.TOOL_MAP", tools):
            run_turn(s, Script(responses), "Сделай расчёт", lambda k, d: events.append((k, d)), max_steps=max_steps)
        return s, events[-1][1]

    def test_unfinished_plan_never_auto_done(self):
        s, answer = self.run_script([call("create_plan", steps=["Расчёт"])])
        self.assertEqual(s.plan["steps"][0]["status"], "pending")
        self.assertFalse(answer["complete"])

    def test_plan_needs_successful_work_in_current_plan(self):
        s, answer = self.run_script([call("create_plan", steps=["Старый расчёт"]), call("work"),
                                    call("create_plan", steps=["Новый расчёт"]),
                                    call("update_plan", step=1, status="done")])
        self.assertEqual(s.plan["steps"][0]["status"], "pending")
        self.assertFalse(answer["complete"])
        self.assertIn("успешный результат инструмента", answer["text"])

    def test_skipping_every_step_keeps_turn_incomplete(self):
        _, answer = self.run_script([call("create_plan", steps=["Расчёт"]),
                                    call("update_plan", step=1, status="skipped", note="Нет входа")])
        self.assertFalse(answer["complete"])

    def test_identical_reads_stay_compact_and_changed_read_is_preserved(self):
        state = SimpleNamespace(plan=None, history=[], id="repeated-reads", lock=threading.Lock())
        args = {"sheet": "Модель", "rows": [42], "years": ["2026", "2027"]}
        ids = ["read-one", "repeat-one", "read-changed", "repeat-changed"]
        client = Script([LLMResponse(tool_calls=[ToolCall(id_, "read_rows", args)]) for id_ in ids])
        readings = iter([42, 42, 43, 43])
        tools = {"read_rows": {"fn": lambda *_: {"value": next(readings)}, "compact_repeated_read": True}}
        visible = []
        with patch("app.agent.harness.system_prompt", return_value="test"), \
             patch("app.agent.harness.TOOL_MAP", tools), patch.object(client, "chat", wraps=client.chat) as requests:
            run_turn(state, client, "Прочитай строку", lambda k, d: visible.append((k, d)), plan_mode=False)
        transcript = requests.call_args_list[-1].args[0]
        model_results = [json.loads(m["content"]) for m in transcript if m["role"] == "tool"]
        self.assertEqual(model_results[0], {"value": 42})
        self.assertEqual(model_results[1]["same_as_call"], ids[0])
        self.assertEqual(model_results[2], {"value": 43})
        self.assertEqual(model_results[3]["same_as_call"], ids[2])
        self.assertEqual([d["result"]["value"] for kind, d in visible if kind == "tool_end"], [42, 42, 43, 43])
        self.assertEqual([e.get("same_as_call") for e in state.evidence], [None, ids[0], None, ids[2]])

    def test_failed_tool_cannot_be_hidden_by_final_or_plan(self):
        _, answer = self.run_script([call("create_plan", steps=["Расчёт"]), call("work"),
                                    call("update_plan", step=1, status="done")],
                                   work=lambda *args: {"error": "Excel не восстановлен"})
        self.assertFalse(answer["complete"])
        self.assertIn("Excel не восстановлен", answer["text"])
        self.assertNotIn("Всё готово", answer["text"])

    def test_recovery_and_explicit_completion(self):
        attempts = 0
        def work(*args):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ValueError("уточните диапазон")
            return {"value": 42}
        s, answer = self.run_script([call("create_plan", steps=["Расчёт"]), call("work"),
                                    LLMResponse(content="Готово"), call("work"),
                                    call("update_plan", step=1, status="done", note="42")], work)
        self.assertTrue(answer["complete"])
        self.assertEqual([e["ok"] for e in s.evidence if e["tool"] == "work"], [False, True])
        self.assertEqual(s.plan["steps"][0]["evidence"], {"id": "work", "tool": "work"})

    def test_limit_keeps_steps_unfinished(self):
        s, answer = self.run_script([call("create_plan", steps=["Расчёт"])], max_steps=1)
        self.assertFalse(answer["complete"])
        self.assertEqual(s.plan["steps"][0]["status"], "pending")

    def test_unrelated_success_does_not_erase_failure(self):
        def work(s, args, emit):
            return {"error": "первый сценарий не посчитан"} if args["scenario"] == "bad" else {"value": 42}
        _, answer = self.run_script([call("create_plan", steps=["Расчёт"]),
                                    call("work", scenario="bad"), call("work", scenario="other"),
                                    call("update_plan", step=1, status="done")], work)
        self.assertFalse(answer["complete"])

    def test_explicit_corrected_retry_clears_only_its_failure(self):
        def work(s, args, emit):
            self.assertNotIn("retry_of", args)
            return {"error": "проверь диапазон"} if args["scenario"] == "bad" else {"value": 42}
        _, answer = self.run_script([call("create_plan", steps=["Расчёт"]),
                                    call("work", scenario="bad"), call("work", scenario="fixed", retry_of="work"),
                                    call("update_plan", step=1, status="done")], work)
        self.assertTrue(answer["complete"])

    def test_non_object_tool_arguments_are_reported(self):
        _, answer = self.run_script([call("create_plan", steps=["Расчёт"]),
                                    LLMResponse(tool_calls=[ToolCall("bad", "work", [])])])
        self.assertFalse(answer["complete"])
        self.assertIn("JSON-объектом", answer["text"])

    def test_tainted_engine_still_allows_plan_to_record_failure(self):
        engine = SimpleNamespace(_tainted=False, n_scen=0, transactions=[])
        def work(s, args, emit):
            engine.n_scen += 1
            engine.transactions.append({"sequence": engine.n_scen, "status": "restore_failed", "restored": False})
            engine._tainted = True
            return {"value": 42}
        s = SimpleNamespace(plan=None, history=[], id="harness-test", lock=threading.Lock(), engine=engine,
                            manifest={}, refresh_manifest=lambda: None)
        events = []
        tools = {"create_plan": {"fn": t_create_plan}, "update_plan": {"fn": t_update_plan}, "work": {"fn": work}}
        responses = [call("create_plan", steps=["Расчёт"]), call("work"),
                     call("update_plan", step=1, status="skipped", note="Excel требует перезагрузки")]
        with patch("app.agent.harness.system_prompt", return_value="test"), \
             patch("app.agent.harness.TOOL_MAP", tools), patch("app.workbook.manifest.summary", return_value={}):
            run_turn(s, Script(responses), "Сделай расчёт", lambda k, d: events.append((k, d)))
        answer = events[-1][1]
        self.assertFalse(answer["complete"])
        self.assertIn("Excel не восстановлен", answer["text"])
        self.assertNotIn("update_plan", answer["text"])
        failed = next(row for row in s.evidence if not row["ok"])
        self.assertEqual(failed["execution"]["recent_transactions"][0]["status"], "restore_failed")

    def test_kpki_stops_at_original_restore_error(self):
        error = RestorationError("Inputs!C3: восстановление не подтверждено")
        s = SimpleNamespace(card={"file": "sample.xlsx"}, require_engine=lambda: None)
        events = []
        tools = {
            "audit_model": {"fn": lambda *args: {"total": 0}},
            "covenant_check": {"fn": lambda *args: {"rules": []}},
            "dependency_analysis": {"fn": lambda *args: {
                "tornados": [{"factors": [{"name": "Цены реализации продукции", "market": True, "delta": -1}]}]
            }},
            "stress_pack": {"fn": t_stress_pack},
        }
        rules = [{"sheet": "Calc", "row": 8, "threshold": 1.3}]
        with patch("app.agent.kpki.TOOL_MAP", tools), \
             patch("app.agent.tools_credit._metric_refs", return_value=[]), \
             patch("app.agent.tools_credit._base_series", return_value=[]), \
             patch("app.agent.tools_credit._run", side_effect=error) as recalc:
            with self.assertRaises(RestorationError) as caught:
                run_kpki(s, None, {"rules": rules}, lambda event, data: events.append((event, data)))
        self.assertIs(caught.exception, error)
        recalc.assert_called_once()
        last = [data for event, data in events if event == "tool_end"][-1]
        self.assertEqual(last["name"], "stress_pack")
        self.assertFalse(last["ok"])
        self.assertIn(str(error), last["result"]["error"])
        self.assertFalse(any(event == "answer" for event, _ in events))


if __name__ == "__main__":
    unittest.main()
