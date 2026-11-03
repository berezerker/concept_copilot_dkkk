"""Небольшая синтетическая модель для тестов (не содержит данных Банка):
Inputs (цена, объём, ставка) → Calc (выручка, EBITDA, долг, проценты, DSCR)."""
import os, sys, tempfile
import openpyxl
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

YEARS = list(range(2024, 2031))


def make_sample(path):
    wb = openpyxl.Workbook()
    inp = wb.active; inp.title = "Inputs"
    inp.append(["Показатель", "Ед."] + YEARS)
    inp.append([])
    inp.append(["Цена продукции", "руб/т"] + [10000 * 1.04 ** i for i in range(len(YEARS))])
    inp.append(["Объём продаж", "тыс. т"] + [100 + 5 * i for i in range(len(YEARS))])
    inp.append(["Ключевая ставка", "%"] + [0.16, 0.18, 0.15, 0.12, 0.10, 0.09, 0.09])
    inp.append(["Себестоимость, доля выручки", "%"] + [0.62] * len(YEARS))
    c = wb.create_sheet("Calc")
    c.append(["Показатель", "Ед."] + YEARS)
    cols = [openpyxl.utils.get_column_letter(3 + i) for i in range(len(YEARS))]
    c.append(["Выручка", "млн руб."] + [f"=Inputs!{x}3*Inputs!{x}4/1000" for x in cols])
    c.append(["Себестоимость", "млн руб."] + [f"={x}2*Inputs!{x}6" for x in cols])
    c.append(["EBITDA", "млн руб."] + [f"={x}2-{x}3" for x in cols])
    c.append(["Долг", "млн руб."] + [1200 - 100 * i for i in range(len(YEARS))])
    c.append(["Проценты", "млн руб."] + [f"={x}5*(Inputs!{x}5+0.03)" for x in cols])
    c.append(["Погашение долга", "млн руб."] + [100] * len(YEARS))
    c.append(["DSCR", "x"] + [f"={x}4/({x}6+{x}7)" for x in cols])
    c.append(["Net Debt / EBITDA", "x"] + [f"={x}5/{x}4" for x in cols])
    wb.save(path)
    return path


def make_sample_session():
    from app.agent.session import Session
    d = tempfile.mkdtemp()
    p = make_sample(os.path.join(d, "sample.xlsx"))
    # формулы надо посчитать, чтобы появились кэшированные значения: прогоняем через LibreOffice
    import subprocess, shutil
    so = shutil.which("soffice") or shutil.which("libreoffice")
    if so:
        subprocess.run([so, "--headless", "--convert-to", "xlsx", "--outdir", os.path.join(d, "calc"), p],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        p2 = os.path.join(d, "calc", "sample.xlsx")
        if os.path.exists(p2):
            p = p2
    s = Session(d)
    s.load(p, "sample.xlsx", os.path.join(d, "cache"), os.environ.get("CF_ENGINE", "libreoffice"))
    s.forecast_start = 2025
    return s
