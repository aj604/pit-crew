import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from pit_crew import worktrees
from pit_crew.worktrees import Worktree, disk_usage, human_size, main, parse_worktrees, prune

OPEN_PR_BRANCH = "pr-open"


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep every git call off the user's config and identity."""
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    monkeypatch.chdir(tmp_path)


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603  fixed git commands on a temporary repo
        ["git", "-C", str(cwd), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def commit(cwd: Path, name: str, text: str = "x\n") -> None:
    (cwd / name).write_text(text)
    git(cwd, "add", name)
    git(cwd, "commit", "-q", "-m", f"add {name}")


def add_worktree(repo: Path, name: str, *, branch: bool = True) -> Path:
    path = repo.parent / "wt" / name
    if branch:
        git(repo, "worktree", "add", "-q", "-b", name, str(path), "main")
    else:
        git(repo, "worktree", "add", "-q", "--detach", str(path), "main")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / ".gitignore").write_text(".venv/\n")
    git(path, "add", ".gitignore")
    commit(path, "README.md")
    return path


@pytest.fixture
def layout(repo: Path) -> dict[str, Path]:
    """One worktree per guard, each failing exactly that guard, plus two finished ones."""
    paths = {name: add_worktree(repo, name) for name in ("merged", "staged", "unstaged", "untracked", "unmerged")}
    paths["detached"] = add_worktree(repo, "detached", branch=False)
    paths[OPEN_PR_BRANCH] = add_worktree(repo, OPEN_PR_BRANCH)

    venv = paths["merged"] / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "big.bin").write_bytes(b"\0" * 200_000)

    (paths["staged"] / "new.txt").write_text("s\n")
    git(paths["staged"], "add", "new.txt")
    (paths["unstaged"] / "README.md").write_text("changed\n")
    (paths["untracked"] / "notes.txt").write_text("u\n")
    commit(paths["unmerged"], "feature.txt")
    paths["main"] = repo
    return paths


def open_pr(_repo: Path) -> frozenset[str]:
    return frozenset({OPEN_PR_BRANCH})


def registered(repo: Path) -> set[Path]:
    return {w.path.resolve() for w in parse_worktrees(git(repo, "worktree", "list", "--porcelain"))}


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    for dirpath, dirs, files in os.walk(root):
        for name in (*dirs, *files):
            stat = (Path(dirpath) / name).lstat()
            result[str(Path(dirpath) / name)] = (stat.st_size, stat.st_mtime_ns)
    return result


KEPT_REASONS = {
    "main": "main checkout",
    "staged": "dirty: uncommitted or untracked changes",
    "unstaged": "dirty: uncommitted or untracked changes",
    "untracked": "dirty: uncommitted or untracked changes",
    "unmerged": "has commits not on the base ref",
    OPEN_PR_BRANCH: "branch has an open pull request",
}


def test_dry_run_reports_without_touching_anything(layout: dict[str, Path], tmp_path: Path) -> None:
    repo = layout["main"]
    before = snapshot(tmp_path)
    lines: list[str] = []

    total = prune(repo, apply=False, open_prs=open_pr, out=lines.append)

    assert snapshot(tmp_path) == before
    assert registered(repo) == {p.resolve() for p in layout.values()}
    assert total >= 200_000
    removals = [line for line in lines if line.startswith("remove")]
    assert len(removals) == 2
    assert any(str(layout["merged"]) in line and "[dry run]" in line for line in removals)
    assert any(str(layout["detached"]) in line for line in removals)
    assert "Would reclaim" in lines[-1]


def test_apply_removes_only_finished_worktrees(layout: dict[str, Path]) -> None:
    repo = layout["main"]
    lines: list[str] = []

    total = prune(repo, apply=True, open_prs=open_pr, out=lines.append)

    assert not layout["merged"].exists()
    assert not layout["detached"].exists()
    assert total >= 200_000
    kept = {name: layout[name].resolve() for name in KEPT_REASONS}
    assert registered(repo) == set(kept.values())
    for name, reason in KEPT_REASONS.items():
        assert all(path.exists() for path in kept.values())
        assert any(line.startswith("keep") and f"{layout[name]}  " in line and reason in line for line in lines), name
    assert lines[-1].startswith("Reclaimed ")
    assert "0 B" not in lines[-1]


@pytest.mark.parametrize("guard", sorted(KEPT_REASONS))
def test_each_guard_alone_keeps_its_worktree(layout: dict[str, Path], guard: str) -> None:
    repo = layout["main"]
    prune(repo, apply=True, open_prs=open_pr, out=lambda _: None)
    assert layout[guard].resolve() in registered(repo)
    assert layout[guard].exists()


def add_nested_worktree(repo: Path, parent: Path, name: str, *, ignored: bool = True) -> Path:
    """A worktree of ``repo`` placed inside ``parent``'s directory, optionally gitignored there."""
    if ignored:
        exclude = Path(git(parent, "rev-parse", "--git-path", "info/exclude").strip())
        exclude = exclude if exclude.is_absolute() else parent / exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(".wt/\n")
    path = parent / ".wt" / name
    git(repo, "worktree", "add", "-q", "-b", name, str(path), "main")
    return path


def test_dirty_worktree_nested_in_a_removable_one_survives_apply(repo: Path) -> None:
    parent = add_worktree(repo, "parent")
    child = add_nested_worktree(repo, parent, "child")
    (child / "wip.txt").write_text("uncommitted\n")
    lines: list[str] = []

    prune(repo, apply=True, open_prs=lambda _: frozenset(), out=lines.append)

    assert (child / "wip.txt").read_text() == "uncommitted\n"
    assert parent.exists()
    assert registered(repo) == {repo.resolve(), parent.resolve(), child.resolve()}
    assert any(line.startswith("keep") and f"{parent}  " in line and "contains worktree" in line for line in lines)
    assert not any(line.startswith("removed") for line in lines)


def test_clean_nested_worktrees_are_removed_before_their_parent(repo: Path) -> None:
    parent = add_worktree(repo, "parent")
    child = add_nested_worktree(repo, parent, "child")

    prune(repo, apply=True, open_prs=lambda _: frozenset(), out=lambda _: None)

    assert not parent.exists()
    assert not child.exists()
    assert registered(repo) == {repo.resolve()}


def test_kept_grandchild_keeps_every_ancestor(repo: Path) -> None:
    parent = add_worktree(repo, "parent")
    child = add_nested_worktree(repo, parent, "child")
    grandchild = add_nested_worktree(repo, child, "grandchild")
    (grandchild / "wip.txt").write_text("uncommitted\n")

    prune(repo, apply=True, open_prs=lambda _: frozenset(), out=lambda _: None)

    assert (grandchild / "wip.txt").exists()
    assert registered(repo) == {repo.resolve(), parent.resolve(), child.resolve(), grandchild.resolve()}


def test_failed_nested_removal_keeps_its_parent(repo: Path, tmp_path: Path) -> None:
    # Git refuses to remove a worktree holding an initialised submodule, though it classifies as finished.
    source = tmp_path / "subsrc"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    commit(source, "f")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source), "sm")
    git(repo, "commit", "-q", "-m", "add sm")
    parent = add_worktree(repo, "parent")
    child = add_nested_worktree(repo, parent, "child")
    git(child, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "-q")
    lines: list[str] = []

    total = prune(repo, apply=True, open_prs=lambda _: frozenset(), out=lines.append)

    assert child.exists()
    assert parent.exists()
    assert total == 0
    assert registered(repo) == {repo.resolve(), parent.resolve(), child.resolve()}
    assert any(line.startswith("failed") and f"{child}  " in line for line in lines)
    assert any(line.startswith("keep") and f"{parent}  " in line and "was not removed" in line for line in lines)
    assert not any(line.startswith("removed") for line in lines)


def test_dry_run_does_not_promise_to_remove_a_parent_of_a_kept_worktree(repo: Path) -> None:
    parent = add_worktree(repo, "parent")
    child = add_nested_worktree(repo, parent, "child")
    (child / "wip.txt").write_text("uncommitted\n")
    lines: list[str] = []

    prune(repo, apply=False, open_prs=lambda _: frozenset(), out=lines.append)

    assert not any(line.startswith("remove ") for line in lines)


def test_apply_prunes_entries_whose_directory_is_gone(repo: Path) -> None:
    gone = add_worktree(repo, "gone")
    shutil.rmtree(gone)

    prune(repo, apply=False, open_prs=open_pr, out=lambda _: None)
    assert gone.resolve() in registered(repo)

    prune(repo, apply=True, open_prs=open_pr, out=lambda _: None)
    assert gone.resolve() not in registered(repo)


def test_unknown_open_prs_keeps_branches_but_not_detached(repo: Path) -> None:
    branch = add_worktree(repo, "feature")
    detached = add_worktree(repo, "loose", branch=False)
    lines: list[str] = []

    prune(repo, apply=True, open_prs=lambda _: None, out=lines.append)

    assert branch.exists()
    assert not detached.exists()
    assert any("could not check for open pull requests" in line for line in lines)
    assert any(line.startswith("warning: could not list open pull requests") for line in lines)


def test_missing_base_keeps_everything(repo: Path) -> None:
    path = add_worktree(repo, "feature")
    lines: list[str] = []

    prune(repo, apply=True, open_prs=open_pr, bases=("nope",), out=lines.append)

    assert path.exists()
    assert any("no base ref to compare against" in line for line in lines)
    assert lines[0].startswith("warning: none of nope resolve")


def test_locked_and_current_directory_are_kept(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    locked = add_worktree(repo, "locked")
    git(repo, "worktree", "lock", str(locked))
    here = add_worktree(repo, "here")
    monkeypatch.chdir(here / ".")
    lines: list[str] = []

    prune(repo, apply=True, open_prs=open_pr, out=lines.append)

    assert locked.exists()
    assert here.exists()
    assert any("locked" in line and str(locked) in line for line in lines)
    assert any("current working directory" in line for line in lines)


def test_failed_removal_is_reported_and_not_counted(repo: Path) -> None:
    path = add_worktree(repo, "racy")

    def lock_then_answer(repo_: Path) -> frozenset[str]:
        # The worktree passes every guard, then gets locked before removal; git refuses without --force.
        git(repo_, "worktree", "lock", str(path))
        return frozenset()

    lines: list[str] = []
    total = prune(repo, apply=True, open_prs=lock_then_answer, out=lines.append)

    assert path.exists()
    assert total == 0
    assert any(line.startswith("failed") for line in lines)


def test_no_lookup_when_only_detached_worktrees(repo: Path) -> None:
    add_worktree(repo, "loose", branch=False)

    def fail(_repo: Path) -> frozenset[str]:
        raise AssertionError

    prune(repo, apply=False, open_prs=fail, out=lambda _: None)


def test_git_status_failure_counts_as_unsafe(repo: Path) -> None:
    path = add_worktree(repo, "broken")
    (path / ".git").write_text("gitdir: /nonexistent\n")
    lines: list[str] = []

    prune(repo, apply=True, open_prs=open_pr, out=lines.append)

    assert path.exists()
    assert any("git status failed" in line for line in lines)


def test_uncomparable_head_counts_as_unsafe(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    worktree = Worktree(path=repo, branch="x")
    monkeypatch.setattr(worktrees, "_git", lambda *_: subprocess.CompletedProcess([], 128, "", ""))
    assert worktrees._unmerged_reason(worktree, "abc") == "could not compare with the base ref"  # noqa: SLF001


def test_parse_worktrees_handles_flags() -> None:
    porcelain = (
        "worktree /repo\nbare\n\n"
        "worktree /a\nHEAD abc\nbranch refs/heads/feat/x\nlocked reason\n\n"
        "worktree /b\nHEAD def\ndetached\nprunable gitdir file points to non-existent location\n\n"
        "junk\n"
    )
    assert parse_worktrees(porcelain) == [
        Worktree(path=Path("/repo"), bare=True),
        Worktree(path=Path("/a"), head="abc", branch="feat/x", locked=True),
        Worktree(path=Path("/b"), head="def", prunable=True),
    ]


def test_bare_entries_are_kept(tmp_path: Path) -> None:
    verdict = worktrees.classify(
        Worktree(path=tmp_path, bare=True), main=Path("/elsewhere"), cwd=Path("/"), base="x", open_prs=frozenset()
    )
    assert verdict.reason == "bare repository"


def test_human_size() -> None:
    assert human_size(512) == "512 B"
    assert human_size(2048) == "2.0 KiB"
    assert human_size(5 * 1024**3) == "5.0 GiB"
    assert human_size(3 * 1024**4) == "3.0 TiB"


def test_disk_usage_does_not_follow_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "big").write_bytes(b"\0" * 100_000)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "link").symlink_to(target)
    assert disk_usage(tree) < 100_000
    assert disk_usage(target) >= 100_000


def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f"#!/bin/sh\n{script}\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def test_gh_lookup_reads_open_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = json.dumps([{"headRefName": "a"}, {"headRefName": "b"}])
    fake_gh(tmp_path, monkeypatch, f"echo '{rows}'")
    assert worktrees.gh_open_pr_branches(tmp_path) == frozenset({"a", "b"})


@pytest.mark.parametrize("script", ["exit 1", "echo not-json", "echo '[{\"x\": 1}]'"])
def test_gh_lookup_failures_are_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    fake_gh(tmp_path, monkeypatch, script)
    assert worktrees.gh_open_pr_branches(tmp_path) is None


def test_gh_lookup_without_gh_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert worktrees.gh_open_pr_branches(tmp_path) is None


def test_main_defaults_to_dry_run(layout: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--repo", str(layout["main"])], open_prs=open_pr) == 0
    assert layout["merged"].exists()
    assert "Would reclaim" in capsys.readouterr().out


def test_main_apply_with_base(layout: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--repo", str(layout["main"]), "--base", "main", "--apply"], open_prs=open_pr) == 0
    assert not layout["merged"].exists()
    assert "Reclaimed" in capsys.readouterr().out


def test_main_rejects_both_modes() -> None:
    with pytest.raises(SystemExit):
        main(["--dry-run", "--apply"])


def test_main_outside_a_repo_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--repo", str(tmp_path)], open_prs=open_pr) == 1
    assert "git worktree list failed" in capsys.readouterr().err
