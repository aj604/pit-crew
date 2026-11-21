---
name: analysis
description: Find where a repository's CI time goes and record each bottleneck as a hypothesis.
---

# Analysis lane

Input: a repository. Output: hypotheses, most valuable first.

1. Read the workflows under `.github/workflows/`, then call `profile_ci` for each workflow
   that runs on pull requests.
2. Work from the critical path. A job's `max_saving_seconds` is the most wall-clock time any
   change to it can save; a job with 0 is off the critical path, so do not propose speeding it
   up. `expected_savings_seconds` must never exceed it.
3. Split runner time from queue time. `median_run_seconds` is what changes to a job's steps can
   shrink; `median_queued_seconds` is waiting for a runner, which step changes cannot fix.
4. Find where the minutes go inside critical jobs: installs that miss the cache, the slowest
   steps, a matrix instance that is always the straggler, flaky retries (`retried_runs`).
5. For each bottleneck, call `record_hypothesis` with the evidence, the playbook skill that
   should fix it, and the seconds you expect to save.

Record one hypothesis per bottleneck. Do not edit the repository in this lane.

TODO(analysis owner): bottleneck detectors, skill-name validation, duplicate checks, ranking.
