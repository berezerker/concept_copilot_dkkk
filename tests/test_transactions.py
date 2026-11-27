"""Сбои записи/чтения/отката: успешный результат только после восстановления."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.test_excel_engine import App, CALLS, Rng
from app.workbook.engine import ExcelEngine
from app.workbook.transaction import RestorationError


class Transactions(unittest.TestCase):
    def setUp(self):
        self.platform = patch("app.workbook.engine.platform.system", return_value="Windows")
        self.platform.start()
        self.e = ExcelEngine.__new__(ExcelEngine)
        self.e.app = App()
        self.e.wb = self.e.app.books.open("test.xlsx")
        self.e.iterate = False
        self.watch = ["Inputs!C7:G7"]
        self.ov = [{"ref": "Inputs!C3:G3", "values": [[90.] * 5]}]
        self.base = self.e._read_raw(self.watch)

    def tearDown(self):
        self.platform.stop()

    def run_scenario(self, overrides=None):
        return self.e._scenario(self.ov if overrides is None else overrides, self.watch, self.watch)

    def assert_base(self):
        self.assertEqual(self.e._read_raw(self.watch), self.base)
        self.assertEqual(self.e._rng("Inputs!C3:G3").value, [[100.] * 5])

    def test_success_and_fresh_snapshot(self):
        result, _ = self.run_scenario()
        self.assertEqual(result, [[[901.] * 5]])
        self.assertEqual(self.e.last_transaction["status"], "verified")
        self.assert_base()
        self.e._rng("Inputs!C3:G3").value = [[120.] * 5]
        self.run_scenario()
        self.assertEqual(self.e._rng("Inputs!C3:G3").value, [[120.] * 5])

    def test_preflight_before_any_write(self):
        invalid = [self.ov + self.ov,
                   [{"ref": "Inputs!C3:G3", "values": [[1]]}],
                   [{"ref": "Inputs!C3", "values": [[float("nan")]]}],
                   [{"ref": "Inputs!C3", "values": [["=1+1"]]}],
                   self.ov + [{"ref": "Missing!A1", "values": [[1]]}]]
        for overrides in invalid:
            before = CALLS["writes"]
            with self.assertRaises((ValueError, KeyError)):
                self.run_scenario(overrides)
            self.assertEqual(CALLS["writes"], before)
            self.assert_base()

    def test_checked_input_that_has_changed_is_not_written(self):
        before = CALLS["writes"]
        with self.assertRaisesRegex(ValueError, "допущение изменилось"):
            self.run_scenario([{"ref": "Inputs!C3", "values": [[89.]], "expected_input": [[99.]]}])
        self.assertEqual(CALLS["writes"], before)
        self.assert_base()

    def test_changed_control_baseline_is_rejected_before_write(self):
        before = CALLS["writes"]
        expected = {self.watch[0]: [[1.] * 5]}
        with self.assertRaisesRegex(ValueError, "база изменилась"):
            self.e._scenario(self.ov, self.watch, self.watch, expected)
        self.assertEqual(CALLS["writes"], before)
        self.assertEqual(self.e.last_transaction["status"], "preflight")
        self.assert_base()

    def test_partial_batch_write_rolls_back(self):
        def batch(name, ops):
            sh, addr, kind, values = ops[0]
            setattr(self.e.wb.sheets[sh].range(addr), kind, values)
            raise RuntimeError("batch interrupted after first write")
        with patch("app.workbook.engine.platform.system", return_value="Darwin"), \
             patch("app.workbook.engine._applescript_batch", side_effect=batch), \
             patch.dict(os.environ, {"CF_MAC_BATCH": "1"}):
            with self.assertRaisesRegex(RuntimeError, "batch interrupted"):
                self.run_scenario(self.ov + [{"ref": "Inputs!C5:G5", "values": [[9.] * 5]}])
        self.assert_base()
        self.assertTrue(self.e.last_transaction["restored"])

    def test_read_failure_restores_formula(self):
        raw = self.e._read_raw
        calls = 0
        def read(refs):
            nonlocal calls
            calls += 1
            # baseline converge, baseline snapshot, scenario converge, result read
            if calls == 4:
                raise RuntimeError("result read failed")
            return raw(refs)
        before = self.e._rng("Inputs!C7:G7").formula
        with patch.object(self.e, "_read_raw", side_effect=read):
            with self.assertRaisesRegex(RuntimeError, "result read failed"):
                self.run_scenario([{"ref": "Inputs!C7:G7", "values": [[1.] * 5]}])
        self.assertEqual(self.e._rng("Inputs!C7:G7").formula, before)
        self.assert_base()

    def test_silent_restore_failure_blocks_future_work(self):
        setter = Rng.value.fset
        def write(rng, values):
            if values and all(x == 100. for row in values for x in row):
                return  # Excel принял команду, но не восстановил диапазон
            setter(rng, values)
        with patch.object(Rng, "value", property(Rng.value.fget, write)):
            with self.assertRaises(RestorationError):
                self.run_scenario()
        self.assertTrue(self.e._tainted)
        with self.assertRaises(RestorationError):
            self.run_scenario()
        with self.assertRaises(RestorationError):
            self.e._read(self.watch)

    def test_unverified_shock_with_verified_rollback_keeps_engine_usable(self):
        setter = Rng.value.fset

        def write(rng, values):
            if all(x == 90. for row in values for x in row):
                return
            setter(rng, values)

        with patch.object(Rng, "value", property(Rng.value.fget, write)):
            with self.assertRaisesRegex(RuntimeError, "не записаны точно") as caught:
                self.run_scenario()
        self.assertNotIsInstance(caught.exception, RestorationError)
        self.assertTrue(self.e.last_transaction["restored"])
        self.assert_base()
        self.run_scenario()
        self.assertEqual(self.e.last_transaction["status"], "verified")

    def test_restore_continues_after_one_failure(self):
        setter = Rng.value.fset
        def write(rng, values):
            if rng.r1 == 3 and values == [[100.] * 5]:
                raise RuntimeError("restore failed")
            setter(rng, values)
        with patch.object(Rng, "value", property(Rng.value.fget, write)):
            with self.assertRaises(RestorationError):
                self.run_scenario(self.ov + [{"ref": "Inputs!C5:G5", "values": [[9.] * 5]}])
        self.assertEqual(self.e._rng("Inputs!C5:G5").value, [[1.] * 5])

    def test_baseline_mismatch_blocks_result(self):
        raw = self.e._read_raw
        calls = 0
        def read(refs):
            nonlocal calls
            calls += 1
            return [[[999.] * 5]] if calls == 6 else raw(refs)
        with patch.object(self.e, "_read_raw", side_effect=read):
            with self.assertRaisesRegex(RestorationError, "отличаются от базы"):
                self.run_scenario()

    def test_nonconvergence_is_not_success(self):
        self.e.iterate = True
        with patch.object(self.e, "_read_raw", side_effect=[[[[i]]] for i in range(3)]):
            with self.assertRaisesRegex(RuntimeError, "сходимости"):
                self.e._converge(self.watch, max_iter=3)

    def test_coalescing_cannot_introduce_overlaps(self):
        def batch(name, ops):
            for sh, addr, kind, values in ops:
                setattr(self.e.wb.sheets[sh].range(addr), kind, values)
        overrides = self.ov + [{"ref": "Inputs!D4:E4", "values": [[11., 11.]]},
                               {"ref": "Inputs!C5:G5", "values": [[9.] * 5]}]
        with patch("app.workbook.engine.platform.system", return_value="Darwin"), \
             patch("app.workbook.engine._applescript_batch", side_effect=batch), \
             patch.dict(os.environ, {"CF_MAC_BATCH": "1"}):
            result, _ = self.run_scenario(overrides)
        self.assertEqual(result, [[[909., 999., 999., 909., 909.]]])
        self.assert_base()

    def test_numeric_metric_cannot_silently_become_empty(self):
        raw = self.e._read_raw
        calls = 0
        def read(refs):
            nonlocal calls
            calls += 1
            return [[[None] * 5]] if calls == 4 else raw(refs)
        with patch.object(self.e, "_read_raw", side_effect=read):
            with self.assertRaisesRegex(ValueError, "числовой метрики"):
                self.run_scenario()
        self.assert_base()

    def test_batch_rounding_is_repaired_with_native_write(self):
        self.e._rng("Inputs!C3:G3").value = [[100.12345678901234] * 5]
        original = self.e._rng("Inputs!C3:G3").value
        def batch(name, ops):
            for sh, addr, kind, values in ops:
                rounded = [[round(x, 6) if isinstance(x, float) else x for x in row] for row in values]
                setattr(self.e.wb.sheets[sh].range(addr), kind, rounded)
        with patch("app.workbook.engine.platform.system", return_value="Darwin"), \
             patch("app.workbook.engine._applescript_batch", side_effect=batch), \
             patch.dict(os.environ, {"CF_MAC_BATCH": "1"}):
            self.run_scenario()
        self.assertEqual(self.e._rng("Inputs!C3:G3").value, original)
        self.assertEqual(self.e.last_transaction["status"], "verified")

    def test_multiline_native_write_is_repaired_rowwise(self):
        setter = Rng.value.fset
        def write(rng, values):
            if rng.r2 > rng.r1:
                damaged = [list(row) for row in values]
                damaged[-1] = [None] * len(damaged[-1])
                return setter(rng, damaged)
            return setter(rng, values)
        overrides = [{"ref": "Inputs!C3:G5", "values": [[90.] * 5, [11.] * 5, [2.] * 5]}]
        with patch.object(Rng, "value", property(Rng.value.fget, write)):
            result, _ = self.run_scenario(overrides)
        self.assertEqual(result, [[[992.] * 5]])
        self.assert_base()
        self.assertEqual(self.e.last_transaction["status"], "verified")


if __name__ == "__main__":
    unittest.main()
