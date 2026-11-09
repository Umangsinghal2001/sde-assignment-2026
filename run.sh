#!/usr/bin/env bash
# Starts the controller and runs it until the scenario finishes. Python 3.11+ with requirements.txt installed.
# State lives in $CONTROLLER_DB (default state/controller.db). Rerunning with the same DB resumes after a crash;
# point it at a fresh path for a new simulator run.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
cd "$(dirname "$0")"
PY="${PYTHON:-python}"
[ -x .venv/bin/python ] && PY=.venv/bin/python
exec "$PY" -m controller run --url "$SIM_URL" --db "${CONTROLLER_DB:-state/controller.db}"
