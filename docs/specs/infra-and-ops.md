# Spec 4A: Runtime Packaging, Webhook Ingress & Repo Governance

## Goal

Package Pit Crew so it installs cleanly as a standalone wheel or Docker service (with all Pit Wall UI and Las Vegas Night Race brand assets intact), automate GitHub `workflow_run` webhook registration and HTTPS tunnel ingress, commit all architectural specs and ownership rules into git, reconcile overlapping tracker issues, and protect developer/CI hosts from worktree disk exhaustion.

---

## Scope & Deliverables

| Area | Files | Responsibility |
|---|---|---|
| **Wheel & CLI Packaging** | `pyproject.toml`, `src/pit_crew/server.py`, `.github/workflows/ci.yml` | Expose `pit-crew` console script (`pit_crew.cli:main`), bundle `web/**` and `brand/**` into wheels/sdists so installed packages serve `/` and `/brand/*` without a repo checkout, and verify in CI's isolated wheel smoke test. |
| **Container & Env Config** | `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.env.example` | Production/demo container (Python 3.12 + `uv`, `git`, `ffmpeg` for `proof/record.py`, non-root user, persistent `/data/ledger.db` WAL volume, `/health` check) and documented least-privilege environment template. |
| **Webhook Ingress & Cron** | `src/pit_crew/cli.py`, `src/pit_crew/github.py`, `scripts/dev_tunnel.sh` | Idempotent `pit-crew register-webhook` subcommand (`workflow_run` webhook + `PIT_CREW_WEBHOOK_SECRET` + ping verification), local HTTPS tunnel launcher (`cloudflared` / `ngrok` / `tailscale funnel`), and scheduled `POST /triggers/cycle` automation. |
| **Governance, Specs & Hygiene** | `docs/specs/*.md`, `.github/CODEOWNERS`, `README.md`, `TODO.md`, `scripts/prune_worktrees.sh` | Track all master specs in git, populate lane owners & `CODEOWNERS`, update `TODO.md` status, reconcile superseded `#44`–`#46` demo tickets, and provide safe merged-worktree cleanup. |

---

## Detailed Design

### 1. Console Entrypoint & Wheel Static Asset Bundling
- **`[project.scripts]` in `pyproject.toml`:**
  ```toml
  [project.scripts]
  pit-crew = "pit_crew.cli:main"
  ```
- **Brand & Web Asset Packaging:**
  - `src/pit_crew/web/` sits inside the package, whereas `brand/` sits at repository root (excluded from wheels by default in `uv_build`).
  - Configure `pyproject.toml` / package data (or ship a synced/packaged fallback resolved via `importlib.resources`) so `create_app()` serves `GET /`, `GET /web/*`, `GET /brand/tokens.css`, `GET /brand/logo-lockup-dark.svg`, `GET /brand/favicon.svg`, and `GET /brand/badges/*.svg` with `200 OK` both from a source checkout **and** from an isolated wheel install (`uv run --isolated --no-project --with dist/*.whl`).
  - Extend `.github/workflows/ci.yml` `Build package` job to smoke-test `pit-crew --help` and verify packaged static asset resolution from `dist/*.whl`.

### 2. Containerization & `.env.example`
- **`.env.example`:**
  - Documents every runtime variable with safe defaults and exact token scopes:
    - `GEMINI_API_KEY` (Google AI / Interactions API key for `gemini-4-hydrogen`, `gemini-4-neon`, `gemini-4-carbon`)
    - `GITHUB_TOKEN` (Fine-grained PAT scoped **strictly** to the target fork repository: `Contents: Read & Write`, `Actions: Read & Write`, `Pull requests: Read & Write`, `Webhooks: Read & Write` — never enters the remote sandbox)
    - `PIT_CREW_WEBHOOK_SECRET` (32-byte hex HMAC secret for `X-Hub-Signature-256` on `POST /webhooks/github`)
    - `PIT_CREW_API_TOKEN` (Optional Bearer token protecting `/api/repos/*` and `POST /triggers/cycle` when exposed publicly)
    - `PIT_CREW_LEDGER_PATH` (Default `/data/ledger.db` in Docker, `.pit_crew/ledger.db` locally)
    - `PIT_CREW_WORKSPACE` (Optional local checkout override; otherwise auto-materialized per cycle)
- **`Dockerfile` & `docker-compose.yml`:**
  - Multi-stage `python:3.12-slim` + pinned `uv` build installing locked production dependencies (`uv sync --locked --no-dev`), `git`, and `ffmpeg` (for `lanes/proof/record.py` terminal recordings; `agg` optional).
  - Runs as non-root user `pitcrew`, mounts `/data` for WAL-mode `SqliteLedger` (`ledger.db`, `ledger.db-wal`, `ledger.db-shm`), exposes port `8000`, defines `HEALTHCHECK` against `http://127.0.0.1:8000/health`, and defaults to `pit-crew serve --host 0.0.0.0 --port 8000 --ledger /data/ledger.db`.
  - Includes scheduled cron trigger example (GitHub Actions schedule or sidecar/cron invoking `POST /triggers/cycle` with `Authorization: Bearer $PIT_CREW_API_TOKEN`).

### 3. Automated GitHub Webhook Provisioning & HTTPS Ingress
- **`GitHubClient.ensure_webhook(repo, *, webhook_url, secret, events=("workflow_run",))` & `pit-crew register-webhook`:**
  - Command:
    ```sh
    GITHUB_TOKEN=... PIT_CREW_WEBHOOK_SECRET=... uv run pit-crew register-webhook \
      --repo owner/fork-repo \
      --url https://pit-crew.example.com/webhooks/github
    ```
  - Lists existing repository hooks (`GET /repos/{owner}/{name}/hooks`); if a hook targeting `--url` already exists, updates its `secret`, `events=["workflow_run"]`, `content_type="json"`, `active=True` via `PATCH`, otherwise creates it via `POST` and triggers a test ping (`POST /repos/{owner}/{name}/hooks/{hook_id}/pings`).
  - Rejects non-HTTPS URLs unless `--allow-insecure-http` is explicitly passed for local test harnesses.
- **Local Demo Tunnel Helper (`scripts/dev_tunnel.sh`):**
  - Detects `cloudflared`, `tailscale`, or `ngrok`, starts an HTTPS tunnel to `http://127.0.0.1:${PORT:-8000}`, prints the public `/webhooks/github` endpoint, and optionally runs `pit-crew register-webhook` when `--repo` is supplied.

### 4. Repository Governance, Spec Tracking & Disk Hygiene
- **Track `docs/specs/*.md` in Git:**
  - Commit `dispatcher-core.md`, `dispatcher-server.md`, `pit-wall-ui.md`, `e2e-integration.md`, `cross-lane-wiring.md`, `infra-and-ops.md`, and `benchmark-target-repo.md`, and index them in `README.md` and `docs/architecture.md`.
- **Ownership & `TODO.md` Alignment:**
  - Coordinate with PR `#82` (`docs/github-branding-pass`, which adds `CONTRIBUTING.md`, `.github/PULL_REQUEST_TEMPLATE.md`, and `.github/ISSUE_TEMPLATE/*`) so `README.md` and `.github/` changes merge cleanly without conflict.
  - Add `.github/CODEOWNERS` mapping `/src/pit_crew/lanes/analysis/` → `@TARUNSAIYAKKALA`, `/src/pit_crew/lanes/optimizer/` → `@mdmorsalinm`, `/src/pit_crew/lanes/proof/` → `@kreda-ship-it`, and core platform/contracts/rater/dispatcher/server/web → `@aj604` (keeping `require_code_owner_review: false` on `main` ruleset until desired so automation isn't deadlocked).
  - Fill in the Lane Owners column in `README.md` and update `TODO.md` checkboxes to match landed milestones.
- **Issue Tracker & `server.py` Demo Reconciliation (`#44`–`#46` / PR `#81` vs `#33`):**
  - Reconcile PR `#81` (`#44`, `src/pit_crew/demo/` REST+SSE endpoints on `server.py`) with `feat/pit-wall-ui` (`#33`, `/web/demo_scenarios.js` + static mounts on `server.py`) and `#45`/`#46` so only one cohesive demo path lands on `server.py` without route or `import-linter` collisions.
- **Safe Worktree Disk Pruner (`scripts/prune_worktrees.sh`):**
  - Supports `--dry-run` (default) and `--apply`: inspects `git worktree list`, skips the main checkout and any worktree with uncommitted changes or an open PR / unmerged active branch, removes finished agent worktrees (`git worktree remove` + `git worktree prune`), and reports reclaimed disk space.

---

## Testing & Acceptance Criteria

- Offline unit tests in `tests/test_cli.py` / `tests/test_github.py` / `tests/test_packaging.py`:
  - `register-webhook` creates a new `workflow_run` hook when absent, updates idempotently when present, rejects plain `http://` by default, and never logs `PIT_CREW_WEBHOOK_SECRET` or `GITHUB_TOKEN`.
  - Isolated wheel build (`uv build`) contains both `web/` and `brand/` assets and passes `pit-crew --help` + FastAPI `TestClient` checks for `/` and `/brand/tokens.css`.
  - `.github/workflows/*.yml` passes `uv run zizmor .github/workflows`.
- Passes `ruff format --check`, `ruff check`, `mypy src tests`, `deptry src`, `lint-imports`, `zizmor .github/workflows`, and `pytest --cov-fail-under=90`.
