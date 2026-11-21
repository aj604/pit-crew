# Pit Crew TODO

Tasks and deliverables per owner. Keep changes scoped to your lane; open a separate PR for any change to `src/pit_crew/contracts.py` or `src/pit_crew/ledger.py`.

See [docs/architecture.md](docs/architecture.md) for system design and [docs/lane-guide.md](docs/lane-guide.md) for working agreements.

---

## Big Picture & Platform (Architecture Owner)

- [ ] Fill in lane owners in [README.md](README.md) and add `.github/CODEOWNERS`.
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

- [ ] Add GitHub Actions run/job/step history fetcher + parser (with offline JSON fixtures under `tests/fixtures/`).
- [ ] Implement critical-path calculation across workflow job DAGs (`needs:`) rather than raw sum of job durations.
- [ ] Detect core bottleneck classes: cache misses, unsharded slow test suites, serial jobs that can parallelize, redundant jobs, and flaky retries.
- [ ] Align `Hypothesis.skill` vocabulary with Optimizer's playbook names (`dep-cache`, `shard-by-timing`, `affected-tests`, `fail-fast-order`, `flake-quarantine`, `dedupe-jobs`).
- [ ] Prevent duplicate hypotheses for bottlenecks already open or recently rejected in the ledger.
- [ ] Expand `SKILL.md` with clear ranking criteria (highest wall-clock savings first) and evidence formatting.

**Deliverables:**
- Tested analysis tools in `src/pit_crew/lanes/analysis/tools.py` + realistic multi-run workflow fixtures.
- Updated `src/pit_crew/lanes/analysis/SKILL.md`.
- Unit tests in `tests/test_analysis.py` verifying critical-path ranking and hypothesis recording without an API key.

---

## Optimizer Lane (`src/pit_crew/lanes/optimizer/`)

- [ ] Create playbook sub-skills for the initial fix catalog:
  - [ ] `dep-cache`
  - [ ] `shard-by-timing`
  - [ ] `affected-tests`
  - [ ] `fail-fast-order`
  - [ ] `flake-quarantine`
  - [ ] `dedupe-jobs`
- [ ] Add a read-only tool to inspect prior `Candidate` and `Verdict` records for the target repo so failed approaches are not repeated.
- [ ] Add static validation helper/tool for workflow changes (YAML syntax, required job/step structure, no deleted checks).
- [ ] Define branch naming convention on the fork (e.g., `pc/<hypothesis_id>-<skill>`) and clean bail-out behavior when a hypothesis cannot be safely implemented.
- [ ] Refine `SKILL.md` to enforce strictly one change per candidate.

**Deliverables:**
- Playbook skill files + updated `src/pit_crew/lanes/optimizer/SKILL.md`.
- Read-only history lookup and static workflow validation tools in `src/pit_crew/lanes/optimizer/tools.py`.
- Unit tests in `tests/test_optimizer.py` covering valid candidate reporting, unknown-hypothesis rejection, prior-attempt lookup, and invalid-workflow rejection.

---

## Proof Lane (`src/pit_crew/lanes/proof/`)

- [ ] Build a red-commit miner that inspects git + CI history to find commits that failed CI and maps each failure to the workflow/job/check that caught it.
- [ ] Implement diff-to-check mapping so Proof prioritizes historical bugs that exercise the checks touched by the `Candidate` diff.
- [ ] Build a mutation-injection fallback for repos or checks with no recorded red commits (aligned with `ReplayPlan` contract).
- [ ] Add validation to prevent empty `ReplayPlan`s (since `rater.py` automatically rejects `bugs_total == 0`).
- [ ] Refine `SKILL.md` with selection heuristics (prefer high-signal bugs most likely to be masked by the candidate optimization).

**Deliverables:**
- Red-commit miner + mutation-injection tools in `src/pit_crew/lanes/proof/tools.py` with offline git/CI fixtures.
- Updated `src/pit_crew/lanes/proof/SKILL.md`.
- Unit tests in `tests/test_proof.py` covering diff-to-bug selection, mutation fallback, and replay plan validation.

---

## Rater (`src/pit_crew/rater.py`)

- [ ] Add edge-case handling and tests: tied/identical timings, zero-duration runs, extreme outliers.
- [ ] Handle larger sample sizes (>10 runs per side) gracefully—switch from exact `combinations` enumeration to Monte Carlo permutation or Mann-Whitney U above a threshold.
- [ ] Decide and implement a minimum practical speedup threshold (e.g., minimum relative speedup or seconds saved) alongside `MAX_P_VALUE = 0.05`.
- [ ] Document the statistical assumptions (paired vs. unpaired, minimum runs, noise floor) in module docstrings.

**Deliverables:**
- Hardened, deterministic `src/pit_crew/rater.py` with bounded runtime for any sample size.
- Expanded edge-case test suite in `tests/test_rater.py`.

---

## Dispatcher & Runtime (`src/pit_crew/dispatcher.py`, `managed_agent.py`, `ledger.py`)

- [ ] Complete `managed_agent.run_lane()` for live execution:
  - [ ] Mount the target fork repository as a source.
  - [ ] Run asynchronously / in background with timeout and error handling.
  - [ ] Implement the `requires_action` tool-call loop backed by `tools.call()`.
- [ ] Build the GitHub webhook receiver (`workflow_run`, cron/manual triggers) with signature verification and idempotency guards.
- [ ] Implement GitHub Actions orchestration:
  - [ ] Trigger `>= 5` interleaved baseline and candidate timing runs on the fork.
  - [ ] Trigger bug replay runs for every entry in `ReplayPlan`.
  - [ ] Aggregate completed run timings and pass/fail outcomes into `Evidence`.
- [ ] Wire `Evidence` → `rater.rate()` → `Verdict` persistence, plus post-verdict actions (open PR on `keep=True`, record rejection on `keep=False`).
- [ ] Implement the production `Ledger` backend once storage is selected.

**Deliverables:**
- Working live `managed_agent.run_lane()` loop tested against a real Gemini Managed Agent call.
- Persistent `Ledger` implementation passing the same contract tests as `InMemoryLedger`.
- End-to-end Dispatcher service capable of driving a full `Hypothesis → Candidate → ReplayPlan → Evidence → Verdict` cycle.

---

## Integration Milestones

- [ ] **M1 — Offline End-to-End Dry Run:** All lanes run against fixtures and `InMemoryLedger` from `Hypothesis` to `Verdict` in `pytest` with no API keys.
- [ ] **M2 — First Live Managed Agent Smoke Test:** Run `analysis` against a test fork using `GEMINI_API_KEY` and write a real `Hypothesis` to the ledger.
- [ ] **M3 — First Automated Verdict on a Fork:** Full loop on a real repository fork producing a measured `Evidence` record and `Verdict` from GitHub Actions runs.
