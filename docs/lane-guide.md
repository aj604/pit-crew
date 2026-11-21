# Lane Owner Guide

This guide is for teammates owning individual lanes in Pit Crew. Read [architecture.md](architecture.md) first for the system overview and end-to-end lifecycle.

## Working Agreements

1. **Stay inside your lane:** Never import from another lane's package (`pit_crew.lanes.<other>`). All cross-lane communication happens through `contracts.py` and `ledger.py`.
2. **Contract changes get their own PR:** If you need a new field on a record in `src/pit_crew/contracts.py` or a new method on `Ledger` in `src/pit_crew/ledger.py`, open a small, separate PR first and tag the repo owner and affected lane owners.
3. **No API key required for tests:** Every lane must be 100% unit-testable offline using `InMemoryLedger` and local fixtures under `tests/fixtures/`. Only `managed_agent.py` talks to the Gemini API.
4. **Keep CI green on every PR:** Before requesting review, run:
   ```sh
   uv run ruff check
   uv run pytest -q
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

To smoke-test a skill against the live Gemini Managed Agent API (optional):
```sh
export GEMINI_API_KEY=...
uv run python -c 'from pit_crew.managed_agent import run_lane; print(run_lane("analysis", "..."))'
```
*(Note: the live loop in `managed_agent.py` is still a stub—check with the Dispatcher owner before relying on live runs.)*

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
  - Align `Hypothesis.skill` names with the Optimizer owner's playbook list (`dep-cache`, `shard-by-timing`, `affected-tests`, `fail-fast-order`, `flake-quarantine`, `dedupe-jobs`).
- **Watch out for:** Don't edit repo files in this lane; avoid duplicate hypotheses for bottlenecks already recorded in the ledger.

### 2. Optimizer Lane (`src/pit_crew/lanes/optimizer/`)

- **Goal:** Implement **one** targeted, safe CI optimization for a given `Hypothesis` on a new branch of the fork and record a `Candidate`.
- **Reads:** Target `Hypothesis`, repository files, and prior attempts/verdicts for this repo in the ledger.
- **Writes:** Pushes a branch to the fork + `Candidate` via `report_candidate`.
- **Starting points (from `TODO`):**
  - Build playbook skills for each fix category (`dep-cache`, `shard-by-timing`, `affected-tests`, `fail-fast-order`, `flake-quarantine`, `dedupe-jobs`).
  - Coordinate with the Dispatcher/platform owner on how sub-skills are mounted in `managed_agent.build_request()`.
  - Add a read-only ledger tool (e.g., `list_prior_attempts`) so the agent can inspect previously rejected candidates and avoid repeating them.
  - Add lightweight static validation (YAML parsing, basic GitHub Actions workflow sanity checks) before pushing.
- **Watch out for:** Strictly **one change per candidate** so the rater can attribute speedups/regressions cleanly. Never weaken, skip, or delete checks to gain speed.

### 3. Proof Lane (`src/pit_crew/lanes/proof/`)

- **Goal:** Select the set of bugs a `Candidate` must still catch so the Rater can verify safety.
- **Reads:** `Candidate` diff + repository git/CI history.
- **Writes:** `ReplayPlan` via `plan_replays`.
- **Starting points (from `TODO`):**
  - Build a miner for historical commits that turned CI red, mapping which workflow/job/check caught each failure.
  - Prioritize bugs that exercise the exact checks/paths touched by the candidate diff (especially regressions the optimization could accidentally hide, like stale caches or misconfigured test selection).
  - Design a mutation-injection fallback for areas with no recorded red commits, and coordinate with the architecture owner if `ReplayPlan` needs a new field for synthetic mutations.
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
  - Complete the live `managed_agent.run_lane()` loop: mount the fork repository source, run asynchronously/in background, and handle `requires_action` tool calls via `tools.call()`.
  - Build the GitHub webhook receiver + Actions runner orchestration (triggering paired baseline/candidate runs and bug replays, then aggregating results into `Evidence`).
  - Implement a persistent `Ledger` backend once the storage decision is finalized.
  - Add concurrency/idempotency guards (e.g., avoid launching duplicate runs for the same webhook delivery or hypothesis).
