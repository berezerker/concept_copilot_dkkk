"""Model Manifest: единый паспорт и детерминированное разрешение целей агента."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent.prompts import system_prompt
from app.agent.registry import TOOL_MAP
from app.workbook import manifest as mf
from tests.foreign_model import make_foreign_session


E = lambda k, d: None


def call(s, name, args):
    return TOOL_MAP[name]["fn"](s, args, E)


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
        from unittest.mock import patch
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
    finally:
        s.close()


if __name__ == "__main__":
    test_manifest()
    print("manifest ok: единый паспорт, readiness и resolver")
