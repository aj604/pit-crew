## Summary

<!-- Describe what this PR changes and why. Keep it terse and data-first. -->

## Lane / Area

- [ ] `ANA` — Analysis (`src/pit_crew/lanes/analysis/`)
- [ ] `OPT` — Optimizer (`src/pit_crew/lanes/optimizer/`)
- [ ] `PRF` — Proof (`src/pit_crew/lanes/proof/`)
- [ ] `DSP` — Dispatcher / Server (`src/pit_crew/dispatcher.py`, `server.py`, `managed_agent.py`)
- [ ] `RAT` — Rater (`src/pit_crew/rater.py`)
- [ ] Contracts / Ledger (`src/pit_crew/contracts.py`, `ledger.py`) — *must be its own focused PR*
- [ ] Docs / Brand / CI

## Verification

- [ ] `uv run ruff format --check && uv run ruff check`
- [ ] `uv run mypy src tests`
- [ ] `uv run deptry src`
- [ ] `uv run lint-imports`
- [ ] `uv run zizmor .github/workflows`
- [ ] `uv run pytest`
- [ ] Trust boundary preserved (no agent writes to `Evidence`/`Verdict`, no cross-lane imports)
