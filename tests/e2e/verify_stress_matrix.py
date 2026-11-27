"""Локальная приёмка отдельных и совместных шоков на полной книге в Excel."""
import argparse
import hashlib
import json
import math
from pathlib import Path

from fastapi.testclient import TestClient

from app import config
from app.agent.registry import TOOL_MAP
from app.agent.session import Session
from app.server import SESSIONS, app
from app.workbook.periods import norm_period
from app.workbook.transaction import same_values, split_ref


def _hash(path):
    with path.open("rb") as book:
        return hashlib.file_digest(book, "sha256").hexdigest()


def _log(**entry):
    print(json.dumps(entry, ensure_ascii=False), flush=True)


def _verified_plan(session, payload):
    base_url = f"/api/excel-scenarios/{session.id}"
    client = TestClient(app)
    SESSIONS[session.id] = session
    try:
        def post(suffix, data):
            response = client.post(base_url + "/" + suffix, json=data)
            assert response.status_code == 200, response.json()
            return response.json()

        before_prepare = session.engine.n_scen
        preview = post("prepare", payload)
        assert session.engine.n_scen == before_prepare, "Предпроверка запустила сценарий"
        checked = post("execute", {"plan_id": preview["plan_id"], "compare_individual": True})
        previous_count = session.engine.n_scen
        expired = client.post(base_url + "/execute", json={"plan_id": preview["plan_id"]})
        assert expired.status_code == 400 and session.engine.n_scen == previous_count
        response = client.get(base_url, params={"plan_id": preview["plan_id"]})
        assert response.status_code == 200, response.json()
        recalled = response.json()
        assert recalled["historical"] and not recalled["recalculated"]
        assert recalled["combined"] == checked["combined"]
        assert session.engine.n_scen == previous_count, "Чтение истории вызвало пересчёт"
        return preview, checked
    finally:
        SESSIONS.pop(session.id, None)
        client.close()


def verify(session, metric, periods, drivers):
    """Проверить один показатель за все выбранные периоды при каждом вызове Excel."""
    session.require_engine()
    if session.engine.name != "excel":
        raise ValueError("Для приёмки нужен Microsoft Excel")

    inspect = TOOL_MAP["inspect_cell_stress"]["fn"]

    def plan(**args):
        return inspect(session, args, lambda *_: None)

    targets = {period: plan(metric=metric, period=period)["target"] for period in periods}
    target_refs = list(targets.values())
    if len(set(target_refs)) != len(target_refs):
        raise ValueError("Разным периодам соответствует один и тот же адрес результата")
    engine = session.engine
    metric_baseline = engine.read(target_refs)

    def run(case, plans):
        sources = [item["source"] for item in plans]
        refs = sources + target_refs
        assert len(refs) == len(set(refs)), f"{case}: адреса сценария пересекаются"
        overrides = [
            {"ref": p["source"], "values": [[p["source_proposed"]]], "expected_input": [[p["source_before"]]]}
            for p in plans
        ]
        source_baseline = [item["expected_input"] for item in overrides]
        expected_inputs = [item["values"] for item in overrides]
        baseline = source_baseline + metric_baseline
        assert same_values(engine.read(sources), source_baseline, tol=0), f"{case}: изменилась база входов"
        previous = engine.n_scen
        changed, _ = engine.scenario(overrides, refs, refs)
        receipt = engine.transactions[-1]
        assert receipt["sequence"] == previous + 1 and receipt["status"] == "verified" and receipt["restored"], receipt
        assert same_values(changed[:len(sources)], expected_inputs, tol=0), f"{case}: Excel применил не все шоки"
        restored = engine.read(refs)
        assert same_values(restored[:len(sources)], source_baseline, tol=0), f"{case}: изменились исходные входы"
        assert same_values(restored, baseline), f"{case}: показатели после отката отличаются от общей базы"
        deltas = {
            period: stress[0][0] - base[0][0]
            for period, stress, base in zip(targets, changed[len(sources):], metric_baseline)
        }
        _log(case=case, inputs=[{"ref": p["source"], "period": p["source_period"], "label": p["source_label"],
                                "requested_pct": p["requested_pct"],
                                "achieved_pct": (v[0][0] / p["source_before"] - 1) * 100}
                               for p, v in zip(plans, changed)], metric_deltas=deltas, restored=True)
        return deltas

    selected = {}
    used = set()
    trials = 0
    for period, target in targets.items():
        selected[period] = []
        for query, pct in drivers:
            discovery = plan(metric=metric, period=period, query=query, source_period=period)
            for candidate in discovery["trace"]["candidates"]:
                source = candidate["ref"]
                if source in used:
                    continue
                item = plan(target=target, source=source, source_period=period, pct=pct)
                trials += 1
                deltas = run(f"Одиночный {period}: {query}", [item])
                # Статическая ссылка может идти по неактивной ветви формулы.
                if abs(deltas[period]) > 1e-7:
                    selected[period].append(item)
                    used.add(source)
                    break
            else:
                raise AssertionError(f"{query}, {period}: измеримый эффект не найден; "
                                     f"ограничения карты {discovery['trace']['limitations']}")

    for period, plans in selected.items():
        run(f"Совместный {period}", plans)
    all_plans = [p for plans in selected.values() for p in plans]
    forward = run("Все периоды", all_plans)
    reverse = run("Все периоды, обратный порядок записей", list(reversed(all_plans)))
    for period in targets:
        assert math.isclose(forward[period], reverse[period], rel_tol=1e-6, abs_tol=1e-6), period
    # Сверяем полный результат инструмента, которым пользуется агент, с прямым пересчётом Excel.
    shocks = []
    for item in all_plans:
        sheet, addr = split_ref(item["source"])
        shocks.append({"sheet": sheet, "cell": addr, "op": "pct", "value": item["requested_pct"]})
    events = []
    name = "Совместный стресс"
    result = TOOL_MAP["run_stress"]["fn"](
        session, {"scenarios": [{"name": name, "shocks": shocks}],
                  "metrics": [{"concept": metric}], "years": periods},
        lambda kind, data: events.append((kind, data)),
    )
    chart = next(data for kind, data in events if kind == "chart")["metrics"][0]
    actual = dict(zip(map(str, chart["years"]), chart["series"][1]["values"]))
    tx = engine.transactions[-1]
    assert tx["status"] == "verified" and tx["restored"] and result["model_restored"], tx
    for period, base in zip(targets, metric_baseline):
        assert math.isclose(actual[period], base[0][0] + forward[period], rel_tol=1e-6, abs_tol=1e-6), period
    assert same_values(engine.read(target_refs), metric_baseline), "База изменилась после run_stress"
    assert same_values(engine.read([p["source"] for p in all_plans]),
                       [[[p["source_before"]]] for p in all_plans], tol=0), "Входы не восстановлены после run_stress"
    _log(case="Инструмент агента run_stress", metrics=actual, restored=True)

    payload = {
        "shocks": [{"source": p["source"], "source_period": p["source_period"], "pct": p["requested_pct"]}
                   for p in all_plans],
        "metrics": [{"metric": metric, "period": period} for period in targets],
    }
    preview, checked = _verified_plan(session, payload)
    assert preview["phase"] == "preflight" and preview["metrics"][0]["target"] == target_refs[0]
    assert checked["phase"] == "verified" and checked["rollback_verified"]
    assert len(checked["individual"]) == len(all_plans)
    for output, period in zip(checked["combined"]["metrics"], targets):
        assert output["ref"] == targets[period]
        assert math.isclose(output["after"], actual[period], rel_tol=1e-6, abs_tol=1e-6), period
    for entry, item in zip(checked["combined"]["inputs"], all_plans):
        assert entry["ref"] == item["source"]
        assert math.isclose(entry["achieved_pct"], item["requested_pct"], abs_tol=1e-8)
    assert same_values(engine.read(target_refs), metric_baseline), "База изменилась после проверенного плана"
    assert preview["plan_id"] not in session.scenario_plans
    _log(case="Проверенный план Excel", inputs=len(all_plans), outputs=len(targets), restored=True)
    return {"targets": targets, "drivers": len(all_plans), "individual_trials": trials,
            "joint_trials": len(periods) + 4, "checked_individual": len(all_plans)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--metric", required=True)
    parser.add_argument("--period", required=True, action="append", help="Точный период; повторите для каждого периода")
    parser.add_argument("--driver", required=True, action="append", nargs=2, metavar=("QUERY", "PCT"),
                        help="Фрагмент подписи и изменение в процентах; повторите для каждого драйвера")
    args = parser.parse_args()
    periods = list(dict.fromkeys(str(norm_period(p)) for p in args.period))
    drivers = [(q.strip(), float(pct)) for q, pct in args.driver]
    if len(periods) < 2 or len(drivers) < 2 or any(not q or not math.isfinite(p) or p == 0 for q, p in drivers):
        parser.error("Укажите два разных периода и минимум два драйвера с конечным ненулевым шоком")
    original = _hash(args.model)
    session = Session(config.workdir())
    try:
        session.load(str(args.model), args.model.name, config.cache_dir(), "excel", progress=print)
        _log(model=args.model.name, sheets=len(session.idx.sheets), formulas=session.idx.n_formulas)
        result = verify(session, args.metric, periods, drivers)
    finally:
        session.close()
        assert _hash(args.model) == original, "Исходная книга изменилась"
    _log(status="PASS", model=args.model.name, **result)


if __name__ == "__main__":
    main()
