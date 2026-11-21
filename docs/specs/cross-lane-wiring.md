# Spec 3B: Cross-Lane Seam & Event-Driven Runtime Wiring

## Goal

Bridge every remaining runtime and protocol seam between the individual lanes (`analysis`, `optimizer`, `proof`), `managed_agent.py`, `Dispatcher` (Spec 1A, `#11`), the FastAPI webhook/telemetry server (`RunTracker` + `server.py`, Spec 1B, `#12`), the Pit Wall UI (Spec 2, `#33`), and the CLI harness (Spec 3, `#40`) so Milestone 2 (`smoke-lane`) and Milestone 3 (`run-cycle` + live Pit Wall server) work end-to-end against real Gemini 4 Managed Agents and GitHub Actions without manual workarounds.

---

## Why This Spec Exists (Gap Audit)

Each lane and service passed unit tests against mocks in isolation, leaving six concrete integration seams unowned:

1. **Optimizer → Dispatcher Branch Publishing Gap:** `lanes/optimizer/tools.py::report_candidate` records `Candidate.files` with `head_sha=""` because the remote sandbox holds zero GitHub credentials (`"The host publishes the branch"`). `Dispatcher.run_cycle()` (`#17`/`#18`) dispatches timing workflows against `candidate.branch` without ever creating the branch or committing `Candidate.files` on GitHub—and because `head_sha` stays `""`, `Dispatcher.resolve_base_sha()` cannot advance `base_sha` during multi-cycle compounding (`#20`).
2. **Host `/workspace` Trap on `@tool` Calls:** All `@tool` functions execute **on the Dispatcher host** via `managed_agent.py::_execute_tool_step` → `tools.call()`, not inside Google's remote sandbox. `lanes/analysis/tools.py` (`profile_ci`, `find_bottlenecks`) reads `.github/workflows/<workflow>` from `os.environ.get("PIT_CREW_WORKSPACE", "/workspace")` on the host disk, crashing with `FileNotFoundError` on live runs unless the host materializes the target repo at `base_sha` in a thread-safe workspace context.
3. **Live Interactions API Continuation, Serialization & Repo URL Normalization:** On `main`, follow-up `interactions.create()` turns omit `environment` (`environment_id`), `agent_config`, and `tools` (HTTP `400` on live tool loops), `_execute_tool_step` passes raw `list`/`int`/`bool`/`None` values rejected by `google-genai>=3.6.1` `FunctionResultStep` validation (fixed in open PR `#61`), and `repository_source(repo)` passes bare `"owner/name"` strings without normalizing to `"https://github.com/owner/name"`.
4. **`RunTracker` ↔ `Dispatcher` Disconnected Islands:** `webhooks.py` resolves `RunTracker` (`pit_crew.run_tracker.WorkflowRunResult`) on `workflow_run.completed`, while `Dispatcher.poll_workflow_run()` runs in a worker thread (`asyncio.to_thread`), ignores `RunTracker`, and busy-polls `github.get_workflow_run()` (`pit_crew.github.WorkflowRunResult`) every 5s. Meanwhile `/api/repos/{owner}/{name}/status` shows no progress granularity while 10+ timing/replay runs execute.
5. **Proof Pre-Pass, Multimodal `gemini-4-carbon` Verification & SVG Proof Card Unwired:** Merged Proof lane (`lanes/proof/{checks,miner,tools,verify,card}.py`, `#9`) expects pre-computed `touched_checks`, `red_commits`, `pick_replays`, and `uncovered_checks` in its prompt, provides `compare_failures(..., ask=managed_agent.ask_multimodal)` so unrelated candidate crashes do not count as caught bugs, and renders `card.render_card()` SVG + `card.pr_body()` for kept PRs—none of which `Dispatcher` currently invokes.
6. **`PIT_CREW_API_TOKEN` Drift Across Server, CLI & Pit Wall UI:** `server.py` on `main` supports optional `api_token` (`HTTPBearer`) and case-insensitive `_normalize_repo`, which branched after `feat/pit-wall-ui` (`#33`) and `feat/e2e-integration-40` (`#40`) started. `cli.py serve` (`#71`) and Pit Wall Live Mode (`#53`) must preserve and forward `PIT_CREW_API_TOKEN` without gating public static routes (`/`, `/web/*`, `/brand/*`, `/health`).

---

## Module & File Layout

| File | Responsibility |
|---|---|
| `src/pit_crew/managed_agent.py` | Reconcile PR `#61` (`environment_id` + `agent_config` + `tools` on follow-up turns, `_serialize_tool_result`), normalize `repository_source(repo)` (`owner/name` → `https://github.com/owner/name`), and fail fast on terminal error statuses (`failed`, `cancelled`, `expired`). |
| `src/pit_crew/workspace.py` | Thread/async-safe `ContextVar` host workspace manager + `materialize_repo_workspace(github, repo, ref)` extracting repo archive/files at `ref` into a temporary directory; wired into `lanes/analysis/tools.py::workspace()`, `Dispatcher`, and `cli.py smoke-lane`. |
| `src/pit_crew/github.py` | Add `download_tarball(repo, ref) -> bytes` (for host workspace materialization), `list_workflow_job_runs(repo, limit)` (for `proof.miner.red_commits`), and `get_run_failure_output(repo, run_id)` (failed job logs / artifact bytes for `proof.verify`). |
| `src/pit_crew/run_tracker.py` | Add thread-safe `wait_sync(run_id, timeout)` alongside async `wait()` (bridging `threading.Event` and `asyncio.Event`) and unify/convert `WorkflowRunResult` cleanly with `pit_crew.github.WorkflowRunResult`. |
| `src/pit_crew/dispatcher.py` | Add `publish_candidate()` (`Candidate.files` → fork branch + `Candidate.head_sha` update in `Ledger`), `run_tracker` wakeup in `poll_workflow_run` with REST fallback + `discard()` cleanup, workspace materialization around lane execution, and active stage/progress callback hooks. |
| `src/pit_crew/proof_bridge.py` | Top-level coordinator allowed by `import-linter` to connect `Dispatcher` with `pit_crew.lanes.proof.{checks,miner,tools,verify,card}` and `managed_agent.ask_multimodal` without violating lane-to-lane independence or agent trust boundaries. |
| `src/pit_crew/server.py` & `src/pit_crew/web/` | Preserve `api_token` (`HTTPBearer`) + `_normalize_repo` alongside Pit Wall static mounts, expose additive `stage` / `progress` fields on `GET /api/repos/{owner}/{name}/status`, and support optional Bearer token header in Pit Wall Live Mode and `cli.py serve`. |

---

## Detailed Design

### 1. Host Candidate Publishing (`Dispatcher.publish_candidate`)
1. Runs in `Dispatcher.run_cycle()` immediately **after** `validate_candidate_paths(candidate)` passes (disallowed paths are still rejected with zero GitHub API mutations).
2. If `candidate.head_sha` is already non-empty and `not candidate.files`, return `candidate` as-is.
3. Create (or force-reset if retrying) `candidate.branch` from `resolved_base_sha` via `github.create_branch(repo, candidate.branch, resolved_base_sha)`.
4. Commit `candidate.files` atop `resolved_base_sha` via `github.commit_files(repo, candidate.branch, resolved_base_sha, candidate.files, f"Pit Crew ({candidate.branch}): {candidate.summary}")` → `head_sha`.
5. Persist `published = dataclasses.replace(candidate, repo=repo, base_sha=resolved_base_sha, head_sha=head_sha)` via `ledger.add(published)` so downstream timing runs, `server.py` `/status`, and `resolve_base_sha()` compounding all read the real `head_sha`.

### 2. Thread-Safe Host Workspace Materialization (`workspace.py`)
1. Define `_ACTIVE_WORKSPACE: ContextVar[Path | None] = ContextVar("pit_crew_workspace", default=None)` and `current_workspace() -> Path` returning `_ACTIVE_WORKSPACE.get() or Path(os.environ.get("PIT_CREW_WORKSPACE", "/workspace"))`.
2. Update `lanes/analysis/tools.py::workspace()` to delegate to `current_workspace()` (preserving `PIT_CREW_WORKSPACE` compatibility for existing unit tests).
3. Context manager `materialized_workspace(github, repo, ref)`:
   - Downloads `GET /repos/{owner}/{name}/tarball/{ref}`, safely extracts regular files (guarding against zip-slip / path traversal and symlinks) into `TemporaryDirectory(prefix="pit-crew-ws-")`, sets `_ACTIVE_WORKSPACE`, and cleans up on exit.
   - Support an explicit `--workspace <path>` CLI flag on `smoke-lane` and `run-cycle` (matching the `README.md` Live Smoke Test quickstart in PR `#82`) that binds `_ACTIVE_WORKSPACE` to the given local checkout, and automatically fall back to `materialized_workspace(github, repo, ref)` around lane execution when `--workspace` is omitted and `PIT_CREW_WORKSPACE` is unset.

### 3. Live `managed_agent.py` & Repo Source Normalization
1. Land/incorporate PR `#61`:
   - Re-attach `environment={"type": "remote", "environment_id": interaction.environment_id}` (falling back to initial `request["environment"]`), `agent_config`, and `tools` on `requires_continuation` and `requires_action` turns.
   - JSON-encode `list`, `tuple`, `int`, `float`, `bool`, and `None` tool outputs via `_serialize_tool_result` while keeping `str` and `dict` native.
2. Update `repository_source(repo, target="/workspace")`:
   - Accept `"owner/name"` and normalize to `"https://github.com/owner/name"` while passing full `https://...` URLs through unchanged.
3. Raise a descriptive `InteractionFailedError(lane, interaction.id, interaction.status)` if polling terminates in `("failed", "cancelled", "expired")`.

### 4. Event-Driven `RunTracker` Bridge & Live Cycle Progress
1. Extend `RunTracker` with a `threading.Condition` / `threading.Event` paired with `loop.call_soon_threadsafe(async_event.set)` so `resolve()` wakes **both** async waiters (`await tracker.wait(...)`) and worker-thread sync waiters (`tracker.wait_sync(run_id, timeout)`).
2. Pass the shared `RunTracker` into `Dispatcher(..., run_tracker=tracker)` and `create_app(..., dispatcher=dispatcher, run_tracker=tracker)` in `cli.py serve`.
3. In `Dispatcher._dispatch_and_poll(repo, workflow, ref)`:
   - Call `run_id = github.dispatch_workflow(repo, workflow, ref)` and `run_tracker.register(run_id)` (cleaning up via `run_tracker.discard(run_id)` in `finally`).
   - Instead of `self._sleep(poll_interval_seconds)`, wait on `run_tracker.wait_sync(run_id, timeout=poll_interval_seconds)` so a `workflow_run.completed` webhook wakes the poller immediately, then confirm/fetch `github.get_workflow_run(repo, run_id)` (ensuring accurate `duration_seconds` and fallback when no webhook arrives).
4. Add an optional progress reporter updating `server.py` per-repo state (`stage`: `"analysis" | "optimizer" | "proof" | "timing" | "replay" | "rating"`, `completed_runs`, `total_runs`) exposed additively on `GET /api/repos/{owner}/{name}/status`.

### 5. Proof Bridge: Pre-Pass, `gemini-4-carbon` Failure Comparison & SVG Proof Card (`proof_bridge.py`)
1. **Import-linter update (`pyproject.toml`):** Keep all 5 existing contracts intact (`Lanes must not import each other`, `Contracts has no internal dependencies`, `Rater is pure and isolated`, `Lanes respect the trust boundary`, `Only managed_agent may import google-genai`), allowing top-level `pit_crew.proof_bridge` to coordinate `github`, `managed_agent.ask_multimodal`, and `pit_crew.lanes.proof.{checks,miner,tools,verify,card}`.
2. **Pre-Proof deterministic context:** Before invoking `lane_runner("proof", ...)`:
   - Diff original workflow YAML (at `base_sha`) against `candidate.files` via `proof.checks.touched_checks()`.
   - Mine deterministic red commits from `github.list_workflow_job_runs(repo)` via `proof.miner.red_commits()`.
   - Compute `required_replays = proof.tools.pick_replays(red, touched)` and `uncovered = proof.tools.uncovered_checks(red, touched)` and embed them in the `proof` task prompt.
3. **Multimodal replay verification (`gemini-4-carbon`):** When both baseline and candidate replay runs conclude `"failure"`, optionally verify failure equivalence via `proof.verify.compare_failures(..., ask=managed_agent.ask_multimodal)` against captured failure logs/recordings; a `"different failure"` or `"unverified"` outcome does **not** increment `bugs_caught` (preserving fail-closed safety).
4. **Visual Proof Card on Kept PRs:** When `verdict.keep` and `config.auto_open_pr`:
   - Render SVG via `proof.card.render_card(candidate.branch, evidence, verdict, replays)`.
   - Commit `.github/pit-crew/proof-card.svg` (permitted by `is_allowed_candidate_path`) onto `candidate.branch` and format the PR description with `proof.card.pr_body(..., card_url=raw_svg_url)`.

### 6. `PIT_CREW_API_TOKEN` Preservation Across Server, CLI & UI
1. Reconcile `server.py` so static Pit Wall routes (`GET /`, `/web/*`, `/brand/*`) and `GET /health` remain public, `POST /webhooks/github` uses HMAC `PIT_CREW_WEBHOOK_SECRET`, and `/api/repos/*` + `POST /triggers/cycle` enforce `HTTPBearer` iff `api_token` (`PIT_CREW_API_TOKEN`) is configured.
2. Wire `api_token=os.environ.get("PIT_CREW_API_TOKEN") or None` in `cli.py serve`.
3. In Pit Wall UI Live Mode, read an optional API token from `?token=` / `sessionStorage` (prompting non-destructively on `401`) and attach `Authorization: Bearer <token>` on `/api/*` and `/triggers/cycle` requests.

---

## Prerequisites & Dependency Map

- Builds on Spec 1A (`#11`, `feat/dispatcher-core`), Spec 1B (`#12`, merged in PR `#65`), Spec 2 (`#33`, `feat/pit-wall-ui`), Spec 3 (`#40`, `feat/e2e-integration-40`), Proof lane (`#9`, merged), Optimizer lane (PR `#59`, merged), Analysis detectors (`#34`–`#37`, `#43`), and live `managed_agent.py` fix PR `#61`.

---

## Testing & Acceptance Criteria

- 100% offline unit + integration tests (`tests/test_workspace.py`, `tests/test_proof_bridge.py`, expanded `tests/test_dispatcher.py`, `tests/test_run_tracker.py`, `tests/test_server.py`, `tests/test_e2e.py`) with zero API keys or live network calls:
  - Optimizer `Candidate(head_sha="", files={...})` is published to `FakeGitHubClient`, `head_sha` is persisted in `SqliteLedger`, and Cycle 2 compounds atop Cycle 1's published `head_sha`.
  - Concurrent `materialized_workspace` contexts isolate `current_workspace()` across worker threads without mutating `os.environ` and block archive path traversal (`..` / absolute paths / external symlinks).
  - `RunTracker.resolve()` from an async FastAPI webhook handler wakes a synchronous `Dispatcher.poll_workflow_run()` running inside `asyncio.to_thread` in `< 50ms` (with `poll_interval_seconds=60.0` proving it did not wait for polling).
  - `ProofBridge` supplies `required_replays` + `uncovered_checks` to `proof`, downgrades an unrelated candidate crash via `compare_failures` so `rater.rate()` rejects it, and commits `.github/pit-crew/proof-card.svg` + `pr_body` on a kept candidate PR.
  - `PIT_CREW_API_TOKEN` protects `/api/repos/*` and `/triggers/cycle` while `/` and `/brand/tokens.css` remain `200` without a token.
- Passes `ruff format --check`, `ruff check`, `mypy src tests`, `deptry src`, `lint-imports`, `zizmor .github/workflows`, and `pytest --cov-fail-under=90`.
