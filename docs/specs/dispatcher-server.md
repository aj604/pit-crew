# Spec 1B: Webhook Server & Pit Wall API (`server.py`)

## Goal

Expose a single FastAPI service (`src/pit_crew/server.py`) that:
1. Receives GitHub `workflow_run` webhooks (with HMAC-SHA256 verification and delivery idempotency) to drive the Dispatcher without busy-polling.
2. Accepts manual / cron cycle triggers for tracked repositories.
3. Serves read-only JSON telemetry from `SqliteLedger` for the Pit Wall UI.

---

## Dependencies

Add to `pyproject.toml`:
- `fastapi`
- `uvicorn`
- `httpx` (shared with `HttpxGitHubClient` in Spec 1A)

---

## Module Layout

| File | Responsibility |
|---|---|
| `src/pit_crew/server.py` | FastAPI app factory `create_app(ledger, dispatcher, webhook_secret)`, webhook signature verification, delivery deduplication, background cycle runner, and read-only telemetry endpoints. |
| `src/pit_crew/run_tracker.py` | Small in-memory/SQLite-backed awaitable registry mapping GitHub `run_id` → `asyncio.Event` + `WorkflowRunResult`, so `workflow_run` webhooks wake the Dispatcher immediately (with polling fallback if webhooks are unavailable). |

---

## HTTP Endpoints

### 1. Webhooks & Triggers
- `POST /webhooks/github`
  - Verifies `X-Hub-Signature-256` against `PIT_CREW_WEBHOOK_SECRET` using `hmac.compare_digest` (returns `401` on mismatch).
  - Deduplicates on `X-GitHub-Delivery` header (returns `200 {"status": "duplicate"}` on replay).
  - Handles `X-GitHub-Event: workflow_run` (`action == "completed"`): resolves any pending `run_id` in `RunTracker` so the Dispatcher wakes immediately.
  - Returns `202` for accepted events, `200` for ignored events.
- `POST /triggers/cycle`
  - Body: `{"repo": "owner/name", "until_sla": false}`
  - Enqueues `dispatcher.run_cycle(repo)` (or `run_until_sla(repo)`) as a background task, guarded by a per-repo lock so two cycles never race on the same fork.
  - Returns `202 {"status": "started", "repo": "owner/name"}` (or `409` if a cycle is already running for that repo).
- `GET /health`
  - Returns `200 {"status": "ok", "ledger": "sqlite"}`.

### 2. Read-Only Telemetry (for Pit Wall UI)
- `GET /api/repos/{owner}/{name}/attempts`
  - Returns `ledger.prior_attempts(f"{owner}/{name}")`.
- `GET /api/repos/{owner}/{name}/records`
  - Returns structured `hypotheses`, `candidates`, `replay_plans`, `evidence`, and `verdicts` for the repo.
- `GET /api/repos/{owner}/{name}/status`
  - Returns whether a cycle is currently active, current `base_sha`, latest `Verdict`, and whether the `60s / 120s` SLA is met.

---

## Configuration (Environment Variables)

| Variable | Required | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | Live runs only | Passed to `managed_agent.run_lane()` |
| `GITHUB_TOKEN` | Live runs only | Passed to `HttpxGitHubClient` (never enters sandbox) |
| `PIT_CREW_WEBHOOK_SECRET` | Production | Verifies `X-Hub-Signature-256` on `/webhooks/github` |
| `PIT_CREW_LEDGER_PATH` | Optional (default `.pit_crew/ledger.db`) | Path to WAL-mode `SqliteLedger` |

---

## Testing & Acceptance Criteria

- `tests/test_server.py` using FastAPI `TestClient` + `InMemoryLedger` / temp `SqliteLedger`:
  - Valid HMAC signature accepted; invalid/missing signature returns `401`.
  - Duplicate `X-GitHub-Delivery` is ignored idempotently.
  - `workflow_run` (`completed`) resolves a pending `RunTracker` entry.
  - `POST /triggers/cycle` starts a cycle and returns `409` if already running for the same repo.
  - `/api/repos/{owner}/{name}/attempts` and `/records` return accurate JSON from the ledger.
- Passes `ruff`, `mypy --strict`, `deptry`, `lint-imports`, and `pytest --cov-fail-under=90`.
