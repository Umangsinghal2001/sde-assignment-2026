"""The controller loop: ingest -> reconcile approvals -> decide -> advance, until the scenario ends.

Every step reads its state from the store or the simulator, so the loop can be killed anywhere and restarted.
"""
import logging
import resource
import sys
import time

from . import callerid
from .client import Sim, SimError
from .governance import Governance
from .ingest import ingest_pending
from .simtime import label
from .store import Store

log = logging.getLogger("controller")

STEP_MINUTES = 60   # approvals have a 2h apply window, so hourly is plenty and keeps round-trips low


def run(url=None, db_path="state/controller.db", http=None, stop_at_minute=None):
    sim = Sim(url, http=http)
    store = Store(db_path)
    wfs = {w["workflow_id"]: w for w in sim.get("/workflows")}
    gov = Governance(sim, store, wfs)
    stats = {"events": 0, "seconds": 0.0, "max_step_s": 0.0}
    started = time.perf_counter()
    if store.get_meta("started") is None:
        with store.tx():
            store.set_meta("started", sim.get("/sim/state")["sim_minute"])
            store.log(None, None, "controller_started", f"managing {', '.join(wfs)}")

    while True:
        t_step = time.perf_counter()
        ingest_pending(sim, store, stats)
        state = sim.get("/sim/state")
        minute = state["sim_minute"]
        gov.reconcile(minute)
        if state["finished"]:
            break
        for wid, wf in wfs.items():
            callerid.evaluate(minute, wid, wf, sim, store, gov)
        # wall time from "new events available" to "decisions made" for this step
        stats["max_step_s"] = max(stats["max_step_s"], time.perf_counter() - t_step)
        if stop_at_minute is not None and minute >= stop_at_minute:
            return store
        try:
            # no automatic retry: a retried advance after a lost response would skip an hour
            sim.post("/sim/advance", {"minutes": STEP_MINUTES}, retry=False)
        except SimError as e:
            if e.code != "finished":
                raise
        if minute % 1440 == 0:
            eps = stats["events"] / stats["seconds"] if stats["seconds"] else 0
            log.info("%s  ingested %d events (%.0f/s), slowest step %.1fs, peak rss %.0fMB, credits %.0f, wall %.0fs",
                     label(minute), stats["events"], eps, stats["max_step_s"], peak_rss_mb(),
                     state["credits_remaining"], time.perf_counter() - started)

    with store.tx():
        if store.get_meta("finished") is None:
            store.set_meta("finished", minute)
            store.log(minute, None, "controller_finished", "scenario finished",
                      {"events": stats["events"], "ingest_seconds": round(stats["seconds"], 1),
                       "max_step_seconds": round(stats["max_step_s"], 2), "peak_rss_mb": round(peak_rss_mb()),
                       "wall_seconds": round(time.perf_counter() - started, 1)})
    log.info("finished at %s", label(minute))
    return store


def peak_rss_mb():
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 1e6 if sys.platform == "darwin" else r / 1e3   # bytes on macOS, KB on Linux
