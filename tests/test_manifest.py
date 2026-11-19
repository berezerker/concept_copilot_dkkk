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
        missing = call(s, "resolve_target", {"query": "стоимость космического корабля", "kind": "parameter"})
        assert missing["status"] == "not_found" and not missing["candidates"]

        prompt = system_prompt(s)
        assert "MODEL MANIFEST" in prompt and "resolve_target" in prompt
    finally:
        s.close()


if __name__ == "__main__":
    test_manifest()
    print("manifest ok: единый паспорт, readiness и resolver")
