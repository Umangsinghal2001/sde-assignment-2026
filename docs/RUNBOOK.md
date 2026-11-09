# Controller runbook

For whoever is on call for the controller. Design and reasoning are in `SUBMISSION.md`.

## Run

```bash
./run.sh http://127.0.0.1:8700                      # state in state/controller.db
CONTROLLER_DB=state/other.db ./run.sh <url>         # separate state per simulator run
scripts/run_scenario.sh public_02_incidents 8702    # baseline + sim + controller + grade, one command
```

`run.sh` uses `.venv/bin/python` if it exists, otherwise `python` (3.11+, `requirements.txt`).

A DB belongs to one simulator run. Starting a new simulator with an old DB makes the controller think it has
already ingested chunks it hasn't. Use a fresh path per run.

## It crashed or was killed

Start it again with the same `CONTROLLER_DB`. It resumes from the last committed chunk and reconciles any
proposal that was mid-flight. Nothing else to do.

If it keeps crashing on the same step, the traceback says where. The usual causes:
- `SimError 4xx` from an endpoint we don't expect to fail: the simulator contract changed.
- `httpx.ConnectError`: the simulator is down. The controller retries 5 times with backoff, then exits.

## What is it doing?

```bash
python -m controller status                                   # one screen, at the latest time
python -m controller status --at "D3 12:00"                   # as it looked then
python -m controller timeline --workflow ks_cancel            # every decision, in order
python -m controller why --workflow ks_cancel --at "D2 12:30" # the live config at that time and why
```

The log prints one line per simulated day: events ingested, events/s, slowest step, peak memory, credits left.

Straight SQL works too (`sqlite3 state/controller.db`):
- `proposals`: our change requests and their status. Anything in `submitting` or `applying` for more than one
  step is stuck.
- `decisions`: the audit log.
- `meta`: `last_chunk` (ingest watermark), `dids:<workflow>` (quarantined and probation numbers).

## Code map

| file | what it does |
|---|---|
| `controller/loop.py` | the hourly loop: ingest, reconcile, decide, advance |
| `controller/ingest.py` | streams event chunks into hourly counts; one transaction per chunk |
| `controller/callerid.py` | caller-ID assessment, quarantine/probation, proposal building. Thresholds at the top. |
| `controller/governance.py` | auto-apply vs approval, request hours and budget, crash-safe send/follow/apply |
| `controller/audit.py` | `why`, `status`, `timeline` |
| `controller/store.py` | SQLite schema and helpers |
| `tests/test_controller.py` | runs the real simulator in-process over HTTP |
| `scripts/crash_test.py` | SIGKILL/restart test against a live simulator |

## Adding a new kind of change

Write an `evaluate(minute, wid, wf, sim, store, gov)` like `callerid.evaluate`: build the full proposed config
from the current one, pick the route with `gov.max_auto_shift` / `gov.request_slot`, and call `gov.submit`. Use a
new `kind` string so `gov.busy` keeps one open proposal per kind. Call it from the loop next to
`callerid.evaluate`. Governance, crash safety and audit come with `gov.submit`.

Note: approval budget is per workflow, not per kind. Two kinds of change competing for two requests a day should
be batched into one proposal; that batching isn't written yet.
