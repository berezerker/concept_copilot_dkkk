"""Кредитные инструменты, отраслевой эксперт и заключение КПКИ на синтетической модели.
LLM эмулируется (httpx.MockTransport) — без сети."""
import json, os, sys
import httpx
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.agent.registry import TOOL_MAP
from tests.sample_model import make_sample_session

EV = []
call = lambda s, n, a: TOOL_MAP[n]["fn"](s, a, lambda k, d: EV.append((k, d)))
DSCR = {"sheet": "Calc", "row": 8, "name": "DSCR", "op": ">=", "threshold": 1.3}
PRICE, VOL = {"sheet": "Inputs", "row": 3, "name": "Цена"}, {"sheet": "Inputs", "row": 4, "name": "Объём"}


def test_tools(s):
    r = call(s, "lender_metrics", {"cfads": {"sheet": "Calc", "row": 4}, "debt_service_parts": [{"sheet": "Calc", "row": 6}, {"sheet": "Calc", "row": 7}],
                                   "debt": {"sheet": "Calc", "row": 5}, "ebitda": {"sheet": "Calc", "row": 4}, "rate_pct": 12})
    base = call(s, "read_rows", {"sheet": "Calc", "rows": [8]})["rows"][0]["values"]
    for row in r["rows"]:
        assert abs(row["dscr"] - base[row["period"]]) < 1e-3, (row, base)
    assert r["summary"]["min_dscr"] and r["capacity"]["sculpted_capacity"] > 0
    p = call(s, "stress_pack", {"drivers": [dict(PRICE, category="price"), dict(VOL, category="volume")], "rules": [DSCR]})
    assert len(p["rows"]) == 1 + 4 + 2 and p["rows"][-1]["cells"][0]["value"] < p["rows"][0]["cells"][0]["value"]
    z = call(s, "safe_zone", {"rule": DSCR, "x": PRICE, "y": VOL, "x_values": [-20, 0], "y_values": [-20, 0]})
    assert z["grid"][1][1]["value"] > z["grid"][0][0]["value"]
    rv = call(s, "reverse_stress", {"rules": [DSCR], "drivers": [dict(PRICE, direction=-1, sd_pct=10), dict(VOL, direction=-1, sd_pct=5)]})
    assert rv.get("breach_found") or rv.get("already_breached") or rv.get("message"), rv
    if rv.get("breach_found"):
        assert abs(rv["shocks"][0]["change_pct"]) > abs(rv["shocks"][1]["change_pct"])   # пропорции σ
    dc = call(s, "debt_capacity", {"driver": {"sheet": "Calc", "row": 5}, "rules": [DSCR]})
    assert "max_change_pct" in dc or "message" in dc, dc
    st = call(s, "run_stress", {"scenarios": [{"name": "задержка", "shocks": [dict(VOL, op="shift", value=1)]}],
                                "metrics": [{"sheet": "Calc", "row": 2, "name": "Выручка"}]})
    t = st["metrics"][0]["table"]
    assert t["задержка"]["2026"] < t["База"]["2026"]
    print("credit tools ok:", r["summary"], "| пакет:", p["breach_scenarios"][:3], "| ёмкость:", dc.get("max_change_pct"))


def expert_openai_handler(seen):
    def h(req):
        body = json.loads(req.content)
        seen.append(body)
        if body.get("tools"):
            assert body["tool_choice"]["function"]["name"] == "propose_assumptions"
            fields = json.loads(body["messages"][1]["content"])["fields"]
            assert fields[0]["ref"] == "Inputs!3" and "2026" in fields[0]["current"]
            args = {"scenario_title": "Падение цен", "summary": "тест", "key_risks": ["r1"], "assumptions": [
                {"field_id": 0, "op": "path", "values_by_period": {"2026": 9000, "2027": 9500}, "rationale": "x", "confidence": "medium"},
                {"field_id": 1, "op": "pct", "value": -5, "rationale": "y", "confidence": "low"}]}
            msg = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "propose_assumptions", "arguments": json.dumps(args)}}]}
        else:
            assert "tool_choice" not in body
            msg = {"role": "assistant", "content": "## Резюме\nтест\n## Ключевые риски\n- a\n## Рекомендации\n- b"}
        return httpx.Response(200, json={"choices": [{"message": msg}], "usage": {}})
    return h


def test_expert_and_kpki(s):
    os.environ["OPENAI_API_KEY"] = "test"
    from app.llm.openai_client import OpenAIClient
    c = OpenAIClient("gpt-test")
    seen = []
    c.http = httpx.Client(transport=httpx.MockTransport(expert_openai_handler(seen)))
    s.client = c
    r = call(s, "industry_expert", {"news": "Цены упали", "fields": [PRICE, VOL], "rules": [DSCR]})
    assert r["assumptions"][0]["new"] == {"2026": 9000.0, "2027": 9500.0}
    assert r["stress"]["metrics"][0]["table"]["Падение цен"]["2026"] < r["stress"]["metrics"][0]["table"]["База"]["2026"]
    assert r["covenants"]["rules"]
    from app.agent.kpki import run_kpki
    ev = []
    md = run_kpki(s, c, {"rules": [DSCR], "mc_runs": 20}, lambda k, d: ev.append((k, d)))
    ends = [d for k, d in ev if k == "tool_end"]
    assert all(d["ok"] for d in ends), [d for d in ends if not d["ok"]]
    assert "## Резюме\nтест" in md and "## 4. Стресс-пакет банка" in md
    from app.report_docx import md_to_docx
    assert len(md_to_docx(md)) > 5000
    print("expert ok · kpki ok:", len(ends), "шагов, отчёт", len(md), "символов")


def sem_openai_handler(seen):
    def h(req):
        body = json.loads(req.content)
        seen.append(body)
        assert body["tool_choice"]["function"]["name"] == "describe_rows"
        rows = json.loads(body["messages"][1]["content"])
        out = [{"id": r["id"], "name": "Тест: " + (r["label"] or "")[:20], "desc": "описание",
                "factor": "price_sales" if "Цена" in (r["label"] or "") else r["rules_factor"], "sub": "", "direction": "up"} for r in rows]
        args = {"industry": "metals", "rows": out}
        msg = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "describe_rows", "arguments": json.dumps(args, ensure_ascii=False)}}]}
        return httpx.Response(200, json={"choices": [{"message": msg}], "usage": {}})
    return h


def test_semantics(s):
    from app.workbook import semantics as sm
    assert s.sem and s.sem["rows"], "смысловой слой строится при загрузке"
    p = s.sem["rows"].get("Inputs!3")
    assert p and p["factor"] == "price_sales", p
    os.environ["OPENAI_API_KEY"] = "test"
    from app.llm.openai_client import OpenAIClient
    c = OpenAIClient("gpt-test")
    seen = []
    c.http = httpx.Client(transport=httpx.MockTransport(sem_openai_handler(seen)))
    n = sm.enrich(s.sem, c, batch=3)
    assert n == len(s.sem["rows"]) and len(seen) >= 2 and s.sem["industry"] == "metals"
    assert s.sem["rows"]["Inputs!3"]["name"].startswith("Тест") and s.sem["llm"]["rows"] == n
    r = call(s, "factor_drivers", {"metric": {"sheet": "Calc", "row": 8, "name": "DSCR"}})
    names = [x["name"] for x in r["factors"]]
    assert "Цены реализации продукции" in names and abs(sum(x["share"] for x in r["factors"]) - 1) < 0.01
    d = call(s, "dependency_analysis", {"rules": [DSCR]})
    assert d["findings"] and "Inputs!" not in " ".join(d["findings"])
    h = call(s, "headroom", {"rule": DSCR, "drivers": [{"factor": "price_sales"}]})
    assert h["drivers"][0]["driver"] == "Цены реализации продукции"
    print("semantics ok:", d["findings"][0])


if __name__ == "__main__":
    s = make_sample_session()
    test_tools(s)
    test_semantics(s)
    test_expert_and_kpki(s)
    s.close()

