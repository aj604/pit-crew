import asyncio
import io
import os
import tarfile
import tempfile
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from pit_crew import workspace
from pit_crew.lanes.analysis import tools as analysis_tools

TOP = "org-repo-abc123"


@pytest.fixture(autouse=True)
def _no_env_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(workspace.WORKSPACE_ENV, raising=False)


def _member(name: str, kind: bytes = tarfile.REGTYPE, *, data: bytes = b"", link: str = "") -> tuple[Any, bytes]:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = link
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    return info, data


def _archive(path: Path, *members: tuple[Any, bytes]) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if info.isfile() else None)
    return path


def _github_like(path: Path, *extra: tuple[Any, bytes]) -> Path:
    return _archive(
        path,
        _member(TOP, tarfile.DIRTYPE),
        _member(f"{TOP}/.github", tarfile.DIRTYPE),
        _member(f"{TOP}/.github/workflows/ci.yml", data=b"jobs: {}\n"),
        _member(f"{TOP}/README.md", data=b"hello"),
        *extra,
    )


# Resolution order and scoping


def test_active_workspace_prefers_the_context_then_the_env_then_slash_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert workspace.configured_workspace() is None
    assert workspace.active_workspace() == Path("/workspace")
    monkeypatch.setenv(workspace.WORKSPACE_ENV, str(tmp_path / "env"))
    assert workspace.active_workspace() == tmp_path / "env"
    with workspace.use_workspace(tmp_path / "ctx") as bound:
        assert bound == tmp_path / "ctx"
        assert workspace.active_workspace() == tmp_path / "ctx"
        assert analysis_tools.workspace() == tmp_path / "ctx"
        with workspace.use_workspace(tmp_path / "inner"):
            assert workspace.active_workspace() == tmp_path / "inner"
        assert workspace.active_workspace() == tmp_path / "ctx"
    assert workspace.active_workspace() == tmp_path / "env"


def test_use_workspace_resets_even_when_the_block_raises(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError), workspace.use_workspace(tmp_path):
        raise RuntimeError
    assert workspace.configured_workspace() is None


def test_concurrent_threads_each_see_only_their_own_workspace(tmp_path: Path) -> None:
    workers = 8
    barrier = threading.Barrier(workers)
    environ_before = dict(os.environ)
    for i in range(workers):
        (tmp_path / str(i) / ".github" / "workflows").mkdir(parents=True)
        (tmp_path / str(i) / ".github" / "workflows" / "ci.yml").write_text(f"name: w{i}\n")

    def work(i: int) -> tuple[Path, str, Path, bool]:
        with workspace.use_workspace(tmp_path / str(i)):
            barrier.wait(timeout=5)  # every worker is inside its own scope at once
            seen = analysis_tools.workspace()
            text = analysis_tools.workflow_path("ci.yml").read_text()
            barrier.wait(timeout=5)
            env_unchanged = dict(os.environ) == environ_before
        after = workspace.active_workspace()
        return seen, text, after, env_unchanged

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(work, range(workers)))

    for i, (seen, text, after, env_unchanged) in enumerate(results):
        assert seen == tmp_path / str(i)
        assert text == f"name: w{i}\n"
        assert after == Path("/workspace")
        assert env_unchanged
    assert dict(os.environ) == environ_before


def test_concurrent_asyncio_tasks_each_see_only_their_own_workspace(tmp_path: Path) -> None:
    async def task(i: int) -> Path:
        with workspace.use_workspace(tmp_path / str(i)):
            await asyncio.sleep(0)  # let every other task bind its own workspace first
            return workspace.active_workspace()

    async def run_all() -> list[Path]:
        return list(await asyncio.gather(*(task(i) for i in range(5))))

    assert asyncio.run(run_all()) == [tmp_path / str(i) for i in range(5)]
    assert workspace.configured_workspace() is None


# Safe extraction


def test_safe_extract_drops_the_top_folder_and_writes_regular_files(tmp_path: Path) -> None:
    dest = tmp_path / "out"
    dest.mkdir()
    workspace.safe_extract(_github_like(tmp_path / "a.tgz"), dest)
    assert (dest / ".github" / "workflows" / "ci.yml").read_text() == "jobs: {}\n"
    assert (dest / "README.md").read_text() == "hello"
    assert sorted(p.name for p in dest.iterdir()) == [".github", "README.md"]


@pytest.mark.parametrize(
    ("member", "reason"),
    [
        (_member(f"{TOP}/../escaped.txt", data=b"x"), "traversal"),
        (_member(f"{TOP}/a/../../escaped.txt", data=b"x"), "traversal"),
        (_member("../escaped.txt", data=b"x"), "traversal"),
        (_member("/var/escaped.txt", data=b"x"), "absolute"),
        (_member("C:/escaped.txt", data=b"x"), "absolute"),
        (_member(f"{TOP}/link", tarfile.SYMTYPE, link="/etc/passwd"), "absolute"),
        (_member(f"{TOP}/link", tarfile.SYMTYPE, link="../../escaped.txt"), "escapes"),
        (_member(f"{TOP}/a/link", tarfile.SYMTYPE, link="../../outside"), "escapes"),
        (_member(f"{TOP}/hard", tarfile.LNKTYPE, link="/etc/passwd"), "absolute"),
        (_member(f"{TOP}/hard", tarfile.LNKTYPE, link=f"{TOP}/../../escaped.txt"), "escapes"),
    ],
)
def test_safe_extract_rejects_members_that_would_escape(tmp_path: Path, member: tuple[Any, bytes], reason: str) -> None:
    dest = tmp_path / "nested" / "out"
    dest.mkdir(parents=True)
    archive = _github_like(tmp_path / "evil.tgz", member)

    with pytest.raises(workspace.UnsafeArchiveError, match=reason):
        workspace.safe_extract(archive, dest)

    assert not (tmp_path / "nested" / "escaped.txt").exists()
    assert not (tmp_path / "escaped.txt").exists()
    assert not any(p.is_symlink() for p in dest.rglob("*"))


def test_safe_extract_skips_links_and_special_files_that_stay_inside(tmp_path: Path) -> None:
    dest = tmp_path / "out"
    dest.mkdir()
    archive = _github_like(
        tmp_path / "a.tgz",
        _member(f"{TOP}/docs/readme-link", tarfile.SYMTYPE, link="../README.md"),
        _member(f"{TOP}/hard", tarfile.LNKTYPE, link=f"{TOP}/README.md"),
        _member(f"{TOP}/fifo", tarfile.FIFOTYPE),
        _member(f"{TOP}/./dot.txt", data=b"dot"),
    )

    workspace.safe_extract(archive, dest)

    assert not (dest / "docs" / "readme-link").exists()
    assert not (dest / "docs" / "readme-link").is_symlink()
    assert not (dest / "hard").exists()
    assert not (dest / "fifo").exists()
    assert (dest / "dot.txt").read_text() == "dot"


def test_safe_extract_never_writes_through_a_preexisting_symlink(tmp_path: Path) -> None:
    dest = tmp_path / "out"
    dest.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (dest / "sub").symlink_to(outside)  # a link the archive could not have made, but check anyway
    archive = _archive(tmp_path / "a.tgz", _member(f"{TOP}/sub/f.txt", data=b"x"))

    with pytest.raises(workspace.UnsafeArchiveError, match="outside the extraction folder"):
        workspace.safe_extract(archive, dest)
    assert not (outside / "f.txt").exists()


# Materialize


def _serving(archive: Path, urls: list[str]) -> workspace.Download:
    def download(url: str, dest: Path) -> None:
        urls.append(url)
        dest.write_bytes(archive.read_bytes())

    return download


def test_materialize_yields_the_checkout_and_deletes_it_after(tmp_path: Path) -> None:
    urls: list[str] = []
    archive = _github_like(tmp_path / "a.tgz")

    with workspace.materialize("Org/Repo", "abc123", download=_serving(archive, urls)) as checkout:
        assert (checkout / ".github" / "workflows" / "ci.yml").is_file()
        assert not (checkout.parent / "repo.tar.gz").exists()
        temp_root = checkout.parent

    assert urls == ["https://api.github.com/repos/Org/Repo/tarball/abc123"]
    assert not temp_root.exists()


def test_materialize_cleans_up_when_extraction_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[Path] = []
    real = tempfile.TemporaryDirectory

    class Tracking(real):  # type: ignore[valid-type,misc]
        def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401
            super().__init__(**kwargs)
            created.append(Path(self.name))

    monkeypatch.setattr(tempfile, "TemporaryDirectory", Tracking)
    archive = _github_like(tmp_path / "evil.tgz", _member(f"{TOP}/../x", data=b"x"))

    with (
        pytest.raises(workspace.UnsafeArchiveError),
        workspace.materialize("o/r", download=_serving(archive, [])),
    ):
        pytest.fail("must not yield")

    assert len(created) == 1
    assert not created[0].exists()


def test_materialize_uses_the_github_token_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tokens: list[str | None] = []
    archive = _github_like(tmp_path / "a.tgz")

    def factory(token: str | None) -> workspace.Download:
        tokens.append(token)
        return _serving(archive, [])

    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(workspace, "http_download", factory)
    with workspace.materialize("o/r") as checkout:
        assert (checkout / "README.md").is_file()
    assert tokens == ["tok"]


@pytest.mark.parametrize(
    ("repo", "ref", "url"),
    [
        ("o/r", None, "https://api.github.com/repos/o/r/tarball"),
        (" https://github.com/o/r.git ", "main", "https://api.github.com/repos/o/r/tarball/main"),
        ("o/r", "feature/x", "https://api.github.com/repos/o/r/tarball/feature/x"),
    ],
)
def test_tarball_url(repo: str, ref: str | None, url: str) -> None:
    assert workspace.tarball_url(repo, ref) == url


@pytest.mark.parametrize(
    ("repo", "ref"),
    [("o", None), ("o/r/x", None), ("o/..", None), ("o/r", "../x"), ("o/r", "a b"), ("o/r", "a/../b")],
)
def test_tarball_url_rejects_odd_repos_and_refs(repo: str, ref: str | None) -> None:
    with pytest.raises(ValueError, match="must"):
        workspace.tarball_url(repo, ref)


# HTTP download


def test_http_download_only_sends_the_token_to_the_github_api(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="refusing"):
        workspace.http_download("secret")("https://evil.example/o/r.tgz", tmp_path / "x")
    assert not (tmp_path / "x").exists()


@pytest.mark.parametrize("token", ["secret", None])
def test_http_download_writes_the_body_and_keeps_the_token_off_redirects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str | None
) -> None:
    requests: list[urllib.request.Request] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> io.BytesIO:
        assert timeout > 0
        requests.append(request)
        return io.BytesIO(b"tarball-bytes")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    workspace.http_download(token)("https://api.github.com/repos/o/r/tarball", tmp_path / "a.tgz")

    assert (tmp_path / "a.tgz").read_bytes() == b"tarball-bytes"
    (request,) = requests
    assert request.full_url == "https://api.github.com/repos/o/r/tarball"
    assert request.get_header("User-agent") == "pit-crew"
    # Redirect handlers copy request.headers only, so the token must not live there.
    assert "Authorization" not in request.headers
    assert request.unredirected_hdrs.get("Authorization") == (f"Bearer {token}" if token else None)
