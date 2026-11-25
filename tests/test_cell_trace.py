"""Регрессии точного пути и предварительной проверки сценария."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.tools_probe import inspect_cell_stress, run_cell_stress
from app.workbook.catalog import display_unit
from app.workbook.cell_trace import cell, trace
from app.workbook.index import RowInfo, SheetInfo, WorkbookIndex


class CellTraceTests(unittest.TestCase):
    def setUp(self):
        # Периоды расположены вертикально, а у драйвера нет подписи в строке.
        self.sheets = {"任意! d'": SheetInfo("任意! d'", rows={3: RowInfo(3, values={4: 17, 5: 29})})}
        prev = "'任意! d'''!$D$3"
        self.expected = [prev.replace("$", "")]
        for i in range(1, 9):
            name = f"工程 {i}"
            formula = f'=IF(TRUE,{prev}*2,INDIRECT("DUMMY!H8"))'
            self.sheets[name] = SheetInfo(name, rows={8: RowInfo(8, formulas={8: formula})})
            prev = f"'{name}'!H8"
            self.expected.append(prev)
        self.idx = WorkbookIndex("sample.xlsx", "hash", self.sheets, {}, {}, {})

    def test_exact_chain_keeps_columns_across_eight_sheets(self):
        target = cell(self.idx, self.expected[-1])
        source = cell(self.idx, self.expected[0])
        result = trace(self.idx, target, source)
        self.assertEqual(result["path"], self.expected)
        self.assertIn("dynamic_reference", result["limitations"])
        other = cell(self.idx, "'任意! d'''!E3")
        self.assertEqual(trace(self.idx, target, other)["status"], "not_found")
        self.assertEqual(trace(self.idx, target)["candidates"][0]["ref"], self.expected[0])

    def test_stale_preview_cannot_write(self):
        session = SimpleNamespace(idx=self.idx, catalog={"concepts": {}},
                                  engine=SimpleNamespace(name="excel", read=lambda refs: [[[17]], [[50]]]),
                                  require_engine=lambda: None)
        args = {"target": self.expected[-1], "source": self.expected[0], "pct": -10}
        preview = inspect_cell_stress(session, args, None)
        self.assertAlmostEqual(preview["source_proposed"], 15.3)
        self.assertIsNone(preview["period"])
        with self.assertRaisesRegex(ValueError, "Допущение изменилось"):
            run_cell_stress(session, {**args, "expected_input": 18, "file_hash": "hash"}, None)
        with self.assertRaisesRegex(ValueError, "Книга изменилась"):
            run_cell_stress(session, {**args, "expected_input": 17, "file_hash": "other"}, None)

    def test_ratio_without_verified_unit_stays_unlabelled(self):
        self.assertEqual(display_unit("ratio", "млн. дол."), "")
        self.assertEqual(display_unit("ratio", "%"), "%")
        self.assertEqual(display_unit("flow", "млн. дол."), "млн. дол.")


if __name__ == "__main__":
    unittest.main()
