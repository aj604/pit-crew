# Playbook: affected-tests

Use when pull requests run the whole suite but most changes touch a small part of the code.
This is the riskiest playbook. The Proof lane replays bugs against it, so be conservative.

## Change

On pull requests only, run the tests the diff can reach. Everywhere else, run everything.

- Select with the runner's own dependency tracking, appended to the original command: Jest
  `--changedSince=origin/${{ github.base_ref }}`, Vitest `--changed origin/${{ github.base_ref }}`,
  Nx `nx affected -t test`, Turborepo `--filter=...[origin/${{ github.base_ref }}]`,
  pytest `--testmon`.
- Fetch enough history for the diff (`fetch-depth: 0` on checkout).
- Decide the mode in its own selection step, separate from running tests, so a selection
  failure is never confused with a test failure.

## Keep

- The full suite on `push` to main and in the merge queue (`merge_group`).
- A list of shared inputs that rerun everything: lockfiles, CI config, test config and setup
  (`jest.config.*`, `jest.setup.*`, `conftest.py`), shared fixtures, and build config. If any of
  them changed, run the full suite.
- Fall back to the full suite whenever selection fails or finds no tests.
- Never select with `-k`, `--deselect` or `--ignore`. The guard rejects them.

## Example

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0
- run: npm ci
- name: Choose affected or full suite
  id: select
  if: github.event_name == 'pull_request'
  run: |
    base="origin/${{ github.base_ref }}"
    shared='^(package(-lock)?\.json|jest\.(config|setup)\.|\.github/|test/(setup|fixtures)/)'
    mode=full
    if ! git diff --name-only "$base...HEAD" | grep -Eq "$shared"; then
      if npx jest --listTests --changedSince="$base" | grep -q .; then
        mode=affected
      fi
    fi
    echo "mode=$mode" >> "$GITHUB_OUTPUT"
- if: steps.select.outputs.mode == 'affected'
  run: npx jest --changedSince=origin/${{ github.base_ref }}
- if: steps.select.outputs.mode != 'affected'
  run: npx jest
```

If the diff or the listing fails, `mode` stays `full`. Outside pull requests the selection
step is skipped, its output is empty, and the full suite runs.
