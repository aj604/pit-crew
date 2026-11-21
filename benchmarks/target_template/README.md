# Status board (benchmark target)

The initial commit (`c0`) of a Pit Crew benchmark target repository. Copy this directory's
contents to the root of a new repository. It is not part of the `pit-crew` package.

A zero-dependency Python service status dashboard: `status_board` models service health,
renders an HTML board, and replays outage scenarios through its poller. Install the dev tools
with `python -m pip install -r requirements-dev.txt`.

## CI checks

| Job          | What it runs                                                                     |
| ------------ | -------------------------------------------------------------------------------- |
| `lint`       | `ruff check .` and `ruff format --check .`                                       |
| `typecheck`  | `mypy` (strict)                                                                  |
| `unit-test`  | `pytest` on `tests/unit` (fast), `tests/e2e` and `tests/integration` (slow)      |
| `ui-check`   | `python -m status_board.ui_check`; on failure uploads `ui-snapshots/ui-check.svg` |
| `smoke-test` | `pytest tests/e2e` again                                                         |

## Planted bottlenecks

The baseline workflow is deliberately slow in four independent ways, one per playbook skill.
Durations are budgets for `ubuntu-latest` taken from `calibration.json`, which the slow tests
also read; they are not measurements. Each saving is measured against the baseline alone.

| Skill             | Bucket  | What is slow                                                       | Budgeted saving |
| ----------------- | ------- | ------------------------------------------------------------------ | --------------- |
| `dep-cache`       | setup   | every job installs dependencies cold (no pip cache), 5 x 5 s        | ~25 s           |
| `parallelize-dag` | DAG     | `typecheck` needs `lint` and `unit-test` needs `typecheck`, for no reason | ~39 s     |
| `dedupe-jobs`     | running | `smoke-test` reruns `tests/e2e`, which `unit-test` already runs     | ~22 s           |
| `shard-by-timing` | running | the 70 s `tests/integration` suite runs on one runner (2 shards)   | ~35 s           |

Budgeted baseline critical path: `lint` 18 s + `typecheck` 21 s + `unit-test` 110 s = 149 s.
