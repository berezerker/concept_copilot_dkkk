#!/bin/bash
# Скорость пересчёта в Excel на обеих обезличенных моделях (без LLM). Лог: tests/e2e/bench.log
cd "$(dirname "$0")"
source .venv/bin/activate 2>/dev/null
D="$HOME/Documents/модели снаружи"
{ for M in "${@:-$D/bjs67r6v2hvp_depersonalized.xlsx}" "$D/aps9fws6luy6_depersonalized.xlsx"; do
    echo "━━ $(basename "$M")"; python -m tests.e2e.bench_engine --model "$M" --n 5
  done; } 2>&1 | tee tests/e2e/bench.log
