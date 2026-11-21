"""The host-side checkout that host-executed tools (such as ``profile_ci``) read.

The active workspace lives in a ``ContextVar``, so each thread or asyncio task scopes its own
checkout with ``use_workspace`` and nothing writes to ``os.environ``. ``PIT_CREW_WORKSPACE`` is still
read as a fallback, then ``/workspace`` (the managed-agent sandbox mount).

When no checkout is given, ``materialize`` downloads the repository tarball from the GitHub API
into a temporary directory, extracts only regular files and directories, and deletes it on exit.
"""

import os
import re
import shutil
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path, PurePosixPath

WORKSPACE_ENV = "PIT_CREW_WORKSPACE"
DEFAULT_WORKSPACE = "/workspace"
API = "https://api.github.com"

_ACTIVE: ContextVar[Path | None] = ContextVar("pit_crew_workspace", default=None)
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_REF = re.compile(r"[A-Za-z0-9_./-]+")

# (url, destination file) -> None; writes the response body to the destination.
Download = Callable[[str, Path], None]


class UnsafeArchiveError(ValueError):
    """Raised when a repository archive holds a member that could write outside the extraction folder."""

    def __init__(self, name: str, reason: str) -> None:
        """Name the offending member and why it was refused."""
        super().__init__(f"refusing archive member {name!r}: {reason}")


def configured_workspace() -> Path | None:
    """The workspace bound in this context, else ``PIT_CREW_WORKSPACE``, else None."""
    bound = _ACTIVE.get()
    if bound is not None:
        return bound
    env = os.environ.get(WORKSPACE_ENV)
    return Path(env) if env else None


def active_workspace() -> Path:
    """Where host tools read the checkout: the configured workspace, or ``/workspace``."""
    return configured_workspace() or Path(DEFAULT_WORKSPACE)


@contextmanager
def use_workspace(path: str | Path) -> Iterator[Path]:
    """Bind ``path`` as the active workspace for this context only, restoring the previous binding after."""
    bound = Path(path)
    token = _ACTIVE.set(bound)
    try:
        yield bound
    finally:
        _ACTIVE.reset(token)


def tarball_url(repo: str, ref: str | None = None) -> str:
    """The GitHub API URL of ``repo``'s tarball at ``ref`` (the default branch when None)."""
    name = repo.strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
    if not _REPO.fullmatch(name) or ".." in name.split("/"):
        msg = f"repo must look like owner/name, got {repo!r}"
        raise ValueError(msg)
    if ref is None:
        return f"{API}/repos/{name}/tarball"
    if not _REF.fullmatch(ref) or ".." in ref.split("/"):
        msg = f"ref must be a branch, tag or commit sha, got {ref!r}"
        raise ValueError(msg)
    return f"{API}/repos/{name}/tarball/{ref}"


def http_download(token: str | None) -> Download:
    """A ``Download`` over HTTPS that sends ``token`` to the GitHub API only, never to its redirect."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "pit-crew"}

    def download(url: str, dest: Path) -> None:
        if not url.startswith(f"{API}/"):  # the token must only ever go to the GitHub API
            msg = f"refusing to fetch {url!r}: not under {API}"
            raise ValueError(msg)
        request = urllib.request.Request(url, headers=headers)  # noqa: S310  https-only, checked above
        if token:
            # Unredirected: the API answers with a signed codeload.github.com URL that needs no token.
            request.add_unredirected_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request, timeout=60) as response, dest.open("wb") as out:  # noqa: S310
            shutil.copyfileobj(response, out)

    return download


def _relative(name: str) -> PurePosixPath | None:
    """``name`` without the archive's top-level folder; None for that folder itself."""
    path = PurePosixPath(name)
    if path.is_absolute() or name.startswith("/") or re.match(r"[A-Za-z]:", name):
        raise UnsafeArchiveError(name, "absolute path")
    if ".." in path.parts:
        raise UnsafeArchiveError(name, "path traversal")
    parts = [p for p in path.parts if p != "."]
    return PurePosixPath(*parts[1:]) if len(parts) > 1 else None


def _check_link(member: tarfile.TarInfo, rel: PurePosixPath) -> None:
    """Refuse a link whose target leaves the archive; links inside it are skipped, never created."""
    target = member.linkname
    if target.startswith("/") or re.match(r"[A-Za-z]:", target):
        raise UnsafeArchiveError(member.name, f"link to absolute path {target!r}")
    # Symlinks resolve from their own folder; hard links name another member from the archive root.
    base = rel.parent.parts if member.issym() else ()
    depth = len(base)
    target_parts = PurePosixPath(target).parts
    if member.islnk():
        target_parts = target_parts[1:]  # hard link targets carry the top-level folder too
    for part in target_parts:
        if part == "..":
            depth -= 1
            if depth < 0:
                raise UnsafeArchiveError(member.name, f"link escapes the archive via {target!r}")
        elif part != ".":
            depth += 1


def safe_extract(archive: Path, dest: Path) -> None:
    """Extract ``archive``'s regular files and folders into ``dest``, dropping its top-level folder.

    Raises ``UnsafeArchiveError`` for absolute paths, ``..`` components, and links pointing outside the
    archive. Links that stay inside, and special files, are skipped: nothing but files and folders is
    ever written, so no later member can write through a link.
    """
    root = dest.resolve()
    with tarfile.open(archive, "r:*") as tar:
        for member in tar:
            rel = _relative(member.name)
            if member.issym() or member.islnk():
                if rel is not None:
                    _check_link(member, rel)
                continue
            if rel is None or not (member.isfile() or member.isdir()):
                continue
            target = (root / rel).resolve()
            if not target.is_relative_to(root):
                raise UnsafeArchiveError(member.name, "resolves outside the extraction folder")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:  # pragma: no cover  isfile() members always have a body
                continue
            with source, target.open("wb") as out:
                shutil.copyfileobj(source, out)


@contextmanager
def materialize(repo: str, ref: str | None = None, *, download: Download | None = None) -> Iterator[Path]:
    """Download and extract ``repo`` at ``ref`` into a temporary folder, yield it, and delete it after."""
    url = tarball_url(repo, ref)
    fetch = download or http_download(os.environ.get("GITHUB_TOKEN"))
    with tempfile.TemporaryDirectory(prefix="pit-crew-workspace-") as tmp:
        archive = Path(tmp) / "repo.tar.gz"
        checkout = Path(tmp) / "repo"
        checkout.mkdir()
        fetch(url, archive)
        safe_extract(archive, checkout)
        archive.unlink()
        yield checkout
