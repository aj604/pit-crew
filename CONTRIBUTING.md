# Contributing to Pit Crew

Welcome to the pit wall. Read [docs/architecture.md](docs/architecture.md) and [docs/lane-guide.md](docs/lane-guide.md) before opening your first PR.

## Core Working Agreements

1. **Stay inside your lane:** Never import from another lane's package (`pit_crew.lanes.<other>`). All cross-lane handoffs go through immutable records in [`src/pit_crew/contracts.py`](src/pit_crew/contracts.py) and [`src/pit_crew/ledger.py`](src/pit_crew/ledger.py). This is enforced automatically by `lint-imports`.
2. **Contract changes get their own PR:** Any change to `contracts.py` or `ledger.py` must land in a small, dedicated PR first so every lane owner can review it.
3. **Respect the Red Trust Wall:** Agent lanes (`analysis`, `optimizer`, `proof`) must never write `Evidence` or `Verdict` records, and must never modify `rater.py`.
4. **Offline-first tests:** Every lane must be 100% unit-testable without a `GEMINI_API_KEY`, using `InMemoryLedger` and local fixtures under `tests/fixtures/`. Only `managed_agent.py` talks to the Gemini API.

## Local Verification Checklist

Run the full suite before requesting review (matches [`.github/workflows/ci.yml`](.github/workflows/ci.yml)):

```sh
uv sync
uv run ruff format --check
uv run ruff check
uv run mypy src tests
uv run deptry src
uv run lint-imports
uv run zizmor .github/workflows
uv run pytest
```

## Adding a `@tool`

Decorate a plain Python function in your lane's `tools.py` with `@tool(lane="<lane>", parameters={...})`:

- Keep `ledger: Ledger` as the first argument (injected automatically by `tools.call`).
- Return JSON-serializable primitives, dicts, or lists.
- Validate upstream foreign keys (`hypothesis_id`, `candidate_id`) via `ledger.get(...)`.

## Brand & Copy Voice

When writing CLI output, PR comments, or docs, follow the [Brand Guidelines](brand/BRAND.md): terse, data-first, calm race-engineer voice with zero hype and explicit statistical transparency.
