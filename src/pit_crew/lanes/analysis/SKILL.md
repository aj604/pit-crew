---
name: analysis
description: Find where a repository's CI time goes and record each bottleneck as a hypothesis.
---

# Analysis lane

Input: a repository. Output: hypotheses, most valuable first.

1. Read the workflows under `.github/workflows/` and the recent run history you are given.
2. Find where the minutes go: installs that miss the cache, serial jobs that could run in
   parallel, the few slowest tests, flaky retries, jobs that duplicate each other.
3. For each bottleneck, call `record_hypothesis` with the evidence, the playbook skill that
   should fix it, and the seconds you expect to save.

Record one hypothesis per bottleneck. Do not edit the repository in this lane.

TODO(analysis owner): tools to fetch run history and compute the critical path.
