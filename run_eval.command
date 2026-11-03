#!/bin/bash
# E2E-тест агента на ваших моделях с оценкой LLM-судьёй. Отчёт: tests/e2e/reports/<время>/report.md
cd "$(dirname "$0")"
source .venv/bin/activate 2>/dev/null
MODELS=("$HOME/Documents/модели снаружи/bjs67r6v2hvp_depersonalized.xlsx" "$HOME/Documents/модели снаружи/aps9fws6luy6_depersonalized.xlsx")
ARGS=(); for m in "${MODELS[@]}"; do [ -f "$m" ] && ARGS+=(--model "$m"); done
python -m tests.e2e.run_eval "${ARGS[@]}" --provider openai --judge openai --fresh "$@" 2>&1 | tee tests/e2e/last_run.log
open "$(ls -dt tests/e2e/reports/*/ | head -1)report.md" 2>/dev/null

