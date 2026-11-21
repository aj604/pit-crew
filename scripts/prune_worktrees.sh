#!/usr/bin/env bash
# Prune finished agent git worktrees. Dry run unless --apply is passed.
# Usage: scripts/prune_worktrees.sh [--repo PATH] [--base REF] [--apply]
# See src/pit_crew/worktrees.py for what counts as finished.
set -euo pipefail
project="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec uv run --project "$project" python -m pit_crew.worktrees "$@"
