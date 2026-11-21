# Spec 1A: Core Dispatcher & GitHub Actions Orchestrator

## Goal

Turn `src/pit_crew/dispatcher.py` from a routing stub into the deterministic orchestration engine that drives a repository through compounding Pit Crew cycles:

`Hypothesis → Candidate → ReplayPlan → GitHub Actions (timings + bug replays) → Evidence → Verdict → PR / Compound`

All GitHub API calls sit behind a narrow `GitHubClient` protocol so the entire orchestration state machine, replay overlay logic, evidence aggregation, and compounding loop are 100% unit-testable offline with zero network calls or API keys.

---

## Module Layout

| File | Responsibility |
|---|---|
| `src/pit_crew/github.py` | `GitHubClient` protocol + `HttpxGitHubClient` implementation (branches, commits/overlays, workflow dispatch, run polling/status, PR creation). Holds `GITHUB_TOKEN` strictly on the host. |
| `src/pit_crew/dispatcher.py` | `next_lane()` (preserved), `DispatcherConfig`, `Dispatcher` class (cycle driver, lane handoff, interleaved timing scheduler, bug replay runner, `Evidence` builder, `Verdict` writer, post-verdict PR / compounding). |
| `src/pit_crew/managed_agent.py` | Small update: accept `repo: str | None = None` in `run_lane()` and forward it to `build_request()`. |

**Import-linter compliance:**
- `pit_crew.dispatcher` imports `contracts`, `ledger`, `rater`, `managed_agent`, `github`.
- `pit_crew.dispatcher` never imports `google` or any `pit_crew.lanes.*` module directly.

---

## Key Interfaces

### 1. `GitHubClient` (`src/pit_crew/github.py`)

```python
@dataclass(frozen=True)
class WorkflowRunResult:
    run_id: int
    status: str            # "completed" | "in_progress" | "queued"
    conclusion: str | None # "success" | "failure" | "cancelled" | ...
    duration_seconds: float
    html_url: str
    head_sha: str
    head_branch: str

class GitHubClient(Protocol):
    def get_default_branch_sha(self, repo: str) -> str: ...
    def create_branch(self, repo: str, branch: str, base_sha: str) -> str: ...
    def delete_branch(self, repo: str, branch: str) -> None: ...
    def commit_files(
        self,
        repo: str,
        branch: str,
        base_sha: str,
        files: dict[str, str],
        message: str,
    ) -> str:
        """Create a commit on ``branch`` atop ``base_sha`` with ``files``; returns new ``head_sha``."""
        ...
    def dispatch_workflow(self, repo: str, workflow: str, ref: str) -> int:
        """Trigger workflow_dispatch (or empty-commit push fallback) and return ``run_id``."""
        ...
    def get_workflow_run(self, repo: str, run_id: int) -> WorkflowRunResult: ...
    def open_pull_request(
        self,
        repo: str,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> str:
        """Open a PR and return its HTML URL."""
        ...
```

### 2. `DispatcherConfig` & `Dispatcher` (`src/pit_crew/dispatcher.py`)

```python
@dataclass(frozen=True)
class DispatcherConfig:
    timing_pairs: int = 5           # >= rater.MIN_RUNS (5); interleaved B, C, B, C, ...
    max_compounding_cycles: int = 5 # safety cap per repo run
    workflow_file: str = "ci.yml"   # default workflow to dispatch if Hypothesis.workflow_path is empty
    run_timeout_seconds: float = 900.0
    poll_interval_seconds: float = 5.0
    auto_open_pr: bool = True
```

Core methods on `Dispatcher(ledger, github, lane_runner, config)`:
- `run_cycle(repo: str, base_sha: str | None = None) -> Verdict | None`
  1. Resolve `base_sha` from `ledger.latest_kept_candidate(repo).head_sha` (compounding) or `github.get_default_branch_sha(repo)`.
  2. Run `analysis` lane → pick highest-impact unattempted `Hypothesis` for `repo`. If none, return `None`.
  3. Run `optimizer` lane with `Hypothesis` + `ledger.prior_attempts(repo)` → read new `Candidate`.
     - **Guard:** Validate every path in `candidate.changed_files` with `contracts.is_allowed_candidate_path()`. If any path is disallowed, immediately record a rejected `Verdict` (`keep=False`, reason `"disallowed path modified"`) without running CI.
  4. Run `proof` lane with `Candidate` → read new `ReplayPlan`.
  5. Measure `Evidence` via `collect_evidence(candidate, replay_plan, base_sha)`.
  6. Score via `verdict = rater.rate(evidence)` and persist `Evidence` + `Verdict` to `ledger`.
  7. Post-verdict:
     - If `verdict.keep` and `config.auto_open_pr`: open PR with speedup, p-value, median/p95, SLA status, and replay table.
     - If not `verdict.keep`: leave rejection in ledger (and optionally clean up replay branches).
  8. Return `verdict`.
- `run_until_sla(repo: str) -> list[Verdict]`
  - Loops `run_cycle(repo)` up to `max_compounding_cycles`, stopping early when `verdict is None` or `verdict.sla_met` is `True`.

---

## Measurement & Replay Execution (`collect_evidence`)

### A. Interleaved Timing Runs
- Alternate `baseline` (`base_sha`) and `candidate` (`candidate.branch` / `candidate.head_sha`) for `config.timing_pairs` pairs (`B1, C1, B2, C2, ..., Bn, Cn`).
- Interleaving cancels out GitHub Actions runner-load drift; includes cold Run #1 per locked ADR #3.
- Any timing run that concludes != `"success"` aborts timing collection for that side (candidate broke green CI → `bugs_caught` or empty timings causes `rater.rate()` to reject).

### B. Bug Replays (`ReplayPlan.cases`)
For each `BugCase` in `replay_plan.cases`:
1. **`kind == "commit"` (`bug_case.commit_sha`):**
   - Create temporary branch `pc/replay-<candidate_id>-<idx>` from `bug_case.commit_sha`.
   - Overlay `candidate.files` onto `bug_case.commit_sha` via `github.commit_files(...)`.
   - Dispatch workflow and wait for completion.
   - **Caught** iff `conclusion == "failure"`.
   - Delete temporary replay branch in `finally`.
2. **`kind == "mutation"` (`bug_case.mutated_files`):**
   - **Baseline verification (anti-gaming):** Create temp branch from `base_sha` with `bug_case.mutated_files` only. Dispatch workflow. If baseline does **not** fail, mark mutation invalid (`baseline_failed=False`) and exclude it from `bugs_total` so dud mutations cannot pad `bugs_caught`.
   - **Candidate verification:** Create temp branch from `base_sha` with `bug_case.mutated_files | candidate.files` (candidate CI config overlaid on the mutated code). Dispatch workflow.
   - **Caught** iff baseline failed **and** candidate concluded `"failure"`.
   - Delete temporary replay branches in `finally`.

Record per-case details in `Evidence.bug_outcomes` (e.g. `{"kind", "ref", "caught", "baseline_failed", "run_url"}`) and all timing/replay URLs in `Evidence.run_urls`.

---

## Testing & Acceptance Criteria

- `tests/test_dispatcher.py` using `InMemoryLedger`, `FakeGitHubClient`, and a fake `lane_runner`:
  - Full `Hypothesis → Candidate → ReplayPlan → Evidence → Verdict(keep=True)` cycle + PR opened.
  - Reject when a bug replay passes (`bugs_caught < bugs_total`) → no PR opened.
  - Reject when `Candidate` touches a disallowed path (e.g. `src/app.py`) → CI never dispatched.
  - Synthetic mutation baseline check: dud mutation that passes baseline is excluded; if all mutations are duds (`bugs_total == 0`), Rater rejects.
  - Compounding loop: `run_until_sla()` advances `base_sha` to `latest_kept_candidate.head_sha` and stops as soon as `verdict.sla_met` is `True`.
  - Temporary replay branches are always cleaned up even on error.
- Passes `ruff`, `mypy --strict`, `deptry`, `lint-imports`, and `pytest --cov-fail-under=90`.
