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

## Keep

- The full suite on `push` to main and in the merge queue (`merge_group`). Gate the selection
  with `if: github.event_name == 'pull_request'` and keep a full-suite step for the other events.
- A list of inputs that rerun everything: lockfiles, CI config, test config and setup
  (`conftest.py`, `jest.config.*`), shared fixtures, and build config. If any of them changed,
  run the full suite.
- Fall back to the full suite whenever selection fails or finds nothing.
- Never select with `-k`, `--deselect` or `--ignore`. The guard rejects them.

## Example

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0
- run: npm ci
- if: github.event_name == 'pull_request'
  run: npx jest --changedSince=origin/${{ github.base_ref }}
- if: github.event_name != 'pull_request'
  run: npx jest
```
