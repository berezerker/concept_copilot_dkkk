"""Различие отраслевых свидетельств, агрегатов и предположений."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.workbook import credit_review
from app.workbook.index import RowInfo, SheetInfo, WorkbookIndex
from app.workbook.periods import Period
from app.agent.registry import tool_specs


def test_aggregate_is_not_a_missing_driver():
    sheet = SheetInfo("Контур без шаблона", years={2: 2026, 3: 2027}, rows={
        7: RowInfo(7, "Реализация золота", values={2: 15, 3: 17}),
        8: RowInfo(8, "Переработка руды", values={2: 400, 3: 420}),
        12: RowInfo(12, "Выручка", values={2: 300, 3: 360}),
    })
    index = WorkbookIndex("", "proof", {sheet.name: sheet}, {}, {}, {})
    metric = {"catalog_id": "revenue", "id": "metric:revenue", "name": "Выручка", "periods": ["2026", "2027"],
              "metric_kind": "flow", "bindings": [{"sheet": sheet.name, "row": 12}]}
    session = SimpleNamespace(idx=index, forecast_start=2026, credit_review_rows=None,
                              credit_review_profiles=None, credit_review_decisions={}, credit_covenant=None,
                              verified_scenarios={}, manifest={"model": {}, "readiness": {}, "scenario_sets": [],
                                                               "limitations": [], "metrics": [metric]})
    report = credit_review.inspect(session)
    assert next(p for p in report["profiles"] if p["id"] == "mining")["status"] == "suggested"
    rule = next(r for r in report["checks"] if r["id"] == "mining_revenue")
    assert rule["status"] == "source_detail_unavailable"
    assert {cell["kind"] for cell in rule["source_gaps"]} == {"constant"}
    assert not rule["fields"][1]["evidence"]
    assert credit_review.covenant_result(session)["status"] == "confirmation_required"


def test_annual_and_quarterly_targets_stay_distinct():
    sheet = SheetInfo("Поток", years={2: 2026, 3: Period(2026, 1, "Q"), 4: 2027},
                      rows={4: RowInfo(4, "Денежный поток", values={2: 7, 3: 2, 4: 9})})
    index = WorkbookIndex("", "proof", {sheet.name: sheet}, {}, {}, {})
    session = SimpleNamespace(idx=index, forecast_start=2026)
    values = credit_review._period_cells(session, sheet.name, 4)
    assert list(values) == ["2026Q1", "2026", "2027"]
    assert credit_review.cells.cell_for_period(index, (sheet.name, 4), 2026) == (sheet.name, 4, 2)


def test_review_compares_only_matching_periods_and_units():
    base = {"label": "DSCR", "unit": "x", "preview": [
        {"period": "2026", "value": 1.5, "ref": "A!B4"},
        {"period": "2027", "value": 1.7, "ref": "A!C4"},
    ]}
    alternative = {"label": "DSCR по договору", "unit": "x", "preview": [
        {"period": "2026Q1", "value": 1, "ref": "B!B6"},
        {"period": "2026", "value": 1.501, "ref": "B!C6"},
        {"period": "2027", "value": 1.2, "ref": "B!D6"},
    ]}
    assert credit_review._different_values([base, dict(alternative, unit="%")]) is None
    difference = credit_review._different_values([base, alternative])
    assert difference["period"] == "2027"
    assert [row["ref"] for row in difference["rows"]] == ["A!C4", "B!D6"]


def test_stress_accepts_optional_operations_without_a_top_level_union():
    schema = next(tool["parameters"] for tool in tool_specs() if tool["name"] == "run_cell_stress")
    assert schema["type"] == "object" and "anyOf" not in schema
    assert schema["properties"]["op"]["enum"] == ["pct", "pp", "set"]
    assert "pct" not in schema["required"]


if __name__ == "__main__":
    test_aggregate_is_not_a_missing_driver()
    test_annual_and_quarterly_targets_stay_distinct()
    test_review_compares_only_matching_periods_and_units()
    test_stress_accepts_optional_operations_without_a_top_level_union()
    print("credit review evidence and period alignment ok")
