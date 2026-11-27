"""Локальная Excel-проверка реальной обезличенной модели, без LLM и сети.

python -m tests.e2e.verify_transaction --model путь.xlsx
"""
import argparse
import json
import os
import time

from app import config
from app.agent.session import Session
from app.agent.registry import TOOL_MAP
from app.agent.tools import _metric_refs
from app.workbook.transaction import same_values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--mode", choices=("default", "direct"), default="default",
                        help="direct явно проверяет временную подмену формул; default использует выбор каталога")
    args = parser.parse_args()
    config.load_dotenv()
    s = Session(config.workdir())
    start = time.monotonic()
    try:
        s.load(args.model, os.path.basename(args.model), config.cache_dir(), "excel", progress=print)
        s.require_engine()
        metrics = [{"concept": key} for key in ("ebitda", "dscr", "net_debt_ebitda") if key in s.catalog["concepts"]]
        assert metrics, "Нет контрольных показателей"
        refs = [ref for m in _metric_refs(s, metrics, None) for ref in m["refs"]]
        baseline = s.engine.read(refs)
        mode = {"mode": "direct"} if args.mode == "direct" else {}
        result = TOOL_MAP["run_stress"]["fn"](s, {
            "scenarios": [
                {"name": "Выручка -10%", "shocks": [{"target": "metric:revenue", "op": "pct", "value": -10, **mode}]},
                {"name": "CAPEX +20%", "shocks": [{"target": "metric:capex", "op": "pct", "value": 20, **mode}]},
            ], "metrics": metrics,
        }, lambda kind, data: print(data.get("text", ""), flush=True) if kind == "progress" else None)
        assert same_values(s.engine.read(refs), baseline), "База отличается после выполнения инструмента"
        assert s.engine.last_transaction["status"] == "verified"
        assert all(r.get("restored") for r in s.engine.transactions), s.engine.transactions
        print(json.dumps({"status": "PASS", "mode": args.mode, "seconds": round(time.monotonic() - start, 1),
                          "transactions": s.engine.transactions, "metrics": result["metrics"],
                          "notes": result["notes"]}, ensure_ascii=False, indent=2), flush=True)
    finally:
        s.close()


if __name__ == "__main__":
    main()
