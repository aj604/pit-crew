# Spec 3: End-to-End Integration & Live Smoke Harness (M1 → M2 → M3)

## Goal

Prove that every Pit Crew component—Analysis, Optimizer, Proof, `managed_agent.py`, `Dispatcher`, `SqliteLedger`, `rater.py`, and the FastAPI Pit Wall API—works together end-to-end across three progressive milestones:

- **M1 (Offline E2E in `pytest`):** Full `Hypothesis → Candidate → ReplayPlan → Evidence → Verdict → PR → Pit Wall API` pipeline with zero API keys or network calls.
- **M2 (Live Managed Agent Smoke):** One real Gemini 4 Managed Agent `analysis` call (`GEMINI_API_KEY`) writing a real `Hypothesis` to `SqliteLedger`.
- **M3 (Live Fork Verdict):** One full `Dispatcher.run_cycle(repo)` on a real GitHub fork (`GEMINI_API_KEY` + `GITHUB_TOKEN`) producing a measured `Evidence` record and `Verdict` from real GitHub Actions runs.

---

## Module & File Layout

| File | Responsibility |
|---|---|
| `tests/test_e2e.py` | M1 offline end-to-end integration test suite (runs in default `pytest`, zero network/secrets). |
| `src/pit_crew/cli.py` | Thin CLI entrypoint (`uv run python -m pit_crew.cli ...`) providing `smoke-lane` (M2), `run-cycle` (M3), and `serve` (FastAPI + Pit Wall UI). |
| `tests/test_cli.py` | Offline unit tests for CLI argument parsing and subcommand wiring (injecting fake client/dispatcher). |

---

## Milestone 1: Offline End-to-End Suite (`tests/test_e2e.py`)

Uses real `SqliteLedger` (in `tmp_path`), real `tools.call()`, real `managed_agent.run_lane()` (with a deterministic fake `InteractionsClientLike`), real `Dispatcher`, real `rater.rate()`, and FastAPI `TestClient` (with `FakeGitHubClient`).

### Required Scenarios
1. **Proven Speedup (`KEEP` + SLA Met):**
   - `analysis` records a valid `Hypothesis` via `tools.call()`.
   - `optimizer` records a `Candidate` touching `.github/workflows/ci.yml`.
   - `proof` records a `ReplayPlan` with both a historical `commit` `BugCase` and a synthetic `mutation` `BugCase`.
   - `Dispatcher` runs 5 interleaved baseline/candidate timing pairs + replays both bugs (both baseline and candidate fail—i.e., catch—both the historical bug and the synthetic mutation), writes `Evidence`, runs `rater.rate()`, persists `Verdict(keep=True, sla_met=True)`, and opens a PR via `FakeGitHubClient`.
   - Verify `GET /api/repos/{owner}/{name}/status`, `/attempts`, and `/records` reflect the persisted `SqliteLedger` state accurately.
2. **Fast-but-Unsafe (`REJECT`):**
   - Candidate is 1.9× faster (`p < 0.05`) but misses 1 of 4 bugs → `Verdict(keep=False)`, no PR opened, rejection persisted in `ledger.prior_attempts(repo)`.
3. **Trust Boundary Violation (`REJECT`):**
   - Optimizer proposes a `Candidate` modifying `src/app.py` → `Dispatcher` rejects before dispatching any GitHub Actions runs.
4. **Compounding Loop (`run_until_sla`):**
   - Cycle 1 produces `Verdict(keep=True, sla_met=False)` (`median = 85s`), advancing `base_sha` to `latest_kept_candidate(repo).head_sha`.
   - Cycle 2 produces `Verdict(keep=True, sla_met=True)` (`median = 48s`, `p95 = 82s`), and `run_until_sla()` stops cleanly.

---

## Milestone 2: Live Managed Agent Smoke (`pit_crew.cli smoke-lane`)

Command:
```sh
GEMINI_API_KEY=... uv run python -m pit_crew.cli smoke-lane \
  --lane analysis \
  --repo owner/fork-repo \
  --ledger .pit_crew/smoke.db
```

Behavior:
- Uses a built-in default task prompt per lane (with an optional `--task` override).
- Calls `managed_agent.run_lane(lane, task, ledger=SqliteLedger(path), repo=repo)`.
- Verifies at least one expected record (`Hypothesis` for `analysis`, `Candidate` for `optimizer`, `ReplayPlan` for `proof`) was persisted to the ledger.
- Prints a concise JSON summary (`interaction_id`, `status`, `records_written`, `elapsed_seconds`) and exits non-zero if the lane fails or writes no record.

---

## Milestone 3: Live Fork Cycle (`pit_crew.cli run-cycle`)

Command:
```sh
GEMINI_API_KEY=... GITHUB_TOKEN=... uv run python -m pit_crew.cli run-cycle \
  --repo owner/fork-repo \
  --ledger .pit_crew/ledger.db \
  --timing-pairs 5 \
  --no-open-pr
```

Behavior:
- Wires `SqliteLedger` + `HttpxGitHubClient` + `managed_agent.run_lane` into `Dispatcher`.
- Runs `run_cycle(repo)` (or `run_until_sla(repo)` with `--until-sla`, bounded by `--max-cycles` and `--sla-seconds` so live runs cannot loop indefinitely).
- Supports `--no-open-pr` to skip opening a PR on the target fork during dry runs.
- Because it writes to WAL-mode `SqliteLedger`, `pit_crew.cli serve --ledger .pit_crew/ledger.db --host 127.0.0.1 --port 8000` (using `uvicorn`) can run concurrently to watch the live cycle on the Pit Wall UI.
- Prints a structured summary of timings, bug replay outcomes, `Verdict`, and PR URL (if opened).

---

## Dependencies & Architecture Notes

- **Prerequisites:** M1 and M3 depend on Spec 1 (`Dispatcher`, `HttpxGitHubClient`, `FakeGitHubClient`) and Spec 2 (FastAPI Pit Wall API endpoints `/api/repos/{owner}/{name}/status`, `/attempts`, `/records`, plus `fastapi`/`uvicorn` dependencies).
- **Import boundaries:** `pit_crew.cli` is a top-level entrypoint and must be permitted by `pyproject.toml`'s `import-linter` contracts to import `SqliteLedger`, `managed_agent`, `Dispatcher`, `HttpxGitHubClient`, and the FastAPI server app.

---

## Testing & Acceptance Criteria

- Default `uv run pytest` runs all M1 E2E tests and CLI unit tests offline with zero API keys and passes `--cov-fail-under=90`.
- `ruff format --check`, `ruff check`, `mypy src tests`, `deptry src`, and `lint-imports` all pass.
- Manual verification checklist documented for M2 (`smoke-lane`) and M3 (`run-cycle` on a test fork).
