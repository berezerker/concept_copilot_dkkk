#!/bin/bash
# Запись сеанса для интерактивного демо. Пройдите сценарий в открывшемся интерфейсе, затем закройте это окно
# (Ctrl+C) и запустите build_demo.command — получится demo.html.
cd "$(dirname "$0")"
source .venv/bin/activate 2>/dev/null
PORT=${CF_PORT:-8000}
lsof -ti tcp:$PORT | xargs kill 2>/dev/null; sleep 1
export CF_RECORD=1
export CF_RECORD_DIR="$PWD/workspace/demo_rec/$(date +%Y%m%d_%H%M%S)"
echo "Запись идёт в $CF_RECORD_DIR"
python run.py 2>&1 | tee server.log
