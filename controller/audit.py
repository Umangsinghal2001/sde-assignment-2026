"""Answering questions from our own store (C12): "why did X change?" and "what's going on right now?"."""
import json

from .simtime import label


def why(store, wid, minute):
    """Explain the config that was live on `wid` at `minute`: who made it, why, who approved, what happened next."""
    ch = store.one("SELECT * FROM config_changes WHERE workflow_id=? AND minute<=? ORDER BY version DESC LIMIT 1",
                   wid, minute)
    out = [f"{wid} at {label(minute)}"]
    if not ch:
        return "\n".join(out + ["  running the business default config (no change seen yet)"])
    out.append(f"  live config: v{ch['version']}, set at {label(ch['minute'])} by {ch['actor']}, "
               f"changed {', '.join(json.loads(ch['changed_dims'])) or 'nothing'}")
    if ch["actor"] == "supervisor":
        out.append("  a client supervisor made this edit directly; it didn't go through us")
    elif ch["actor"] == "business_default":
        out.append("  business default config")
    p = store.proposal(ch["proposal_id"]) if ch["proposal_id"] else None
    if p:
        body = json.loads(p["body"])
        out.append(f"  proposal {p['proposal_id']} ({p['route']}), created {label(p['created_minute'])}")
        created = store.one("SELECT * FROM decisions WHERE proposal_id=? AND kind='proposal_created'", p["proposal_id"])
        req = json.loads(created["detail"]) if created else body
        out.append(f"  what: {req.get('change_summary', '')}")
        out.append(f"  why: {req.get('rationale', body.get('rationale', ''))}")
        if req.get("dry_run"):
            d = req["dry_run"]
            out.append(f"  dry run: pickup {d.get('expected_pickup_current')} -> {d.get('expected_pickup_proposed')} "
                       f"({d.get('relative_change', 0):+.1%}), affected leads {req.get('affected_leads')}, "
                       f"impact {req.get('impact_score')}")
        ev = req.get("evidence") or {}
        for did, e in (ev.get("per_number") or {}).items():
            s = e.get("fast") or e.get("slow")
            if s:
                out.append(f"    {did}: share {e['share']:.0%}, pickup {s['pickup']:.3f} vs {s['others']:.3f} "
                           f"({s['rel']:+.0%}, z={s['z']}, n={s['n']}) -> {ev['verdicts'].get(did)}")
        if p["route"] == "approval":
            dec = store.one("SELECT * FROM decisions WHERE kind='approval_decided' AND summary LIKE ?",
                            f"{p['approval_id']} %")
            who = f"approved by {wid}'s client approver at {label(dec['minute'])}" if dec else "approval record missing"
            out.append(f"  approval: {p['approval_id']}, {who}")
        else:
            out.append("  approval: auto-applied under the workflow's auto-apply policy (no human in the loop)")
    # what happened next: pickup per number in the 24h before vs after
    h = ch["minute"] // 60
    before, after = store.did_window(wid, h - 24, h), store.did_window(wid, h, h + 24)
    w0, w1 = store.wf_window(wid, h - 24, h), store.wf_window(wid, h, h + 24)
    out.append("  what happened next (24h before -> 24h after):")
    out.append(f"    pickup {_rate(w0['connected'], w0['dials'])} -> {_rate(w1['connected'], w1['dials'])}, "
               f"engagement {_rate(w0['engaged'], w0['connected'])} -> {_rate(w1['engaged'], w1['connected'])}")
    for did in sorted(set(before) | set(after)):
        b, a = before.get(did, (0, 0)), after.get(did, (0, 0))
        out.append(f"    {did}: {b[0]} dials @ {_rate(b[1], b[0])} -> {a[0]} dials @ {_rate(a[1], a[0])}")
    nxt = store.one("SELECT * FROM config_changes WHERE workflow_id=? AND version>? ORDER BY version LIMIT 1",
                    wid, ch["version"])
    if nxt:
        out.append(f"  superseded by v{nxt['version']} at {label(nxt['minute'])} ({nxt['actor']})")
    return "\n".join(out)


def status(store, wfs, minute):
    """One screen per workflow: KPIs, numbers, open requests, today's changes."""
    h = minute // 60
    out = [f"status at {label(minute)}"]
    for wid in wfs:
        w0, w1 = store.wf_window(wid, h - 48, h - 24), store.wf_window(wid, h - 24, h)
        out.append(f"\n{wid}")
        out.append(f"  pickup     {_rate(w1['connected'], w1['dials'])} (prev 24h {_rate(w0['connected'], w0['dials'])})")
        out.append(f"  engagement {_rate(w1['engaged'], w1['connected'])} (prev 24h {_rate(w0['engaged'], w0['connected'])})")
        out.append(f"  hallucination flags {_rate(w1['halluc_flags'], w1['connected'])} of connected calls")
        dids = store.did_window(wid, h - 24, h)
        # number states as of `minute`, replayed from the audit log (the live state in meta is "now")
        tag = {}
        for r in store.q("SELECT kind, summary FROM decisions WHERE workflow_id=? AND minute<=? AND kind IN "
                         "('did_flagged','did_released','did_cleared') ORDER BY id", wid, minute):
            did = r["summary"].split()[2]
            tag[did] = {"did_flagged": " QUARANTINED", "did_released": " PROBATION", "did_cleared": ""}[r["kind"]]
        out.append("  caller IDs (24h dials@pickup): " + ", ".join(
            f"{d} {v[0]}@{_rate(v[1], v[0])}{tag.get(d, '')}" for d, v in sorted(dids.items())))
        for p in store.q("SELECT * FROM proposals WHERE workflow_id=? AND created_minute<=? AND created_minute>? "
                         "AND route='approval' ORDER BY created_minute", wid, minute, minute - 1440):
            out.append(f"  asked approver: {label(p['created_minute'])} {p['approval_id'] or ''} -> now {p['status']}: "
                       f"{json.loads(p['body']).get('change_summary', '')}")
        for c in store.q("SELECT * FROM config_changes WHERE workflow_id=? AND minute>? AND minute<=? ORDER BY version",
                         wid, minute - 1440, minute):
            out.append(f"  changed: v{c['version']} at {label(c['minute'])} by {c['actor']}: {', '.join(json.loads(c['changed_dims']))}")
    return "\n".join(out)


def timeline(store, wid):
    rows = store.q("SELECT * FROM decisions WHERE workflow_id=? ORDER BY id", wid)
    return "\n".join(f"{label(r['minute']) if r['minute'] is not None else '-':>10}  {r['kind']:<18} {r['summary']}"
                     for r in rows)


def _rate(a, b):
    return f"{a / b:.3f}" if b else "  -  "
