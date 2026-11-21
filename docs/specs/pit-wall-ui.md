# Spec 2: Pit Wall UI & Telemetry Dashboard

## Goal

Build the **Pit Wall** web dashboard—styled strictly with the Pit Crew F1 Las Vegas Night Race brand kit (`brand/tokens.css`, `brand/logo-lockup-dark.svg`, `brand/favicon.svg`)—that visualizes the autonomous CI optimization loop and makes the core thesis immediately legible:

> **The agent proposes. Deterministic code judges.**

The UI runs in two modes with zero external build/runtime overhead:
1. **Live Mode:** Served directly by FastAPI (`src/pit_crew/server.py` at `/`), reading from Spec 1B endpoints (`/api/repos/{owner}/{name}/status`, `/attempts`, `/records`) and triggering cycles via `POST /triggers/cycle`.
2. **Offline Demo Mode (M1):** Built-in deterministic scenario player (no `GEMINI_API_KEY` or `GITHUB_TOKEN` required) for instant demos and UI verification.

---

## Brand & Design System Integration

All colors, typography, radii, and assets come directly from `brand/` (no ad-hoc hex values):

- **Stylesheet:** Mount `brand/tokens.css` at `/brand/tokens.css`.
- **Assets:** `brand/logo-lockup-dark.svg`, `brand/favicon.svg`, `brand/badges/*`.
- **Surfaces:** `--pc-bg-void` (`#0B0B11`), `--pc-bg-carbon` (`#15151E`), `--pc-bg-paddock` (`#1C1C28`), `--pc-bg-pitwall` (`#242435`), borders `--pc-border-grid` / `--pc-border-bright`.
- **Typography:** Display headings in `--pc-font-display` (`Chakra Petch`), body in `--pc-font-sans` (`Titillium Web`), all timings/SHAs/p-values in `--pc-font-mono` (`JetBrains Mono`) with `font-variant-numeric: tabular-nums`.
- **Lane & Verdict Color Coding:**
  - Analysis: `--pc-sphere-cyan` (`#00F0FF`)
  - Optimizer: `--pc-sector-purple` (`#B84DFF`)
  - Proof: `--pc-vegas-gold` (`#FFD000`)
  - Dispatcher / Fork CI: `--pc-vegas-magenta` (`#FF008C`)
  - Trust Wall / `REJECT` (Red Flag): `--pc-f1-red` (`#FF1E00`)
  - `KEEP` (DRS / Green Flag): `--pc-drs-green` (`#00E676`)
- **Playbook Compound Ring Badges (`Hypothesis.skill`):**
  - `dep-cache` → `--pc-compound-soft` (`#FF1E00`)
  - `shard-by-timing` → `--pc-compound-medium` (`#FFD000`)
  - `affected-tests` → `--pc-compound-hard` (`#F4F4F7`)
  - `dedupe-jobs` → `--pc-compound-hypersoft` (`#FF008C`)
  - `parallelize-dag` → `--pc-compound-inter` (`#00E676`)

---

## Module & File Layout

| File | Responsibility |
|---|---|
| `src/pit_crew/web/index.html` | Single-page Pit Wall shell loading `/brand/tokens.css`, `/web/styles.css`, and `/web/app.js` (ES module, zero build step). |
| `src/pit_crew/web/styles.css` | Pit Wall layout, F1 telemetry cards, Trust Wall divider, compound ring badges, and responsive grid built on `--pc-*` tokens. |
| `src/pit_crew/web/app.js` | State store, Live API poller (`/api/repos/{owner}/{name}/status`, `/attempts`, `/records`), `POST /triggers/cycle` trigger, and interactive chart/matrix renderers. |
| `src/pit_crew/web/demo_scenarios.js` | Three deterministic offline scenarios (`unsafe_reject`, `noise_reject`, `proven_keep`) with step-by-step playback controls for M1 demos. |
| `src/pit_crew/server.py` | Mount `brand/` at `/brand` and `src/pit_crew/web/` at `/web`, and serve `index.html` at `GET /`. |

---

## UI Sections & Components

### 1. Pit Wall Header Bar
- Left: `brand/logo-lockup-dark.svg` + repo selector (`owner/name`) + current `base_sha` chip.
- Center: **SLA Target Meter** showing current `median_seconds` vs `60s` and `p95_seconds` vs `120s`, with `--pc-drs-green` badge when `sla_met` is `true`.
- Right: **Live / Demo** mode toggle + **Run Pit Stop** button (`POST /triggers/cycle`, disabled with spinner while a cycle is active).

### 2. Sector Pipeline Strip (with Trust Wall)
Horizontal 5-stage telemetry strip showing the active or selected attempt:
1. **Analysis** (`--pc-sphere-cyan`): target `workflow_path`, `job`, `step`, `bucket`, `expected_savings_seconds`, `confidence`.
2. **Optimizer** (`--pc-sector-purple`): compound ring badge for `skill`, `branch`, `changed_files`, `summary`.
3. **Proof** (`--pc-vegas-gold`): count of `commit` SHAs + `mutation` cases in `ReplayPlan`.
4. **TRUST WALL** (`--pc-f1-red` glowing divider labeled `AGENT PROPOSES | CODE JUDGES`).
5. **Fork CI & Rater** (`--pc-vegas-magenta` → `--pc-drs-green` / `--pc-f1-red`): interleaved timing progress, `bugs_caught / bugs_total`, `speedup`, `p_value`, and final `KEEP` / `REJECT` flag.

### 3. Telemetry Center (2-Column Grid)
- **Left — Interleaved Timing Chart (SVG):**
  - Paired bars/dots for `baseline_seconds` vs `candidate_seconds` across runs `1..N` (highlighting cold Run #1).
  - Horizontal reference lines for baseline median, candidate median, and the `60s` SLA target.
  - Summary readout: `speedup` (`e.g. 1.85×`), `p_value` (`e.g. p = 0.004`), `median`, `p95`.
- **Right — Bug Replay Matrix:**
  - Table of `Evidence.bug_outcomes` rows: `kind` (`commit` / `mutation`), `ref` (short SHA or mutated file), `baseline_failed` badge, `caught` (`CAUGHT` in `--pc-drs-green` / `MISSED` in `--pc-f1-red`), and external link to `run_url`.

### 4. Lap History (Ledger Attempts Table)
- Backed by `GET /api/repos/{owner}/{name}/attempts`.
- Columns: Attempt #, Skill (compound ring), Target/Summary, Speedup, p-value, Bugs Caught, Verdict (`KEEP` / `REJECT` / `PENDING`), and PR link.
- Clicking any row loads that attempt into the Sector Pipeline Strip and Telemetry Center.

### 5. Offline Demo Mode (M1 Playback)
- Scenario switcher bar (visible in Demo mode):
  1. **Fast but Unsafe (`REJECT`)**: `1.92×` faster, `p = 0.004`, but `3/4` bugs caught → Red Flag (`--pc-f1-red`), Trust Wall blocks merge.
  2. **Within Noise (`REJECT`)**: `1.03×` faster, `p = 0.24`, `4/4` bugs caught → rejected as noise.
  3. **Proven Speedup (`KEEP` + SLA Met)**: `1.85×` faster, `p = 0.004`, `4/4` bugs caught, `median 48s <= 60s`, `p95 82s <= 120s` → Green Flag (`--pc-drs-green`), PR opened.
- Play / Step / Reset controls to walk through `Analysis → Optimizer → Proof → CI → Rater` live during a presentation.

---

## API Contract & Runtime Behavior

### Expected Spec 1B JSON Shapes
- `GET /api/repos/{owner}/{name}/status`:
  ```json
  {
    "repo": "owner/name",
    "active": false,
    "base_sha": "abc1234",
    "sla_met": true,
    "latest_verdict": {
      "candidate_id": "...",
      "keep": true,
      "speedup": 1.85,
      "p_value": 0.004,
      "median_seconds": 48.0,
      "p95_seconds": 82.0,
      "sla_met": true,
      "reason": "1.85x faster, caught 4/4 bugs (SLA met)"
    }
  }
  ```
- `GET /api/repos/{owner}/{name}/attempts`: exact list returned by `ledger.prior_attempts(repo)`.
- `GET /api/repos/{owner}/{name}/records`:
  ```json
  {
    "hypotheses": [],
    "candidates": [],
    "replay_plans": [],
    "evidence": [],
    "verdicts": []
  }
  ```

### Landing State, Polling & Fonts
- **Default landing behavior:** If `?repo=owner/name` is in the URL, boot in **Live Mode** for that repo. Otherwise (or when `?mode=demo`), boot in **Demo Mode** with `proven_keep` loaded so `/` never lands on an empty screen.
- **Polling cadence:** In Live Mode, poll `/status`, `/attempts`, and `/records` every `3s` while `status.active === true`, `15s` when idle, and pause when `document.hidden` is `true` or in Demo Mode. Show a non-blocking `--pc-vegas-gold` status pill if a fetch fails.
- **Fonts:** Load `Chakra Petch`, `Titillium Web`, and `JetBrains Mono` via Google Fonts (`display=swap`) in `index.html`, falling back cleanly to the font stacks in `brand/tokens.css` when offline.
- **Candidate diff/file preview:** Include a collapsible monospace viewer (`--pc-font-mono` on `--pc-bg-void`) on the Optimizer card showing `candidate.files` for the selected attempt.

---

## Testing & Acceptance Criteria

- `tests/test_web.py`:
  - `GET /` returns `200` with `text/html` referencing `/brand/tokens.css` and `/web/app.js`.
  - `GET /brand/tokens.css` and `GET /brand/logo-lockup-dark.svg` return `200`.
  - `GET /web/styles.css`, `GET /web/app.js`, and `GET /web/demo_scenarios.js` return `200`.
- Visual check in browser (desktop `1440px` + mobile `390px`) verifying all 3 demo scenarios render cleanly with `brand/tokens.css` and zero console errors.
- Passes `ruff`, `mypy --strict`, `deptry`, `lint-imports`, and `pytest --cov-fail-under=90`.
