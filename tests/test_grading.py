"""Проверки методики оценки E2E: обоснованность чисел, адреса ячеек, судья на фейковом клиенте."""
import os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.e2e import grading as g


def test_grounding():
    tools = [{"ndebt_ebitda": [2.84, 3.1], "dscr": 1.27, "share": 0.43}]
    r = g.grounding("Net Debt/EBITDA растёт до 3,1x, DSCR 1,27; доля цены 43%. В 2027 году 3 риска. Выдумка 7,77", tools, "цены -10%")
    assert r["numbers"] == 4 and r["ungrounded"] == [7.77] and r["score"] == 0.75, r
    assert g.cell_refs("Зависит от Dashboard!15 и Inputs!C7\nГде в модели: Dashboard!15") == 2


def test_judge_and_verdict():
    call = types.SimpleNamespace(name="grade", arguments={k: 4 for k in g.CRITERIA} | {"pass": True, "fix": "—"})
    cl = types.SimpleNamespace(name="openai", chat=lambda m, t, c: types.SimpleNamespace(tool_calls=[call]))
    case = {"id": "x", "query": "q", "expect_tools": [["run_stress"]]}
    run = {"tools": [{"name": "run_stress", "args": {}, "ok": True, "result": {"v": 1.5}}], "answer": "DSCR 1,5", "seconds": 1}
    jd = g.judge(cl, case, run)
    assert jd["score"] == 4 and jd["pass"]
    assert g.verdict(g.deterministic(case, run), jd)[0] == "PASS"
    run["answer"] = "DSCR 9,9 и 12,5"
    assert g.verdict(g.deterministic(case, run), jd)[0] == "FAIL"


def test_verified_scenario_requires_matching_plan_and_actual_shocks():
    expect = {"periods": ["2026"], "shocks_per_period": [-10, 10], "metric": "dscr"}
    plan = {"plan_id": "p", "phase": "preflight",
            "inputs": [{"source": "X!A1", "source_period": "2026", "requested_pct": -10},
                       {"source": "X!B1", "source_period": "2026", "requested_pct": 10}],
            "metrics": [{"target": "Y!A1", "period": "2026", "published_binding": "dscr", "before": 1.5}]}
    result = {"plan_id": "p", "phase": "verified", "rollback_verified": True,
              "combined": {"transaction": {"status": "verified", "restored": True},
                           "inputs": [{"ref": "X!A1", "achieved_pct": -10},
                                      {"ref": "X!B1", "achieved_pct": 10}],
                           "metrics": [{"ref": "Y!A1", "after": 2.0}]}}
    tools = [{"name": "prepare_excel_scenario", "ok": True, "result": plan},
             {"name": "execute_excel_scenario", "ok": True, "result": result}]
    assert g.verified_scenario_check(expect, tools)
    assert not g.verified_scenario_check(expect, tools, answer="План выполнен")
    assert g.verified_scenario_check(expect, tools, answer="DSCR с 1,5 до 2. Ячейки X!A1, X!B1, Y!A1")
    assert not g.verified_scenario_check(expect, list(reversed(tools)))
    result["combined"]["inputs"][0]["achieved_pct"] = 0
    assert not g.verified_scenario_check(expect, tools)
    result["combined"]["inputs"][0]["achieved_pct"] = -10
    result["combined"]["transaction"]["restored"] = False
    assert not g.verified_scenario_check(expect, tools)


if __name__ == "__main__":
    test_grounding(); test_judge_and_verdict(); test_verified_scenario_requires_matching_plan_and_actual_shocks()
    print("grading ok")
