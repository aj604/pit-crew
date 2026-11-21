# Playbook: dedupe-jobs

Use when the same check runs more than once for the same commit: a duplicated matrix entry, a
job copied into two workflows, or both `push` and `pull_request` running the full suite on a
pull request's branch.

## Change

- Remove a duplicate only when it is identical: same command, environment, OS and tool
  versions. A check on Python 3.12 and the same check on 3.13 are not duplicates.
- Duplicated matrix values: list each value once.
- `push` plus `pull_request` on every branch: limit `push` to the default branch
  (`branches: [main]`). Pull requests are still checked by the `pull_request` trigger.
  An unfiltered `push` also runs for tags, and a `branches` filter alone stops that, so add
  `tags: ['**']` (or keep the existing tag filter). The guard rejects a branch filter that drops
  tag runs.
- Add `concurrency` with `cancel-in-progress: true` keyed on the workflow and ref, so a new
  push cancels the stale run on that branch. Never cancel runs on the default branch.

## Keep

- Every distinct check runs at least once for every pull request and every commit to main.
- Every trigger event, and tag runs. The guard rejects dropping an event or adding `paths`,
  `*-ignore` or (outside `push`) `branches` filters.
- Jobs that run the same command on a different runner, OS, toolchain version or directory are
  not duplicates. The guard compares checks per runner and version.
- Required status check names. If branch protection requires the job you remove, keep a job with
  that name.

## Example

```yaml
on:
  push:
    branches: [main]
    tags: ['**']
  pull_request:

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: ${{ github.ref != 'refs/heads/main' }}
```
