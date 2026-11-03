"""Замер скорости пересчёта на вашей модели и Excel — без LLM и UI.

  python -m tests.e2e.bench_engine --model путь.xlsx [--n 5] [--passes 1]

Печатает: время открытия, затем для N сценариев (шок −10% по одному фактору) разбивку
запись / пересчёт Excel / чтение / откат. По ней видно, что тормозит: AppleScript-записи или сам расчёт Excel.
"""
import argparse, os, sys, time
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from app import config  # noqa: E402
config.load_dotenv(os.path.join(ROOT, ".env"))
from app.agent.session import Session  # noqa: E402
from app.agent.registry import TOOL_MAP  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--passes", default="1", help="проходов пересчёта для моделей без циклов (CF_CALC_PASSES)")
    a = ap.parse_args()
    os.environ["CF_CALC_PASSES"] = a.passes
    p = os.path.expanduser(a.model)
    s = Session(config.workdir())
    t0 = time.time()
    s.load(p, os.path.basename(p), config.cache_dir(), os.environ.get("CF_ENGINE"), progress=lambda t: print("  ", t))
    print(f"Открытие: {time.time() - t0:.1f} с · движок {s.engine.name} · итеративный: {s.idx.iterate}")
    from app.workbook import semantics as sm
    km = [{"sheet": m["candidates"][0]["sheet"], "row": m["candidates"][0]["row"], "name": m["metric"]}
          for m in s.card["key_metrics"] if m["candidates"]]
    jobs = [(d["label"], [{"sheet": d["sheet"], "row": d["row"], "op": "pct", "value": -10}]) for d in s.card["drivers"][: a.n]]
    jobs += [(f"фактор {f['id']} ({f['rows']} строк)", [{"factor": f["id"], "op": "pct", "value": -10}])
             for f in sm.summary(s.sem)["factors"][:3]]
    tot = 0
    for i, (label, shocks) in enumerate(jobs, 1):
        d = {"label": label}
        sc = [{"name": f"{label[:30]} −10%", "shocks": shocks}]
        t = time.time()
        try:
            TOOL_MAP["run_stress"]["fn"](s, {"scenarios": sc, "metrics": km[:4]}, lambda k, x: None)
            ok = "ok"
        except Exception as e:
            ok = f"ошибка: {str(e)[:120]}"
        dt = time.time() - t
        tot += dt
        print(f"{i}. {d['label'][:40]:40} {dt:5.1f} с  {getattr(s.engine, 'last_timing', '')}  калк {getattr(s.engine, 'calc_times', '')}  {ok}")
    print(f"Среднее на сценарий: {tot / max(1, len(jobs)):.1f} с")
    be = getattr(s.engine._e, "batch_error", None)
    print("Пакетная запись (osascript): " + (f"ОТКЛЮЧЕНА после ошибки: {be}" if be else "работает"))
    s.engine.close()


if __name__ == "__main__":
    main()
