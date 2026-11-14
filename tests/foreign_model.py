"""«Чужая» модель для проверки универсальности: в ней нет ничего от привычного шаблона.

• листов Inputs / Outputs / Dashboard нет: допущения лежат на листах «База» и «Стресс» (два набора-близнеца,
  выбор — переключателем), параметры одной ячейкой — на листе «Параметры», расчёт — «Расчёт», итоги — «Свод»;
• выручка подписана «Поступления от продаж» (по словарю не находится — её должна сопоставить LLM или пользователь);
• ставка налога задана именованным диапазоном TaxRate, ставка по кредиту — параметр одной ячейкой;
• есть столбец с нумерацией строк левее подписей.
"""
import os, shutil, subprocess, sys, tempfile
import openpyxl
from openpyxl.workbook.defined_name import DefinedName
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

YEARS = list(range(2025, 2032))
N = len(YEARS)
FILLER = ["Индекс потребительских цен", "Индекс цен производителей", "Рост тарифов на электроэнергию", "Рост тарифов на газ",
          "Рост заработной платы", "Тариф на перевозку", "Ставка страховых взносов", "Норма амортизации", "Оборачиваемость запасов",
          "Оборачиваемость дебиторки", "Оборачиваемость кредиторки", "Курс доллара средний", "Курс юаня средний"]


def _scenario_sheet(wb, title, k):
    ws = wb.create_sheet(title)
    ws.append(["№", "Наименование", "Ед."] + YEARS)                                  # строка 1 — шапка
    ws.append([1, "Цена продукции", "руб/т"] + [12000 * k * 1.03 ** i for i in range(N)])      # 2
    ws.append([2, "Объём продаж", "тыс. т"] + [200 + 10 * i for i in range(N)])                # 3
    ws.append([3, "Удельные затраты", "руб/т"] + [7000 * 1.04 ** i for i in range(N)])         # 4
    ws.append([4, "Капитальные вложения", "млн руб."] + [300, 250, 200, 150, 150, 150, 150])   # 5
    for j, lab in enumerate(FILLER):                                                 # 6…18 — не участвуют в расчёте
        ws.append([5 + j, lab, "%"] + [round(0.04 + 0.001 * j, 4)] * N)
    return ws


def make_foreign(path):
    wb = openpyxl.Workbook()
    par = wb.active
    par.title = "Параметры"
    par.append(["Параметр", "Значение", "Комментарий"])                # 1
    par.append(["Номер сценария", 1, "1 — База, 2 — Стресс"])          # 2
    par.append(["Ставка налога на прибыль", 0.2, "доля"])              # 3  (именованный диапазон TaxRate)
    par.append(["Процентная ставка по кредиту", 0.15, "доля"])         # 4
    par.append(["Постоянные расходы", 250, "млн руб. в год"])          # 5
    wb.defined_names["TaxRate"] = DefinedName("TaxRate", attr_text="'Параметры'!$B$3")
    _scenario_sheet(wb, "База", 1.0)
    _scenario_sheet(wb, "Стресс", 0.8)
    c = wb.create_sheet("Расчёт")
    cols = [openpyxl.utils.get_column_letter(3 + i) for i in range(N)]          # C…I на расчётном листе
    src = [openpyxl.utils.get_column_letter(4 + i) for i in range(N)]           # D…J на листах сценариев
    c.append(["Статья", "Ед."] + YEARS)                                                                              # 1
    c.append(["Цена (выбранный сценарий)", "руб/т"] + [f"=CHOOSE('Параметры'!$B$2,'База'!{s}2,'Стресс'!{s}2)" for s in src])      # 2
    c.append(["Объём (выбранный сценарий)", "тыс. т"] + [f"=CHOOSE('Параметры'!$B$2,'База'!{s}3,'Стресс'!{s}3)" for s in src])    # 3
    c.append(["Удельные затраты (выбранный сценарий)", "руб/т"] + [f"=CHOOSE('Параметры'!$B$2,'База'!{s}4,'Стресс'!{s}4)" for s in src])  # 4
    c.append(["Поступления от продаж", "млн руб."] + [f"={x}2*{x}3/1000" for x in cols])                              # 5
    c.append(["Переменные затраты", "млн руб."] + [f"={x}4*{x}3/1000" for x in cols])                                 # 6
    c.append(["EBITDA", "млн руб."] + [f"={x}5-{x}6-'Параметры'!$B$5" for x in cols])                                 # 7
    c.append(["Долг", "млн руб."] + [1500 - 150 * i for i in range(N)])                                               # 8
    c.append(["Проценты к уплате", "млн руб."] + [f"={x}8*'Параметры'!$B$4" for x in cols])                            # 9
    c.append(["Чистая прибыль", "млн руб."] + [f"=({x}7-{x}9)*(1-TaxRate)" for x in cols])                            # 10
    c.append(["Погашение долга", "млн руб."] + [150] * N)                                                             # 11
    c.append(["Капвложения (выбранный сценарий)", "млн руб."] + [f"=CHOOSE('Параметры'!$B$2,'База'!{s}5,'Стресс'!{s}5)" for s in src])   # 12
    c.append(["Денежный поток после обслуживания долга", "млн руб."] + [f"={x}10-{x}11-{x}12" for x in cols])          # 13
    c.append(["Денежные средства на конец периода", "млн руб."] + [f"=100+{x}13" if i == 0 else f"={cols[i - 1]}14+{x}13" for i, x in enumerate(cols)])  # 14
    c.append(["DSCR", "x"] + [f"={x}7/({x}9+{x}11)" for x in cols])                                                   # 15
    sv = wb.create_sheet("Свод")
    sv.append(["Показатель", "Ед."] + YEARS)
    sv.append(["Поступления от продаж, млрд руб.", "млрд руб."] + [f"='Расчёт'!{x}5/1000" for x in cols])             # 2 — другие единицы
    sv.append(["EBITDA", "млн руб."] + [f"='Расчёт'!{x}7" for x in cols])                                             # 3
    sv.append(["Чистая прибыль", "млн руб."] + [f"='Расчёт'!{x}10" for x in cols])                                    # 4
    sv.append(["Долг / EBITDA", "x"] + [f"='Расчёт'!{x}8/'Расчёт'!{x}7" for x in cols])                               # 5
    sv.append(["DSCR", "x"] + [f"='Расчёт'!{x}15" for x in cols])                                                     # 6
    sv.append(["Денежные средства", "млн руб."] + [f"='Расчёт'!{x}14" for x in cols])                                 # 7
    wb.save(path)
    return path


def make_foreign_session():
    from app.agent.session import Session
    d = tempfile.mkdtemp()
    p = make_foreign(os.path.join(d, "foreign.xlsx"))
    so = shutil.which("soffice") or shutil.which("libreoffice")
    if so:                                           # посчитать формулы, чтобы в файле появились значения
        subprocess.run([so, "--headless", "--convert-to", "xlsx", "--outdir", os.path.join(d, "calc"), p],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        p2 = os.path.join(d, "calc", "foreign.xlsx")
        if os.path.exists(p2):
            p = p2
    s = Session(d)
    s.load(p, "foreign.xlsx", os.path.join(d, "cache"), os.environ.get("CF_ENGINE", "libreoffice"))
    return s
