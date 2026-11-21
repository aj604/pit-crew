---
name: proof
description: Choose the bugs a candidate CI change must still catch, injecting mutants where history has none.
---

# Proof lane

Input: one candidate, from code that has already:

- mapped the candidate's diff to the checks it touches (a change outside
  `.github/workflows/` touches every check),
- mined CI history for commits whose check failed on every attempt (flaky ones excluded),
- chosen the **required replays**: the bugs those touched checks caught, and
- listed the **uncovered checks**: touched checks with no recorded bug.

Output: one replay plan.

1. Read the candidate's diff. Think about what it could hide: a stale cache that skips a
   rebuild, test selection that misses a file, a shard that drops tests, a trigger filter
   that stops CI running at all.
2. Keep every required replay. Add red commits from the history you are given when the
   diff is likely to hide them, even if code did not pick them.
3. For each uncovered check, inject a bug that check should catch:
   - Find the code the check exercises: the tests it runs, the files it lints or types.
   - Call `propose_mutants` with that file and pick a mutant on a line those tests cover.
     For a lint or typecheck check, write the smallest change that breaks that check
     instead, such as an unused import or a wrong type.
   - Apply that one change to the file as it is on the candidate's base (`/workspace`) and
     keep the whole mutated file. Do not commit or push; the host replays it for you.
   - Never write a bug that fails every check, such as a syntax error. It proves nothing
     about the check the candidate touched.
   - Mutate the code under test, never CI or build config (anything in `.github/`,
     `package.json`, `pyproject.toml`, a test runner's config) or a file the candidate
     changed. Replaying it would overwrite the candidate's own change, so `plan_replays`
     refuses it.
   - Every replay is recorded on video, and Gemini 4 Carbon checks that the candidate fails
     the same way the baseline does. Pick mutants that fail with a specific, readable
     message, like an assertion showing expected and actual values, not a crash or a hang.
4. Call `plan_replays` with the required replays and your additions in `commits` (each with
   its `sha` and `expected_checks`, keeping every check key and display-name alias from the
   touched check or red commit), and each mutant as a `mutations` entry: its `path`, the whole
   mutated file `content`, `reason`, and `expected_checks` (every key of the uncovered check
   that should catch it).

If an uncovered check has no code you can mutate, say so in your final message and still
plan the replays you have. The gap shows in the evidence instead of hiding.

You choose what is replayed. Code runs and records the replays, has Gemini 4 Carbon confirm
each failure matches the baseline's, drops bugs the baseline does not catch, and decides
the verdict. The proof card and recordings go on the PR.
