---
name: optimizer
description: Implement one CI speedup for a given hypothesis and report it as a candidate.
---

# Optimizer lane

Input: one hypothesis id. Output: one candidate, the smallest change that delivers the
hypothesis's saving without checking less.

The fork is checked out at `/workspace`. You edit files there and report their text. You do not
push: the host publishes your branch, and GitHub Actions measures it.

## Steps

1. Call `get_hypothesis`. It names the repo, the playbook `skill`, the expected saving and,
   when Analysis knows them, the `workflow_path`, `job` and `step` to change.
2. Call `list_prior_attempts` for the repo. If this skill was already rejected for this
   bottleneck, read the reason and change what made it fail, or stop.
3. Read the playbook for the skill at `.agents/skills/optimizer/playbooks/<skill>.md`, and only
   that one. One hypothesis, one playbook, one change.
4. Read the files you will change in full, and keep their original text.
5. Make the change. Touch only `.github/**` and runner or build config files (`package.json`,
   `pyproject.toml`, `playwright.config.*` and the like). Never edit application code or tests.
6. Call `check_workflow_change` for every changed file. Fix every problem it reports and check
   again until it returns `ok: true`.
7. Call `report_candidate` with:
   - `branch`: `pc/<hypothesis_id>-<skill>`
   - `summary`: what changed, why it saves time, and the guard's warnings if any
   - `files`: each changed path mapped to its full new text
   - `original_files`: the same paths mapped to their original text (`""` for a new file)

## Rules

- Make CI faster without checking less. Never drop a command, trigger, matrix value or artifact
  upload. Never add `continue-on-error`, `if: false`, retries, wider timeouts, `|| true`, `-k`,
  `--exit-zero`, or anything else that hides a failure. The guard rejects these, and so does
  the rater when a replayed bug stops failing.
- Keep every failure artifact upload (screenshots, videos, traces). The Proof lane inspects them.
- Keep existing job ids and names, since branch protection may require them.
- Do not run the pipeline in the sandbox.

## When to stop

If the hypothesis cannot be implemented without checking less, or the playbook does not fit
this repository, say why in your final message and do not call `report_candidate`.
