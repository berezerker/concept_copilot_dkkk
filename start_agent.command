#!/bin/bash
# Двойной щелчок в Finder: перезапускает агента (останавливает старый процесс на порту) и пишет журнал в server.log
cd "$(dirname "$0")"
PORT=$(grep -E '^CF_PORT=' .env 2>/dev/null | cut -d= -f2); PORT=${PORT:-8000}
OLD=$(lsof -ti tcp:$PORT 2>/dev/null); [ -n "$OLD" ] && kill $OLD && sleep 1
source .venv/bin/activate
echo "=== $(date) запуск ===" >> server.log
python run.py 2>&1 | tee -a server.log
