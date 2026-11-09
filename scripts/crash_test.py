"""Kill the controller with SIGKILL at random moments, restart it, and check the run still ends correctly.

    .venv/bin/python scripts/crash_test.py [scenario] [kills]

Checks afterwards (from the simulator's run dir, which the controller never reads):
  - the scenario finished
  - no proposal was applied twice, no apply conflicts, no refused auto-applies
  - every approval request the simulator received is in our store (none lost), none stuck half-done
  - our dial count equals the simulator's own (every chunk ingested exactly once)
"""
import json
import os
import random
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv/bin/python")


def main(scenario="public_02_incidents", kills=8, port=8710, seed=1):
    rnd = random.Random(seed)
    run_dir, db = ROOT / f"runs/crash_{scenario}", ROOT / f"state/crash_{scenario}.db"
    subprocess.run(["rm", "-rf", str(run_dir), str(db), f"{db}-wal", f"{db}-shm"], check=True)
    db.parent.mkdir(exist_ok=True)
    sim = subprocess.Popen([PY, "-m", "sim", "serve", "--scenario", f"scenarios/{scenario}.yaml",
                            "--run-dir", str(run_dir), "--port", str(port)], cwd=ROOT,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    try:
        while True:
            try:
                httpx.get(url + "/health").raise_for_status()
                break
            except httpx.HTTPError:
                time.sleep(0.3)
        env = {**os.environ, "CONTROLLER_DB": str(db)}
        killed = 0
        while True:
            ctl = subprocess.Popen([str(ROOT / "run.sh"), url], cwd=ROOT, env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
            if killed < kills:
                # let the run get a random 2-30 sim hours further, then kill at a random instant after that
                goal = httpx.get(url + "/sim/state").json()["sim_minute"] + rnd.randint(2, 30) * 60
                while ctl.poll() is None and httpx.get(url + "/sim/state").json()["sim_minute"] < goal:
                    time.sleep(0.02)
                time.sleep(rnd.uniform(0, 0.05))
                if ctl.poll() is None:
                    os.killpg(ctl.pid, signal.SIGKILL)
                    ctl.wait()
                    killed += 1
                    st = httpx.get(url + "/sim/state").json()
                    print(f"kill {killed}: controller SIGKILLed at sim {st['ts']}")
                    continue
            err = ctl.communicate()[1].decode()
            if ctl.returncode != 0:
                print(err[-3000:])
                raise SystemExit(f"controller exited {ctl.returncode}")
            break
    finally:
        sim.terminate()
        sim.wait()

    log = [json.loads(l) for l in (run_dir / "sim_log.jsonl").read_text().splitlines()]
    kinds = [r["kind"] for r in log]
    agg = json.loads((run_dir / "_truth" / "aggregates.json").read_text())
    con = sqlite3.connect(db)
    applied = [r["proposal_id"] for r in log if r["kind"] == "auto_applied"] + \
              [r["approval_id"] for r in log if r["kind"] == "approval_applied"]
    requested = {r["proposal_id"] for r in log if r["kind"] == "approval_requested"}
    ours = {r[0] for r in con.execute("SELECT proposal_id FROM proposals WHERE route='approval'")}
    # a request can legitimately still be pending when the scenario ends; half-done states can't
    stuck = con.execute("SELECT COUNT(*) FROM proposals WHERE status IN ('submitting','applying')").fetchone()[0]
    our_dials = con.execute("SELECT SUM(dials) FROM wf_hourly WHERE hour >= 0").fetchone()[0]
    checks = {
        "scenario finished": "sim_finished" in kinds,
        "controller was killed mid-run": killed > 0,
        "no proposal applied twice": len(applied) == len(set(applied)),
        "no apply conflicts": kinds.count("apply_conflict") == 0,
        "no refused auto-applies": kinds.count("auto_apply_rejected") == 0,
        "no lost approval requests": requested <= ours,
        "no proposal stuck half-done": stuck == 0,
        "every dial ingested exactly once": our_dials == sum(d["dials"] for d in agg["days"]),
    }
    print(f"\n{scenario}: {killed} kills, {kinds.count('auto_applied')} auto-applied, "
          f"{kinds.count('approval_applied')} applied via approval, {len(requested)} approval requests, "
          f"dials ours={our_dials} sim={sum(d['dials'] for d in agg['days'])}")
    for k, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {k}")
    if not all(checks.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    a = sys.argv[1:]
    main(a[0] if a else "public_02_incidents", int(a[1]) if len(a) > 1 else 8)
