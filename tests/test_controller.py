"""Controller tests. The simulator runs in-process behind FastAPI's TestClient, so the controller talks to it
over the same HTTP surface it uses in production. (Tests may import `sim`; the controller never does.)

    python -m pytest -q tests
"""
import copy
import json
import random

import pytest
from fastapi.testclient import TestClient

from controller import callerid
from controller.callerid import assess, compare, step_towards, target, tv
from controller.client import Sim
from controller.governance import Governance
from controller.ingest import ingest_chunk, ingest_pending
from controller.loop import run
from controller.store import Store
from sim import server
from sim.grader import grade
from sim.scenario import load_scenario
from sim.world import World


def make_world(tmp_path, scenario="public_01_dev", days=2, history_days=2):
    sc = load_scenario(f"scenarios/{scenario}.yaml")
    sc["days"], sc["history_days"] = days, history_days
    server._world = World(sc, tmp_path / "run")
    return server._world


@pytest.fixture()
def env(tmp_path):
    world = make_world(tmp_path)
    sim = Sim(http=TestClient(server.app))
    store = Store(str(tmp_path / "c.db"))
    wfs = {w["workflow_id"]: w for w in sim.get("/workflows")}
    return world, sim, store, Governance(sim, store, wfs), wfs


def advance_to(sim, minute):
    now = sim.get("/sim/state")["sim_minute"]
    if minute > now:
        sim.post("/sim/advance", {"minutes": minute - now})


def approval_body(sim, wid, pid="p-test", **over):
    cur = sim.get(f"/workflows/{wid}/config")
    cfg = copy.deepcopy(cur["config"])
    cfg["allocations"]["default"]["caller_id"] = {"D1": 0.4, "D2": 0.4, "D3": 0.2}
    body = {"proposal_id": pid, "workflow_id": wid, "base_version": cur["version"], "proposed_config": cfg,
            "change_summary": "test", "diff": {"x": 1}, "affected_leads": 1, "dry_run": {"x": 1},
            "impact_score": 0.1, "rationale": "test"}
    body.update(over)
    return body


def insert_proposal(store, body, route, status="submitting", minute=0, approval_id=None):
    with store.tx():
        store.db.execute(
            "INSERT INTO proposals (proposal_id, workflow_id, kind, route, base_version, status, approval_id, body, "
            "created_minute, updated_minute) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (body["proposal_id"], body.get("workflow_id", "nw_renewal"), "caller_id", route, body["base_version"],
             status, approval_id, json.dumps(body), minute, minute))


# ---------------------------------------------------------------- ingestion

def test_ingest_counts_match_the_raw_events(env):
    world, sim, store, *_ = env
    advance_to(sim, 24 * 60)
    ingest_pending(sim, store)
    raw = [json.loads(l) for ch in sim.get("/events/chunks", kind="events")
           for l in sim.text(f"/events/chunks/{ch['chunk']}").splitlines()]
    calls = [e for e in raw if e["type"] == "call"]
    got = store.one("SELECT SUM(dials) d, SUM(connected) c FROM wf_hourly WHERE hour >= 0")
    assert got["d"] == len(calls) and got["c"] == sum(e["connected"] for e in calls)
    by_did = store.one("SELECT SUM(dials) d FROM did_hourly WHERE hour >= 0")
    assert by_did["d"] == len(calls)


def test_crash_mid_chunk_leaves_nothing_and_rereads(env, monkeypatch):
    world, sim, store, *_ = env
    advance_to(sim, 12 * 60)
    chunks = sorted(sim.get("/events/chunks", kind="history") + sim.get("/events/chunks", kind="events"),
                    key=lambda c: c["chunk"])
    first = chunks[0]["chunk"]
    text = sim.text(f"/events/chunks/{first}")

    real = store.set_meta
    def boom(key, value):
        raise RuntimeError("killed")
    monkeypatch.setattr(store, "set_meta", boom)
    with pytest.raises(RuntimeError):
        ingest_chunk(store, first, text.splitlines())
    assert store.one("SELECT COUNT(*) n FROM wf_hourly")["n"] == 0
    monkeypatch.setattr(store, "set_meta", real)

    ingest_pending(sim, store)
    once = store.one("SELECT SUM(dials) d FROM wf_hourly")["d"]
    ingest_chunk(store, first, text.splitlines())   # replaying an already-ingested chunk is a no-op
    assert store.one("SELECT SUM(dials) d FROM wf_hourly")["d"] == once
    assert once == sum(1 for c in chunks for l in sim.text(f"/events/chunks/{c['chunk']}").splitlines()
                       if '"type":"call"' in l)


# ---------------------------------------------------------------- caller-ID statistics

def test_flags_a_clearly_worse_number():
    v, ev = assess(["D1", "D2", "D3"], fast={"D1": (1000, 300), "D2": (1000, 150), "D3": (1000, 290)},
                   slow={}, current={"D1": 0.34, "D2": 0.33, "D3": 0.33})
    assert v == {"D1": "healthy", "D2": "bad", "D3": "healthy"}
    assert ev["D2"]["fast"]["z"] < -3


def test_no_verdict_on_small_samples():
    # 40% worse but only 50 dials: not enough to act on
    v, _ = assess(["D1", "D2", "D3"], fast={"D1": (50, 15), "D2": (50, 9)}, slow={}, current={"D1": 0.03, "D2": 0.03})
    assert v == {"D1": "unknown", "D2": "unknown", "D3": "unknown"}


def test_small_but_significant_drop_is_not_flagged():
    # 8% worse with lots of data: statistically real, but under the 15% bar
    assert compare({"D1": (20000, 6000), "D2": (20000, 5520)}, "D2")["z"] < -3
    v, _ = assess(["D1", "D2"], fast={"D1": (20000, 6000), "D2": (20000, 5520)}, slow={}, current={"D1": .5, "D2": .5})
    assert v["D2"] != "bad"


def test_lone_incumbent_is_healthy_and_others_get_explored():
    v, _ = assess(["D1", "D2", "D3"], fast={"D1": (2000, 600)}, slow={"D1": (8000, 2400)}, current={"D1": 1.0})
    assert v == {"D1": "healthy", "D2": "unknown", "D3": "unknown"}
    t = target(v)
    assert t["D2"] == t["D3"] == callerid.EXPLORE_SHARE and abs(sum(t.values()) - 1) < 1e-9


def test_quarantine_beats_good_looking_history():
    # D2 was fine for days before it got flagged; the long window must not wave it back in
    slow = {"D1": (5000, 1500), "D2": (5000, 1500), "D3": (5000, 1500)}
    v, _ = assess(["D1", "D2", "D3"], fast={}, slow=slow, current={"D1": .5, "D3": .5}, quarantined={"D2"})
    assert v["D2"] == "bad"
    # on probation only post-release dials count: none yet -> unknown, not healthy
    v, _ = assess(["D1", "D2", "D3"], fast={}, slow=slow, current={"D1": .5, "D3": .5}, probation={"D2": (0, 0)})
    assert v["D2"] == "unknown"
    v, _ = assess(["D1", "D2", "D3"], fast={}, slow=slow, current={"D1": .5, "D3": .5}, probation={"D2": (300, 40)})
    assert v["D2"] == "bad"


def test_target_never_routes_to_bad_numbers():
    assert target({"D1": "bad", "D2": "healthy", "D3": "unknown"}) == {"D2": 1 - callerid.EXPLORE_SHARE,
                                                                       "D3": callerid.EXPLORE_SHARE}
    assert target({"D1": "bad", "D2": "unknown", "D3": "unknown"}) == {"D2": 0.5, "D3": 0.5}
    assert target({"D1": "bad"}) is None


def test_step_towards_never_exceeds_the_cap():
    rnd = random.Random(7)
    for _ in range(500):
        keys = ["D1", "D2", "D3", "D4"]
        a = {k: rnd.random() for k in keys}
        b = {k: rnd.random() for k in keys}
        a = {k: v / sum(a.values()) for k, v in a.items()}
        b = {k: v / sum(b.values()) for k, v in b.items()}
        cap = rnd.uniform(0.01, 0.5)
        s = step_towards(a, b, cap)
        assert tv(a, s) <= cap + 1e-5
        assert tv(s, b) <= tv(a, b) + 1e-9
        assert abs(sum(s.values()) - 1) < 1e-5


# ---------------------------------------------------------------- governance

def test_request_hours_and_budget(env):
    _, _, store, gov, _ = env
    # nw_kyc approvers work 10-18; we only send 11-17
    assert gov.request_slot("nw_kyc", 10 * 60) is not None
    assert gov.request_slot("nw_kyc", 11 * 60) is None
    assert gov.request_slot("nw_kyc", 17 * 60) is not None
    for i in range(2):
        insert_proposal(store, {"proposal_id": f"x{i}", "workflow_id": "nw_kyc", "base_version": 1}, "approval",
                        status="rejected", minute=11 * 60)
    assert "budget" in gov.request_slot("nw_kyc", 12 * 60)
    assert gov.request_slot("nw_kyc", 24 * 60 + 12 * 60) is None   # a day later the budget is back


def test_crash_after_approval_request_sent_is_not_resent(env):
    _, sim, store, gov, _ = env
    advance_to(sim, 11 * 60)
    body = approval_body(sim, "nw_renewal")
    sim.post("/approvals", body)                      # the request went out...
    insert_proposal(store, body, "approval")          # ...but we died before recording the response
    gov.reconcile(11 * 60)
    p = store.proposal(body["proposal_id"])
    assert p["status"] == "pending" and p["approval_id"]
    assert len(sim.get("/approvals", workflow_id="nw_renewal")) == 1


def test_crash_after_apply_does_not_double_apply(env):
    world, sim, store, gov, _ = env
    advance_to(sim, 11 * 60)
    body = approval_body(sim, "nw_renewal")
    a = sim.post("/approvals", body)
    insert_proposal(store, body, "approval", status="pending", approval_id=a["approval_id"])
    world.approvals[a["approval_id"]].update(_decision="approved", _reasons=[])  # take the approver's dice out of it
    while sim.get(f"/approvals/{a['approval_id']}")["status"] == "pending":
        sim.post("/sim/advance", {"minutes": 15})
    sim.post("/workflows/nw_renewal/apply", {"approval_id": a["approval_id"]})   # applied, then we crashed
    with store.tx():
        store.set_status(body["proposal_id"], "applying", 0)
    gov.reconcile(sim.get("/sim/state")["sim_minute"])
    assert store.proposal(body["proposal_id"])["status"] == "applied"
    hist = sim.get("/workflows/nw_renewal/config/history")
    assert sum(h["approval_id"] == a["approval_id"] for h in hist) == 1


def test_crash_after_auto_apply_is_recovered(env):
    _, sim, store, gov, _ = env
    cur = sim.get("/workflows/nw_renewal/config")
    cfg = copy.deepcopy(cur["config"])
    cfg["allocations"]["default"]["caller_id"] = {"D1": 0.45, "D2": 0.45, "D3": 0.1}
    body = {"proposal_id": "auto-1", "base_version": cur["version"], "proposed_config": cfg, "rationale": "t"}
    sim.put("/workflows/nw_renewal/config", body)
    insert_proposal(store, body, "auto")
    gov.reconcile(0)
    p = store.proposal("auto-1")
    assert p["status"] == "applied" and p["applied_version"] == cur["version"] + 1
    assert len(sim.get("/workflows/nw_renewal/config/history")) == cur["version"] + 1


def test_supervisor_caller_id_choice_is_left_alone(env):
    _, sim, store, gov, wfs = env
    advance_to(sim, 12 * 60)
    ingest_pending(sim, store)
    with store.tx():
        store.db.execute("INSERT INTO config_changes VALUES ('nw_renewal', 99, ?, 'supervisor', '[\"caller_id\"]', "
                         "NULL, NULL)", (11 * 60,))
    assert callerid.evaluate(12 * 60, "nw_renewal", wfs["nw_renewal"], sim, store, gov) is None
    # a day later we're allowed to suggest changes again (here: exploring the unused numbers)
    assert callerid.evaluate(36 * 60, "nw_renewal", wfs["nw_renewal"], sim, store, gov) is not None


def test_changes_keep_everything_we_dont_own(env):
    """We only ever touch caller_id. A supervisor's voice/prompt edits must survive our proposals."""
    world, sim, store, gov, wfs = env
    cur = sim.get("/workflows/nw_renewal/config")
    edited = copy.deepcopy(cur["config"])
    edited["allocations"]["default"]["voice"] = {"V2": 1.0}
    edited["prompt_version"] = "P3"
    world._apply(world.wfs["nw_renewal"], edited, "supervisor", None, None)
    advance_to(sim, 40 * 60)   # past the supervisor hold
    ingest_pending(sim, store)
    pid = callerid.evaluate(40 * 60, "nw_renewal", wfs["nw_renewal"], sim, store, gov)
    assert pid and store.proposal(pid)["status"] == "applied"
    now = sim.get("/workflows/nw_renewal/config")["config"]
    assert now["allocations"]["default"]["voice"] == {"V2": 1.0} and now["prompt_version"] == "P3"
    assert now["allocations"]["default"]["caller_id"] != edited["allocations"]["default"]["caller_id"]


# ---------------------------------------------------------------- whole loop

def test_kill_and_restart_finishes_cleanly(tmp_path):
    """Run to D1 12:00, drop every object (as a SIGKILL would), start again on the same DB, finish."""
    make_world(tmp_path, "public_02_incidents", days=4, history_days=3)
    http = TestClient(server.app)
    db = str(tmp_path / "c.db")
    run(http=http, db_path=db, stop_at_minute=36 * 60)
    store = run(http=http, db_path=db)

    log = [json.loads(l) for l in (tmp_path / "run" / "sim_log.jsonl").read_text().splitlines()]
    kinds = [r["kind"] for r in log]
    assert "sim_finished" in kinds
    assert kinds.count("apply_conflict") == 0 and kinds.count("auto_apply_rejected") == 0
    applied = [r["proposal_id"] for r in log if r["kind"] == "auto_applied"]
    assert len(applied) == len(set(applied))
    # every chunk ingested exactly once: our dial count equals the sim's own
    agg = json.loads((tmp_path / "run" / "_truth" / "aggregates.json").read_text())
    assert store.one("SELECT SUM(dials) d FROM wf_hourly WHERE hour >= 0")["d"] == sum(d["dials"] for d in agg["days"])
    # every config version the sim made is in our audit trail
    for wid in ("nw_renewal", "nw_kyc", "ks_cancel"):
        n = store.one("SELECT COUNT(*) n FROM config_changes WHERE workflow_id=?", wid)["n"]
        assert n == len(server._world.wfs[wid].versions) - 1


def test_full_public_02_run_is_never_harmful(tmp_path):
    """The flagged caller ID gets moved off within the day, and the grader judges nothing we did as harmful."""
    make_world(tmp_path, "public_02_incidents", days=5, history_days=7)
    run(http=TestClient(server.app), db_path=str(tmp_path / "c.db"))
    g = grade(str(tmp_path / "run"), str(tmp_path / "run"))
    assert g["controller_changes"]["verdicts"].get("harmful", 0) == 0
    assert g["governance"]["auto_apply_rejected"] == 0 and g["governance"]["lapsed"] == 0
    did = next(i for i in g["incidents"] if i["type"] == "did_flagged")
    assert did["flagged_did_share_by_day"][3] < 0.05   # D2 flagged at D2 10:00; by D3 it's (nearly) off
    for wid, peak in g["governance"]["peak_requests_per_24h"].items():
        assert peak <= 2


@pytest.mark.parametrize("start", [
    {"D1": 1 / 3, "D2": 1 / 3, "D3": 1 / 3},
    {"D1": 0.475, "D2": 0.475, "D3": 0.05},
    {"D1": 0.38, "D2": 0.38, "D3": 0.24},
])
def test_auto_apply_steps_are_accepted_at_the_cap(env, start):
    """ks_cancel allows a 0.2 shift. Steps that land exactly on the cap get refused by float noise; ours mustn't."""
    world, sim, store, gov, wfs = env
    cur = sim.get("/workflows/ks_cancel/config")
    cfg = copy.deepcopy(cur["config"])
    cfg["allocations"]["default"]["caller_id"] = start
    world._apply(world.wfs["ks_cancel"], cfg, "business_default", None, None)
    advance_to(sim, 12 * 60)
    ingest_pending(sim, store)
    with store.tx():   # D2 has just been flagged
        store.set_meta("dids:ks_cancel", {"quarantined": {"D2": 12 * 60}, "probation": {}})
    for h in range(4):
        m = (12 + h) * 60
        pid = callerid.evaluate(m, "ks_cancel", wfs["ks_cancel"], sim, store, gov)
        if pid is None:
            break
        assert store.proposal(pid)["status"] == "applied", store.proposal(pid)["note"]
    assert sim.get("/workflows/ks_cancel/config")["config"]["allocations"]["default"]["caller_id"].get("D2", 0) == 0
