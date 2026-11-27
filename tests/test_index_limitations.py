"""Ограничения карты не должны скрываться за произвольными именами листов."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from openpyxl import Workbook
from app.workbook.index import build_index


class IndexLimitations(unittest.TestCase):
    def test_long_formula_range_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "model.xlsx")
            wb = Workbook()
            ws = wb.active
            ws.title = "任意 ' A"
            ws["A1"] = "Parameter"
            ws["B1"] = 2
            ws["B2"] = "=SUM(B1:B500)"
            wb.save(path)
            idx = build_index(path)
            self.assertIn(ws.title, idx.sheets)
            self.assertTrue(any("длинные диапазоны" in w for w in idx.warnings))

    def test_partial_sheet_read_is_reported(self):
        class BrokenSheet:
            title = "任意 sheet"
            def iter_rows(self):
                raise ValueError("unreadable")
        class FakeWorkbook:
            worksheets = [BrokenSheet()]
            calculation = None
            defined_names = {}
            def close(self):
                pass
        with tempfile.NamedTemporaryFile(suffix=".xlsx") as f:
            with patch("app.workbook.index.openpyxl.load_workbook", side_effect=lambda *a, **k: FakeWorkbook()):
                idx = build_index(f.name)
        self.assertTrue(any("формулы прочитаны не полностью" in w for w in idx.warnings))
        self.assertTrue(any("значения прочитаны не полностью" in w for w in idx.warnings))


if __name__ == "__main__":
    unittest.main()
