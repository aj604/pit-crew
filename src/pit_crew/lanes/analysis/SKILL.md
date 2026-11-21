---
name: analysis
description: Find where a repository's CI time goes and record each bottleneck as a hypothesis.
---

# Analysis lane

Input: a repository. Output: hypotheses, most valuable first.

1. Read the workflows under `.github/workflows/`, then call `profile_ci` for each workflow
   that runs on pull requests, passing `workflow_content` (and `ref` when profiling a specific
   base commit) so the host profiler does not need a local checkout.
2. Work from the critical path. A job's `max_saving_seconds` is the most wall-clock time any
   change to it can save; a job with 0 is off the critical path, so do not propose speeding it
   up. `expected_savings_seconds` must never exceed it.
3. Fix the biggest bucket first. `critical_path_buckets` splits the critical path into
   waiting (for a runner), setup (checkout, toolchain, installs, caches) and running (the
   checks themselves) at p50 and p95; `biggest_bucket` and `biggest_bucket_p95` name the largest.
   Waiting is fixed by runner choice and cancelling superseded runs, setup by caches and
   prebuilt images, running by sharding, parallel jobs and test selection. Changes to a job's
   steps cannot shrink its waiting.
4. Find where the minutes go inside critical jobs: installs that miss the cache, the slowest
   steps, a matrix instance that is always the straggler, flaky retries (`retried_runs`).
5. For each bottleneck, call `record_hypothesis` with the evidence, the playbook skill that
   should fix it, and the seconds you expect to save.

Record one hypothesis per bottleneck. Do not edit the repository in this lane.

TODO(analysis owner): bottleneck detectors, skill-name validation, duplicate checks, ranking.
