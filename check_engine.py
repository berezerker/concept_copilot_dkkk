"""Быстрая проверка движка пересчёта без интерфейса:
    python check_engine.py путь/к/модели.xlsx [excel|libreoffice]
Открывает модель, читает несколько ячеек, делает тестовый шок первой числовой строки и откатывает его."""
import os, sys, time, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.config import load_dotenv
load_dotenv()
from app.workbook.engine import make_engine
from app.workbook.index import build_index

path = os.path.abspath(sys.argv[1])
kind = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] in ("excel", "libreoffice") else None
print("1) Индекс модели…")
idx = build_index(path)
sh = next(s for s in idx.sheets.values() if s.years and s.n_formulas)
row = next(r for r, ri in sh.rows.items() if ri.formulas and any(isinstance(v, (int, float)) for v in ri.values.values()))
cols = sorted(c for c in sh.rows[row].values if c in sh.years)[:3]
from app.workbook.index import col_letter
ref = f"'{sh.name}'!{col_letter(cols[0])}{row}:{col_letter(cols[-1])}{row}"
print(f"   лист {sh.name}, строка {row}, диапазон {ref}")
print(f"2) Запуск движка ({kind or os.environ.get('CF_ENGINE', 'auto')})…")
t = time.time()
eng = make_engine(kind)
print("   движок:", eng.name, getattr(eng, "errors", ""))
if eng.name == "none":
    sys.exit(1)
try:
    print("3) Открытие файла…", eng.open(path, idx.iterate), f"{time.time() - t:.1f} с")
    base = eng.read([ref])
    print("4) Чтение:", base)
    vals, it = eng.scenario([{"ref": ref, "values": [[(v or 0) * 1.1 if isinstance(v, (int, float)) else v for v in base[0][0]]]}], [ref], [ref])
    print("5) Шок +10%:", vals, "итераций:", it)
    print("6) После отката:", eng.read([ref]), "(должно совпасть с п.4)")
    print("ГОТОВО: движок работает")
except Exception:
    traceback.print_exc()
finally:
    eng.close()
