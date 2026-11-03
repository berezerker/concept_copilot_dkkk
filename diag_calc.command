#!/bin/bash
# Диагностика: почему стресс не меняет метрику в Excel. Лог: tests/e2e/diag_calc.log
cd "$(dirname "$0")"
source .venv/bin/activate 2>/dev/null
M="${1:-$HOME/Documents/модели снаружи/bjs67r6v2hvp_depersonalized.xlsx}"
python -m tests.e2e.diag_calc --model "$M" 2>&1 | tee tests/e2e/diag_calc.log
