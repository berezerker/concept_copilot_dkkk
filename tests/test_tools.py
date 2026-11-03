"""Инструменты на синтетической модели: поиск, трассировка, стресс, подбор, восстановление модели."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.agent.tools import TOOL_MAP
from tests.sample_model import make_sample_session

E = lambda k, d: None


def call(s, name, args):
    return TOOL_MAP[name]["fn"](s, args, E)


def test_all():
    s = make_sample_session()
    r = call(s, "search_rows", {"query": "DSCR"})
    assert r["rows"][0]["sheet"] == "Calc" and r["rows"][0]["row"] == 8
    r = call(s, "search_rows", {"query": "цена", "only_inputs": True})
    assert (r["rows"][0]["sheet"], r["rows"][0]["row"]) == ("Inputs", 3)
    r = call(s, "trace_row", {"sheet": "Inputs", "row": 3, "direction": "dependents", "depth": 3})
    assert any(x["ref"] == "Calc!8" for lv in r["levels"] for x in lv["rows"])
    base = s.engine.read(["Calc!D8:I8"])
    r = call(s, "run_stress", {"scenarios": [{"name": "-20%", "shocks": [{"sheet": "Inputs", "row": 3, "op": "pct", "value": -20}]}],
                               "metrics": [{"sheet": "Calc", "row": 8}]})
    t = r["metrics"][0]["table"]
    assert t["-20%"]["2026"] < t["База"]["2026"]
    assert s.engine.read(["Calc!D8:I8"]) == base, "модель должна восстановиться"
    r = call(s, "goal_seek", {"shock": {"sheet": "Inputs", "row": 3, "op": "pct"}, "metric": {"sheet": "Calc", "row": 8},
                              "aggregate": "min", "target": 1.0, "lower": -60, "upper": 0})
    assert r["converged"] and -60 < r["value"] < 0 and abs(r["metric_value"] - 1.0) < 0.02
    s.close()


if __name__ == "__main__":
    test_all(); print("tools ok")
