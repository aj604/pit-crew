---
name: analysis
description: Find where a repository's CI time goes and record each bottleneck as a hypothesis.
---

# Analysis lane

Input: a repository. Output: hypotheses, most valuable first.

1. Read the workflows under `.github/workflows/`, then call `find_bottlenecks` for each
   workflow that runs on pull requests. It profiles the workflow, runs every detector, leaves
   out bottlenecks already in the ledger, and returns `findings` ranked by
   `expected_savings_seconds`, biggest first, plus `uncovered` notes. Use `profile_ci` when you
   need the full per-job and per-step numbers behind a finding. When profiling without a local
   checkout, pass `workflow_content` (and `ref` for a specific base commit) to `profile_ci`.
2. Read each finding against the workflow file before recording it. Drop any finding that
   would remove or weaken a check: Pit Crew never checks less and never hides failures.
3. Record the top 5 at most, highest `expected_savings_seconds` first, with `record_hypothesis`,
   passing every field from the finding (skill, workflow_path, job, step, bucket, description,
   expected_savings_seconds, evidence, confidence, risk) plus `base_sha`, which is
   `profile_summary.base_sha` from `find_bottlenecks` (the commit of the newest analyzed run).
4. Report each `uncovered` note in your final message. Example: when the biggest bucket is
   waiting, no playbook skill shrinks runner queueing, so say so rather than inventing a fix.
5. If no finding survives, record nothing and say why.

How to read the numbers:
- A job's `max_saving_seconds` is the most wall-clock time any change to it can save; a job
  with 0 is off the critical path. `expected_savings_seconds` never exceeds it.
- `critical_path_buckets` splits the critical path into waiting (for a runner), setup
  (checkout, toolchain, installs, caches) and running (the checks themselves) at p50 and p95;
  `biggest_bucket` and `biggest_bucket_p95` name the largest. Changes to a job's steps cannot
  shrink its waiting.
- `parallelize-dag` findings have an empty `bucket`; their savings are scored independently,
  so do not add them to other findings' savings.

Record one hypothesis per bottleneck. Do not edit the repository in this lane.
