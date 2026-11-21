---
name: proof
description: Choose the historical bugs a candidate CI change must still catch.
---

# Proof lane

Input: one candidate. Output: a replay plan.

1. Read the candidate's diff and work out which checks it touches.
2. From the repository's history of commits that turned CI red, pick the ones those checks
   caught. Prefer bugs the change is most likely to hide.
3. Call `plan_replays` with those commits.

The replays run on GitHub Actions and the rater scores them. You do not report results or
decide the verdict.

TODO(proof owner): the miner for red commits, and mutation injection for areas with no
recorded failures.
