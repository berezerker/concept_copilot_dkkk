#!/bin/bash
# Собрать demo.html из последней записи (record_demo.command). Файл появится рядом: demo.html
cd "$(dirname "$0")"
source .venv/bin/activate 2>/dev/null
python -m tools.build_demo "$@" && open -R demo.html 2>/dev/null
read -p "Enter — закрыть"
