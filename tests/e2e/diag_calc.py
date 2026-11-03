"""Почему стресс не меняет метрику? Диагностика пересчёта в Excel (без LLM, ~2–5 мин).

  python -m tests.e2e.diag_calc --model путь.xlsx

Шокирует главный ценовой фактор −10% и показывает изменение КАЖДОЙ ключевой метрики (все кандидаты карточки)
в режимах пересчёта: calc (обычный) и full (CalculateFull), с 1 и 3 проходами. Плюс — включены ли итерации в Excel.
Если в LibreOffice/full метрика меняется, а в calc — нет, виноват режим пересчёта Excel.
"""
import argparse, os, sys, time
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from app import config  # noqa: E402
config.load_dotenv(os.path.join(ROOT, ".env"))
from app.agent.session import Session  # noqa: E402
from app.agent.registry import TOOL_MAP  # noqa: E402
from app.workbook import semantics as sm  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--factor", default=None, help="id фактора (по умолчанию — ценовой с наибольшим числом строк)")
    a = ap.parse_args()
    p = os.path.expanduser(a.model)
    s = Session(config.workdir())
    s.load(p, os.path.basename(p), config.cache_dir(), os.environ.get("CF_ENGINE"))
    e = s.engine._e
    print(f"Движок: {s.engine.name} · модель итеративная: {s.idx.iterate} · итерации в Excel: "
          f"{getattr(e, 'iteration_on', '—')} {getattr(e, 'iteration_error', '')}")
    facs = sm.summary(s.sem)["factors"]
    fid = a.factor or next((f["id"] for f in facs if f["id"].startswith("price")), facs[0]["id"])
    metrics = [dict(sheet=c["sheet"], row=c["row"], name=f"{m['metric']} {c['sheet']}!{c['row']}")
               for m in s.card["key_metrics"] for c in m["candidates"]]
    print(f"Шок: фактор {fid} −10%; метрик: {len(metrics)}\n")
    res = {}
    for mode, passes in (("calc", "1"), ("calc", "3"), ("full", "1")):
        os.environ["CF_CALC_MODE"], os.environ["CF_CALC_PASSES"] = mode, passes
        t = time.time()
        try:
            res[(mode, passes)] = {}
            for i in range(0, len(metrics), 8):           # инструмент берёт до 8 метрик за раз
                try:
                    r = TOOL_MAP["run_stress"]["fn"](s, {"scenarios": [{"name": "x", "shocks": [{"factor": fid, "op": "pct", "value": -10}]}],
                                                         "metrics": metrics[i:i + 8]}, lambda k, d: None)
                    res[(mode, passes)].update({m["metric"]: (m["summary"][0]["max_abs_delta"] if m["summary"] else 0) for m in r["metrics"]})
                except ValueError as ex:                  # «метрики не изменились» — это и есть ноль
                    res[(mode, passes)].update({f"{m['sheet']}!{m['row']} {m['name']}": 0 for m in metrics[i:i + 8]})
                    print("   ", str(ex)[:160])
            print(f"{mode}/{passes} прохода: {time.time() - t:.1f} с · {getattr(e, 'last_timing', '')} · калк {getattr(e, 'calc_times', '')}")
        except Exception as ex:
            print(f"{mode}/{passes}: ошибка {str(ex)[:300]}")
    print("\nМаксимальное изменение метрики по годам:")
    keys = list(res)
    print(f"{'метрика':55}" + "".join(f"{k[0]}/{k[1]:>8}" for k in keys))
    for m in next(iter(res.values()), {}):
        print(f"{m[:55]:55}" + "".join(f"{res[k].get(m, 0):>12.4g}" for k in keys))
    s.engine.close()


if __name__ == "__main__":
    main()
