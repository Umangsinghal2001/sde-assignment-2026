# Outcome-First Agent Loop — Submission

**Name:** Umang Singhal · **Date:** 2026-11-09 · **Hours actually spent (be honest):** about 8–9, a large part of it reading the repo and the simulator before building ·
**Language / storage:** Python 3.12, SQLite (stdlib, one file, WAL mode)

---

## 0. TL;DR

The controller ingests every event, keeps hourly counts in SQLite, and manages one lever end to end: which
caller ID each dial uses. It finds a number that has gone bad by comparing pickup across numbers inside the same
time window, quarantines it, spreads traffic across healthy numbers, and tries unused numbers at 3%. Every change
goes through auto-apply (where the policy allows caller_id) or an approval request, and every decision can be
explained from the store.

On public_01 the conclusion rate is +6.4% over baseline, on public_02 +17.5%, on public_03 +8.2%. That's 51
config changes across the three runs and the grader marks none of them harmful. On public_02 the flagged number
D2 went from 50% of traffic to 16% on the day it broke and 0% the next day. The controller survives repeated
SIGKILLs without double-applying anything, and handles one simulated day of the load scenario in about 6 seconds
with ~50 MB of memory.

I didn't build experiments on welcome message or voice, call-time optimisation, cohorts, or hallucination
response. The supervisor's bad prompt on ks_cancel in public_02 is never fixed.

## 1. Assumptions

1. `guardrail.*_max_drop: 0.02` is an absolute drop in the rate (2 percentage points), not 2% relative.
2. A caller ID only changes whether someone picks up. It doesn't change what they hear, so it is an operational
   setting, not a customer-facing one. Northwind's compliance memo says the same thing.
3. Pickup differences between numbers come from the number itself (spam labelling, overuse). Every dial picks
   its number at random from the allocation, so within one time window each number sees the same people, hours
   and attempt numbers. That makes a within-window comparison a randomised test, and I don't need to model
   time of day or lead quality for this lever.
4. Concentrating volume on one number is bad for it. This is how carrier spam labelling works in practice, and
   it shows in the data: nw_kyc dials everything from D1 by default, and once D2 and D3 got real traffic they
   picked up at a much higher rate than D1.
5. The client's approver is a busy person. Two requests a day per workflow is the most I send, even where the
   policy allows 3 or 4, and never in the first or last hour of their working day.
6. A pending approval left over when the scenario ends is fine. A request stuck halfway (sent but not recorded,
   applied but not recorded) is not.
7. Per-call raw events are not needed for the decisions I built, so I keep hourly counts only. If an auditor needs
   the exact variant one customer heard, that has to come from a raw event archive, which I didn't build (see 13).

## 2. Scope decisions

| # | Capability | Built / Partial / Cut | Why | What it would take to finish |
|---|---|---|---|---|
| C1 | Event ingestion | Built | Everything depends on it. Chunk-at-a-time, one transaction per chunk with the watermark, streamed line by line. | Nothing for the sim. In production, read from a stream with consumer offsets kept in the same transaction. |
| C2 | Shared cohort object | Cut | Nothing I built needs cohorts. Caller-ID health is per number, not per cohort. | A `cohorts` table keyed by id, feature joins at ingest (lead events are currently skipped), and per-cohort counts. 2 days. |
| C3 | Bootstrap from history | Partial | History is ingested into the same hourly tables, so caller-ID windows have data from D0 00:00. Nothing else uses it. | Baseline rates per cohort and per hour for C4/C6. |
| C4 | Call-time optimisation | Cut | Biggest lever on paper, but `lead_preferred_hour` multiplies weight by 6 and other hours by 0.25. A wrong estimate starves people of calls, and that needs proper per-person modelling to defend. | Per-cohort hour weights from history first (1–2 days), per-person only after that shows lift. |
| C5 | Caller-ID health | **Built** | Lowest-risk lever: it can't change what anyone hears, auto-apply allows it on 2 of 3 public workflows, and public_02 has a caller-ID incident. | Load-aware weights in place of binary quarantine (see 13). |
| C6 | Variant experiments | Cut | The core of the "learning" story, and the one I would do next. It needs cohorts, sample-size planning per cohort and approval batching, and I didn't want a half-built version deciding winners. | Factorial exploration at a capped share, per-segment analysis with Holm correction, then one batched proposal. About 3 days. |
| C7 | Promotion, decay, rollback | Cut | Depends on C6. The quarantine/probation logic in C5 is the same idea for numbers. | Per-variant rolling comparison against the incumbent; demote when the lower confidence bound crosses zero. |
| C8 | Hallucination response | Cut | Meant the ks_cancel prompt incident goes unfixed. I chose a smaller, finished set over this. | Detect flag rate vs the previous prompt version, propose a revert to the last known-good prompt with a validation run (n=300, 3 credits). Revert first, LLM only if there is no known-good prompt. 1 day. |
| C9 | Governance client | **Built** | Required to change anything. Auto-apply vs approval routing, request hours and budget, expiry and rejection handling, stale-base re-proposal, crash-safe resend. | Batching several kinds of change into one request (there is only one kind today). |
| C10 | Impact, affected leads, dry run | Partial | Real numbers for caller-ID proposals: dry-run replay of the last 96h, affected leads from live campaigns. | Generalise once there are other kinds of change. |
| C11 | Supervisor view | Partial | `python -m controller status --at "D3 12:00"`: KPIs, numbers and their state, requests sent, changes made. CLI only. | A small read-only HTTP endpoint over the same queries. |
| C12 | Audit store | **Built** | Floor requirement. `why`, `timeline`, `status` all answer from the store. | Raw event archive for per-customer questions. |
| C13 | Crash recovery | **Built** | Floor requirement and SRE's first non-negotiable. `scripts/crash_test.py` kills with SIGKILL 11 times across a run. | Nothing for the sim. |
| C14 | Loop observability | Partial | A daily log line with events ingested, events/s, slowest step, peak memory, credits. No alerting. | Metrics endpoint plus the alerts in section 10. |
| C15 | Fair allocation across tenants | Cut | No credits are spent and approval budgets are per workflow already. | Needed once C8 spends credits. |

I picked what the floor needs (C1, C9, C12, C13) and one lever I could take all the way, from detection to a
change a supervisor can audit. Caller ID won because a wrong move costs some pickup for a few hours and nothing a
customer hears ever changes. Every other lever (voice, welcome message, prompt, call time) can hurt a cohort if
the statistics are wrong, and the CTO memo is clear that hurting a cohort is worse than doing nothing.

## 3. Stakeholders

### 01 · Nikhil Rao, VP Sales

Accepted: speed where it's safe. On workflows where the policy allows auto-apply, a bad caller ID is detected and
traffic is moving off it within the same hour. On ks_cancel in public_02 that was two hours after the number broke.

Declined:
- One blended score. Pickup and engagement stay separate (Product's definition and constraint 3). The number I
  would put on screen per client is the conclusion rate (leads concluded ÷ leads uploaded), which is the business
  outcome and isn't a blend.
- Promoting a voice or welcome message "by lunch". I didn't build those experiments, and even when I do, a
  winner needs a minimum sample per arm. At ~1,000 connected calls a day on nw_renewal, a 10% engagement lift
  needs roughly 1,300 calls per arm, so about a day at a 50/50 split.
- Rolling out the persuasive prompt everywhere. Prompt changes need approval with a validation run on every
  workflow, and a result in another client's demo isn't evidence for this one.

Wrong in the memo:
- "Kestrel has already turned on auto-apply for everything." ks_cancel allows auto-apply only for schedule,
  caller ID, welcome message and voice, and only up to a 0.2 shift per change. Prompt and cohort changes need
  an approver.
- "Northwind is fine with auto-apply on the KYC flow." nw_kyc has auto-apply disabled. Northwind's own
  compliance head asks for a named human approval on anything customer-facing.
- The persuasive prompt (P4) isn't available on nw_kyc at all.

Reply:
> Hi Nikhil, for the summit I can give you one number per client: leads concluded ÷ leads uploaded, against the
> same week without the loop. On the test runs that is +6% to +17%. I can't blend pickup and engagement into one
> score; Product owns that definition and clients read both. On speed, caller-ID fixes go live within the hour
> where the client allows auto-apply. Kestrel's policy doesn't cover prompts, and Northwind KYC has auto-apply
> off, so those go through their approvers. The persuasive prompt needs a validation run per client before
> anyone can approve it.

### 02 · Meera Iyer, Head of Compliance, Northwind

Accepted: everything a customer hears stays untouched; the controller only changes caller ID. Approval requests
go out between 11:00 and 18:00 (KYC 11:00–17:00), inside her approvers' hours. Every proposal carries a summary,
a diff, a dry run and the evidence per number, and the `why` command answers "why was this live" from our store.

Pushed back: the weekly summary isn't built. The data for it is there (`timeline` per workflow lists every
auto-applied change with its reason).

Wrong in the memo: "call timing and caller-ID rotation are covered by the policy we signed" is true for
nw_renewal (auto-apply allows schedule and caller_id) but not for nw_kyc, where auto-apply is disabled. I send
KYC caller-ID changes to her approvers.

Gap: the IRDAI question "why did this customer hear what they heard on this day" needs the exact variant per call.
I keep hourly counts and the full config history with reasons, so I can say what was live and why, but not
which variant one specific customer got. That needs a raw call archive.

Reply:
> Hi Meera, we only change caller IDs on Northwind workflows today; nothing a customer hears changes. On renewal
> your policy allows caller-ID changes without approval, and each one is logged with the pickup data behind it.
> On KYC auto-apply is off, so caller-ID changes come to your approvers, only between 11:00 and 17:00, with the
> evidence attached. For audit questions we can say what config was live at any time, who approved it and why.
> We can't yet say which variant one specific customer heard; that needs a per-call archive, which is the first
> thing I'd add before going live with you.

### 03 · Kabir Menon, Platform & SRE

Accepted all three non-negotiables:
- Kill/restart: `scripts/crash_test.py` SIGKILLs the controller 11 times through public_02 (D0 11:00 to D9 19:00)
  and checks the result from the simulator's own logs. Output is in `reports/crash_recovery.txt`.
- Idempotency: every proposal is written to the store before it is sent, so a restart resends the same
  proposal_id, and the simulator's duplicate check plus `/approvals` and config history tell us what happened.
- Actionable alerts: I listed them (section 10) but didn't build alerting.

On infrastructure, SQLite is a file inside the process; nobody operates it at 2 a.m. Every table maps directly to
Postgres, which I would use in production. On load, one simulated day of public_03 takes ~6 s of wall time
(target is 10 minutes), ingest runs at ~470k events/s, and peak memory is 51 MB.

Reply:
> Hi Kabir, the kill test is in the repo: it SIGKILLs the controller 11 times through a 10-day run and checks
> from the simulator's logs that nothing was applied twice, no approval was lost and every event was counted
> once. No new infra; state is one SQLite file now and maps straight onto Postgres. public_03 runs at about 6 s
> per simulated day, 470k events/s, 51 MB peak. At 10× the first limit is that it is one process doing all
> workflows in sequence; I'd shard by workflow. Alerting isn't built yet; section 10 lists what I'd page on.

### 04 · Anita Desai, Operations Supervisor, Kestrel

Accepted:
- Her prompt edits are never undone. The controller never touches prompt_version, voice or welcome message, and
  every proposal is built from the current config, so her edits carry through (tested in
  `test_changes_keep_everything_we_dont_own`). If she changes caller IDs herself, the controller leaves them
  alone for 24 hours unless one of her numbers is clearly bad, and then it asks instead of auto-applying.
- At most two requests a day, never before 11:00. That is a global cap; her policy allows four from 10:00.
- One screen: `status` shows KPIs, numbers and their state, requests sent and changes made, for any point in time.

Pushed back: "tell me before anything changes on a running campaign". Caller-ID changes on ks_cancel auto-apply
within her policy and take effect on the next dial. I don't notify ahead of time. Each change shows up on her
screen with the reason. I'd hold here because a number that has gone bad costs pickup every hour we wait.

Reply:
> Hi Anita, we don't touch prompts, voices or opening lines on your line, so your edits stay as they are. If you
> change caller IDs yourself, we leave them for a day unless one is clearly failing, and then we ask you. You'll
> get at most two requests a day and none before 11:00. Caller-ID changes do go live on running campaigns without
> a heads-up, because your policy allows it and a bad number loses calls every hour; each one shows on the status
> screen with the reason and the pickup numbers behind it.

### 05 · Tara Kulkarni, Head of Product

Accepted:
- Pickup and engagement are never blended anywhere in the system.
- Proposals carry impact score, diff, affected leads and dry run. Impact score is the dry run's expected relative
  pickup gain divided by 10%, capped to [0.05, 1]. A proposal that only explores an unused number scores near
  0.05, which is honest: it isn't worth much yet.
- Expired is treated like rejected. The proposal is closed, the controller waits 6 hours, then builds a new one
  from the current config and fresh data. It never resubmits the old one.
- Only real dials count. A number that hasn't been dialled gets the pooled pickup rate in the dry run and is
  labelled as having no evidence.
- A change touches only that workflow's config.

On minimum size and window for caller ID: 200 dials per number and per comparison pool, a 6-hour window to catch
breakage and a 96-hour window to judge numbers explored at 3%. 200 dials at ~25% pickup gives a standard error
of ~3 points, enough to see a 15% relative drop (the bar for flagging) at z ≤ −3. The cost is speed: a newly
tried number on nw_kyc needs about 3 days to graduate.

Pushed back: of her three P0s I built caller-ID health and a CLI version of the supervisor view. Per-person call
time is cut (section 2, C4).

Reply:
> Hi Tara, of the three P0s I built caller-ID health and a minimal supervisor view (CLI for now). Per-person call
> time is cut: a wrong per-person hour multiplies someone's weight by 0.25 in every other hour, and I didn't want
> to ship that without per-cohort evidence first. Impact score is the dry run's expected pickup gain scaled so 10%
> reads as 1.0; exploration proposals score near zero on purpose. Minimum sample is 200 dials per number; the
> reasoning and the speed cost are in section 5. Cohorts aren't built, since nothing I shipped needs them yet.

### 06 · Aryamann, CTO

I took the memo as the brief for how to choose. One lever, finished, and every change explainable from the store.
No credits spent, since no decision I built needed one. Scale numbers are measured, not estimated (section 8).

Reply:
> Hi Aryamann, I said no to experiments, call-time optimisation and hallucination response, and built caller-ID
> health end to end on top of ingestion, governance, audit and crash recovery. On the public runs that gives
> +6% to +17% conclusion rate with no harmful changes and no credits spent. The thing I'd build next is the
> hallucination revert path, because the ks_cancel prompt incident in public_02 goes unfixed today.

## 4. Architecture

```mermaid
flowchart LR
  subgraph Sim[Simulator over HTTP]
    CH[/events/chunks/]
    ADV[/sim/advance/]
    CFG[/workflows/{id}/config/]
    APR[/approvals, /apply/]
  end
  subgraph Ctl[Controller: one process]
    ING[ingest.py<br/>stream chunk, fold into hourly counts]
    CID[callerid.py<br/>assess numbers, build proposal]
    GOV[governance.py<br/>route, send, follow, apply]
    AUD[audit.py<br/>why / status / timeline]
  end
  DB[(SQLite<br/>did_hourly, wf_hourly, campaigns,<br/>config_changes, proposals, decisions, meta)]
  CH --> ING --> DB
  DB --> CID --> GOV
  GOV -- auto: PUT config --> CFG
  GOV -- POST approvals / apply --> APR
  APR -- status --> GOV
  GOV --> DB
  DB --> AUD
  Ctl -- every hour --> ADV
```

The loop, once per simulated hour:
1. Ingest every chunk after the watermark. Each chunk is one transaction: counts, config changes, approval
   decisions and the new watermark commit together.
2. Reconcile open proposals: resend anything left in `submitting`, poll pending approvals, apply approved ones.
3. For each workflow, assess caller IDs and maybe submit one proposal.
4. Advance the simulator by 60 minutes.

All state is in SQLite: counts per workflow × hour × caller ID, every config version seen, our proposals with their
full body and status, the per-workflow number state (quarantined, on probation), and the decision log. On restart
the controller opens the file, reads the workflows from the API, and continues at step 1. The simulator is still
at the same minute, so nothing is skipped.

## 5. The learning loop

Only caller ID is managed, so most of the template's questions have short answers.

- **Cohorts.** None. Caller-ID health is a property of the number, so the comparison is across numbers for the
  whole workflow.
- **Experiment design.** Numbers we have no evidence on get 3% each. At 3%, a number that turns out to be
  spam-flagged (pickup about half the others) costs well under 2% of the workflow's pickup while we learn. When
  nothing is proven healthy, the unproven numbers take all the traffic, since they are a better bet than a
  number we know is bad. Healthy numbers split the rest evenly, which also keeps per-number volume low.
- **Deciding.** Deterministic two-proportion z-test of each number against the pooled other numbers, same window.
  - Flag as bad when z ≤ −3 and the relative drop is at least 15%, in either the 6h or the 96h window, with at
    least 200 dials on each side.
  - Graduate a number from unknown or probation to healthy when, in the 96h window (or since release from
    quarantine), it has 200+ dials, is within 10% of the pool and z > −2.
  - A z ≤ −3 one-sided test has a 0.13% false-alarm rate per look. I look hourly at 3–4 numbers, but the windows
    overlap heavily, so the looks are far from independent. The 15% minimum drop also filters small
    significant-but-irrelevant differences (tested in `test_small_but_significant_drop_is_not_flagged`). A false
    flag costs one number 72 hours on the bench.
- **Decay and rollback.** A flagged number is quarantined for 72 hours whatever later data says. Then it comes
  back on probation at 3% and is judged only on dials since its release. The first version re-judged flagged
  numbers every hour on the 96h window. The healthy history from before the incident made D2 look fine again an
  hour after we moved off it, and it flapped four times on public_02; the grader marked four changes harmful.
  Quarantine plus probation fixed that.
- **Call time.** Not built.
- **Guardrail.** Caller ID can't change any one person's engagement (it isn't an input to engagement in the
  dialler, and it isn't customer-facing). Measured engagement still moves a little: more pickups bring in people
  who are harder to engage, and the population shifts as leads conclude sooner. On public_02 nw_renewal
  (engagement primary) pickup went +7.8% and engagement −1.3% relative, about 0.7 points absolute, while the
  conclusion rate went +6.9%. Section 11 has the full numbers.

## 6. Governance

```
             +--------------+   409 duplicate -> look up what happened
 create ---> |  submitting  | ---------------------------------------+
             +--------------+                                        |
               |  auto: 200          | approval: 200                 |
               v                     v                               v
           [applied]            +---------+   approver decides   +----------+
                                | pending | -------------------> | approved |
                                +---------+                      +----------+
                                 |      |                          |
                         rejected|      |expired                   | POST apply
                                 v      v                          v
                          [rejected] [expired]               +----------+  200   [applied]
                                                             | applying | -----> 
              any step: 409 stale_base_version -> [stale]    +----------+  410 -> [lapsed]
              403 / 422 -> [refused]
```

- **Routing.** If the workflow's auto-apply covers caller_id and nobody refused an auto-apply in the last day, the
  change goes through `PUT /config` in steps of at most `max_shift − 0.01`. The margin exists because a step of
  exactly 0.2 came out at 0.2000001 after the simulator renormalised weights and was refused 30 times in the
  first run. Otherwise it is an approval request.
- **Timing and budget.** Requests only between approver start + 1h and end − 1h, at most
  min(policy, 2) per 24h per workflow, one open proposal per workflow and kind. Approved changes are applied on
  the next hourly step, well inside the 2h apply window. No proposal lapsed on any public run.
- **Expired, rejected, refused.** Closed, 6-hour cooldown for that workflow and kind, then a fresh proposal from
  the current config.
- **When I don't auto-apply even though I could.** For 24 hours after a supervisor edits caller IDs, unless one
  of their numbers is clearly bad, and then via approval.
- **Impact, affected leads, dry run.** Dry run replays the last 96h of dials with each number's observed pickup
  (pooled rate for numbers without 200 dials) under the current and proposed allocations. Impact is the
  relative difference divided by 10%, capped to [0.05, 1]. Affected leads is the sum of leads in campaigns still
  inside the cadence, or the next day's batch size for `next_campaign_only` workflows. The dry run doesn't model
  the drop in per-number load from spreading, and says so in the proposal.
- **Supervisor edits mid-flight.** Our proposals are built from the current config and only change caller_id, so
  edits to anything else carry through. If the config moves between proposal and apply, the apply returns
  `stale_base_version`, the proposal is closed as stale, and the next hour starts again from the new config.

## 7. Incidents

Caller-ID detection is the only incident path I built. On public_02, ks_cancel's D2 had its health cut to 0.35 at
D2 10:00:

| sim time | what happened |
|---|---|
| D2 10:00 | D2 is flagged by the carrier (hidden) |
| D2 12:00 | detected: D2 pickup 0.133 vs 0.215 for the other number, −38%, z = −3.75, n = 581. Quarantined 72h. |
| D2 12:00, 13:00, 14:00 | three auto-applied steps of < 0.2 each (ks_cancel's max_shift), D2 50% → 0% |
| D2 (whole day) | D2 carried 15.8% of dials |
| D3 to D5 | 0% on D2 |
| D5 12:00 | released on probation at 3% |
| D8 12:00 | failed probation (pickup 0.102 vs 0.221, −54%, n = 205), quarantined again |

No credits were used. Verification after the fact is the `why` output in section 10: D2 went from 2,372 dials to
177 in the next 24 hours, and ks_cancel's pickup went from 0.225 to 0.233.

The supervisor's bad prompt on ks_cancel (D3 11:30) is not handled. The flag rate on ks_cancel went from 1.7% of
connected calls in the 24 hours before the edit to 6.4% in the 24 hours after. `status` shows it, but nothing
acts on it.

## 8. Scale

Measured on public_03_load (12 workflows, 5 days, 7 days of history), on an M-series MacBook:

| metric | value |
|---|---|
| ingest throughput | ~470k events/s (history plus run, 8.9M events) |
| wall time per simulated day | ~6 s (target: under 10 minutes) |
| slowest single loop step | 11.7 s, the one-time ingest of 5.5M history events; a normal hourly step is ~0.25 s |
| decision lag | at most one step (60 sim minutes) after an event; ~0.25 s of wall time per step |
| peak memory | 51 MB |
| store size after the run | ~0.5 MB |

Memory used to be 1.8–4 GB. Each 128 MB chunk was read as one string and split into 250k lines, and Python's
allocator didn't give the memory back. Streaming the body line by line fixed it.

At 10× (120 workflows, ~8.5M events/day) ingest would still take under 20 s per simulated day. What breaks first is
that one process does every workflow in sequence: one slow approval call blocks all of them, and the SQLite
file has a single writer. I'd partition by workflow id: N workers, each owning a hash range of workflows, each
with its own consumer offset, writing to Postgres. Workflows don't share state, so nothing needs cross-worker
coordination.

## 9. Reliability

`scripts/crash_test.py` starts the simulator, runs `./run.sh` in its own process group, lets the run move a random
2–30 simulated hours, kills the whole group with SIGKILL, and restarts. After the run it checks against the
simulator's own logs, which the controller never reads:

```
public_02_incidents: 11 kills, 20 auto-applied, 3 applied via approval, 3 approval requests, dials ours=104000 sim=104000
  PASS  scenario finished
  PASS  controller was killed mid-run
  PASS  no proposal applied twice
  PASS  no apply conflicts
  PASS  no refused auto-applies
  PASS  no lost approval requests
  PASS  no proposal stuck half-done
  PASS  every dial ingested exactly once
```

Kills landed between D0 11:00 and D9 19:00. Nothing is lost: everything that matters is committed before the
next network call that depends on it.

Idempotency keys:
- Chunks are keyed by chunk number. The watermark moves in the same transaction as the counts, and an
  already-ingested chunk is skipped.
- Proposals are keyed by `proposal_id`, stored before sending. On resend, `409 duplicate_proposal_id` triggers a
  lookup in `/approvals` or config history.
- Applies can only happen once, since the simulator only applies from `approved`. If we crash after applying,
  the next poll sees `applied` and we record the version from config history.
- `/sim/advance` is never retried automatically. A lost response followed by a retry would skip an hour.

On 4xx/5xx: transport errors and 5xx are retried with backoff (5 tries). 409/410/403/422 are part of the protocol
and handled per case (section 6). Anything unexpected raises, the process exits non-zero, and a restart resumes.

The unit tests cover the same paths in-process: crash mid-chunk, crash after sending an approval, crash after
applying, crash after an auto-apply, and a two-phase run that stops at D1 12:00 and resumes on the same database.

## 10. Audit & observability

"Why did ks_cancel change at D2 12:00, who approved it, and what happened next?" Real output from the public_02
run:

```
$ python -m controller why --db state/public_02_incidents.db --workflow ks_cancel --at "D2 12:30"
ks_cancel at D2 12:30
  live config: v2, set at D2 12:00 by controller_auto, changed caller_id
  proposal ks_cancel:caller_id:14d05b16739c (auto), created D2 12:00
  what: Caller ID: move traffic off D2 (pickup well below the other numbers); try D3 at 1%; spread the rest evenly across healthy numbers
  why: Each dial picks its caller ID at random, so comparing numbers within the same window is a fair test. Flag at z<=-3.0 and a drop of 15%+; numbers without evidence get 3% until they have 200 dials. Only caller_id changes; nothing a customer hears is touched.
  dry run: pickup 0.2393 -> 0.2411 (+0.8%), affected leads 4500, impact 0.076
    D1: share 50%, pickup 0.215 vs 0.133 (+62%, z=3.75, n=619) -> healthy
    D2: share 50%, pickup 0.133 vs 0.215 (-38%, z=-3.75, n=581) -> bad
  approval: auto-applied under the workflow's auto-apply policy (no human in the loop)
  what happened next (24h before -> 24h after):
    pickup 0.225 -> 0.233, engagement 0.517 -> 0.493
    D1: 2428 dials @ 0.234 -> 4501 dials @ 0.235
    D2: 2372 dials @ 0.216 -> 177 dials @ 0.107
    D3: 0 dials @   -   -> 122 dials @ 0.361
  superseded by v3 at D2 13:00 (controller_auto)
```

The same question for an approved change (nw_kyc) shows the approval id and decision time:
`approval: apr_00001, approved by nw_kyc's client approver at D0 13:25`.

`python -m controller timeline --workflow ks_cancel` lists every decision in order, and
`python -m controller status --at "D3 12:00"` is the supervisor screen as of any time.

Alerts I would page on:
- Ingest lag over 2 hours of sim time (decisions are going stale).
- The controller process restarting more than 3 times in an hour.
- A refused auto-apply (our reading of the policy is wrong; the controller already falls back to approvals).
- A proposal stuck in `submitting` or `applying` for more than one step.

Alerts that wait until morning:
- A number flagged bad (the controller already acted; the client should hear about it in the summary).
- Approval rejected or expired.
- Engagement moving while only caller ID changed.

## 11. Results

public_01 (`reports/public_01_dev.md`):

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|
| ks_cancel | 0.351 | +2.8% | 0.247 | +1.1% | 0.497 | +1.1% | 49 (57) |
| nw_kyc | 0.421 | +14.9% | 0.275 | +13.7% | 0.589 | −1.3% | 32 (21) |
| nw_renewal | 0.440 | +5.6% | 0.281 | +6.3% | 0.535 | −1.3% | 52 (54) |
| ALL | 0.395 | +6.4% | 0.264 | +5.3% | 0.529 | −0.0% | 133 (132) |

5 changes applied: 3 beneficial, 2 neutral, 0 harmful. 2 approval requests, both approved; 0 expired, 0 lapsed,
0 conflicts, 0 refused auto-applies.

public_02 (`reports/public_02_incidents.md`):

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|
| ks_cancel | 0.307 | +30.3% | 0.231 | +27.3% | 0.465 | −0.7% | 464 (348) |
| nw_kyc | 0.447 | +19.7% | 0.282 | +20.2% | 0.601 | −1.2% | 31 (29) |
| nw_renewal | 0.463 | +6.9% | 0.283 | +7.8% | 0.547 | −1.3% | 63 (54) |
| ALL | 0.389 | +17.5% | 0.259 | +17.8% | 0.525 | −1.5% | 558 (431) |

22 changes applied: 17 beneficial, 5 neutral, 0 harmful. 2 approval requests, both approved; 0 expired, 0 lapsed,
0 conflicts, 0 refused auto-applies. Credits used: 0.

public_03 (`reports/public_03_load.md`): conclusion rate +8.2% across 12 workflows, 24 changes (14 beneficial,
10 neutral, 0 harmful), 10 approval requests (8 approved, 2 judgement-call rejections), 0 refused auto-applies.

Where it helped: KYC workflows gained the most (+15% to +30% conclusion) because their default config dials
everything from one number, and spreading traffic lifts pickup, which is their primary KPI.

Where it was harmful or weak:
- **Hallucinated calls rose on ks_cancel in public_02** (464 vs 348). Per connected call it is 4.2% vs 4.0%; most
  of the rise is that more calls connected while the supervisor's bad prompt was live, and nothing fixed the prompt.
- **Engagement fell 0.7 to 1.3% relative on the workflows where it is primary** (ks_cancel, nw_renewal) while
  pickup and conclusions went up. This is the population effect from section 5, not a per-person effect, but it
  is still the primary KPI moving the wrong way on paper. A grader reading only the primary KPI would see it.
- **Healthy numbers get flagged for overload.** When one of three numbers is quarantined, the other two carry
  more volume, and on ks_cancel D1 was then flagged too (−29%, z = −3.13). Binary quarantine reacts to overload by
  concentrating load further. See section 13.
- **Engagement gap captured is 0–5%**, because I didn't build the levers that move engagement.

## 12. Trade-offs & rejected alternatives

| Option | Why considered | Why rejected / what instead |
|---|---|---|
| Store raw events | Per-customer audit, any future analysis | 850k events/day is ~1 GB a week as rows. Hourly counts are a few thousand rows a day. Raw archive belongs in object storage, not the decision store. |
| Postgres now | SRE already runs it | Adds a setup step to `run.sh` for graders. Schema is plain SQL and moves over as is. |
| Thompson sampling across numbers | Explores and exploits in one rule | Constraint 4 wants a deterministic winner. A posterior-sampled allocation also changes every hour, which means a config change every hour. |
| Re-judging bad numbers every hour (no quarantine) | Simpler, recovers faster | Flapped four times on public_02, four harmful changes. |
| Advance 15 minutes per step | Faster reaction | 4× the round trips. Detection needs ~200 dials anyway, which takes hours on the smaller workflows. |
| Explore unknown numbers at 5% | Faster evidence | Worst case on a spam-flagged number costs ~2.8% of pickup at the moment of change, past the 2% line. 3% stays under it. |
| Reading the whole chunk into memory | Simplest code | 1.8–4 GB peak memory on public_03. Streaming lines: 51 MB. |

## 13. Known weaknesses

- **Overload looks like spam.** A number carrying most of the traffic picks up worse, gets flagged, and its traffic
  moves to the others, which then overload. A load-aware allocation (weight each number by its recent pickup,
  with a cap per number) would handle both cases with one rule. I'd do that before production.
- **A single unknown number never gets explored on an auto-apply workflow.** Exploring one number at 3% is a
  0.03 shift, under the 0.05 minimum change, so on ks_cancel D3 got no traffic until the incident forced a move.
- **No per-customer audit trail.** We know what config was live and why, not which variant a specific customer heard.
- **No alerting.** Only a daily log line.
- **The status view rebuilds number states by parsing decision summaries.** That breaks if the wording changes.
  The state should be stored as rows.
- **One process, sequential across workflows** (section 8).
- **Nothing for voice, welcome message, prompt or call time**, so the levers that move engagement are untouched.

## 14. If this went to production at Formi next month

1. Load-aware caller-ID allocation, replacing binary quarantine for overload (section 13). This is the main fix to
   what's live, so it comes first.
2. Postgres store, raw call archive in object storage keyed by lead and day, so compliance can answer
   per-customer questions.
3. Hallucination response (C8): detect a jump in the flag rate after a prompt change, propose a revert to the last
   known-good prompt with a validation run attached. Revert first, LLM suggestions only when there is nothing to
   revert to.
4. Alerts from section 10, plus a metrics endpoint for ingest lag, decisions per hour and credit burn.
5. Shard the loop by workflow id.
6. Then start on cohorts and experiments for voice and welcome message, one client and one workflow first.

---

### Running it

```bash
uv venv -p 3.12 .venv && uv pip install -p .venv/bin/python -r requirements.txt
scripts/run_scenario.sh public_01_dev          # baseline, sim, controller, grade -> reports/public_01_dev.md
.venv/bin/python -m pytest -q tests            # 26 tests, ~10 s
.venv/bin/python scripts/crash_test.py public_02_incidents 15
```

The runbook for whoever is on call is in [docs/RUNBOOK.md](docs/RUNBOOK.md).
