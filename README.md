# Pit Crew

An autonomous agent that makes CI faster and proves it still catches every bug.

## Architecture

```mermaid
flowchart TD
    GH[GitHub webhooks + cron] --> D[Dispatcher]
    D -->|start lane| MDA
    subgraph MDA[Managed agent in a Google sandbox]
        A[Analysis] ~~~ O[Optimizer] ~~~ P[Proof]
    end
    MDA <-->|tools| L[(Ledger)]
    O -->|push branch| CI[GitHub Actions on the fork]
    CI -->|run finished| D
    CI -->|timings + bug replays| R[Rater]
    R -->|verdict| L
```

The agent proposes and code judges. The agent chooses what to try and makes the change;
the rater decides from measured runs whether it is kept.

## Lanes

Lanes never call each other. Each reads and writes records in the ledger
(`src/pit_crew/contracts.py`), so each can be built and tested alone.

| Lane | Owner | Reads | Writes | Code |
|---|---|---|---|---|
| Analysis | | repo, run history | `Hypothesis` | `src/pit_crew/lanes/analysis/` |
| Optimizer | | `Hypothesis` | `Candidate` | `src/pit_crew/lanes/optimizer/` |
| Proof | | `Candidate` | `ReplayPlan` | `src/pit_crew/lanes/proof/` |
| Rater | | `Evidence` | `Verdict` | `src/pit_crew/rater.py` |
| Dispatcher | | ledger, webhooks | `Evidence` | `src/pit_crew/dispatcher.py`, `managed_agent.py` |

A lane is a `SKILL.md` (what the agent is told) and a `tools.py` (plain functions the
agent may call). Each has a `TODO(<lane> owner)` marking where to start.

## Working on a lane

```sh
uv sync
uv run ruff format --check
uv run ruff check
uv run mypy src tests
uv run deptry src
uv run lint-imports
uv run zizmor .github/workflows
uv run pytest
```

You do not need Managed Agents access to work on a lane. Tools are ordinary Python tested
against fixtures and an in-memory ledger. Only `managed_agent.py` calls the Gemini API; to
try a skill against the live agent, set `GEMINI_API_KEY` and call
`pit_crew.managed_agent.run_lane("<lane>", "<task>")`. That path has not been run against
the live API yet.

To add a tool, decorate a function in your lane's `tools.py` with `@tool`. To change a
record in `contracts.py`, open a separate PR, because every lane depends on it.

## Documentation

- [Architecture](docs/architecture.md) — end-to-end lifecycle, trust boundaries, data contracts, and open architectural decisions.
- [Lane Owner Guide](docs/lane-guide.md) — working agreements, `@tool` and `SKILL.md` conventions, testing, and per-lane starting points.
- [TODO](TODO.md) — tasks, deliverables, and integration milestones per lane.

## Locked Architecture Decisions

- **Models (Gemini 4 Managed Agents via Interactions API):** `gemini-4-hydrogen` (`analysis`), `gemini-4-neon` (`optimizer`), and `gemini-4-carbon` (`proof`, including multimodal CI failure artifact inspection).
- **Target CI Goal:** Compounding verified optimizations toward **`median <= 60s` and `p95 <= 120s`** (`MIN_SPEEDUP = 1.05x`, `p <= 0.05`, `100%` replayed bugs caught).
- **Ledger & Transport:** Event-driven `workflow_run` webhook + Pit Wall FastAPI server backed by WAL-mode `SqliteLedger` (`src/pit_crew/ledger.py`) and host-mediated `@tool` gates (`src/pit_crew/tools.py`) with zero secrets inside the remote sandbox.
