"""Caller-ID health (C5): spot a number going bad, move traffic off it, and try unused numbers carefully.

Why within-window comparisons work: the dialler samples a caller ID independently for every dial, so in any
time window each number sees the same mix of people, hours and attempt numbers. Comparing a number's pickup
with the pooled pickup of the others in the *same window* is a randomised comparison, and time-of-day,
lead quality and the rest cancel out.

Caller ID only touches pickup, so engagement (the guardrail on pickup-primary workflows) can't be hurt by this.
"""
import copy
import math

FAST_WINDOW_H = 6       # catches a number that has just been flagged
SLOW_WINDOW_H = 96      # long enough that a number explored at 3% reaches MIN_DIALS on a ~2k dials/day workflow
MIN_DIALS = 200         # per number, and for the pool it's compared with, before we judge anything
BAD_Z = -3.0            # one-sided; ~0.1% false-alarm rate per look
BAD_REL_DROP = 0.15     # and the drop has to matter, not just be significant
OK_REL_DROP = 0.10      # an explored number graduates only if it's within 10% of the pool
OK_Z = -2.0
# Share for each number we have no evidence on. Small enough that trying a number which turns out to be
# spam-flagged (pickup ~half the others) costs well under 2% of the workflow's pickup.
EXPLORE_SHARE = 0.03
QUARANTINE_MIN = 72 * 60  # a flagged number sits out this long, then comes back on probation at EXPLORE_SHARE
MIN_CHANGE = 0.05       # don't churn config for smaller moves (total-variation distance)
SUPERVISOR_HOLD_MIN = 24 * 60

KIND = "caller_id"


def compare(stats, did, exclude=()):
    """Pickup of `did` vs everyone else in the same window -> (n, pickup, rel_diff, z) or None."""
    if did not in stats:
        return None
    d, c = stats[did]
    rd = sum(v[0] for k, v in stats.items() if k != did and k not in exclude)
    rc = sum(v[1] for k, v in stats.items() if k != did and k not in exclude)
    if d < MIN_DIALS or rd < MIN_DIALS or rc == 0:
        return None
    p1, p0 = c / d, rc / rd
    pool = (c + rc) / (d + rd)
    se = math.sqrt(pool * (1 - pool) * (1 / d + 1 / rd)) or 1e-9
    return {"n": d, "pickup": round(p1, 4), "others": round(p0, 4), "rel": round(p1 / p0 - 1, 4),
            "z": round((p1 - p0) / se, 2)}


def assess(options, fast, slow, current, quarantined=(), probation=None):
    """Label every number healthy / bad / unknown. Returns (verdicts, evidence).

    quarantined: numbers flagged recently; they stay bad whatever the data says (no flapping).
    probation: {number: (dials, connected) since it left quarantine}. Only post-release dials count for
    these, so the healthy history from before the incident can't wave a bad number back in.
    """
    probation = probation or {}
    verdicts, evidence = {}, {}
    for did in options:
        f = compare(fast, did, quarantined)
        s = compare({**slow, did: probation[did]} if did in probation else slow, did, quarantined)
        evidence[did] = {"fast": f, "slow": s, "share": round(current.get(did, 0), 3)}
        if did in quarantined or any(x and x["z"] <= BAD_Z and x["rel"] <= -BAD_REL_DROP for x in (f, s)):
            verdicts[did] = "bad"
        elif s and s["rel"] > -OK_REL_DROP and s["z"] > OK_Z:
            verdicts[did] = "healthy"
        elif did not in probation and current.get(did, 0) >= 0.2:
            verdicts[did] = "healthy"   # an incumbent we can't compare (e.g. the only number in use)
        else:
            verdicts[did] = "unknown"
    return verdicts, evidence


def target(verdicts):
    healthy = sorted(d for d, v in verdicts.items() if v == "healthy")
    unknown = sorted(d for d, v in verdicts.items() if v == "unknown")
    if not healthy:
        # nothing proven good: unproven numbers are a better bet than ones we know are bad
        healthy, unknown = unknown, []
    if not healthy:
        return None
    explore = min(EXPLORE_SHARE, 0.5 / max(len(unknown), 1))
    out = {d: explore for d in unknown}
    out.update({d: (1 - explore * len(unknown)) / len(healthy) for d in healthy})
    return out


def tv(a, b):
    return 0.5 * sum(abs(a.get(k, 0) - b.get(k, 0)) for k in set(a) | set(b))


def step_towards(cur, tgt, cap):
    """Move from cur towards tgt by at most `cap` total-variation distance."""
    dist = tv(cur, tgt)
    if dist <= cap:
        return dict(tgt)
    f = cap / dist
    out = {k: cur.get(k, 0) + (tgt.get(k, 0) - cur.get(k, 0)) * f for k in set(cur) | set(tgt)}
    return {k: round(v, 6) for k, v in out.items() if v > 1e-6}


def expected_pickup(dist, slow):
    """Dry run: what pickup would the last 48h of dials have had under `dist`?"""
    tot_d = sum(v[0] for v in slow.values())
    if not tot_d:
        return None
    pooled = sum(v[1] for v in slow.values()) / tot_d
    rate = {k: (v[1] / v[0] if v[0] >= MIN_DIALS else pooled) for k, v in slow.items()}
    return sum(w * rate.get(k, pooled) for k, w in dist.items())


def evaluate(minute, wid, wf, sim, store, gov):
    if gov.busy(wid, KIND, minute):
        return None
    cur = sim.get(f"/workflows/{wid}/config")
    cfg, version = cur["config"], cur["version"]
    options = wf["options"]["caller_id"]
    hour = minute // 60
    fast = store.did_window(wid, hour - FAST_WINDOW_H, hour)
    slow = store.did_window(wid, hour - SLOW_WINDOW_H, hour)
    default = cfg["allocations"]["default"]["caller_id"]

    state = store.get_meta(f"dids:{wid}", {"quarantined": {}, "probation": {}})
    quarantined, probation = state["quarantined"], state["probation"]
    log = []
    for d, m in list(quarantined.items()):
        if minute - m >= QUARANTINE_MIN:
            del quarantined[d]
            probation[d] = minute
            log.append(("did_released", f"caller ID {d} out of quarantine; back on probation at "
                                        f"{EXPLORE_SHARE:.0%} until it has {MIN_DIALS} fresh dials"))
    prob_stats = {d: store.did_window(wid, m // 60, hour).get(d, (0, 0)) for d, m in probation.items()}

    verdicts, evidence = assess(options, fast, slow, default, set(quarantined), prob_stats)
    for d, v in verdicts.items():
        if v == "bad" and d not in quarantined:
            quarantined[d] = minute
            probation.pop(d, None)
            e = evidence[d]["fast"] if evidence[d]["fast"] and evidence[d]["fast"]["z"] <= BAD_Z else evidence[d]["slow"]
            log.append(("did_flagged", f"caller ID {d} pickup {e['pickup']:.3f} vs {e['others']:.3f} for the other "
                                       f"numbers ({e['rel']:+.0%}, z={e['z']}, n={e['n']}); quarantined "
                                       f"{QUARANTINE_MIN // 60}h"))
        elif v == "healthy" and d in probation:
            del probation[d]
            log.append(("did_cleared", f"caller ID {d} passed probation ({evidence[d]['slow']})"))
    if log:
        with store.tx():
            store.set_meta(f"dids:{wid}", state)
            for kind, summary in log:
                store.log(minute, wid, kind, summary, evidence)
    known_bad = set(quarantined)

    tgt = target(verdicts)
    if tgt is None:
        return None
    bad_in_use = sorted(d for d in known_bad if any(
        (a.get(KIND) or {}).get(d, 0) > 0 for a in cfg["allocations"].values()))

    sup = store.one("SELECT MAX(minute) m FROM config_changes WHERE workflow_id=? AND actor='supervisor' "
                    "AND changed_dims LIKE '%caller_id%'", wid)["m"]
    supervisor_hold = sup is not None and minute - sup < SUPERVISOR_HOLD_MIN
    if supervisor_hold and not bad_in_use:
        return None  # they chose these numbers on purpose; only step in if one has gone bad

    keys = [k for k, a in cfg["allocations"].items() if KIND in a]
    shift = max(tv(cfg["allocations"][k][KIND], tgt) for k in keys)
    if shift < MIN_CHANGE and not bad_in_use:
        return None

    # stay a little under max_shift: the sim re-normalises weights and a step of exactly the cap can land a hair over
    cap = 0.0 if supervisor_hold or gov.auto_refused_recently(wid, minute) else gov.max_auto_shift(wid, KIND) - 0.01
    if cap > 0:
        route = "auto"
    else:
        why_not = gov.request_slot(wid, minute)
        if why_not:
            return None
        route, cap = "approval", 1.0

    new = copy.deepcopy(cfg)
    for k in keys:
        new["allocations"][k][KIND] = step_towards(cfg["allocations"][k][KIND], tgt, cap)
    new_default = new["allocations"]["default"][KIND]

    before, after = expected_pickup(default, slow), expected_pickup(new_default, slow)
    lift = (after / before - 1) if before and after else 0.0
    explored = sorted(d for d, v in verdicts.items() if v == "unknown")
    parts = []
    if bad_in_use:
        parts.append(f"move traffic off {', '.join(bad_in_use)} (pickup well below the other numbers)")
    if explored:
        parts.append(f"try {', '.join(explored)} at {new_default.get(explored[0], 0):.0%}"
                     f"{' each' if len(explored) > 1 else ''}")
    parts.append("spread the rest evenly across healthy numbers")
    summary = "Caller ID: " + "; ".join(parts)
    request = {
        "change_summary": summary,
        "diff": {f"allocations.{k}.caller_id": {"from": cfg["allocations"][k][KIND],
                                               "to": new["allocations"][k][KIND]} for k in keys},
        "affected_leads": affected_leads(store, wid, wf, minute),
        "dry_run": {"method": f"replay last {SLOW_WINDOW_H}h of dials with per-number observed pickup; "
                              "numbers without enough dials get the pooled rate",
                    "expected_pickup_current": before and round(before, 4),
                    "expected_pickup_proposed": after and round(after, 4),
                    "relative_change": round(lift, 4),
                    "not_modelled": "lower per-number load from spreading, which also protects pickup"},
        "impact_score": round(min(1.0, max(0.05, lift / 0.10)), 3),
        "rationale": ("Each dial picks its caller ID at random, so comparing numbers within the same window is a "
                      f"fair test. Flag at z<={BAD_Z} and a drop of {BAD_REL_DROP:.0%}+; numbers without evidence "
                      f"get {EXPLORE_SHARE:.0%} until they have {MIN_DIALS} dials. Only caller_id changes; "
                      "nothing a customer hears is touched."),
        "evidence": {"verdicts": verdicts, "per_number": evidence, "known_bad": sorted(known_bad)},
    }
    return gov.submit(minute, wid, KIND, version, new, request, route)


def affected_leads(store, wid, wf, minute):
    """Leads that can still be dialled once this applies."""
    if wf["mid_flight"] == "next_campaign_only":
        return int(wf["daily_batch"]["size"])  # running campaigns keep their config; only the next batch sees it
    days = len(wf["cadence"])
    r = store.one("SELECT COALESCE(SUM(leads),0) n FROM campaigns WHERE workflow_id=? AND minute>?",
                  wid, minute - days * 1440)
    return int(r["n"])
