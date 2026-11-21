# Pit Crew Architecture

Pit Crew is an autonomous CI optimization system with one core rule: **the agent proposes, and deterministic code judges.**

The agent inspects a repository, proposes a single targeted CI optimization, pushes a branch on a fork, and selects historical bugs that must still fail. Deterministic code runs the measurements on GitHub Actions and decides whether the change is kept.

## System Overview

```mermaid
flowchart TD
    Trigger["GitHub Webhooks + Cron"] --> Dispatcher["Dispatcher (orchestration)"]

    subgraph Sandbox["Google Managed Agent Sandbox"]
        Analysis["Analysis Lane"]
        Optimizer["Optimizer Lane"]
        Proof["Proof Lane"]
    end

    Dispatcher -->|"run_lane(lane, task)"| Sandbox
    Sandbox <-->|"tools.call(name, ledger, args)"| Ledger[("Ledger")]
    Dispatcher <-->|"read/write records"| Ledger

    Optimizer -->|"push branch"| Fork["GitHub Fork"]
    Dispatcher -->|"dispatch timing + replay runs"| Actions["GitHub Actions (on Fork)"]
    Fork --> Actions
    Actions -->|"workflow_run webhook"| Dispatcher
    Dispatcher -->|"write Evidence"| Ledger
    Dispatcher -->|"rate(evidence)"| Rater["Rater (deterministic)"]
    Rater -->|"write Verdict"| Ledger
```

## End-to-End Lifecycle

Each optimization attempt moves through a strict pipeline of immutable records in `src/pit_crew/contracts.py`. Lanes never call each other directly—every handoff goes through the ledger.

```mermaid
sequenceDiagram
    participant D as Dispatcher
    participant L as Ledger
    participant A as Analysis Lane
    participant O as Optimizer Lane
    participant P as Proof Lane
    participant GH as GitHub Actions (Fork)
    participant R as Rater

    D->>A: Start analysis (repo + recent run history)
    A->>L: record_hypothesis() -> Hypothesis
    D->>L: next_lane(Hypothesis) == "optimizer"

    D->>O: Start optimizer (Hypothesis + prior Verdicts)
    O->>GH: Push single-change branch to fork
    O->>L: report_candidate() -> Candidate
    D->>L: next_lane(Candidate) == "proof"

    D->>P: Start proof (Candidate diff + red commit history)
    P->>L: plan_replays() -> ReplayPlan
    D->>L: next_lane(ReplayPlan) == None (code takes over)

    D->>GH: Trigger N baseline runs & N candidate runs (N >= 5)
    D->>GH: Trigger bug replay runs for each sha in ReplayPlan.bug_shas
    GH-->>D: Workflow results (timings + pass/fail per bug)
    D->>L: Write Evidence (agent never writes this)

    D->>R: rate(Evidence)
    R-->>D: Verdict (keep, speedup, p_value, reason)
    D->>L: Write Verdict
```

### Stage-by-Stage Responsibilities

1. **Trigger → Analysis:** Cron or a webhook triggers the Dispatcher for a tracked repository. The Dispatcher starts the `analysis` lane with the repository mounted at `/workspace` and recent workflow run history. Analysis outputs one or more `Hypothesis` records, ordered by expected impact.
2. **Hypothesis → Optimizer:** The Dispatcher picks a `Hypothesis` and starts the `optimizer` lane with the hypothesis and prior ledger history for that repo (so failed approaches are not repeated). Optimizer makes **one** change on a new branch on the fork, validates workflow syntax, pushes the branch, and writes a `Candidate`.
3. **Candidate → Proof:** The Dispatcher starts the `proof` lane with the `Candidate` diff. Proof identifies which checks the candidate touches and selects historical commits that turned CI red (or synthetic mutations when history is sparse), writing a `ReplayPlan`.
4. **ReplayPlan → Measurement (Code Only):** No agent runs here. The Dispatcher triggers GitHub Actions on the fork to collect:
   - **Timing samples:** At least `MIN_RUNS = 5` runs (up to ~10) for both baseline and candidate, ideally interleaved to reduce runner-load drift.
   - **Bug replays:** Running the candidate's CI configuration against each `bug_sha` in `ReplayPlan` and checking whether CI still fails (`bugs_caught` vs `bugs_total`).
   The Dispatcher records the measured results as an `Evidence` record.
5. **Evidence → Verdict (Code Only):** `rater.rate(evidence)` runs deterministically:
   - Rejects if `bugs_total == 0` (safety unproven).
   - Rejects if `bugs_caught < bugs_total` (regression in bug detection).
   - Rejects if either side has fewer than `MIN_RUNS` timing samples.
   - Computes median speedup and an exact one-sided permutation test (`p <= 0.05`).
   - Writes a `Verdict` (`keep=True` or `keep=False`) to the ledger.
6. **Post-Verdict:**
   - If `keep=True`: The Dispatcher opens a PR (or queues the change for upstream review) with the verdict numbers and replay summary.
   - If `keep=False`: The branch is discarded or archived, and the `Verdict.reason` remains in the ledger so future Optimizer runs avoid repeating the failed approach.

## Trust & Security Boundaries

| Boundary | Rule | Why |
|---|---|---|
| **Agent vs. Rater** | The agent never writes `Evidence` or `Verdict` and never edits `rater.py`. | Prevents reward hacking (e.g., skipping tests or faking timings). |
| **Sandbox vs. CI** | The agent does not run the full CI pipeline in the sandbox; it pushes a branch and GitHub Actions measures it. | Ensures timings reflect real CI runners and prevents sandbox resource abuse. |
| **Fork vs. Upstream** | Optimizer only has push access to a dedicated fork, never upstream `main`. | Keeps untrusted/unverified branches isolated until a `Verdict(keep=True)` is produced. |
| **Lane Isolation** | Each lane interaction only receives `agent/AGENTS.md`, its own `SKILL.md`, and its own `@tool` functions. | Enforced in `managed_agent.build_request()` and tested in `tests/test_lanes.py`. |

## Data Contracts (`src/pit_crew/contracts.py`)

All cross-lane state is represented by five frozen dataclasses:

| Record | Producer | Consumers | Key Fields |
|---|---|---|---|
| `Hypothesis` | Analysis | Dispatcher, Optimizer | `repo`, `description`, `skill`, `expected_savings_seconds`, `id` |
| `Candidate` | Optimizer | Dispatcher, Proof | `hypothesis_id`, `branch`, `summary`, `id` |
| `ReplayPlan` | Proof | Dispatcher | `candidate_id`, `bug_shas`, `id` |
| `Evidence` | Dispatcher (code) | Rater | `candidate_id`, `baseline_seconds`, `candidate_seconds`, `bugs_total`, `bugs_caught`, `id` |
| `Verdict` | Rater (code) | Dispatcher, Optimizer (history) | `candidate_id`, `keep`, `speedup`, `p_value`, `reason`, `id` |

### Contract Evolution Rules

- **Separate PRs:** Any change to `contracts.py` or `ledger.py` must land in its own focused PR so every lane owner can review it.
- **Additive by default:** Prefer adding optional fields with sensible defaults over renaming or removing existing fields.
- **JSON-serializable types only:** Keep fields restricted to primitives, `tuple`, `dict`, and `None` so any future database or MCP serialization layer works cleanly.

### Locked v1 Contract & Platform Decisions

1. **Gemini 4 Multi-Model Lanes (`managed_agent.py`):**
   - `analysis`: **`gemini-4-hydrogen`** (high-throughput DAG & step-log scanning).
   - `optimizer`: **`gemini-4-neon`** (surgical workflow & runner-config refactoring, auto-mounting `lanes/optimizer/playbooks/*.md`).
   - `proof`: **`gemini-4-carbon`** (adversarial & multimodal failure artifact inspection via base64-mounted screenshots/traces under `/workspace/.pit-crew/artifacts/`).
2. **Zero-Secret Sandbox & Candidate Coordinates:** All untrusted repo/log/image inspection runs inside Google's remote sandbox. `GITHUB_TOKEN` stays on the Dispatcher host. `Candidate` carries `repo`, `base_sha`, `head_sha`, `files`, and `changed_files`, validated by `contracts.is_allowed_candidate_path()` (`.github/**` and runner configs only).
3. **Compounding Pit Stops Toward 60s / 120s SLA (`rater.py`):** Target repo CI goal is **`median <= 60s` and `p95 <= 120s`**. Any candidate achieving `speedup >= 1.05x` (`MIN_SPEEDUP`), `p <= 0.05` (exact permutation up to 10 runs/side, deterministic Monte Carlo above 10, including cold Run #1 across 5 staggered pairs), and `100%` bugs caught is kept (`keep=True`) and promoted as the new `base_sha` until `Verdict.sla_met` is `True`.
4. **Dual Replay Plan (`BugCase` in `ReplayPlan`):** Supports both historical red SHAs (`kind="commit"`) via 3-way overlay of Candidate files onto `bug_sha` and synthetic mutations (`kind="mutation"`, `mutated_files`) verified to fail Baseline so dud mutations cannot game Rater.
5. **Runtime, Storage & UI:** Single FastAPI service (`workflow_run` webhooks + live Pit Wall UI + PR SVG/PNG charts) backed by WAL-mode `SqliteLedger` (`ledger.prior_attempts(repo)`, `ledger.latest_kept_candidate(repo)`) and in-process `@tool` gates via the Interactions API `requires_action` loop.
