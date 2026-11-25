"""Локальный Excel-тест связи конкретной ячейки с показателем, без API."""
import argparse
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from app import config
from app.agent.registry import TOOL_MAP
from app.agent.session import Session
from app.server import SESSIONS, app
from app.workbook.transaction import same_values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--metric", required=True)
    parser.add_argument("--period", required=True)
    parser.add_argument("--driver-query", required=True)
    parser.add_argument("--driver-period")
    args = parser.parse_args()
    with args.model.open("rb") as workbook:
        original = hashlib.file_digest(workbook, "sha256").hexdigest()
    session = Session(config.workdir())
    client = TestClient(app)
    SESSIONS[session.id] = session

    def call(name, payload):
        return TOOL_MAP[name]["fn"](session, payload, lambda *_: None)

    def post(path, payload):
        response = client.post(path, json=payload)
        assert response.status_code == 200, response.json()
        return response.json()

    try:
        session.load(str(args.model), args.model.name, config.cache_dir(), "excel", progress=print)
        with TemporaryDirectory() as cache:
            session.cache_dir = cache  # не меняем пользовательские привязки
            metric = next(m for m in session.manifest["metrics"] if m["catalog_id"] == args.metric)
            binding = metric["bindings"][0]
            post(f"/api/manifest/{session.id}/binding",
                 {"concept": args.metric, "sheet": binding["sheet"], "row": binding["row"],
                  "file_hash": session.idx.file_hash})
            assert any(m["catalog_id"] == args.metric and m["binding_confirmed"] for m in session.manifest["metrics"])
            target = call("inspect_cell_stress", {"metric": args.metric, "period": args.period,
                                                  "query": args.driver_query, "source_period": args.driver_period})
        print(json.dumps({"target": target["target"], "sheets": len(session.idx.sheets),
                          "formulas": session.idx.n_formulas, "candidates": len(target["trace"]["candidates"]),
                          "limitations": target["trace"]["limitations"]}, ensure_ascii=False), flush=True)
        api_path = f"/api/cell-stress/{session.id}"
        for candidate in target["trace"]["candidates"][:4]:
            payload = {"target": target["target"], "source": candidate["ref"], "pct": -10}
            try:
                plan = call("inspect_cell_stress", payload)
            except ValueError as exc:
                print(f"Пропуск {candidate['ref']}: {exc}", flush=True)
                continue
            refs = [plan["source"], plan["target"]]
            base = session.engine.read(refs)
            out = post(api_path, {**payload, "run": True, "file_hash": plan["file_hash"],
                                  "expected_input": plan["source_before"]})
            assert same_values(session.engine.read(refs), base), "База не восстановилась"
            assert out["rollback_verified"] and abs(out["achieved_pct"] + 10) < 1e-8
            assert out["trace"]["status"] == "found"
            print(json.dumps(out, ensure_ascii=False), flush=True)
            if abs(out["target_delta"]) > 1e-7:
                break
        else:
            raise AssertionError("Выбранные кандидаты не дали измеримого эффекта")
    finally:
        session.close()
        SESSIONS.pop(session.id, None)
        client.close()
        with args.model.open("rb") as workbook:
            assert hashlib.file_digest(workbook, "sha256").hexdigest() == original, "Исходная книга изменилась"


if __name__ == "__main__":
    main()
