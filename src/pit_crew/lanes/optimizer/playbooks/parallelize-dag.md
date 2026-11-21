# Playbook: parallelize-dag

Use when jobs wait on each other without needing each other's output, or one job runs
independent checks back to back (lint, then typecheck, then tests).

## Change

- Remove a `needs:` entry when the job does not use the other job's outputs or artifacts.
- Split a serial job into parallel jobs, one per independent check. Each new job repeats
  checkout and setup; pair this with dep-cache so the repeated setup is cheap.
- Keep `needs:` that passes artifacts or outputs (`needs.<job>.outputs`, `download-artifact`).

## Keep

- Every check command. Moving a command to another job is fine; dropping it is not.
- Existing job ids and `name:` values. Branch protection may require them by name, so add new
  jobs rather than renaming old ones.
- Deploy or publish jobs must still wait for every check.

## Example

Before: one `check` job runs `ruff check`, `mypy src` and `pytest`. After:

```yaml
jobs:
  check:          # kept, so a required status check named "check" still reports
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: uv sync --locked
      - run: uv run ruff check
  typecheck:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: uv sync --locked
      - run: uv run mypy src
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: uv sync --locked
      - run: uv run pytest
```
