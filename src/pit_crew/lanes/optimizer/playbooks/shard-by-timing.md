# Playbook: shard-by-timing

Use when one test job is on the critical path and its suite can be split across machines.

## Change

Add a shard matrix and pass the shard to the test runner. Keep the original command and add
arguments to it; the guard accepts a command that gains arguments, not one that is rewritten.

| Runner | Arguments |
|---|---|
| pytest | `pytest-split`: `--splits N --group ${{ matrix.group }}`, using a committed `.test_durations` |
| Jest | `--shard=${{ matrix.shard }}/N` |
| Vitest | `--shard=${{ matrix.shard }}/N` |
| Playwright | `--shard=${{ matrix.shard }}/N` |
| cargo-nextest | `--partition count:${{ matrix.shard }}/N` |

Pick N from the measured suite time so each shard lands near the target, and buy cores before
shards: a bigger runner with more workers can beat more machines that each pay setup again.

If the matrix has `include` entries, add `shard` to **every** entry too (one copy per shard).
GitHub adds a new axis only to the rows the axes produce. An include row that matches no axis
value, or a matrix built only from `include`, never gets `matrix.shard` and would run
`--shard=/2`. A matrix that is only `include` gets no `shard` axis at all, just the copies.

## Keep

- Every test runs in exactly one shard. Use timing-balanced splitting (pytest-split durations,
  Playwright and Jest balance by file) rather than splitting by file count.
- `fail-fast: false` on the shard matrix, so one red shard does not hide the others.
- Each shard uploads its own report and failure artifacts under a unique name
  (`name: results-${{ matrix.shard }}`). The Proof lane needs every shard's screenshots and videos.
- No `-k`, `--deselect`, `--ignore` or retries. The guard rejects them.

## Example

```yaml
test:
  runs-on: ubuntu-latest
  strategy:
    fail-fast: false
    matrix:
      shard: [1, 2, 3, 4]
  steps:
    - uses: actions/checkout@v4
    - run: npm ci
    - run: npx playwright test --shard=${{ matrix.shard }}/4
    - uses: actions/upload-artifact@v4
      if: failure()
      with:
        name: playwright-report-${{ matrix.shard }}
        path: test-results/
```
