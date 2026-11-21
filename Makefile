UV ?= uv
HOST ?= 127.0.0.1
PORT ?= 8000

.DEFAULT_GOAL := help

.PHONY: help sync lock-check format format-check lint lint-fix typecheck deptry arch workflow-lint test build check demo clean

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

sync: ## Install/sync project dependencies
	$(UV) sync

lock-check: ## Verify uv.lock matches pyproject.toml
	$(UV) lock --check

format: ## Format code with ruff
	$(UV) run ruff format

format-check: ## Check code formatting with ruff
	$(UV) run ruff format --check

lint: ## Run ruff linter
	$(UV) run ruff check

lint-fix: ## Run ruff linter and apply safe fixes
	$(UV) run ruff check --fix

typecheck: ## Run mypy strict typecheck
	$(UV) run mypy src tests

deptry: ## Check dependency hygiene with deptry
	$(UV) run deptry src

arch: ## Verify architecture import contracts
	$(UV) run lint-imports

workflow-lint: ## Audit GitHub Actions workflows with zizmor
	$(UV) run zizmor .github/workflows

test: ## Run pytest suite with coverage
	$(UV) run pytest

build: ## Build wheel and sdist
	$(UV) build

check: lock-check format-check lint typecheck deptry arch workflow-lint test ## Run full CI verification suite

demo: ## Run Pit Wall UI + demo engine locally (HOST=127.0.0.1 PORT=8000)
	$(UV) run python -c "import uvicorn; from pit_crew.ledger import InMemoryLedger; from pit_crew.server import create_app; uvicorn.run(create_app(InMemoryLedger()), host='$(HOST)', port=$(PORT))"

clean: ## Remove build and cache artifacts
	rm -rf dist .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
