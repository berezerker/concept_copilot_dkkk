"""Model Manifest: единый паспорт и детерминированное разрешение целей агента."""
import os
import sys
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent.prompts import system_prompt
from app.agent.registry import TOOL_MAP
from app.agent.tools_probe import _target, resolve_input_selector
from app.workbook.cell_trace import ref_for
from app.workbook import manifest as mf
from app.workbook import params as pr
from tests.foreign_model import make_foreign_session


E = lambda k, d: None


def call(s, name, args):
    return TOOL_MAP[name]["fn"](s, args, E)


def test_parameter_search_can_keep_partial_alternatives():
    common = {"kind": "input", "sheet": "Допущения", "factor": "rate"}
    s = SimpleNamespace(sem={"rows": {
        "Допущения!2": {**common, "row": 2, "label": "Эталонная средняя ставка"},
        "Допущения!3": {**common, "row": 3, "label": "Средняя ставка"},
    }})
    query = "эталонная средняя ставка"
    exact, _ = pr.search(s, query)
    candidates, _ = pr.search(s, query, include_partial=True)
    assert [p["row"] for p in exact] == [2]
    assert [p["row"] for p in candidates] == [2, 3]


def check_scenario_parameter(s):
    args = {"parameter": mf.parameter_id("База", 2), "source_period": "2026", "pct": -10}
    _, detail = resolve_input_selector(s, args)
    assert detail["source"] == "'База'!E2" and detail["parameter_id"] == args["parameter"]
    _, by_row = resolve_input_selector(s, {"source": "'База'!2", "source_period": "2026", "pct": -10})
    assert by_row["source"] == detail["source"]
    check = TestCase()
    with check.assertRaisesRegex(ValueError, "точный период входа"):
        resolve_input_selector(s, {**args, "source_period": None})
    with check.assertRaisesRegex(ValueError, "адрес входа или параметр"):
        resolve_input_selector(s, {**args, "source": detail["source"]})
    with patch.object(s.idx.sheet("База"), "years_for", return_value={4: 2026, 5: 2026}):
        with check.assertRaisesRegex(ValueError, "нет единственной ячейки"):
            resolve_input_selector(s, args)
    with patch.dict(s.idx.row("База", 2).formulas, {5: "=D2*1.03"}):
        with check.assertRaisesRegex(ValueError, "нет единственной ячейки"):
            resolve_input_selector(s, args)
    output = {"metric": "metric:ebitda", "period": "2026"}
    preview = call(s, "inspect_cell_stress", {**args, **output})
    by_row_preview = call(s, "inspect_cell_stress", {
        **output, "source": "'База'!2", "source_period": "2026", "pct": -10,
    })
    assert preview["source"] == by_row_preview["source"] == detail["source"]
    assert preview["trace"] == by_row_preview["trace"]
    assert preview["source_before"] == by_row_preview["source_before"]
    with check.assertRaisesRegex(ValueError, "точный адрес A1 входа из плана"):
        call(s, "run_cell_stress", {**args, **output, "file_hash": preview["file_hash"],
                                   "expected_input": preview["source_before"]})
    node, entry = _target(s, output)
    ref = ref_for(node)
    assert _target(s, {**output, "target": ref}) == (node, entry)
    assert _target(s, {**output, "target": f"'{node[0]}'!{node[1]}"}) == (node, entry)
    assert _target(s, {"concept": "ebitda", "period": "2026"}) == (node, entry)
    with check.assertRaisesRegex(ValueError, "не соответствует периоду"):
        _target(s, {**output, "target": ref, "period": "2027"})
    with check.assertRaisesRegex(ValueError, "не соответствует привязке"):
        _target(s, {**output, "target": ref_for((node[0], node[1] + 1, node[2]))})


def test_manifest():
    s = make_foreign_session()
    try:
        m = s.manifest
        assert m and m["schema_version"] == 1
        assert m["model"]["file"] == "foreign.xlsx"
        assert len(m["metrics"]) >= 5 and len(m["parameters"]) >= 8
        assert m["readiness"]["capabilities"]["analyze"]
        assert m["readiness"]["coverage"]["parameters_with_known_impact"] >= 8
        assert m["scenario_sets"]
        assert m["scenario_sets"][0]["verified"] == bool(s.catalog["alt_sets"][0].get("detected"))

        ebitda = next(x for x in m["metrics"] if x["catalog_id"] == "ebitda")
        assert ebitda["id"] == "metric:ebitda"
        assert ebitda["location"]["reported"].startswith("Свод!")
        assert ebitda["location"]["computed"].startswith("Расчёт!")
        assert ebitda["evidence"] and ebitda["confidence"]["identity"] >= 0.6

        ids = {x["id"] for x in m["parameters"]}
        assert len(ids) == len(m["parameters"])
        assert all(x.startswith("parameter:") for x in ids)

        summary = call(s, "get_model_manifest", {"section": "summary"})
        assert summary["counts"]["metrics"] == len(m["metrics"])
        rows = call(s, "get_model_manifest", {"section": "parameters", "query": "ставка", "limit": 10})
        assert rows["found"] >= 2

        resolved = call(s, "resolve_target", {"query": "EBITDA", "kind": "metric", "intent": "read"})
        assert resolved["status"] == "resolved" and resolved["selected"]["id"] == "metric:ebitda"
        param = call(s, "resolve_target", {"query": "ставка налога на прибыль", "kind": "parameter", "intent": "shock"})
        assert param["status"] == "resolved" and param["selected"]["location"]["ref"] == "Параметры!3"
        by_id = call(s, "resolve_target", {"query": param["selected"]["id"], "kind": "parameter"})
        assert by_id["selected"]["id"] == param["selected"]["id"]
        # Ограничение выдачи не должно скрывать неоднозначность одинаковых подписей.
        original = s.manifest
        import copy
        s.manifest = copy.deepcopy(original)
        s.manifest["metrics"][0]["name"] = "Совпадающий показатель"
        duplicate = copy.deepcopy(s.manifest["metrics"][0])
        duplicate.update(id="metric:duplicate", catalog_id="duplicate")
        s.manifest["metrics"].append(duplicate)
        ambiguous = mf.resolve(s, duplicate["name"], kind="metric", limit=1)
        assert ambiguous["status"] == "ambiguous" and ambiguous["selected"] is None
        s.manifest = original
        from app.agent.tools import _build_overrides
        overrides, _, _, _ = _build_overrides(s, [{"target": param["selected"]["id"], "op": "pct", "value": 10}])
        assert overrides and "Параметры" in overrides[0]["ref"]
        # В смешанной строке параметра формула не должна попадать в запись шока.
        from app.agent.tools import _shock_cols
        sh, ri, cols = _shock_cols(s, "База", 2, None)
        formula_col = sorted(cols)[1]
        with patch.dict(ri.formulas, {formula_col: "=D2*1.03"}):
            mixed, _, _, _ = _build_overrides(s, [{"sheet": "База", "row": 2, "_inputs_only": True,
                                                  "op": "pct", "value": -10}])
        from openpyxl.utils.cell import range_boundaries
        for item in mixed:
            c1, _, c2, _ = range_boundaries(item["ref"].rsplit("!", 1)[1])
            assert not c1 <= formula_col <= c2, "формула смешанной строки не должна заменяться значением"
        missing = call(s, "resolve_target", {"query": "стоимость космического корабля", "kind": "parameter"})
        assert missing["status"] == "not_found" and not missing["candidates"]

        prompt = system_prompt(s)
        assert "MODEL MANIFEST" in prompt and "resolve_target" in prompt
        check_scenario_parameter(s)
    finally:
        s.close()


if __name__ == "__main__":
    test_parameter_search_can_keep_partial_alternatives()
    test_manifest()
    print("manifest ok: единый паспорт, readiness и resolver")
