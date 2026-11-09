# Grade report — public_03_load

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| arav_cancel_2 | 0.340 | +0.8% | 0.244 | +0.2% | 0.500 | +0.7% | 0% | 391 (409) |
| arav_kyc_1 | 0.467 | +30.2% | 0.272 | +26.6% | 0.592 | -1.4% | 0% | 444 (352) |
| arav_renewal_0 | 0.432 | +6.6% | 0.283 | +7.1% | 0.545 | -0.4% | 0% | 448 (408) |
| kest_cancel_2 | 0.341 | +1.4% | 0.244 | +0.6% | 0.500 | +0.2% | 0% | 405 (418) |
| kest_kyc_1 | 0.388 | +9.2% | 0.231 | +7.9% | 0.593 | -0.8% | 0% | 359 (370) |
| kest_renewal_0 | 0.420 | +4.4% | 0.276 | +4.1% | 0.547 | +0.5% | 0% | 445 (403) |
| nort_cancel_2 | 0.341 | +0.4% | 0.245 | +0.3% | 0.498 | -0.2% | 0% | 405 (368) |
| nort_kyc_1 | 0.369 | +3.6% | 0.221 | +2.7% | 0.597 | +0.2% | 0% | 348 (318) |
| nort_renewal_0 | 0.427 | +5.3% | 0.279 | +5.7% | 0.547 | -0.1% | 0% | 449 (419) |
| tide_cancel_2 | 0.342 | -0.2% | 0.245 | +0.4% | 0.499 | -0.3% | 0% | 405 (401) |
| tide_kyc_1 | 0.463 | +29.8% | 0.269 | +25.2% | 0.591 | -1.1% | 0% | 406 (376) |
| tide_renewal_0 | 0.430 | +6.2% | 0.282 | +6.7% | 0.546 | -0.1% | 0% | 439 (425) |
| ALL | 0.397 | +8.2% | 0.258 | +6.9% | 0.547 | +0.1% | 0% | 4944 (4667) |

## Changes applied by the controller

24 applied — neutral: 10, beneficial: 14

- nort_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.006 → **neutral**
- kest_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.006 → **neutral**
- arav_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.006 → **neutral**
- tide_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.006 → **neutral**
- nort_renewal_0 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.050 → **beneficial**
- kest_renewal_0 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.050 → **beneficial**
- arav_renewal_0 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.050 → **beneficial**
- tide_renewal_0 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.050 → **beneficial**
- arav_kyc_1 v2 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.035 → **beneficial**
- kest_kyc_1 v2 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.038 → **beneficial**
- tide_kyc_1 v2 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.039 → **beneficial**
- nort_renewal_0 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.019 → **neutral**
- kest_renewal_0 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.014 → **neutral**
- nort_kyc_1 v2 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.031 → **beneficial**
- tide_kyc_1 v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.556 → **beneficial**
- arav_kyc_1 v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.622 → **beneficial**
- kest_kyc_1 v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.460 → **beneficial**
- nort_renewal_0 v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.007 → **neutral**
- kest_renewal_0 v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.006 → **neutral**
- kest_renewal_0 v6 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nort_renewal_0 v6 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.044 → **beneficial**
- kest_renewal_0 v7 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.015 → **neutral**
- kest_renewal_0 v8 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.068 → **beneficial**
- nort_kyc_1 v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.525 → **beneficial**

## Governance

```
{
  "approval_requests": 10,
  "decisions": {
    "approved": 8,
    "rejected": 2,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 2
  },
  "applied_via_approval": 8,
  "auto_applied": 16,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nort_kyc_1": 1,
    "kest_kyc_1": 1,
    "arav_kyc_1": 1,
    "tide_kyc_1": 1
  }
}
```

## Incidents

```
[]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0