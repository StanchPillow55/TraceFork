# SearchHax evaluation: B0 / B1 / B2 (2026-11-20)

To reproduce (offline; no provider, network or LLM call):

```bash
PYTHONPATH=.:focus python -m exploration.backend.evaluation --branch-usefulness docs/evaluation/branch_usefulness_live_2026-11-20.json --out docs/evaluation/b0_b1_b2_story_c.json
```

`exploration/tests/test_evaluation.py` checks that a fresh run matches the committed
results (`b0_b1_b2_story_c.json`) apart from latency.

## What was compared

| | Configuration | What it does |
|---|---|---|
| **B0** | one-shot | Reads the corpus once. No follow-up, no provenance, no contradiction checks. |
| **B1** | conventional iterative (**offline proxy**) | The same planner-driven follow-up loop, but every page counts as an independent source and conflicting amounts are not reconciled. No LLM orchestrator is simulated; this is not a measurement of any specific agent product. |
| **B2** | SearchHax | Provenance and origin clustering, contradiction reconciliation, and the in-process Focus convergence controller deciding the follow-up. |

The corpus is Story C: 4 local fixture documents, one wire story republished three times,
and one conflicting amount. It is **one story, one run per configuration**: a behavioural
comparison, not a statistical result.

## Measured (offline, deterministic except latency)

| Metric | B0 | B1 | B2 |
|---|---|---|---|
| Evidence spans with exact quotes | 13/13 | 13/13 | 13/13 |
| Supported relationships (distinct facts) | 21 (8) | 21 (8) | 20 (7) |
| Contradicted relationships | 0 | 0 | **1** |
| Contradictions detected | 0 | 0 | **1** |
| Independent origins claimed (of 4 documents) | 4 | 4 | **1** |
| Derivative documents detected | 0 | 0 | **3** |
| Open questions | 0 | 0 | 1 |
| Searches / URLs / LLM calls / network calls | 0/0/0/0 | 0/0/0/0 | 1/0/0/0 (local follow-up) |
| Follow-ups executed | 0 | 0 | 1 |
| Stop | (none recorded) | no_useful_actions (fallback) | unresolved_uncertainty (convergence) |
| Latency (local, ms) | ~25 | ~25 | ~30 |

What the numbers show:
- **Grounding:** all three keep only exact-quote spans. That comes from the shared
  extractor, so it is not what separates them.
- **Independence:** B0 and B1 treat four copies of one wire story as four independent
  confirmations, and they accept both of the conflicting amounts as supported. B2 reduces
  the four documents to one origin and marks the conflicting amount contradicted.
- **Iteration alone did not help here.** Without provenance classification, B1's planner
  found no gap worth searching, so it behaved exactly like B0. The independence gap only
  becomes visible once origins are classified.
- **Novelty:** the Focus policy's novelty is 1.0 for every page in all three configurations,
  because this fixture has no known claims. It does not discriminate on this corpus and is
  not claimed as a result.
- **Cost and latency:** negligible offline; B2 spends one local follow-up.

## Branch usefulness (recorded live, n = 1)

From the authorized capped live session (`branch_usefulness_live_2026-11-20.json`): a
"challenge_evidence" child of `inv_51b381d9c7d24e59` with the vague objective
"Is this true?" searched "independent evidence for or against: NVIDIA has invested
$2 billion in CoreWeave". It added **5 new sources** (4 shared), **0 new claims**,
**1 new open question** and 0 contradictions, for 1 search, 5 URLs and 5 LLM calls. It
widened coverage without changing the finding. The live pages' origins were mostly
"unknown", so independence could not be established live either.

## Not measured (untested claims)

- Any comparison with a real LLM-orchestrated research agent (B1 is a proxy).
- Results on more than one story, or with live retrieval for B0/B1.
- Whether SearchHax answers are more *correct*: exact-quote grounding is not
  fact-checking, and Focus scores are not truth.
- Personalization benefit from knowledge value (no user study).
- Branch usefulness beyond the one recorded pair.
