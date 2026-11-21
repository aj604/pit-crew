# Playbook: parallelize-dag

Use when jobs wait on each other without needing each other's output, or one job runs
independent checks back to back (lint, then typecheck, then tests).

## Change

- Remove a `needs:` entry when the job does not use the other job's outputs or artifacts.
- Split a serial job into parallel jobs, one per independent check. Each new job repeats
  checkout and setup; pair this with dep-cache so the repeated setup is cheap.
- Keep `needs:` that passes artifacts or outputs (`needs.<job>.outputs`, `download-artifact`).

## Keep

- Every check, on the same runner, toolchain versions and directory. Moving a check to another
  job is fine; dropping it, or moving it to a different OS or version, is not.
- Existing job ids and `name:` values. Branch protection may require a job by name. When you
  split a job, keep its id as an **aggregate gate**: it `needs:` every job that now runs its
  checks, runs with `if: always()`, and fails unless every one of them succeeded. Without
  `if: always()` a failed dependency makes the gate *skipped*, and GitHub counts a skipped
  required check as passing. The guard rejects a split without this gate.
- Deploy or publish jobs must still wait for every check.

## Example

Before: one `check` job runs `ruff check`, `mypy src` and `pytest`. After:

```yaml
jobs:
  lint:
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
  check:          # kept id: the required status check now reports all three
    needs: [lint, typecheck, test]
    if: always()
    runs-on: ubuntu-latest
    steps:
      - name: Fail unless every check succeeded
        run: |
          echo '${{ toJSON(needs) }}' | jq -e 'all(.[]; .result == "success")'
```
