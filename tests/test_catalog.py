"""Универсальность: «чужая» модель без привычных листов и подписей (tests/foreign_model.py).
Проверяется, что структура книги, справочник показателей и шоки работают без опоры на названия."""
import os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.agent.registry import TOOL_MAP
from app.workbook import catalog as cg
from tests.foreign_model import make_foreign_session

E = lambda k, d: None


def call(s, name, args):
    return TOOL_MAP[name]["fn"](s, args, E)


def table(r, i=0):
    return r["metrics"][i]["table"]


def test_structure():
    s = make_foreign_session()
    roles = {n: x.role for n, x in s.idx.sheets.items()}
    assert roles == {"Параметры": "inputs", "База": "inputs", "Стресс": "inputs", "Расчёт": "calc", "Свод": "report"}, roles
    # именованный диапазон TaxRate дал связь «чистая прибыль ← ставка налога»
    assert ("Параметры", 3) in s.idx.precedents[("Расчёт", 10)]
    # наборы-близнецы найдены по раскладке, выбранный — пробным пересчётом
    a = s.catalog["alt_sets"][0]
    assert a["sheets"] == ["База", "Стресс"] and a["active"] == ["База"] and a["detected"], a
    assert s.idx.inactive == {"Стресс"}
    # подпись параметра — без комментария справа от значения; нумерация слева — не параметр
    assert s.idx.sheets["Параметры"].rows[3].label == "Ставка налога на прибыль"
    # одиночные параметры попали в паспорт строк с факторами
    rows = s.sem["rows"]
    assert rows["Параметры!3"]["scalar"] and rows["Параметры!3"]["factor"] == "tax"
    assert rows["Параметры!4"]["factor"] == "rate" and "Стресс!2" not in rows
    return s


def test_catalog_and_shocks():
    s = test_structure()
    cat = s.catalog
    # показатели найдены по структуре: читаем со «Свода», считается в «Расчёте»
    e = cat["concepts"]["ebitda"]
    assert (e["primary"]["sheet"], e["origin"]["sheet"], e["origin"]["row"], e["shock"]) == ("Свод", "Расчёт", 7, "direct"), e
    assert cat["concepts"]["dscr"]["shock"] == "drivers"                 # коэффициент напрямую не задаётся
    assert cat["concepts"]["capex"]["origin"]["sheet"] == "База"         # «Капвложения (выбранный сценарий)» → допущение
    assert "revenue" in cat["missing"]                                   # «Поступления от продаж» по словарю не находятся

    # LLM сопоставляет ненайденное (здесь — эмулятор ответа); строка проходит ту же структурную проверку
    def chat(messages, tools, choice):
        import json
        rows = json.loads(messages[1]["content"])["rows"]
        rid = next(r["id"] for r in rows if "Поступления" in r["label"])
        call_ = types.SimpleNamespace(name="map_concepts", arguments={"mappings": [{"concept": "revenue", "row": rid, "confidence": 0.9}]})
        return types.SimpleNamespace(tool_calls=[call_], content="")
    client = types.SimpleNamespace(name="fake", chat=chat, describe=lambda: "fake")
    assert cg.llm_map(s.idx, cat, client, s.forecast_start) == 1
    rev = cat["concepts"]["revenue"]
    assert rev["match"] == "llm" and (rev["origin"]["sheet"], rev["origin"]["row"]) == ("Расчёт", 5) and rev["shock"] == "direct", rev
    s.refresh_card()

    # 1) шок по показателю напрямую: «выручка −10 %» — агент сам знает, где она
    r = call(s, "run_stress", {"scenarios": [{"name": "выручка −10%", "shocks": [{"concept": "выручка", "op": "pct", "value": -10}]}],
                               "metrics": [{"concept": "revenue"}, {"concept": "ebitda"}, {"concept": "dscr"}]})
    t0, t1 = table(r, 0), table(r, 1)
    assert abs(t0["выручка −10%"]["2027"] / t0["База"]["2027"] - 0.9) < 1e-3
    assert t1["выручка −10%"]["2027"] < t1["База"]["2027"]
    assert any("напрямую" in n for n in r["notes"])
    # затраты считаются от объёма, а не от выручки: прямой шок выручки до них не доходит — инструмент говорит об этом прямо
    try:
        call(s, "run_stress", {"scenarios": [{"name": "x", "shocks": [{"concept": "revenue", "op": "pct", "value": -10}]}],
                               "metrics": [{"sheet": "Расчёт", "row": 6}]})
        raise AssertionError("ожидалась ошибка: метрика не зависит от выручки")
    except ValueError as e:
        assert "не изменились" in str(e) and "Объём" in str(e)

    # 2) тот же шок через допущения (только ценами), с подгонкой до −10 % выручки
    r = call(s, "run_stress", {"scenarios": [{"name": "через цены", "shocks": [
        {"concept": "revenue", "mode": "drivers", "via": ["price_sales"], "op": "pct", "value": -10}]}],
        "metrics": [{"concept": "revenue"}]})
    t = table(r)
    assert abs(t["через цены"]["2028"] / t["База"]["2028"] - 0.9) < 0.005, t
    assert any("подобрано" in n for n in r["notes"])

    # 3) параметр одной ячейкой: поиск, шок строкой и шок фактором
    f = call(s, "search_rows", {"query": "ставка налога"})["rows"][0]
    assert (f["sheet"], f["row"], f.get("scalar")) == ("Параметры", 3, True) and f["cells"] == {"B3": 0.2}
    r = call(s, "run_stress", {"scenarios": [{"name": "налог 30%", "shocks": [{"sheet": "Параметры", "row": 3, "op": "set", "value": 0.3}]},
                                              {"name": "ставки +20%", "shocks": [{"factor": "rate", "op": "pct", "value": 20}]}],
                               "metrics": [{"concept": "net_income"}]})
    t = table(r)
    assert t["налог 30%"]["2027"] < t["База"]["2027"] and t["ставки +20%"]["2027"] < t["База"]["2027"]

    # 4) ковенант и запас прочности через concept (подстановка в любом инструменте)
    r = call(s, "covenant_check", {"rules": [{"concept": "dscr", "op": ">=", "threshold": 1.2}]})
    assert r["rules"][0]["ref"].startswith("Свод!6") or "Свод" in str(r["rules"][0]), r["rules"][0]
    r = call(s, "headroom", {"rule": {"concept": "dscr", "op": ">=", "threshold": 1.2},
                             "drivers": [{"factor": "price_sales"}, {"concept": "revenue"}]})
    assert len(r["drivers"]) == 2, r

    # 5) справочник: просмотр, свой показатель, закрепление сохраняется
    g = call(s, "get_catalog", {"query": "ставк"})
    assert any(p["ref"] == "Параметры!4" for p in g["parameters"]) and any(c["id"] == "revenue" for c in call(s, "get_catalog", {})["concepts"])
    r = call(s, "catalog_set", {"concept": "Переменные затраты", "sheet": "Расчёт", "row": 6})
    assert r["ok"] and cg.resolve(s, "переменные затраты")["origin"]["row"] == 6
    assert cg.load_pins(s.cache_dir, s.idx.file_hash)[r["concept"]]["row"] == 6
    try:
        call(s, "run_stress", {"scenarios": [{"name": "x", "shocks": [{"concept": "dscr", "mode": "direct", "op": "pct", "value": -10}]}],
                               "metrics": [{"concept": "dscr"}]})
        raise AssertionError("коэффициент нельзя шоковать напрямую")
    except ValueError as e:
        assert "коэффициент" in str(e)

    # модель после всех прогонов восстановлена
    base = s.engine.read(["'Расчёт'!C15:I15"])
    r = call(s, "run_stress", {"scenarios": [{"name": "y", "shocks": [{"concept": "revenue", "op": "pct", "value": -30}]}], "metrics": [{"concept": "dscr"}]})
    assert s.engine.read(["'Расчёт'!C15:I15"]) == base
    s.close()
    print("catalog ok: роли по структуре, наборы сценариев, именованные диапазоны, параметры одной ячейкой, шок по показателю")


if __name__ == "__main__":
    test_catalog_and_shocks()
