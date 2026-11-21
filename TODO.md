# Pit Crew TODO

Tasks and deliverables per owner. Keep changes scoped to your lane; open a separate PR for any change to `src/pit_crew/contracts.py` or `src/pit_crew/ledger.py`.

See [docs/architecture.md](docs/architecture.md) for system design and [docs/lane-guide.md](docs/lane-guide.md) for working agreements.

---

## Big Picture & Platform (Architecture Owner)

- [x] Fill in lane owners in [README.md](README.md) and add `.github/CODEOWNERS`.
- [x] Commit the seven master specs under [`docs/specs/`](docs/specs/) and index them from the README and architecture overview.
- [x] Finalize v1 contract updates in `src/pit_crew/contracts.py` (separate PR, 100% backwards-compatible):
  - [x] `Candidate` carries `repo`, `base_sha`, `head_sha`, `files`, `changed_files` + `is_allowed_candidate_path()`.
  - [x] `BugCase` (`commit` + `mutation` + multimodal `artifact_ref`) synced with `ReplayPlan.bug_shas`.
  - [x] `SqliteLedger` (WAL) + `prior_attempts(repo)` and `latest_kept_candidate(repo)` in `Ledger`.
- [x] Lock in the three open architectural decisions:
  - [x] Managed Agents runtime: Gemini Interactions API (`antigravity-preview-09-2026` on `gemini-4-hydrogen`, `gemini-4-neon`, `gemini-4-carbon`).
  - [x] Production ledger storage backend: WAL-mode `SqliteLedger`.
  - [x] Tool transport: Host-mediated `@tool` capability gates via `requires_action` loop (zero secrets in sandbox).
- [x] Define the GitHub Actions replay execution spec (3-way overlay on `bug_sha` + baseline-verified synthetic mutations).
- [x] Update `managed_agent.build_request()` to auto-mount `lanes/optimizer/playbooks/*.md` + multimodal `extra_sources`.

**Deliverables:** Locked v1 `contracts.py` + `Ledger` protocol, recorded ADRs for the three open decisions, and a shared replay execution spec so Dispatcher and Proof align.

---

## Analysis Lane (`src/pit_crew/lanes/analysis/`)

- [x] Add GitHub Actions run/job/step history fetcher + parser (with offline JSON fixtures under `tests/fixtures/`).
- [x] Implement critical-path calculation across workflow job DAGs (`needs:`) rather than raw sum of job durations.
- [x] Detect core bottleneck classes: cache misses, unsharded slow test suites, serial jobs that can parallelize, and redundant jobs.
- [ ] Detect flaky retries (`retried_runs` is profiled but no detector or skill acts on it yet).
- [x] Align `Hypothesis.skill` vocabulary with Optimizer's playbook names via `contracts.PLAYBOOK_SKILLS` (`dep-cache`, `parallelize-dag`, `shard-by-timing`, `dedupe-jobs`, `affected-tests`).
- [x] Prevent duplicate hypotheses for bottlenecks already open or recently rejected in the ledger.
- [x] Expand `SKILL.md` with clear ranking criteria (highest wall-clock savings first) and evidence formatting.

**Deliverables:**
- Tested analysis tools in `src/pit_crew/lanes/analysis/tools.py` + realistic multi-run workflow fixtures.
- Updated `src/pit_crew/lanes/analysis/SKILL.md`.
- Unit tests in `tests/test_analysis.py` verifying critical-path ranking and hypothesis recording without an API key.

---

## Optimizer Lane (`src/pit_crew/lanes/optimizer/`)

- [x] Create playbook sub-skills for the initial fix catalog (`contracts.PLAYBOOK_SKILLS`):
  - [x] `dep-cache`
  - [x] `shard-by-timing`
  - [x] `affected-tests`
  - [x] `parallelize-dag`
  - [x] `dedupe-jobs`
  - [x] `artifact-capture` companion: failure recordings for the Proof lane, off in timing runs (#89)
- [x] ~~`fail-fast-order`~~ Left out of v1 on purpose. It shortens time-to-red, not green-run time, and the
  rater measures green-run medians, so every candidate would be rejected as "within noise". Revisit
  if the rater gains a time-to-first-failure metric.
- [x] ~~`flake-quarantine`~~ Left out of v1 on purpose. Quarantining a flaky test hides failures, which the
  guard rejects by design ("a check that needs a retry has a bug"), and the Optimizer may not edit
  test code to fix the flake. Analysis has no flake detector feeding it either.
- [x] Read-only tool for prior `Candidate`/`Verdict` records (`list_prior_attempts`).
- [x] Static "never check less" guard for workflows (`validate.py`) and runner configs (`configs.py`),
  enforced by `check_workflow_change` and `report_candidate`.
- [x] Branch naming `pc/<hypothesis_id>-<skill>` and bail-out behavior in `SKILL.md`.
- [x] `SKILL.md` enforces one change per candidate.
- [x] Eval against baselines (`uv run python -m pit_crew.evals.optimizer_guard`): 100% of weakening edits
  caught and 0% of safe edits blocked on 18 held-out repos; every live single-call Gemini rewrite
  tried was blocked (#116).
- [x] Dispatcher re-checks each candidate against the real base files before publishing, so the
  guard never relies on the agent's `original_files`.

**Deliverables:**
- Playbook skill files + updated `src/pit_crew/lanes/optimizer/SKILL.md`.
- Read-only history lookup and static workflow and config validation tools in `src/pit_crew/lanes/optimizer/tools.py`.
- Unit tests in `tests/test_optimizer*.py` and `tests/test_eval_optimizer_guard.py`.

---

## Proof Lane (`src/pit_crew/lanes/proof/`)

- [x] Build a red-commit miner that inspects git + CI history to find commits that failed CI and maps each failure to the workflow/job/check that caught it.
- [x] Implement diff-to-check mapping so Proof prioritizes historical bugs that exercise the checks touched by the `Candidate` diff.
- [x] Build a mutation-injection fallback for repos or checks with no recorded red commits (aligned with `ReplayPlan` contract).
- [x] Add validation to prevent empty `ReplayPlan`s (since `rater.py` automatically rejects `bugs_total == 0`).
- [x] Refine `SKILL.md` with selection heuristics (prefer high-signal bugs most likely to be masked by the candidate optimization).
- [x] Visual proof: record each replay as a terminal video, have Gemini 4 Carbon confirm the candidate fails the same way as the baseline, and render a proof card for the PR.
- [ ] Agree with Dispatcher on recording replays (`record.py`, plus `agg` and `ffmpeg` on the runner) and wiring `verify_replays(..., compare=partial(compare_failures, ask=managed_agent.ask_multimodal))`, `tally()` and `Replay.as_outcome()` into `Evidence`.
- [x] Carry the expected failing check per bug in `ReplayPlan` (contract PR), so a replay must fail on the same check.

**Deliverables:**
- Red-commit miner + mutation-injection tools in `src/pit_crew/lanes/proof/tools.py` with offline git/CI fixtures.
- Updated `src/pit_crew/lanes/proof/SKILL.md`.
- Unit tests in `tests/test_proof.py` covering diff-to-bug selection, mutation fallback, and replay plan validation.

---

## Rater (`src/pit_crew/rater.py`)

- [ ] Add edge-case handling and tests: tied/identical timings, zero-duration runs, extreme outliers.
- [x] Handle larger sample sizes (>10 runs per side) gracefully—switch from exact `combinations` enumeration to Monte Carlo permutation or Mann-Whitney U above a threshold.
- [x] Decide and implement a minimum practical speedup threshold (e.g., minimum relative speedup or seconds saved) alongside `MAX_P_VALUE = 0.05`.
- [ ] Document the statistical assumptions (paired vs. unpaired, minimum runs, noise floor) in module docstrings.

**Deliverables:**
- Hardened, deterministic `src/pit_crew/rater.py` with bounded runtime for any sample size.
- Expanded edge-case test suite in `tests/test_rater.py`.

---

## Dispatcher & Runtime (`src/pit_crew/dispatcher.py`, `managed_agent.py`, `ledger.py`)

- [ ] Complete `managed_agent.run_lane()` for live execution:
  - [x] Mount the target fork repository as a source.
  - [ ] Run asynchronously / in background with timeout and error handling.
  - [x] Implement the `requires_action` tool-call loop backed by `tools.call()`.
- [x] Build the GitHub webhook receiver (`workflow_run`, cron/manual triggers) with signature verification and idempotency guards.
- [ ] Implement GitHub Actions orchestration:
  - [ ] Trigger `>= 5` interleaved baseline and candidate timing runs on the fork.
  - [ ] Trigger bug replay runs for every entry in `ReplayPlan`.
  - [ ] Aggregate completed run timings and pass/fail outcomes into `Evidence`.
- [ ] Wire `Evidence` → `rater.rate()` → `Verdict` persistence, plus post-verdict actions (open PR on `keep=True`, record rejection on `keep=False`).
- [x] Implement the production `Ledger` backend once storage is selected (WAL-mode `SqliteLedger`).
- [x] Track live run telemetry for the Pit Wall API (`run_tracker.RunTracker`).
- [x] Add the `smoke-lane` CLI for live single-lane runs (`src/pit_crew/cli.py`).

**Deliverables:**
- Working live `managed_agent.run_lane()` loop tested against a real Gemini Managed Agent call.
- Persistent `Ledger` implementation passing the same contract tests as `InMemoryLedger`.
- End-to-end Dispatcher service capable of driving a full `Hypothesis → Candidate → ReplayPlan → Evidence → Verdict` cycle.

---

## Integration Milestones

- [ ] **M1 — Offline End-to-End Dry Run:** All lanes run against fixtures and `InMemoryLedger` from `Hypothesis` to `Verdict` in `pytest` with no API keys.
- [ ] **M2 — First Live Managed Agent Smoke Test:** Run `analysis` against a test fork using `GEMINI_API_KEY` and write a real `Hypothesis` to the ledger.
- [ ] **M3 — First Automated Verdict on a Fork:** Full loop on a real repository fork producing a measured `Evidence` record and `Verdict` from GitHub Actions runs.
