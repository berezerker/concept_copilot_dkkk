"""Проверить пользовательскую привязку через API и инструменты агента."""
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.agent.session import Session
from app.agent.tools import t_find_parameters, t_get_model_manifest, t_resolve_target
from app.agent.tools_probe import inspect_cell_stress
from app.server import SESSIONS, app
from app.workbook.manifest import parameter_id
from tests.foreign_model import make_foreign


def test_parameter_bindings():
    with TemporaryDirectory() as workdir, TestClient(app) as client:
        path = Path(workdir)
        model = Path(make_foreign(path / "foreign.xlsx"))
        other_model = path / "different.xlsx"
        changed = load_workbook(model)
        changed.create_sheet("Проверка версии")
        changed.save(other_model)
        changed.close()
        sessions = []

        def load(source, filename):
            session = Session(workdir)
            sessions.append(session)
            session.load(str(source), filename, str(path / "cache"), "none")
            SESSIONS[session.id] = session
            return session

        def resolve(session, query):
            return t_resolve_target(session, {"query": query, "kind": "parameter"}, lambda *_: None)

        phrase = "Ставка налога на прибыль"
        try:
            original = load(model, model.name)
            old = resolve(original, phrase)["selected"]
            target = resolve(original, "Процентная ставка по кредиту")["selected"]
            assert target["id"] != old["id"]
            route = f"/api/manifest/{original.id}/parameter-bindings"
            data = {"query": phrase, "parameter_id": target["id"], "file_hash": original.idx.file_hash}
            assert client.post(route, json={**data, "file_hash": "different"}).status_code == 409
            assert client.post(route, json={**data, "query": old["id"]}).status_code == 400
            assert client.post(route, json={**data, "parameter_id": "missing"}).status_code == 400
            assert client.post(route, json=data).status_code == 200
            assert client.post(route, json={"query": phrase, "file_hash": original.idx.file_hash}).status_code == 422
            restored = load(model, "identical-with-a-new-name.xlsx")
            assert resolve(restored, "   СТАВКА  НАЛОГА НА ПРИБЫЛЬ  ")["selected"]["id"] == target["id"]
            assert resolve(restored, old["id"])["selected"]["id"] == old["id"]
            search = t_find_parameters(restored, {"query": phrase}, lambda *_: None)
            assert search["found"] == 1 and search["parameters"][0]["ref"] == target["location"]["ref"]
            assert search["parameters"][0]["user_override"]["parameter_id"] == target["id"]
            filtered = t_get_model_manifest(restored, {"section": "parameters", "query": phrase}, lambda *_: None)
            assert [p["id"] for p in filtered["parameters"]] == [target["id"]]
            trace = inspect_cell_stress(restored, {"target": "'Расчёт'!D9", "query": phrase}, lambda *_: None)
            assert [item["ref"] for item in trace["trace"]["candidates"]] == ["'Параметры'!B4"]
            assert trace["user_override"]["parameter_id"] == target["id"]
            manifest = client.get(f"/api/manifest/{restored.id}").json()
            assert manifest["user_overrides"]["parameters"][0]["parameter_id"] == target["id"]
            different = load(other_model, model.name)
            assert resolve(different, phrase)["selected"]["id"] == old["id"]
            assert client.post(route, json={**data, "parameter_id": None}).status_code == 200
            assert resolve(restored, phrase)["selected"]["id"] == old["id"]

            # Сценарий и новое исправление могут прийти из разных сессий той же книги.
            binding = {**data, "query": "Допущение для проверки", "parameter_id": parameter_id("База", 2)}
            assert client.post(route, json=binding).status_code == 200
            restored.engine = Mock()
            restored.engine.name = "excel"
            restored.engine._tainted = False
            restored.engine.read.return_value = [[[123.0]], [[456.0]]]
            restored.engine_open = True
            scenario_url = f"/api/excel-scenarios/{restored.id}"
            request = {"shocks": [{"parameter": binding["query"], "source_period": "2026", "pct": -10}],
                       "metrics": [{"target": "'Расчёт'!D7"}]}
            plan = client.post(scenario_url + "/prepare", json=request)
            assert plan.status_code == 200, plan.json()
            assert plan.json()["inputs"][0]["source"] == "'База'!E2"
            assert client.post(route, json={**binding, "parameter_id": parameter_id("База", 3)}).status_code == 200
            result = client.post(scenario_url + "/execute", json={"plan_id": plan.json()["plan_id"]})
            assert result.status_code == 400 and "привязка изменились" in result.json()["detail"]
            restored.engine.scenario.assert_not_called()
        finally:
            for session in sessions:
                SESSIONS.pop(session.id, None)
                session.close()


if __name__ == "__main__":
    test_parameter_bindings()
    print("parameter bindings ok")
