"""Prune finished agent git worktrees: ``python -m pit_crew.worktrees [--apply]``.

Agents each work in their own git worktree, and every worktree carries its own ``.venv``, so
they pile up on disk. This finds the ones whose work is finished and removes them.

It is a dry run unless ``--apply`` is passed: it then only reads, reporting what it would remove
and roughly how much disk that frees. A worktree is removed only when nothing in it could be
lost, so it is skipped when it is:

- the main checkout, a bare entry, locked, or the one the command runs from;
- dirty: staged, unstaged, or untracked changes;
- holding commits that are not on the base ref (``origin/HEAD``, ``origin/main`` or ``main``);
- on a branch with an open pull request;
- holding another worktree's directory that is itself kept (every worktree is judged before any
  is removed, and nested ones are removed before the ones that contain them).

Anything it cannot check (a failed ``git status``, an unknown base, no answer from ``gh``)
counts as unsafe, so the worktree is kept. Removal uses ``git worktree remove`` without
``--force``, so git's own cleanliness check is a second guard, then ``git worktree prune``
clears entries whose directory is already gone. The base ref is not fetched first, so a branch
merged only on the remote since the last fetch reads as unmerged and is kept.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASES = ("origin/HEAD", "origin/main", "main")
PR_LIMIT = "1000"

# Given the repo, the branch names (without refs/heads/) that have an open pull request,
# or None when that cannot be found out.
OpenPrLookup = Callable[[Path], frozenset[str] | None]


@dataclass(frozen=True)
class Worktree:
    """One entry of ``git worktree list --porcelain``."""

    path: Path
    head: str = ""
    branch: str | None = None  # short name; None when detached or bare
    bare: bool = False
    locked: bool = False
    prunable: bool = False


@dataclass(frozen=True)
class Verdict:
    """What to do with one worktree: remove it, or keep it for ``reason``."""

    worktree: Worktree
    reason: str | None  # None means it is safe to remove

    @property
    def removable(self) -> bool:
        """Whether every guard passed."""
        return self.reason is None


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git in ``cwd`` without taking optional locks, so reads never touch the index."""
    return subprocess.run(  # noqa: S603  fixed git subcommands built in this module
        ["git", "--no-optional-locks", "-C", str(cwd), *args],  # noqa: S607  git from PATH, like a user would
        capture_output=True,
        text=True,
        check=False,
    )


def parse_worktrees(porcelain: str) -> list[Worktree]:
    """Parse ``git worktree list --porcelain`` output. The main checkout comes first."""
    worktrees: list[Worktree] = []
    for block in porcelain.strip().split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            fields[key] = value
        if "worktree" not in fields:
            continue
        branch = fields.get("branch")
        worktrees.append(
            Worktree(
                path=Path(fields["worktree"]),
                head=fields.get("HEAD", ""),
                branch=branch.removeprefix("refs/heads/") if branch else None,
                bare="bare" in fields,
                locked="locked" in fields,
                prunable="prunable" in fields,
            )
        )
    return worktrees


def list_worktrees(repo: Path) -> list[Worktree]:
    """Every worktree registered with ``repo``, main checkout first."""
    done = _git(repo, "worktree", "list", "--porcelain")
    if done.returncode != 0:
        msg = f"git worktree list failed in {repo}: {done.stderr.strip()}"
        raise RuntimeError(msg)
    return parse_worktrees(done.stdout)


def resolve_base(repo: Path, candidates: Iterable[str]) -> str | None:
    """The commit of the first candidate ref that exists in ``repo``, or None."""
    for ref in candidates:
        done = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if done.returncode == 0:
            return done.stdout.strip()
    return None


def gh_open_pr_branches(repo: Path) -> frozenset[str] | None:
    """Branches with an open pull request, asked of ``gh``; None if ``gh`` is missing or fails."""
    gh = shutil.which("gh")
    if gh is None:
        return None
    done = subprocess.run(  # noqa: S603  fixed read-only gh query
        [gh, "pr", "list", "--state", "open", "--limit", PR_LIMIT, "--json", "headRefName"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        return None
    try:
        rows = json.loads(done.stdout)
        return frozenset(str(row["headRefName"]) for row in rows)
    except (ValueError, TypeError, KeyError):
        return None


def _dirty_reason(path: Path) -> str | None:
    done = _git(path, "status", "--porcelain", "--untracked-files=all")
    if done.returncode != 0:
        return "git status failed"
    if done.stdout.strip():
        return "dirty: uncommitted or untracked changes"
    return None


def _unmerged_reason(worktree: Worktree, base: str | None) -> str | None:
    if base is None:
        return "no base ref to compare against"
    done = _git(worktree.path, "merge-base", "--is-ancestor", "HEAD", base)
    if done.returncode == 1:
        return "has commits not on the base ref"
    if done.returncode != 0:
        return "could not compare with the base ref"
    return None


def _pr_reason(worktree: Worktree, open_prs: frozenset[str] | None) -> str | None:
    if worktree.branch is None:
        return None
    if open_prs is None:
        return "could not check for open pull requests"
    if worktree.branch in open_prs:
        return "branch has an open pull request"
    return None


def _static_reason(worktree: Worktree, *, main: Path, cwd: Path) -> str | None:
    path = worktree.path.resolve()
    if path == main:
        return "main checkout"
    if worktree.bare:
        return "bare repository"
    if worktree.locked:
        return "locked"
    if worktree.prunable or not path.is_dir():
        return "directory missing; git worktree prune clears it"
    if cwd.is_relative_to(path):
        return "current working directory"
    return None


def classify(
    worktree: Worktree,
    *,
    main: Path,
    cwd: Path,
    base: str | None,
    open_prs: frozenset[str] | None,
) -> Verdict:
    """Decide whether ``worktree`` is finished. The first failing guard is the reason to keep it."""
    reason = (
        _static_reason(worktree, main=main, cwd=cwd)
        or _dirty_reason(worktree.path)
        or _unmerged_reason(worktree, base)
        or _pr_reason(worktree, open_prs)
    )
    return Verdict(worktree, reason)


def _keep_parents_of_kept(verdicts: Sequence[Verdict]) -> list[Verdict]:
    """Keep any worktree whose directory still holds a kept worktree, so removal can't delete it.

    Deepest first, so a kept grandchild also keeps its parent and grandparent.
    """
    kept: set[Path] = set()
    resolved: dict[Path, Verdict] = {}
    for verdict in sorted(verdicts, key=lambda v: len(v.worktree.path.resolve().parts), reverse=True):
        path = verdict.worktree.path.resolve()
        inner = next((k for k in kept if k != path and k.is_relative_to(path)), None)
        if verdict.removable and inner is not None:
            verdict = Verdict(verdict.worktree, f"contains worktree that is kept: {inner}")  # noqa: PLW2901
        if not verdict.removable and path.is_dir():
            kept.add(path)
        resolved[path] = verdict
    return [resolved[v.worktree.path.resolve()] for v in verdicts]


def disk_usage(path: Path) -> int:
    """Bytes on disk under ``path``, not following symlinks (so ``.venv`` links aren't double counted)."""
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in (*dirs, *files):
            try:
                stat = (Path(root) / name).lstat()
            except OSError:
                continue
            blocks = getattr(stat, "st_blocks", None)
            total += blocks * 512 if blocks is not None else stat.st_size
    return total


def human_size(size: int) -> str:
    """``size`` bytes as a short human-readable string."""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024:  # noqa: PLR2004  binary units
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def _label(worktree: Worktree) -> str:
    return worktree.branch or f"detached {worktree.head[:12]}"


def prune(  # noqa: PLR0913  keyword-only seams for tests and the CLI
    repo: Path,
    *,
    apply: bool,
    open_prs: OpenPrLookup = gh_open_pr_branches,
    bases: Sequence[str] = DEFAULT_BASES,
    cwd: Path | None = None,
    out: Callable[[str], object] | None = None,
) -> int:
    """Report (and with ``apply``, remove) finished worktrees of ``repo``. Returns bytes reclaimed or reclaimable."""
    out = out or sys.stdout.write
    worktrees = list_worktrees(repo)
    main = worktrees[0].path.resolve()
    here = (cwd or Path.cwd()).resolve()
    base = resolve_base(repo, bases)
    prs = open_prs(repo) if any(w.branch for w in worktrees[1:]) else frozenset[str]()
    if base is None:
        out(f"warning: none of {', '.join(bases)} resolve; keeping every worktree\n")
    if prs is None:
        out("warning: could not list open pull requests; keeping every worktree on a branch\n")

    total = 0
    # Judge every worktree before removing any: a removal can delete directories nested inside it.
    verdicts = _keep_parents_of_kept([classify(w, main=main, cwd=here, base=base, open_prs=prs) for w in worktrees])
    # Remove nested worktrees before the ones that contain them.
    for verdict in sorted(verdicts, key=lambda v: len(v.worktree.path.resolve().parts), reverse=True):
        worktree = verdict.worktree
        if not verdict.removable:
            out(f"keep    {worktree.path}  ({_label(worktree)}): {verdict.reason}\n")
            continue
        size = disk_usage(worktree.path)
        if not apply:
            out(f"remove  {worktree.path}  ({_label(worktree)})  {human_size(size)}  [dry run]\n")
            total += size
            continue
        done = _git(main, "worktree", "remove", str(worktree.path))
        if done.returncode != 0:
            out(f"failed  {worktree.path}  ({_label(worktree)}): {done.stderr.strip()}\n")
            continue
        out(f"removed {worktree.path}  ({_label(worktree)})  {human_size(size)}\n")
        total += size

    if apply:
        _git(main, "worktree", "prune")
        out(f"Reclaimed {human_size(total)}.\n")
    else:
        out(f"Would reclaim {human_size(total)}. Pass --apply to remove.\n")
    return total


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prune_worktrees", description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--repo", type=Path, default=Path(), help="any checkout of the repository (default: .)")
    parser.add_argument(
        "--base",
        action="append",
        help=f"ref a worktree must be merged into; repeat to try several (default: {', '.join(DEFAULT_BASES)})",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="apply", action="store_false", help="report only (the default)")
    mode.add_argument("--apply", dest="apply", action="store_true", help="remove finished worktrees")
    # Set explicitly: with two actions sharing a dest, argparse would take --dry-run's default (True).
    parser.set_defaults(apply=False)
    return parser


def main(argv: Sequence[str] | None = None, *, open_prs: OpenPrLookup = gh_open_pr_branches) -> int:
    """Command-line entry point. Returns the process exit code."""
    args = _parser().parse_args(argv)
    try:
        prune(args.repo, apply=args.apply, open_prs=open_prs, bases=args.base or DEFAULT_BASES)
    except RuntimeError as error:
        sys.stderr.write(f"error: {error}\n")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
