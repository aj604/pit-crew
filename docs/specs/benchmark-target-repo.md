# Spec 4B: Target Benchmark Repository & Seeded CI History (M2/M3 Live Fork)

## Goal

Provide a deterministic, reproducible **target benchmark repository template and automated GitHub Actions history seeder** so Milestone 2 (`pit-crew smoke-lane`), Milestone 3 (`pit-crew run-cycle --until-sla`), and live Pit Wall demonstrations can run against a real GitHub fork and achieve a statistically significant, safety-proven multi-cycle CI speedup down to the `<= 60s` median / `<= 120s` p95 SLA.

---

## Why a Purpose-Built Benchmark Repo Is Required

1. **`aj604/pit-crew` cannot target itself:** Its own CI already caches `uv`, runs all jobs in parallel, and finishes in ~25s—already below the `60s` median SLA.
2. **Fresh forks have empty CI & bug history:** Without completed baseline runs, `lanes/analysis/history.py::fetch_history` returns zero runs (`find_bottlenecks` emits nothing). Without deterministic red-to-green bug commits, `lanes/proof/miner.py::red_commits` finds zero historical bugs.
3. **Workflow-version filtering invariant (PR `#6`):** `lanes/analysis` excludes Actions runs executed against older versions of `.github/workflows/ci.yml`. Therefore, `.github/workflows/ci.yml` **must be frozen first**, before seeding bug commits and green baseline runs!
4. **Multimodal Proof showcase (`gemini-4-carbon`):** To exercise `lanes/proof` artifact inspection and `verify.py` failure comparison end-to-end, at least one check must produce a real visual/structured failure artifact (`actions/upload-artifact`) when broken.

---

## Module & File Layout

| File | Responsibility |
|---|---|
| `benchmarks/target_template/` | Self-contained micro-application (Python API + CLI/UI check + test suite + `.github/workflows/ci.yml`) designed as the initial commit of the target benchmark repo (excluded from `pit_crew` wheel, linted/tested cleanly). |
| `src/pit_crew/benchmark_seeder.py` (or `scripts/seed_benchmark_repo.py` + `pit-crew seed-target` / `check-target`) | Idempotent GitHub API seeder & readiness verifier that provisions/resets a target fork (`e.g. aj604/pit-crew-bench`), seeds deterministic red-to-green bug commits across every check, dispatches `>= 10` green baseline runs, and verifies Analysis + Proof readiness. |
| `tests/test_benchmark_seeder.py` | 100% offline unit tests verifying commit sequence ordering, workflow-hash preservation across bug/fix pairs, and readiness preflight assertions against `analysis` and `proof` parsers. |

---

## Benchmark Target Design (`benchmarks/target_template/`)

### 1. Application & Checks
Compact, zero-external-service Python + HTML status dashboard package with 4 real CI checks:
1. **`lint`:** `ruff check` + format check.
2. **`typecheck`:** `mypy --strict`.
3. **`unit-test`:** `pytest` suite (~40 fast unit tests + 4 realistic integration/simulation tests).
4. **`ui-check`:** Headless render/DOM assertion script that writes a diagnostic SVG/PNG snapshot on failure and uploads `.artifacts/` via `actions/upload-artifact` (`if: failure()`).

### 2. Calibrated Multi-Skill CI Bottlenecks (`.github/workflows/ci.yml`)
Configured with `on: [push, pull_request, workflow_dispatch]` and baseline wall-clock duration calibrated to **`~125s–155s` median** on `ubuntu-latest` across 4 orthogonal bottlenecks matching `contracts.PLAYBOOK_SKILLS`:

| Bottleneck | Playbook Skill | Bucket | How It Is Planted | Expected Wall-Clock Saving |
|---|---|---|---|---|
| Uncached dependency install | `dep-cache` | `setup` | `setup-uv` / `pip` runs with cache disabled and cold wheel/env install in each job | `~20–30s` |
| Serialized independent jobs | `parallelize-dag` | DAG (`""`) | `lint -> typecheck -> unit-test -> ui-check` chained sequentially via unnecessary `needs:` edges | `~35–45s` |
| Duplicate check execution | `dedupe-jobs` | `running` | Redundant `smoke-test` job duplicating `unit-test` setup and suite | `~20–30s` |
| Unsharded integration suite | `shard-by-timing` | `running` | Slow multi-module integration suite running serially in a single runner instead of matrix shards | `~30–40s` |

**Compounding Trajectory (`run_until_sla`):**
- **Baseline:** `median ~140s`, `p95 ~160s` (`sla_met = False`).
- **Cycle 1** (e.g. `parallelize-dag` or `dep-cache`): `median ~82s` (`~1.7x` speedup, `p <= 0.05`, `4/4` bugs caught → **`KEEP`, `sla_met = False`**), advancing `base_sha`.
- **Cycle 2** (compounding atop Cycle 1 `head_sha`, e.g. `shard-by-timing` or `dep-cache`): `median ~46s`, `p95 ~78s` (`>= 1.05x` incremental speedup, `p <= 0.05`, `4/4` bugs caught → **`KEEP`, `sla_met = True`**, loop terminates!).

---

## Deterministic Seeding Protocol (`seed_benchmark_repo`)

Given `--repo <owner>/<bench-repo>` (e.g. `aj604/pit-crew-bench`):

1. **Step 1 — Provision & Freeze Workflow (`c0`):**
   - Create or reset `<owner>/<bench-repo>` default branch `main` with `benchmarks/target_template/` (including the final baseline `.github/workflows/ci.yml` with `workflow_dispatch:`).
   - **Invariant:** `.github/workflows/ci.yml` is **never modified** in Steps 2 or 3 so every subsequent run shares the exact same workflow version (satisfying Analysis PR `#6`).
2. **Step 2 — Seed 4 Deterministic Red → Green Bug Pairs (`b1/f1 .. b4/f4`):**
   - Each bug modifies **application/test code only** (never `.github/`), waits for the push workflow run to conclude `"failure"` on the targeted check, then immediately commits the clean revert/fix and waits for `"success"` so `lanes/proof/miner.py::red_commits` classifies every bug as deterministic (`failed on every attempt`) and **`fixed=True`** (highest priority in `proof.tools.pick_replays`):
     1. **`lint` bug:** Unused import in module → fails `lint` → fix commit restores clean code.
     2. **`typecheck` bug:** Return-type mismatch → fails `typecheck` → fix commit restores clean code.
     3. **`unit-test` bug:** Off-by-one boundary error in calculation → fails `unit-test` with clear expected-vs-actual assertion → fix commit restores clean code.
     4. **`ui-check` bug:** Missing Trust Wall element / broken status badge → fails `ui-check` and uploads failure artifact snapshot → fix commit restores clean code.
3. **Step 3 — Seed Green Baseline Timing Window (`>= 10` runs on `main`):**
   - Dispatches additional `workflow_dispatch` runs on `main` until at least `N = 10` (configurable `--baseline-runs`, default `10`) green completed runs exist on the current workflow version, giving `lanes/analysis` stable `p50`/`p95` critical-path and step-bucket measurements.
4. **Step 4 — Automated Readiness Preflight (`check-target`):**
   - Calls `lanes/analysis/history.py::fetch_history` + `find_bottlenecks` against `<owner>/<bench-repo>` and asserts `>= 3` ranked findings in `PLAYBOOK_SKILLS` with `expected_savings_seconds > 0`.
   - Calls `lanes/proof/miner.py::red_commits` against the repo's job history and asserts all 4 checks (`lint`, `typecheck`, `unit-test`, `ui-check`) have at least one `fixed=True` `RedCommit` (`uncovered_checks == set()`).
   - Prints a structured JSON readiness report (`repo`, `base_sha`, `green_runs`, `baseline_p50_seconds`, `baseline_p95_seconds`, `findings_count`, `red_commits_by_check`, `ready: true`).

---

## Live Milestone Verification Checklist (M2 & M3)

1. **Provision & Seed:**
   ```sh
   GITHUB_TOKEN=... uv run pit-crew seed-target --repo aj604/pit-crew-bench --baseline-runs 10
   GITHUB_TOKEN=... uv run pit-crew check-target --repo aj604/pit-crew-bench
   ```
2. **M2 — Live Managed Agent Smoke (All 3 Lanes):**
   ```sh
   GEMINI_API_KEY=... GITHUB_TOKEN=... uv run pit-crew smoke-lane --lane analysis --repo aj604/pit-crew-bench --ledger .pit_crew/m2.db
   GEMINI_API_KEY=... GITHUB_TOKEN=... uv run pit-crew smoke-lane --lane optimizer --repo aj604/pit-crew-bench --ledger .pit_crew/m2.db
   GEMINI_API_KEY=... GITHUB_TOKEN=... uv run pit-crew smoke-lane --lane proof --repo aj604/pit-crew-bench --ledger .pit_crew/m2.db
   ```
3. **M3 — Live Compounding Verdict + Pit Wall UI:**
   ```sh
   # Terminal 1: Pit Wall server + webhook tunnel
   GEMINI_API_KEY=... GITHUB_TOKEN=... PIT_CREW_WEBHOOK_SECRET=... uv run pit-crew serve --ledger .pit_crew/live.db
   # Terminal 2: Run cycle until 60s/120s SLA is met
   GEMINI_API_KEY=... GITHUB_TOKEN=... uv run pit-crew run-cycle --repo aj604/pit-crew-bench --ledger .pit_crew/live.db --until-sla
   ```

---

## Testing & Acceptance Criteria

- `benchmarks/target_template/` workflow YAML passes `zizmor` and `lanes/optimizer/validate.py` baseline parsing, and its template app tests pass offline.
- `tests/test_benchmark_seeder.py` tests seeding sequence, idempotency (re-running on an already-seeded repo skips completed steps), workflow-hash invariance across bug commits, and `check-target` preflight failure/pass conditions offline using `FakeGitHubClient` + fixture payloads.
- Passes `ruff format --check`, `ruff check`, `mypy src tests`, `deptry src`, `lint-imports`, `zizmor .github/workflows`, and `pytest --cov-fail-under=90`.
