"""Синтетические модели с разной периодичностью шапки (для проверки поддержки периодов).
Структура как в sample_model: Inputs (цена, объём, ставка, доля себестоимости) → Calc (выручка, EBITDA, долг, проценты, DSCR, ND/EBITDA)."""
import datetime as dt
import os
import openpyxl
from openpyxl.utils import get_column_letter

VARIANTS = {
    "quarterly_text": [f"{q} кв. {y}" for y in (2025, 2026, 2027) for q in (1, 2, 3, 4)],
    "monthly_dates": [dt.datetime(y, m, 28) for y in (2026, 2027) for m in range(1, 13)],
    "halfyear_mixed": [2024, 2025] + [f"{h}П{y}" for y in (2026, 2027, 2028) for h in (1, 2)],
    "quarterly_Q": [f"Q{q}'{str(y)[2:]}" for y in (2025, 2026, 2027) for q in (1, 2, 3, 4)],
}


def make(path, header, per_year=4):
    n = len(header)
    wb = openpyxl.Workbook()
    inp = wb.active
    inp.title = "Допущения"
    inp.append(["Показатель", "Ед."] + header)
    inp.append([])
    inp.append(["Цена продукции", "руб/т"] + [10000 * (1.01 ** i) for i in range(n)])
    inp.append(["Объём продаж", "тыс. т"] + [25 + i * 0.5 for i in range(n)])
    inp.append(["Ключевая ставка", "%"] + [0.16 - 0.003 * i for i in range(n)])
    inp.append(["Себестоимость, доля выручки", "%"] + [0.62] * n)
    c = wb.create_sheet("Расчёт")
    c.append(["Показатель", "Ед."] + header)
    cols = [get_column_letter(3 + i) for i in range(n)]
    c.append(["Выручка", "млн руб."] + [f"='Допущения'!{x}3*'Допущения'!{x}4/1000" for x in cols])
    c.append(["Себестоимость", "млн руб."] + [f"={x}2*'Допущения'!{x}6" for x in cols])
    c.append(["EBITDA", "млн руб."] + [f"={x}2-{x}3" for x in cols])
    c.append(["Долг", "млн руб."] + [600 - 10 * i for i in range(n)])
    c.append(["Проценты", "млн руб."] + [f"={x}5*('Допущения'!{x}5+0.03)/{per_year}" for x in cols])
    c.append(["Погашение долга", "млн руб."] + [100 / per_year] * n)
    c.append(["DSCR", "x"] + [f"={x}4/({x}6+{x}7)" for x in cols])
    c.append(["Net Debt / EBITDA", "x"] + [f"={x}5/({x}4*{per_year})" for x in cols])
    wb.save(path)
    return path


def build_all(out_dir):
    import shutil, subprocess
    os.makedirs(out_dir, exist_ok=True)
    so = shutil.which("soffice") or shutil.which("libreoffice")
    res = []
    for name, header in VARIANTS.items():
        per = 12 if "monthly" in name else (2 if "half" in name else 4)
        p = make(os.path.join(out_dir, f"{name}.xlsx"), header, per)
        if so:   # посчитать кэш значений
            subprocess.run([so, "--headless", "--convert-to", "xlsx", "--outdir", os.path.join(out_dir, "calc"), p],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
            p2 = os.path.join(out_dir, "calc", f"{name}.xlsx")
            p = p2 if os.path.exists(p2) else p
        res.append(p)
    return res


if __name__ == "__main__":
    print(build_all("/tmp/cf_periods"))
