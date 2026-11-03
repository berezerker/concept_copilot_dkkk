"""Матрица совместимости: прогоняет все функции агента (без LLM) на списке моделей и печатает,
что сработало, за сколько и с каким ключевым результатом.

    python tests/compat_report.py модель1.xlsx модель2.xlsx ...
"""
import json, os, sys, time, traceback
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.agent.session import Session
from app.agent.registry import TOOL_MAP
from app.workbook.card import numeric_years


def pick_metric(s):
    """Главная долговая метрика из карточки: ND/EBITDA → DSCR → ISCR → EBITDA."""
    for name in ("Чистый долг / EBITDA", "DSCR", "ISCR / ICR", "EBITDA", "Выручка"):
        for m in s.card["key_metrics"]:
            if m["metric"] == name:
                for c in m["candidates"]:
                    if len(numeric_years(s.idx, c["sheet"], c["row"])) >= 3:
                        return name, c
    return None, None


def run(path, engine="libreoffice"):
    rep = {"file": os.path.basename(path)}
    t = time.time()
    s = Session("/tmp/cf_compat")
    s.load(path, os.path.basename(path), "/tmp/cf_compat/cache", engine)
    rep["load_s"] = round(time.time() - t, 1)
    rep["engine"] = s.engine.name if s.engine_open else f"нет: {s.engine_error}"
    call = lambda n, a: TOOL_MAP[n]["fn"](s, a, lambda k, d: None)
    name, m = pick_metric(s)
    rep["metric"] = f"{name}: {m['sheet']}!{m['row']} «{m['label'][:40]}»" if m else "не найдена"
    op = "<=" if name and "долг" in name.lower() else ">="

    def step(key, fn):
        t0 = time.time()
        try:
            res = fn()
            rep[key] = {"ok": True, "s": round(time.time() - t0, 1), "result": res}
        except Exception as e:
            rep[key] = {"ok": False, "s": round(time.time() - t0, 1), "error": f"{type(e).__name__}: {e}"[:300]}

    step("model_map", lambda: (lambda r: {"листов": len(r["sheets"]), "доля драйверов вне Inputs": [
        (x["metric"], x["outside_inputs_share"]) for x in r["metrics_drivers"][:3]]})(call("model_map", {})))
    step("audit_model", lambda: call("audit_model", {})["summary"])
    if not m:
        return rep
    met = {"sheet": m["sheet"], "row": m["row"], "name": name}
    ys = sorted(numeric_years(s.idx, m["sheet"], m["row"]))
    yr = next((y for y in ys if y >= (s.forecast_start or 0)), ys[-1])
    step("explain_cell", lambda: (lambda r: f"{r['cell']} = {r['formula']} ({len(r['parts'])} слагаемых)")(
        call("explain_cell", {"sheet": m["sheet"], "row": m["row"], "year": yr})))
    step("find_drivers", lambda: (lambda r: {"проверено": r["candidates_tested"], "найдено": len(r["drivers"]),
        "вне Inputs": r["top_outside_inputs"], "топ-3": [f"{d['sheet']}!{d['row']} {d['label'][:30]}" for d in r["drivers"][:3]]})(
        call("find_drivers", {"metric": met, "aggregate": "max" if op == "<=" else "min", "max_candidates": 40, "top": 5})))
    drv = rep["find_drivers"]["result"]["топ-3"] if rep["find_drivers"]["ok"] and rep["find_drivers"]["result"]["топ-3"] else []
    dsh = [{"sheet": x.split("!")[0], "row": int(x.split("!")[1].split()[0])} for x in drv[:2]]
    base_vals = numeric_years(s.idx, m["sheet"], m["row"])
    thr = round(max(base_vals[y] for y in ys if y >= (s.forecast_start or 0)) * 1.1, 2) if op == "<=" else \
        round(min(base_vals[y] for y in ys if y >= (s.forecast_start or 0)) * 0.9, 2)
    rule = dict(met, op=op, threshold=thr)
    step("run_stress (row)", lambda: (lambda r: r["metrics"][0]["summary"])(call("run_stress", {"scenarios": [
        {"name": "-10%", "shocks": [dict(d, op="pct", value=-10) for d in dsh]}], "metrics": [met]})) if dsh else "нет драйверов")
    step("run_stress (match: цена)", lambda: (lambda r: r["scenarios"][0]["shocks"][:3])(call("run_stress", {"scenarios": [
        {"name": "цены -15%", "shocks": [{"match": "цена", "op": "pct", "value": -15}]}], "metrics": [met]})))
    step("covenant_check", lambda: (lambda r: {"порог": f"{op} {thr}", "пробой": r["rules"][0]["breach_years"],
        "мин. запас": r["rules"][0]["worst"]})(call("covenant_check", {"rules": [rule]})))
    step("headroom", lambda: call("headroom", {"rule": rule, "drivers": dsh})["drivers"] if dsh else "нет драйверов")
    step("goal_seek", lambda: (lambda r: {k: r.get(k) for k in ("converged", "value", "metric_value", "message")})(
        call("goal_seek", {"shock": dict(dsh[0], op="pct"), "metric": met, "aggregate": "max" if op == "<=" else "min",
                           "target": thr, "lower": -60, "upper": 60})) if dsh else "нет драйверов")
    step("monte_carlo (n=40)", lambda: (lambda r: {k: r[k] for k in ("p5", "p50", "p95", "breach_probability")})(
        call("monte_carlo", {"drivers": [dict(d, sd_pct=10) for d in dsh], "metric": met,
                             "aggregate": "max" if op == "<=" else "min", "op": op, "threshold": thr, "n": 40})) if dsh else "нет драйверов")
    # ── кредитные инструменты ──
    def first(q, inputs=False):
        r = call("search_rows", {"query": q, "limit": 5, "only_inputs": inputs})["rows"]
        r = [x for x in r if x["values"] and (not inputs or x["dependents"])]
        return {"sheet": r[0]["sheet"], "row": r[0]["row"], "name": r[0]["label"][:30]} if r else None
    cfads, ds, debt = first("CFADS") or first("денежные средства доступные для обслуживания"), first("обслуживание долга"), first("долг")
    step("lender_metrics", lambda: (lambda r: {"summary": r["summary"], "capacity": {k: v for k, v in r["capacity"].items() if k != "sculpted_schedule"}})(
        call("lender_metrics", {"cfads": cfads, "debt_service": ds, "debt": debt, "rate_pct": 10})) if cfads and ds else "нет рядов CFADS/обслуживания")
    cats = ["price", "volume", "opex"]
    step("stress_pack", lambda: (lambda r: {"сценариев": len(r["rows"]) - 1, "пробой": r["breach_scenarios"][:4]})(
        call("stress_pack", {"drivers": [dict(d, category=cats[i]) for i, d in enumerate(dsh)], "rules": [rule]})) if dsh else "нет драйверов")
    step("safe_zone (4x4)", lambda: (lambda r: {"safe_share": r["safe_share"], "edge": r["edge"]})(
        call("safe_zone", {"rule": rule, "x": dsh[0], "y": dsh[1] if len(dsh) > 1 else dsh[0],
                           "x_values": [-30, -15, 0, 10], "y_values": [-30, -15, 0, 10]})) if dsh else "нет драйверов")
    step("reverse_stress", lambda: (lambda r: {k: r.get(k) for k in ("multiple", "shocks", "binding", "sigma_distance", "message")})(
        call("reverse_stress", {"rules": [rule], "drivers": [dict(d, direction=-1, sd_pct=10) for d in dsh]})) if dsh else "нет драйверов")
    ddrv = first("кредит", True) or first("долг", True)
    step("debt_capacity", lambda: (lambda r: {k: r.get(k) for k in ("driver", "max_change_pct", "binding_covenant", "message")})(
        call("debt_capacity", {"driver": ddrv, "rules": [rule]})) if ddrv else "нет строки-драйвера долга")
    step("industry_expert (offline)", lambda: (lambda r: {"полей": len(r["assumptions"]), "сценарии": [x["summary"] for x in r["stress"]["metrics"]] if r.get("stress") else None})(
        call("industry_expert", {"news": "Цены на продукцию упали на 12% из-за санкций", "fields": dsh, "metrics": [met]})) if dsh else "нет драйверов")
    s.close()
    return rep


if __name__ == "__main__":
    reports = [run(p) for p in sys.argv[1:]]
    keys = [k for k in reports[0] if isinstance(reports[0].get(k), dict)]
    print("\n=== МАТРИЦА СОВМЕСТИМОСТИ ===")
    for r in reports:
        print(f"\n## {r['file']} · загрузка {r['load_s']} с · движок {r['engine']} · метрика {r['metric']}")
        for k in keys:
            x = r.get(k)
            if not isinstance(x, dict):
                continue
            mark = "✓" if x["ok"] else "✗"
            print(f"  {mark} {k:26s} {x['s']:>5} с  {json.dumps(x.get('result', x.get('error')), ensure_ascii=False)[:230]}")
    with open("/tmp/cf_compat_report.json", "w") as f:
        json.dump(reports, f, ensure_ascii=False, indent=1, default=str)
