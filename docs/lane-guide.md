# Lane Owner Guide

This guide is for teammates owning individual lanes in Pit Crew. Read [architecture.md](architecture.md) first for the system overview and end-to-end lifecycle.

## Working Agreements

1. **Stay inside your lane:** Never import from another lane's package (`pit_crew.lanes.<other>`). All cross-lane communication happens through `contracts.py` and `ledger.py`.
2. **Contract changes get their own PR:** If you need a new field on a record in `src/pit_crew/contracts.py` or a new method on `Ledger` in `src/pit_crew/ledger.py`, open a small, separate PR first and tag the repo owner and affected lane owners.
3. **No API key required for tests:** Every lane must be 100% unit-testable offline using `InMemoryLedger` and local fixtures under `tests/fixtures/`. Only `managed_agent.py` talks to the Gemini API.
4. **Keep CI green on every PR:** Before requesting review, run:
   ```sh
   uv run ruff format --check
   uv run ruff check
   uv run mypy src tests
   uv run deptry src
   uv run lint-imports
   uv run zizmor .github/workflows
   uv run pytest
   ```
5. **Respect the trust boundary:** Agent lanes (`analysis`, `optimizer`, `proof`) must never write `Evidence` or `Verdict` records, and must never modify `rater.py`.

## Anatomy of an Agent Lane

Each agent lane lives in `src/pit_crew/lanes/<lane>/` and has two core files:

- **`SKILL.md`**: Instructions mounted into the managed agent sandbox at `.agents/skills/<lane>/SKILL.md` (alongside the shared `agent/AGENTS.md`).
- **`tools.py`**: Plain Python functions decorated with `@tool(lane="<lane>", parameters={...})` that the agent can call.

### Rules for Writing `@tool` Functions

- **First argument is always `ledger: Ledger`.** The tool dispatcher (`tools.call`) injects `ledger` automatically; do not include `ledger` in the JSON Schema `parameters`.
- **Docstring = agent tool description.** Keep the docstring concise and explicit about what the tool does and what it returns.
- **JSON-serializable inputs and outputs.** Arguments come from JSON tool calls, and outputs will be serialized back to the model (and potentially over MCP later). Return strings, numbers, dicts, or lists—not raw dataclass instances.
- **Validate foreign keys.** If your tool references an upstream record (e.g., `hypothesis_id` or `candidate_id`), call `ledger.get(...)` first so invalid IDs fail fast instead of writing orphan records.
- **Keep pure logic separate from `@tool` wrappers.** Put computation helpers (like `slowest_jobs` in `analysis/tools.py`) in plain functions so they are easy to unit-test directly.

### Rules for Writing `SKILL.md`

Every lane `SKILL.md` should clearly state:
- **Input & Output:** What the agent is given and which ledger tool it must call before finishing.
- **Step-by-step workflow:** Concrete sequence of inspection, computation, and reporting.
- **Scope guardrails:** What the lane must *not* do (e.g., Analysis never edits files; Optimizer makes only one change and never runs full CI in the sandbox; Proof never judges verdicts).
- **Bail-out behavior:** What to do if no safe/valid output exists (e.g., explain why in the final message and call no write tool).

### Testing Your Lane

Add tests under `tests/` (e.g., `tests/test_<lane>.py`) covering:
1. **Pure helper logic** against realistic JSON/text fixtures in `tests/fixtures/`.
2. **Tool contracts** via `tools.call("<tool_name>", InMemoryLedger(), {...})`, including error cases (missing foreign keys, empty/invalid inputs).
3. **Prompt/request wiring** via `managed_agent.build_request("<lane>", ...)` to confirm your skill and tools register cleanly.

### Live smoke test (Milestone 2)

`python -m pit_crew.cli smoke-lane` runs one lane against the live Managed Agents API and checks that it wrote its record to a SQLite ledger. It prints one JSON line (`lane`, `interaction_id`, `status`, `expected_record`, `records_written`, `elapsed_seconds`, plus `error` on failure). It exits 0 only when the interaction ends `completed` **and** at least one new expected record (`Hypothesis`, `Candidate` or `ReplayPlan`) was written.

The default prompts chain the lanes: `optimizer` targets the newest `Hypothesis` for `--repo` in the ledger, and `proof` targets the newest `Candidate`. Run them in order against one ledger. Pass `--task "..."` to send your own prompt instead (this skips the lookup).

Manual M2 verification checklist:

1. Use a fork you own as `--repo`, and keep the ledger outside the repo (for example under `~/pit-crew-smoke/`).
2. `export GEMINI_API_KEY=...`
3. Analysis:
   ```sh
   uv run python -m pit_crew.cli smoke-lane --lane analysis --repo <owner/name> --ledger ~/pit-crew-smoke/ledger.db
   ```
   Check: exit code 0, `"status": "completed"`, `"records_written"` ≥ 1.
4. Optimizer, same ledger: `--lane optimizer`. Check the same three things. The `Candidate` is recorded only. Nothing is pushed.
5. Proof, same ledger: `--lane proof`. Check the same three things.
6. Note each `interaction_id` and `elapsed_seconds` in the PR or issue that records the M2 run.

If you re-run analysis on the same ledger, the agent can hit the duplicate-hypothesis refusal and write nothing (non-zero exit). Use a fresh ledger path to start over. Without `GEMINI_API_KEY`, the command fails fast with `"error": "KeyError: 'GEMINI_API_KEY'"`.

---

## Per-Lane Guidance

### 1. Analysis Lane (`src/pit_crew/lanes/analysis/`)

- **Goal:** Identify the highest-leverage CI bottlenecks in a repository and record prioritized `Hypothesis` records.
- **Reads:** Repository files (`.github/workflows/`, build/test configs) + recent CI run/job/step timings.
- **Writes:** `Hypothesis` via `record_hypothesis`.
- **Starting points (from `TODO`):**
  - Build helpers/tools to fetch or parse GitHub Actions run/job/step history (test against fixture JSON first).
  - Compute the **critical path** across DAG dependencies (`needs:`), not just total job duration—speeding up a non-critical job won't reduce wall-clock CI time.
  - Detect common patterns: cache misses, unsharded slow test suites, serial jobs that can parallelize, redundant/duplicate jobs, flaky retries.
  - Align `Hypothesis.skill` names with the shared `PLAYBOOK_SKILLS` catalog in `contracts.py` (`dep-cache`, `parallelize-dag`, `shard-by-timing`, `dedupe-jobs`, `affected-tests`).
  - Query `ledger.prior_attempts(repo)` so you don't emit duplicate hypotheses for bottlenecks already open or rejected.
- **Watch out for:** Don't edit repo files in this lane; avoid duplicate hypotheses for bottlenecks already recorded in the ledger.

### 2. Optimizer Lane (`src/pit_crew/lanes/optimizer/`)

- **Goal:** Implement **one** targeted, safe CI optimization for a given `Hypothesis` on a new branch of the fork and record a `Candidate`.
- **Reads:** Target `Hypothesis`, repository files, and prior attempts/verdicts for this repo in the ledger.
- **Writes:** Pushes a branch to the fork + `Candidate` via `report_candidate`.
- **Starting points (from `TODO`):**
  - Drop playbook markdown files under `src/pit_crew/lanes/optimizer/playbooks/*.md` (`dep-cache`, `parallelize-dag`, `shard-by-timing`, `dedupe-jobs`, `affected-tests`) — `managed_agent.build_request()` mounts them automatically at `.agents/skills/optimizer/playbooks/`.
  - Expose a read-only ledger tool wrapping `ledger.prior_attempts(repo)` so `gemini-4-neon` sees previously rejected candidates and avoids repeating them.
  - Validate changed paths with `contracts.is_allowed_candidate_path(path)` (`.github/**` + runner configs only, never `src/` or `tests/`) and submit modified `files: dict[str, str]` via `report_candidate` so the host publishes the fork branch with zero secrets in the sandbox.
- **Watch out for:** Strictly **one change per candidate** so the rater can attribute speedups/regressions cleanly. Never weaken, skip, or delete checks to gain speed.

### 3. Proof Lane (`src/pit_crew/lanes/proof/`)

- **Goal:** Select the set of bugs a `Candidate` must still catch so the Rater can verify safety.
- **Reads:** `Candidate` diff/files + repository git/CI history + failure artifacts mounted at `/workspace/.pit-crew/artifacts/`.
- **Writes:** `ReplayPlan` via `plan_replays`.
- **Starting points (from `TODO`):**
  - Build a miner for historical commits that turned CI red, mapping which workflow/job/check caught each failure.
  - Inspect multimodal CI failure artifacts (Playwright failure screenshots, traces, visual build outputs) with `gemini-4-carbon` to diagnose failure modes text logs lose, recording `artifact_ref` on `BugCase`.
  - Populate `ReplayPlan.cases` using `BugCase(kind="commit", commit_sha=...)` for historical red commits and `BugCase(kind="mutation", mutated_files=...)` as fallback (`ReplayPlan` stays backwards-compatible with `bug_shas`).
- **Watch out for:** `rater.py` automatically rejects any candidate with `bugs_total == 0`, so an empty `ReplayPlan` guarantees rejection. Proof chooses *what* to replay—it never executes replays or decides verdicts.

### 4. Rater (`src/pit_crew/rater.py`)

- **Goal:** Deterministically decide whether a `Candidate` is kept based on measured `Evidence`.
- **Reads:** `Evidence`.
- **Writes:** `Verdict` (returned by `rate()`).
- **Current state:** Core logic is implemented—rejects 0 replayed bugs, missed bugs, `< 5` runs per side, or non-significant speedup (`p > 0.05` via exact permutation test).
- **Starting points:**
  - Verify edge cases: ties/identical timings, zero-duration runs, extreme outliers, and performance when sample sizes exceed ~10 per side (`combinations` grows combinatorially—consider switching to Monte Carlo permutation or Mann-Whitney U above a threshold).
  - Decide whether a minimum practical speedup threshold (e.g., >5% or >10s saved) should accompany statistical significance so tiny noisy wins aren't churned into PRs.
  - Keep it 100% deterministic and free of model/network calls.

### 5. Dispatcher & Runtime (`src/pit_crew/dispatcher.py`, `managed_agent.py`, `ledger.py`)

- **Goal:** Glue everything together—receive triggers, run lanes, execute GitHub Actions timing/replay runs, collect `Evidence`, invoke `rater.rate()`, and persist state.
- **Reads/Writes:** All ledger records.
- **Starting points (from `TODO`):**
  - Run `managed_agent.run_lane()` asynchronously/in the background (the synchronous loop, repo mounting and `requires_action` tool servicing are done; see the smoke test above).
  - Build the GitHub webhook receiver + Actions runner orchestration (triggering paired baseline/candidate runs and bug replays, then aggregating results into `Evidence`).
  - Implement a persistent `Ledger` backend once the storage decision is finalized.
  - Add concurrency/idempotency guards (e.g., avoid launching duplicate runs for the same webhook delivery or hypothesis).
