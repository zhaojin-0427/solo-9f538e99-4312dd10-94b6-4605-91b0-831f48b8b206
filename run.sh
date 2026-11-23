#!/usr/bin/env bash
# 本地（无 Docker 时）启动开发服务器：./run.sh
set -euo pipefail
cd "$(dirname "$0")"

export PYTHONPATH="$(pwd):${PYTHONPATH:-}"
export PORT="${PORT:-8000}"
export TIMETABLE_STATE_PATH="${TIMETABLE_STATE_PATH:-$(pwd)/data/timetable_state.json}"
exec python3 -m flask --app app.web run --host 0.0.0.0 --port "${PORT}"
