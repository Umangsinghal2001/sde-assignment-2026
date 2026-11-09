#!/usr/bin/env bash
# End to end on one scenario: baseline (if missing) -> sim server -> controller -> grade.
#   scripts/run_scenario.sh public_01_dev [port]
set -euo pipefail
cd "$(dirname "$0")/.."
NAME="$1"; PORT="${2:-8700}"
PY=.venv/bin/python
SC="scenarios/$NAME.yaml"
[ -f "runs/base_$NAME/_truth/aggregates.json" ] || $PY -m sim baseline --scenario "$SC" --run-dir "runs/base_$NAME"
rm -rf "runs/mine_$NAME" "state/$NAME.db" "state/$NAME.db-wal" "state/$NAME.db-shm"
mkdir -p state reports
$PY -m sim serve --scenario "$SC" --run-dir "runs/mine_$NAME" --port "$PORT" > "state/$NAME.sim.log" 2>&1 &
SIM=$!
trap 'kill $SIM 2>/dev/null || true' EXIT
until curl -sf "localhost:$PORT/health" > /dev/null; do sleep 0.5; kill -0 $SIM || { cat "state/$NAME.sim.log"; exit 1; }; done
CONTROLLER_DB="state/$NAME.db" ./run.sh "http://127.0.0.1:$PORT"
$PY -m sim grade --run "runs/mine_$NAME" --baseline "runs/base_$NAME" --out "reports/$NAME.md"
