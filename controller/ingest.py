"""Event ingestion (C1).

Each chunk is folded into hourly aggregates and the watermark moves forward in the same SQLite transaction,
so a crash mid-chunk leaves nothing behind and the chunk is simply re-read. That's the whole idempotency story.

We keep counts, not raw events: ~850k events/day on the load scenario would be ~1 GB/week as rows, while the
hourly aggregates are a few thousand rows a day.
"""
import json
import time
from collections import defaultdict

from .simtime import minute_of

WATERMARK = "last_chunk"


def pending_chunks(sim, store):
    after = store.get_meta(WATERMARK, -1)
    chunks = sim.get("/events/chunks", kind="history", after=after) + \
        sim.get("/events/chunks", kind="events", after=after)
    return sorted(chunks, key=lambda c: c["chunk"])


def ingest_pending(sim, store, stats=None):
    """Ingest every chunk past the watermark. Returns number of events read."""
    total = 0
    for ch in pending_chunks(sim, store):
        t = time.perf_counter()
        total += ingest_chunk(store, ch["chunk"], sim.lines(f"/events/chunks/{ch['chunk']}"))
        if stats is not None:
            stats["events"] += ch["events"]
            stats["seconds"] += time.perf_counter() - t
    return total


def ingest_chunk(store, chunk_no, lines):
    did = defaultdict(lambda: [0, 0])
    wf = defaultdict(lambda: [0, 0, 0, 0])
    campaigns, changes, decided = [], [], []
    n = 0
    for line in lines:
        if not line:
            continue
        n += 1
        if line.startswith('{"type":"lead"'):
            continue  # features aren't used by anything we built; skip the parse
        ev = json.loads(line)
        t = ev["type"]
        if t == "call":
            h = minute_of(ev["ts"]) // 60
            wid = ev["workflow_id"]
            c = ev["connected"]
            d = did[(wid, h, ev["variant"]["caller_id"])]
            d[0] += 1
            d[1] += c
            w = wf[(wid, h)]
            w[0] += 1
            w[1] += c
            w[2] += ev["engaged"]
            w[3] += bool(c and ev["qa"] and ev["qa"]["hallucination_flag"])
        elif t == "campaign_created":
            campaigns.append((ev["campaign_id"], ev["workflow_id"], minute_of(ev["ts"]), ev["leads"]))
        elif t == "config_changed":
            changes.append(ev)
        elif t == "approval_decided":
            decided.append(ev)

    with store.tx():
        if store.get_meta(WATERMARK, -1) >= chunk_no:
            return 0  # another run got here first
        store.db.executemany(
            "INSERT INTO did_hourly VALUES (?,?,?,?,?) ON CONFLICT DO UPDATE SET "
            "dials=dials+excluded.dials, connected=connected+excluded.connected",
            [(*k, *v) for k, v in did.items()])
        store.db.executemany(
            "INSERT INTO wf_hourly VALUES (?,?,?,?,?,?) ON CONFLICT DO UPDATE SET dials=dials+excluded.dials, "
            "connected=connected+excluded.connected, engaged=engaged+excluded.engaged, "
            "halluc_flags=halluc_flags+excluded.halluc_flags",
            [(*k, *v) for k, v in wf.items()])
        store.db.executemany("INSERT OR IGNORE INTO campaigns VALUES (?,?,?,?)", campaigns)
        for ev in changes:
            m = minute_of(ev["ts"])
            store.db.execute("INSERT OR IGNORE INTO config_changes VALUES (?,?,?,?,?,?,?)",
                             (ev["workflow_id"], ev["version"], m, ev["actor"], json.dumps(ev["changed_dims"]),
                              ev.get("proposal_id"), ev.get("approval_id")))
            if ev["actor"] == "supervisor":
                store.log(m, ev["workflow_id"], "supervisor_edit",
                          f"supervisor changed {', '.join(ev['changed_dims'])} (v{ev['version']})", ev)
        for ev in decided:
            store.log(minute_of(ev["ts"]), ev["workflow_id"], "approval_decided",
                      f"{ev['approval_id']} {ev['status']}", ev)
        store.set_meta(WATERMARK, chunk_no)
    return n
