# SearchHax V1 — milestone tracker

Status is set from **observed evidence only**: a test run, a screenshot or a persisted record. Last update: 2026-11-20, after PR #4 and PR #5 were merged into `main` (`d8f9ad5`, `a639ebc`).

Branches: `main` holds all integrated work. Follow-up work is on `fix/llm-output-bounds` (open PR, not merged). `qa/exploration-verification` is pushed and intentionally not merged (it holds the QA failure report and snapshot imports).

**Overall: V1 is INCOMPLETE for public release.** Everything in M0–M10 has local, offline or one-time live evidence. Public release is blocked on items that need your decisions: a host, accounts and data isolation, and a confirming live run for the extraction fix.

Status legend:
- ✅ exit criteria verified
- 🟡 partial
- 🔲 not started
- ⛔ needs your authorization (billable or outward-facing)

| # | Milestone | Status | Evidence / what is left |
|---|---|---|---|
| M0 | Integration baseline | ✅ | PR #4 → `main` (merge commit `d8f9ad5`), PR #5 retargeted to `main`, CI rerun green (run 44827459209), merged `a639ebc`. `main`'s tree equals the tested PR #5 head. No secrets in the diff. The QA branch's 61 local-only commits were scanned (no credentials; home paths already public on that branch) and pushed. A bundle of all refs is in `~/SearchHax-preserve`. |
| M1 | Real-data React reader | ✅ | 10/10 checks on the live API; screenshots |
| M2 | Grounded branching and lifecycle | ✅ (live, once) | Live 2026-11-20: a challenge child `inv_7647b6b549474dd2` was created from claim `clm_2d994c177a997b8f` with the vague objective "Is this true?". Its search was "independent evidence for or against: NVIDIA has invested $2 billion in CoreWeave". The root question was kept, the objective stored separately, the claim and source attached, the parent unchanged, and the lifecycle went saved → stopped. Offline: backend + vitest + e2e (seeded CI DB includes a legacy row). |
| M3 | Live research execution | ✅ (live, once; local) | Live root `inv_51b381d9c7d24e59` through React: save → billable confirm → step (1 search, 4 URLs, 4 LLM calls) → **Continue research** (`36370d6`, added after the live run showed no way to continue) → convergence stop `unresolved_uncertainty`. Fork & Investigate ran a child behind its in-page authorization (1/5/5 of a 1/5/6 budget). Budgets enforced server-side. **Open defect:** one extraction recorded `LLM extraction failed: JSONDecodeError`; the cause is unconfirmed (blocker now records the finish reason, `9caa669`). |
| M4 | Truthful trace and playback | ✅ (new runs) | Playback, snapshots and compare on real data. New searches record `round` and `planned_from` (prompting question/action), and the adapter uses them. Replay determinism and "only persisted checkpoints are forkable" are tested. **Left:** older runs keep derived rounds (their events predate the fields). |
| M5 | Evidence-grounded findings | ✅ (live, once) | Live Findings: "PARTIAL / NOT VERIFIED", 6 records grouped as 1 claim ("repetition is not extra support"), 6 exact-quote, 0 independent origins, 3 of unknown origin, the recorded stop rationale. Repeated open questions are now grouped (`9d06b18`). **Left:** the "generate cited summary" button (billable, not built). Source independence is mostly "unknown origin" on live pages, so independence is rarely established. |
| M6 | Lineage and comparison | ✅ (live, once) | Live parent↔child compare: 1 claim unchanged, 4 shared + 5 new sources, a new claim-grounded open question, and the stop reasons of both. Constellation, grandchild root question, and canvas restore covered offline (e2e). |
| M7 | Real 3D Focus space | ✅ (live, once) | Live child: passage → its own point ("C2 · passages · 4 recorded points", investigate = orange) → "Show in investigation trace" → `evidence_spans · span_fbbf324e71fa7966`. Passage rows carry their own investigation id (parent and child points differ for the same span ids). Offline requirements 1–10 as before (`cdbb6a5`). **Not done (P3):** a parent+child overlay. |
| M8 | Personal knowledge | ✅ (offline + live once) | Live correction persisted across reload and restart (2026-11-20). **Now ranking input** (`49cb4c4`): knowledge value scores new pages against the profile priors plus the user's latest `known`/`user_confirmation` assertions. Encountered or retrieved material never counts. Earlier scores are never recomputed. Each score records the confirmation ids it used (contract 1.3.0), shown in the source inspector. **Not observed:** a live step after a confirmation. |
| M9 | UX and accessibility | ✅ (local) | Text below 10px outside the canvas raised (244 rules); header wraps; map supports keyboard pan, zoom, fit and Escape, and nodes are focusable buttons with visible focus (e2e). |
| M10 | QA, security, evaluation | 🟡 | QA failure report: 64/64 tests present, 62 pass, 2 skipped pending decisions (Q13, Q17). CI runs backend, frontend and seeded browser jobs. **Evaluation** (`b76bc71`, `docs/evaluation/EVALUATION.md`): offline B0/B1/B2 on Story C, plus recorded live branch usefulness (n = 1). The live JSONDecodeError has a bounded fix with offline regression tests (`b61b803`) but **needs one capped live step to confirm**. **Left:** accounts and authorization; multi-story or live evaluation. |
| M11 | Deploy and release | 🟡 (private pilot ready; public blocked) | Isolated compose stack verified on `e9c62d0`: images build; health shows contract 1.3.0; UI and `/api` routed through nginx (404 for unknown ids); only web published, on 127.0.0.1; API runs non-root with no keys in its environment; data persists across down/up; idempotent replay works. **Server-side billable gate** (`SEARCHHAX_ALLOW_BILLABLE`, compose default false): step and answer return 403 through nginx even with valid basic auth. Basic auth: 401/401/200, /healthz open. `DEPLOY.md` lists private-pilot vs public requirements. **Not done:** any real host, TLS, accounts, per-user isolation, Postgres/migrations, quotas ⛔. |

## V1 release acceptance (11 / 13 observed; local, live once on 2026-11-20)

- [x] Submit a new research question through React and create a real backend investigation
- [x] Watch real search, extraction, evaluation and convergence activity (live root, 2 steps)
- [x] Receive grounded findings with citations and explicit uncertainty (live Findings: partial / not verified)
- [x] Replay and inspect the sources behind each recorded decision
- [x] Return to an eligible intermediate checkpoint (live C1: persisted, resumable)
- [x] Challenge a claim and fork without losing the original question (live, "Is this true?")
- [x] Run child research using the attached claim and checkpoint (live Fork & Investigate)
- [x] Compare parent and child findings, evidence and unresolved questions (live)
- [x] Select a passage in the Lantern Map and find the same point in 3D (live child)
- [x] Inspect and correct a knowledge assertion; see it persisted (live, across reload and restart)
- [x] Restart without losing investigations, branches or checkpoints (live DB: identical snapshot after a backend restart; container volume after compose down/up)
- [ ] Pass the offline, integration, browser, security and budget tests: offline, browser and budget pass; there is no account/authorization model to test
- [ ] Use the deployed app without local developer tools: container verified locally only ⛔ host not chosen

## Defect status (2026-11-20, end of session)

| ID | Severity | Status |
|---|---|---|
| Live `LLM extraction failed: JSONDecodeError` (2 live runs) | P1 | **Fix landed, unconfirmed live.** Likely truncation: the answer is now bounded (8/4/3/12 items, 300-char quotes), parsing still strict, Anthropic `max_tokens` 2048, and the blocker records the finish reason (`b61b803`). |
| Q13: degenerate question searched | P2 | **Needs product decision:** search the first non-empty sentence (current) or refuse with a "not searchable" reason? |
| Q17: Story C independence follow-up | P2 | **Needs product decision:** should the offline fixture auto-run the follow-up (current) or leave it pending for Focus to approve? Changes the recorded baselines. |
| Parent+child 3D overlay | P3 | Not built. |
| Accounts / per-user isolation / quotas | release blocker | Not implemented (see `docs/deploy/DEPLOY.md` §6). |

Closed this session: compare counted repeated records (`b8913b7`); confirmed knowledge did not affect ranking (`49cb4c4`); billable routes reachable when the UI flag was off (`e9c62d0`); Anthropic token usage recorded as null (`b61b803`).

## Handoff: next actionable tasks

1. Review and merge the `fix/llm-output-bounds` PR (yours).
2. Authorize **one** capped live step to confirm the extraction fix. It now records the finish reason; expect no JSONDecodeError blocker.
3. Decide Q13 and Q17; then un-skip and adapt the two tests.
4. Choose a pilot host (VM or reverse proxy with TLS), then run `docs/deploy/DEPLOY.md` with basic auth and `SEARCHHAX_ALLOW_BILLABLE=true` only for the pilot window.
5. For public release, design accounts and per-user data isolation (schema change), quotas, and a server database.
6. Local cleanup (`~/SearchHax-preserve/WORKSPACE-PLAN.md` steps 0, 2–5): back up the ignored state, update the remote URL, move the worktrees. Nothing was moved this session.
