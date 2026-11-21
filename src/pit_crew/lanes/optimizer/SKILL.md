---
name: optimizer
description: Implement one CI speedup for a given hypothesis on a new branch and report it.
---

# Optimizer lane

Input: one hypothesis. Output: one candidate branch.

1. Read the hypothesis and anything the ledger says was already tried for this repository.
2. Create a branch and make the single change the hypothesis calls for.
3. Check that the workflow files still parse. Do not run the pipeline here.
4. Push the branch and call `report_candidate` with what changed and why it should be faster.

If the hypothesis cannot be implemented safely, say so in your final message and report
nothing.

TODO(optimizer owner): one playbook skill per fix (dep-cache, shard-by-timing,
affected-tests, fail-fast-order, flake-quarantine, dedupe-jobs).
