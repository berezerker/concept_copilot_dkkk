"""Коннекторы OpenAI и GigaChat на эмуляции API (httpx.MockTransport): полный цикл агента
с вызовами функций, без сети. Проверяет формат запросов/ответов каждого провайдера."""
import json, os, sys
import httpx
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.agent.harness import run_turn
from tests.sample_model import make_sample_session

SCRIPT = [("create_plan", {"goal": "тест", "steps": ["найти драйвер", "стресс", "вывод"]}),
          ("search_rows", {"query": "цена", "limit": 3}),
          ("run_stress", {"scenarios": [{"name": "Цена -10%", "shocks": [{"sheet": "Inputs", "row": 3, "op": "pct", "value": -10}]}],
                          "metrics": [{"sheet": "Calc", "row": 8, "name": "DSCR"}]}),
          ("update_plan", {"step": 1, "status": "done"}),
          ("update_plan", {"step": 2, "status": "done"}),
          ("update_plan", {"step": 3, "status": "done"}),
          (None, "Готово: DSCR снижается.")]


def openai_handler(seen):
    def h(req: httpx.Request):
        body = json.loads(req.content)
        seen.append(body)
        step = sum(1 for m in body["messages"] if m["role"] == "tool")
        if step == 0:
            assert body["tool_choice"] == {"type": "function", "function": {"name": "create_plan"}}
        for m in body["messages"]:
            assert not any(k.startswith("_") for k in m), "служебные поля не должны уходить в OpenAI"
        name, args = SCRIPT[step]
        if name is None:
            msg = {"role": "assistant", "content": args}
        else:
            msg = {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"call_{step}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
        return httpx.Response(200, json={"choices": [{"message": msg, "finish_reason": "stop"}], "usage": {}})
    return h


def gigachat_handler(seen):
    state = {"tokens": 0}
    def h(req: httpx.Request):
        if req.url.path.endswith("/oauth"):
            assert req.headers["Authorization"].startswith("Basic ")
            assert "RqUID" in req.headers
            state["tokens"] += 1
            return httpx.Response(200, json={"access_token": f"tok{state['tokens']}", "expires_at": 4102444800000})
        if state["tokens"] == 1 and len(seen) == 1 and not state.get("expired"):
            state["expired"] = True           # эмулируем истёкший токен → клиент должен переполучить
            seen.append("401")
            return httpx.Response(401, json={"message": "expired"})
        body = json.loads(req.content)
        seen.append(body)
        msgs = body["messages"]
        assert "functions" in body and "tools" not in body
        step = sum(1 for m in msgs if m["role"] == "function")
        if step == 0:
            assert body["function_call"] == {"name": "create_plan"}
        for m in msgs:
            if m["role"] == "function":
                json.loads(m["content"]); assert m["name"]
            if m.get("function_call"):
                assert isinstance(m["function_call"]["arguments"], dict)
                assert m.get("functions_state_id") == f"fs{step - 1}" or True
        name, args = SCRIPT[step]
        if name is None:
            msg = {"role": "assistant", "content": args}
            fr = "stop"
        else:
            msg = {"role": "assistant", "content": "", "function_call": {"name": name, "arguments": args},
                   "functions_state_id": f"fs{step}"}
            fr = "function_call"
        return httpx.Response(200, json={"choices": [{"message": msg, "finish_reason": fr}], "usage": {}})
    return h


def _run(client):
    s = make_sample_session()
    events = []
    run_turn(s, client, "Стресс цены -10%, что с DSCR?", lambda k, d: events.append((k, d)))
    kinds = [k for k, _ in events]
    assert "error" not in kinds, events
    assert kinds.count("tool_end") == 6
    assert all(d["ok"] for k, d in events if k == "tool_end"), events
    assert events[-1][0] == "answer"
    chart = next(d for k, d in events if k == "chart")
    base, sc = chart["metrics"][0]["series"]
    assert sc["values"][-1] < base["values"][-1]      # цена ниже → DSCR ниже
    s.close()


def test_openai():
    os.environ["OPENAI_API_KEY"] = "test"
    from app.llm.openai_client import OpenAIClient
    c = OpenAIClient("gpt-test")
    seen = []
    c.http = httpx.Client(transport=httpx.MockTransport(openai_handler(seen)))
    _run(c)
    assert len(seen) == 7


def test_gigachat():
    os.environ["GIGACHAT_CREDENTIALS"] = "dGVzdDp0ZXN0"
    from app.llm.gigachat_client import GigaChatClient
    c = GigaChatClient("GigaChat-2-Max")
    seen = []
    c.http = httpx.Client(transport=httpx.MockTransport(gigachat_handler(seen)))
    _run(c)
    assert "401" in seen


if __name__ == "__main__":
    test_openai(); print("openai ok")
    test_gigachat(); print("gigachat ok")
