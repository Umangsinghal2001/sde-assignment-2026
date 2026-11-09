"""Governance client (C9): the only code path that changes a workflow's config.

Every proposal is written to the store *before* it goes over the wire, keyed by its proposal_id. On restart,
anything left in `submitting` or `applying` is resent; the simulator's own idempotency (duplicate proposal_id,
apply only from `approved`) means a resend can't double-apply, and we reconcile from what it tells us.
"""
import json
import uuid

from .client import SimError

# Never ask a human more than this many times a day per workflow, whatever the policy allows
# (Kestrel's supervisor asked for 2; it's a sane cap for everyone).
MAX_REQUESTS_PER_DAY = 2
# Send requests only from 1h after approvers start to 1h before they stop. Keeps us out of
# standups and makes sure the approver sees it the same day.
REQUEST_MARGIN_H = 1
# After a rejection/expiry, leave that kind of change alone for a while before trying again with fresh data.
RETRY_COOLDOWN_MIN = 6 * 60


class Governance:
    def __init__(self, sim, store, workflows):
        self.sim, self.store, self.wfs = sim, store, workflows

    # ------------------------------------------------------------------ routing
    def can_auto_apply(self, wid, dims, shift):
        pol = self.wfs[wid]["governance"]["auto_apply"]
        return bool(pol["enabled"]) and set(dims) <= set(pol["allowed_dims"]) and shift <= pol["max_shift"] + 1e-9

    def max_auto_shift(self, wid, dim):
        pol = self.wfs[wid]["governance"]["auto_apply"]
        return pol["max_shift"] if pol["enabled"] and dim in pol["allowed_dims"] else 0.0

    def auto_refused_recently(self, wid, minute):
        """A refused auto-apply means our reading of the policy is off; go via a human for a day."""
        return self.store.one("SELECT 1 FROM proposals WHERE workflow_id=? AND route='auto' AND status='refused' "
                              "AND updated_minute>?", wid, minute - 1440) is not None

    def request_slot(self, wid, minute):
        """None if we may send an approval request now, otherwise the reason we can't."""
        gov = self.wfs[wid]["governance"]
        lo, hi = gov["approver_hours"]
        hour = (minute % 1440) // 60
        if not lo + REQUEST_MARGIN_H <= hour < hi - REQUEST_MARGIN_H:
            return f"outside request hours [{lo + REQUEST_MARGIN_H},{hi - REQUEST_MARGIN_H})"
        sent = self.store.one("SELECT COUNT(*) n FROM proposals WHERE workflow_id=? AND route='approval' "
                              "AND created_minute>?", wid, minute - 1440)["n"]
        if sent >= min(gov["max_requests_per_24h"], MAX_REQUESTS_PER_DAY):
            return f"request budget used ({sent} in 24h)"
        return None

    def busy(self, wid, kind, minute):
        """True if there's an open proposal of this kind, or a recent refusal we should respect."""
        if any(p["kind"] == kind for p in self.store.open_proposals(wid)):
            return True
        r = self.store.one("SELECT MAX(updated_minute) m FROM proposals WHERE workflow_id=? AND kind=? "
                           "AND status IN ('rejected','expired','refused')", wid, kind)
        return r["m"] is not None and minute - r["m"] < RETRY_COOLDOWN_MIN

    # ------------------------------------------------------------------ submit
    def submit(self, minute, wid, kind, base_version, config, request, route):
        """request: change_summary, diff, affected_leads, dry_run, impact_score, rationale, evidence."""
        pid = f"{wid}:{kind}:{uuid.uuid4().hex[:12]}"
        if route == "auto":
            body = {"proposal_id": pid, "base_version": base_version, "proposed_config": config,
                    "rationale": request["rationale"], "evidence": request.get("evidence")}
        else:
            body = {"proposal_id": pid, "workflow_id": wid, "base_version": base_version,
                    "proposed_config": config, **request}
        with self.store.tx():
            self.store.db.execute(
                "INSERT INTO proposals (proposal_id, workflow_id, kind, route, base_version, status, body, "
                "created_minute, updated_minute) VALUES (?,?,?,?,?,?,?,?,?)",
                (pid, wid, kind, route, base_version, "submitting", json.dumps(body), minute, minute))
            self.store.log(minute, wid, "proposal_created", request["change_summary"],
                           {k: v for k, v in request.items() if k != "proposed_config"}, pid)
        self._send(self.store.proposal(pid), minute)
        return pid

    def _send(self, p, minute):
        body = json.loads(p["body"])
        pid, wid = p["proposal_id"], p["workflow_id"]
        try:
            if p["route"] == "auto":
                r = self.sim.put(f"/workflows/{wid}/config", body)
                self._close(p, "applied", minute, "auto-applied", applied_version=r["version"])
            else:
                r = self.sim.post("/approvals", body)
                self._close(p, "pending", minute, f"sent as {r['approval_id']}, expires {r['expires_at']}",
                            approval_id=r["approval_id"])
        except SimError as e:
            if e.code == "duplicate_proposal_id":
                self._recover_duplicate(p, minute)
            elif e.code == "stale_base_version":
                self._close(p, "stale", minute, "config moved under us before submit; will re-propose")
            else:
                self._close(p, "refused", minute, f"{e.status} {e.code}: {e.body.get('detail') if isinstance(e.body, dict) else e.body}")

    def _recover_duplicate(self, p, minute):
        """We crashed after the sim accepted this proposal. Find out what became of it."""
        wid, pid = p["workflow_id"], p["proposal_id"]
        if p["route"] == "auto":
            v = next(h for h in self.sim.get(f"/workflows/{wid}/config/history") if h["proposal_id"] == pid)
            self._close(p, "applied", minute, "recovered after restart", applied_version=v["version"])
        else:
            a = next(a for a in self.sim.get("/approvals", workflow_id=wid) if a["proposal_id"] == pid)
            self._close(p, "pending", minute, "recovered after restart", approval_id=a["approval_id"])
            self._follow(self.store.proposal(pid), minute)

    def _close(self, p, status, minute, note, **cols):
        with self.store.tx():
            self.store.set_status(p["proposal_id"], status, minute, note, **cols)
            self.store.log(minute, p["workflow_id"], f"proposal_{status}", note, cols, p["proposal_id"])

    # ------------------------------------------------------------------ follow-up
    def reconcile(self, minute):
        for p in self.store.open_proposals():
            if p["status"] == "submitting":
                self._send(p, minute)
            else:
                self._follow(p, minute)

    def _follow(self, p, minute):
        if p["route"] != "approval" or not p["approval_id"]:
            return
        a = self.sim.get(f"/approvals/{p['approval_id']}")
        st = a["status"]
        if st == "pending":
            return
        if st in ("rejected", "expired", "lapsed"):
            self._close(p, st, minute, "; ".join(a.get("reasons") or []) or st)
        elif st == "applied":
            # we applied it and crashed before writing that down
            v = next(h for h in self.sim.get(f"/workflows/{p['workflow_id']}/config/history")
                     if h["approval_id"] == p["approval_id"])
            self._close(p, "applied", minute, "applied (recovered after restart)", applied_version=v["version"])
        elif st == "approved":
            self._apply(p, minute, a)

    def _apply(self, p, minute, a):
        if p["status"] != "applying":
            self._close(p, "applying", minute, f"approved at {a.get('decided_at')}, applying")
        try:
            r = self.sim.post(f"/workflows/{p['workflow_id']}/apply", {"approval_id": p["approval_id"]})
            self._close(p, "applied", minute, f"applied as v{r['version']}", applied_version=r["version"])
        except SimError as e:
            if e.code == "stale_base_version":
                self._close(p, "stale", minute, "config changed after approval (supervisor edit?); will re-propose")
            elif e.status == 410:
                self._close(p, "lapsed", minute, "missed the apply window")
            elif e.code == "not_approved":
                self._follow(p, minute)  # someone else moved it on; look again
            else:
                raise
