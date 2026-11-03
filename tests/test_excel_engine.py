"""ExcelEngine на эмуляторе xlwings (в среде разработки нет Excel): ручной режим пересчёта, блочная запись шоков,
восстановление формул, ленивый пересчёт базы после сценария."""
import os, re, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from openpyxl.utils import column_index_from_string as ci, get_column_letter as cl

CALLS = {"calc": 0, "writes": 0}


class Sheet:
    def __init__(self, cells):
        self.cells = cells                       # (row, col) -> число или "=формула"

    def range(self, addr):
        return Rng(self, addr)


class Rng:
    def __init__(self, sh, addr):
        m = re.match(r"([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?$", addr)
        self.sh, self.c1, self.r1 = sh, ci(m.group(1)), int(m.group(2))
        self.c2, self.r2 = (ci(m.group(3)), int(m.group(4))) if m.group(3) else (self.c1, self.r1)

    def options(self, **k):
        return self

    def _cells(self):
        return [[(r, c) for c in range(self.c1, self.c2 + 1)] for r in range(self.r1, self.r2 + 1)]

    @property
    def value(self):
        return [[self.sh.vals.get(k) for k in row] for row in self._cells()]

    @value.setter
    def value(self, v):
        CALLS["writes"] += 1
        for row, vr in zip(self._cells(), v):
            for k, x in zip(row, vr):
                self.sh.cells[k] = x
                self.sh.vals[k] = x

    @property
    def formula(self):
        return [[self.sh.cells.get(k) for k in row] for row in self._cells()]

    @formula.setter
    def formula(self, f):
        for row, fr in zip(self._cells(), f):
            for k, x in zip(row, fr):
                self.sh.cells[k] = x


class Book:
    def __init__(self, sheets, name):
        self.sheets, self.name = sheets, name

    def close(self):
        pass


class App:
    def __init__(self, visible=False, add_book=False):
        self.books = Books(self)
        self.calculation = "automatic"
        self.display_alerts = self.screen_updating = True
        self.api = types.SimpleNamespace()

    def calculate(self):
        CALLS["calc"] += 1
        for b in self.books.items:
            for sh in b.sheets.values():
                for (r, c), x in sh.cells.items():
                    if isinstance(x, str) and x.startswith("="):
                        expr = re.sub(r"([A-Z]+)(\d+)", lambda m: repr(sh.vals.get((int(m.group(2)), ci(m.group(1))), 0) or 0), x[1:])
                        sh.vals[(r, c)] = eval(expr)
                    else:
                        sh.vals[(r, c)] = x

    def quit(self):
        pass


class Books:
    def __init__(self, app):
        self.app, self.items = app, []

    def open(self, path, update_links=False):
        cells = {}
        for c in range(3, 8):                    # строки 3–5 — допущения, 7 — расчёт
            cells[(3, c)] = 100.0
            cells[(4, c)] = 10.0
            cells[(5, c)] = 1.0
            L = cl(c)
            cells[(7, c)] = f"={L}3*{L}4+{L}5"
        sh = Sheet(cells)
        sh.vals = dict(cells)
        b = Book({"Inputs": sh}, os.path.basename(path))
        self.items.append(b)
        self.app.calculate()
        return b

    def __len__(self):
        return len(self.items)

    def __iter__(self):
        return iter(self.items)


def test_excel_engine():
    sys.modules["xlwings"] = types.SimpleNamespace(App=App)
    import platform
    platform.system = lambda: "Windows"
    from app.workbook.engine import ExcelEngine
    from app.agent.tools import _merge_overrides
    e = ExcelEngine()
    e.open("/tmp/model.xlsx", iterate=False)
    assert e.app.calculation == "manual" and e.app.screen_updating is False, "ручной пересчёт и без перерисовки"
    base = e.read(["Inputs!C7:G7"])[0][0]
    assert base == [1001.0] * 5
    ov = _merge_overrides([{"ref": "Inputs!C3:G3", "values": [[90.0] * 5]}, {"ref": "Inputs!C4:G4", "values": [[11.0] * 5]}])
    assert len(ov) == 1 and ov[0]["ref"] == "Inputs!C3:G4", ov    # две соседние строки — один блок
    w0 = CALLS["writes"]
    vals, iters = e.scenario(ov, ["Inputs!C7:G7"], ["Inputs!C7:G7"])
    assert CALLS["writes"] - w0 == 2, "одна запись шока + одна запись отката на блок"
    assert vals[0][0] == [991.0] * 5
    c0 = CALLS["calc"]
    vals2, _ = e.scenario(ov, ["Inputs!C7:G7"], ["Inputs!C7:G7"])    # повтор: формулы из кэша
    assert vals2 == vals
    after = e.read(["Inputs!C7:G7"])[0][0]                          # ленивое восстановление базы
    assert after == base and CALLS["calc"] > c0
    assert e.read(["Inputs!C3:G3"])[0][0] == [100.0] * 5, "допущения восстановлены"
    e.close()
    assert e.app.calculation == "automatic"


if __name__ == "__main__":
    test_excel_engine(); print("excel engine ok")


def test_coalesce():
    from app.workbook.engine import ExcelEngine
    e = ExcelEngine.__new__(ExcelEngine)
    e._box_cache = {}
    # строки 3..8: 3,4 — шок; 5 — пусто/значения; 6 — формула; 7,8 — шок
    F = [[1.0, 1.0], [2.0, 2.0], [5.0, 5.0], ["=A1", "=A1"], [7.0, 7.0], [8.0, 8.0]]
    e._rng = lambda ref: types.SimpleNamespace(options=lambda **k: types.SimpleNamespace(formula=F, value=F))
    ov = [{"ref": "S!K3:L3", "values": [[0, 0]]}, {"ref": "S!K4:L4", "values": [[0, 0]]},
          {"ref": "S!K7:L8", "values": [[9, 9], [9, 9]]}]
    out = e._coalesce(ov)
    assert [o["ref"] for o in out] == ["S!K3:L4", "S!K7:L8"], out
    ov2 = [{"ref": "S!K3:L3", "values": [[0, 0]]}, {"ref": "S!K5:L5", "values": [[1, 1]]}]
    out2 = e._coalesce(ov2)
    assert [o["ref"] for o in out2] == ["S!K3:L5"] and out2[0]["values"][1] == [2.0, 2.0], out2   # перешагнули строку 4
    print("coalesce ok")


if __name__ == "__main__":
    test_coalesce()
