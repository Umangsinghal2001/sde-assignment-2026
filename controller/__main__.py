"""
python -m controller run --url http://127.0.0.1:8700 [--db state/controller.db]
python -m controller why --workflow ks_cancel --at "D2 14:00"
python -m controller status [--at "D3 12:00"]
python -m controller timeline --workflow ks_cancel
"""
import argparse
import logging
import os

from . import audit
from .simtime import parse_label
from .store import Store


def main():
    ap = argparse.ArgumentParser(prog="controller")
    ap.add_argument("cmd", choices=["run", "why", "status", "timeline"])
    ap.add_argument("--url", default="http://127.0.0.1:8700")
    ap.add_argument("--db", default=os.environ.get("CONTROLLER_DB", "state/controller.db"))
    ap.add_argument("--workflow")
    ap.add_argument("--at", help='sim time, e.g. "D3 14:00"')
    a = ap.parse_args()

    if a.cmd == "run":
        from .loop import run
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        os.makedirs(os.path.dirname(a.db) or ".", exist_ok=True)
        run(a.url, a.db)
        return

    store = Store(a.db)
    if a.cmd == "why":
        print(audit.why(store, a.workflow, parse_label(a.at)))
    elif a.cmd == "timeline":
        print(audit.timeline(store, a.workflow))
    else:
        wfs = [r["workflow_id"] for r in store.q("SELECT DISTINCT workflow_id FROM wf_hourly ORDER BY 1")]
        minute = parse_label(a.at) if a.at else store.get_meta("finished") or \
            (store.one("SELECT MAX(hour) h FROM wf_hourly")["h"] + 1) * 60
        print(audit.status(store, wfs, minute))


if __name__ == "__main__":
    main()
