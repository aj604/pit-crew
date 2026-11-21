"""Typed CI failure artifacts, safe ZIP/Playwright trace extraction, and media selection.

Standard library only so the Proof lane stays pure and isolated from ``httpx`` and
``google-genai``. Magic bytes are always checked before assigning a MIME type, and
all archive reads enforce strict path, symlink, entry-count, size, and compression
ratio bounds.
"""

import io
import json
import re
import stat
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

Modality = Literal["browser_video", "screenshot", "trace_frame", "terminal_video", "log"]

MODALITY_PRIORITY: tuple[Modality, ...] = (
    "browser_video",
    "screenshot",
    "trace_frame",
    "terminal_video",
    "log",
)
VISUAL_MODALITIES: frozenset[Modality] = frozenset({"browser_video", "screenshot", "trace_frame"})

MAX_ZIP_DOWNLOAD_BYTES = 50 * 1024 * 1024  # 50 MB raw archive limit
MAX_ZIP_DECOMPRESSED_BYTES = 60 * 1024 * 1024  # 60 MB cumulative decompressed limit
MAX_ZIP_ENTRIES = 200
MAX_COMPRESSION_RATIO = 50.0

MAX_BROWSER_VIDEOS = 1
MAX_VISUAL_IMAGES = 2
MAX_TERMINAL_VIDEOS = 1
MAX_TRACE_FRAMES = 2
MAX_LOG_CHARS = 8_000
MAX_SHARD_ARTIFACTS = 4

_READ_CHUNK_BYTES = 65_536
_MIN_WEBP_BYTES = 12
_MIN_MP4_BYTES = 12
_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/webp"})
_TEXT_EXTENSIONS = frozenset({".log", ".txt", ".out", ".md", ".xml", ".json", ".junit"})
_BROWSER_HINTS = ("playwright", "cypress", "test-results", "videos/", "browser", "e2e", "spec", "ui-check", "ui_check")
_TERMINAL_HINTS = ("terminal", "asciicast", "replay", "record", "pty", "cli-video")
_PRIORITY_ARTIFACT_HINTS = (
    "failure-recordings",
    "playwright",
    "cypress",
    "ui-check",
    "screenshot",
    "test-results",
    "trace",
    "video",
    "diff",
)
_MATRIX_SUFFIX = re.compile(r"\s*\(.*\)$")
_NON_PRINTABLE_RATIO = 0.15
_MIN_PRINTABLE_ASCII = 32


class UnsafeArchiveError(ValueError):
    """Raised when a ZIP or Playwright trace archive violates safety limits."""


@dataclass(frozen=True)
class MediaPart:
    """One typed failure artifact extracted from a workflow run or archive."""

    name: str
    mime_type: str
    data: bytes
    modality: Modality
    source_url: str = ""


@dataclass(frozen=True)
class ArtifactBundle:
    """Typed, budgeted collection of failure evidence gathered for one workflow run."""

    run_id: str
    run_url: str = ""
    parts: tuple[MediaPart, ...] = ()
    truncated: bool = False

    @property
    def primary_modality(self) -> Modality | None:
        """Return the highest-priority modality present in this bundle, or ``None`` when empty."""
        present = {part.modality for part in self.parts}
        for modality in MODALITY_PRIORITY:
            if modality in present:
                return modality
        return None

    @property
    def has_visual(self) -> bool:
        """True when the bundle contains at least one Tier 1 visual artifact."""
        return any(part.modality in VISUAL_MODALITIES for part in self.parts)

    @property
    def artifact_urls(self) -> tuple[str, ...]:
        """Return unique non-empty artifact URLs across parts, falling back to ``run_url``."""
        urls = list(dict.fromkeys(part.source_url for part in self.parts if part.source_url))
        if not urls and self.run_url:
            return (self.run_url,)
        return tuple(urls)


_MAGIC_PREFIXES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"\x1a\x45\xdf\xa3", "video/webm"),
    (b"PK\x03\x04", "application/zip"),
    (b"PK\x05\x06", "application/zip"),
    (b"PK\x07\x08", "application/zip"),
)


def detect_magic_mime(data: bytes) -> str | None:
    """Identify supported image, video, or ZIP MIME types solely from magic bytes."""
    for prefix, mime in _MAGIC_PREFIXES:
        if data.startswith(prefix):
            return mime
    if len(data) >= _MIN_WEBP_BYTES and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= _MIN_MP4_BYTES and data[4:8] == b"ftyp":
        return "video/mp4"
    return None


def is_png(data: bytes) -> bool:
    """Return True when ``data`` starts with valid PNG magic bytes."""
    return detect_magic_mime(data) == "image/png"


def trim_log_tail(text: str, max_chars: int = MAX_LOG_CHARS) -> tuple[str, bool]:
    """Return the trailing ``max_chars`` of ``text`` and whether truncation occurred."""
    if len(text) <= max_chars:
        return text, False
    return text[-max_chars:], True


def sniff_media_part(
    data: bytes,
    name: str = "artifact",
    *,
    source_url: str = "",
    fallback_mime: str | None = None,
) -> MediaPart:
    """Detect a real MIME type and modality from ``data`` instead of assuming ``video/mp4``.

    Magic bytes always win over filenames. When no binary media signature matches, UTF-8
    logs (or explicit text extensions) become ``text/plain``; opaque bytes fall back to
    ``fallback_mime`` when given (used by legacy ``compare_failures`` callers).
    """
    magic = detect_magic_mime(data)
    if magic is not None and magic != "application/zip":
        return MediaPart(
            name=name,
            mime_type=magic,
            data=data,
            modality=_modality_for_mime(magic, name),
            source_url=source_url,
        )
    if _looks_like_text_log(data, name, prefer_fallback=fallback_mime is not None):
        trimmed, _ = trim_log_tail(data.decode("utf-8", errors="replace"))
        return MediaPart(
            name=name,
            mime_type="text/plain",
            data=trimmed.encode("utf-8"),
            modality="log",
            source_url=source_url,
        )
    resolved_mime = fallback_mime or "text/plain"
    return MediaPart(
        name=name,
        mime_type=resolved_mime,
        data=data,
        modality=_modality_for_mime(resolved_mime, name),
        source_url=source_url,
    )


def bundle_from_raw(
    data: bytes,
    *,
    run_id: str,
    name: str = "recording",
    run_url: str = "",
    fallback_mime: str | None = None,
) -> ArtifactBundle:
    """Build a budgeted ``ArtifactBundle`` from raw run bytes (ZIP archive, media, or log)."""
    if not data:
        return ArtifactBundle(run_id=run_id, run_url=run_url)
    if detect_magic_mime(data) == "application/zip":
        extracted = extract_zip_artifact(data, source_url=run_url, artifact_name=name)
        return select_bundle_parts(extracted, run_id=run_id, run_url=run_url)
    part = sniff_media_part(data, name=name, source_url=run_url, fallback_mime=fallback_mime)
    return ArtifactBundle(run_id=run_id, run_url=run_url, parts=(part,))


def extract_zip_artifact(
    zip_bytes: bytes,
    *,
    source_url: str = "",
    artifact_name: str = "",
) -> list[MediaPart]:
    """Safely unpack a GitHub Actions artifact ZIP into typed ``MediaPart`` entries.

    Enforces download size, entry count, decompressed byte total, compression ratio,
    no symlinks, and no path traversal, and validates media via magic bytes.
    """
    entries = _read_safe_zip_entries(zip_bytes)
    parts: list[MediaPart] = []
    for entry_name, payload in entries:
        magic = detect_magic_mime(payload)
        if magic == "application/zip" and _is_playwright_trace_name(entry_name):
            parts.extend(extract_playwright_trace(payload, source_url=source_url, trace_name=entry_name))
            continue
        part = _part_from_entry(entry_name, payload, magic, source_url=source_url, artifact_name=artifact_name)
        if part is not None:
            parts.append(part)
    return parts


def extract_playwright_trace(
    trace_bytes: bytes,
    *,
    source_url: str = "",
    trace_name: str = "trace.zip",
) -> list[MediaPart]:
    """Safely inspect a Playwright ``trace.zip`` and extract up to 2 failure keyframes."""
    entries = _read_safe_zip_entries(trace_bytes)
    entry_map = dict(entries)
    failure_ts, failure_summary, frames = _parse_trace_ndjson(entries)

    selected_paths = _pick_trace_frame_paths(entry_map, frames, failure_ts)
    parts: list[MediaPart] = []
    for rel_path in selected_paths[:MAX_TRACE_FRAMES]:
        payload = entry_map[rel_path]
        mime = detect_magic_mime(payload)
        if mime in _IMAGE_MIMES:
            parts.append(
                MediaPart(
                    name=f"{trace_name}:{PurePosixPath(rel_path).name}",
                    mime_type=mime,
                    data=payload,
                    modality="trace_frame",
                    source_url=source_url,
                )
            )
    if failure_summary:
        parts.append(
            MediaPart(
                name=f"{trace_name}:failure.txt",
                mime_type="text/plain",
                data=failure_summary.encode("utf-8"),
                modality="log",
                source_url=source_url,
            )
        )
    return parts


def score_artifact_name(name: str, check: str = "") -> tuple[int, str]:
    """Rank artifact names for shard-aware selection: lower score tuple sorts first."""
    lower = name.lower()
    score = 0
    for hint in _PRIORITY_ARTIFACT_HINTS:
        if hint in lower:
            score -= 10
    for token in _check_tokens(check):
        if token in lower:
            score -= 25
    return (score, lower)


def select_bundle_parts(
    parts: Iterable[MediaPart],
    *,
    run_id: str,
    run_url: str = "",
) -> ArtifactBundle:
    """Apply the per-side modality hierarchy and byte/count budget to collected parts."""
    unique = _dedupe_parts(parts)
    by_modality: dict[Modality, list[MediaPart]] = {m: [] for m in MODALITY_PRIORITY}
    for part in unique:
        by_modality[part.modality].append(part)

    selected: list[MediaPart] = []
    truncated = False

    browser_videos = sorted(by_modality["browser_video"], key=_media_sort_key)
    selected.extend(browser_videos[:MAX_BROWSER_VIDEOS])
    truncated = truncated or len(browser_videos) > MAX_BROWSER_VIDEOS

    visual_images = [
        *sorted(by_modality["screenshot"], key=_screenshot_sort_key),
        *sorted(by_modality["trace_frame"], key=_media_sort_key),
    ]
    selected.extend(visual_images[:MAX_VISUAL_IMAGES])
    truncated = truncated or len(visual_images) > MAX_VISUAL_IMAGES

    has_tier1 = bool(selected)
    terminal_videos = sorted(by_modality["terminal_video"], key=_media_sort_key)
    if not has_tier1:
        selected.extend(terminal_videos[:MAX_TERMINAL_VIDEOS])
        truncated = truncated or len(terminal_videos) > MAX_TERMINAL_VIDEOS
    elif terminal_videos:
        truncated = True

    log_part, log_trimmed = _merge_logs(by_modality["log"])
    if log_part is not None:
        selected.append(log_part)
    truncated = truncated or log_trimmed

    return ArtifactBundle(
        run_id=run_id,
        run_url=run_url,
        parts=tuple(selected),
        truncated=truncated,
    )


def _read_safe_zip_entries(zip_bytes: bytes) -> list[tuple[str, bytes]]:
    if len(zip_bytes) > MAX_ZIP_DOWNLOAD_BYTES:
        msg = f"archive size {len(zip_bytes)} exceeds {MAX_ZIP_DOWNLOAD_BYTES}-byte download limit"
        raise UnsafeArchiveError(msg)
    if detect_magic_mime(zip_bytes) != "application/zip":
        msg = "payload is not a valid ZIP archive"
        raise UnsafeArchiveError(msg)
    try:
        archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        msg = f"corrupt ZIP archive: {exc}"
        raise UnsafeArchiveError(msg) from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            msg = f"archive contains {len(infos)} entries, exceeding limit of {MAX_ZIP_ENTRIES}"
            raise UnsafeArchiveError(msg)
        _preflight_zip_infos(infos)
        return _extract_checked_entries(archive, infos)


def _preflight_zip_infos(infos: Sequence[zipfile.ZipInfo]) -> None:
    declared_total = 0
    for info in infos:
        _validate_zip_info(info)
        if info.is_dir():
            continue
        declared_total += info.file_size
        if declared_total > MAX_ZIP_DECOMPRESSED_BYTES:
            msg = f"archive decompressed size exceeds {MAX_ZIP_DECOMPRESSED_BYTES}-byte limit"
            raise UnsafeArchiveError(msg)
        _check_compression_ratio(info.filename, info.file_size, info.compress_size)


def _validate_zip_info(info: zipfile.ZipInfo) -> None:
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        msg = f"archive entry {info.filename!r} is a symlink"
        raise UnsafeArchiveError(msg)
    name = info.filename
    if not name or "\\" in name or name.startswith("/"):
        msg = f"archive contains unsafe path {name!r}"
        raise UnsafeArchiveError(msg)
    parts = PurePosixPath(name).parts
    if not parts or ".." in parts or ":" in parts[0]:
        msg = f"archive contains unsafe path {name!r}"
        raise UnsafeArchiveError(msg)


def _check_compression_ratio(name: str, uncompressed: int, compressed: int) -> None:
    if uncompressed <= 0:
        return
    if compressed <= 0 or (uncompressed / compressed) > MAX_COMPRESSION_RATIO:
        msg = f"archive entry {name!r} exceeds {MAX_COMPRESSION_RATIO:.0f}:1 compression ratio limit"
        raise UnsafeArchiveError(msg)


def _extract_checked_entries(archive: zipfile.ZipFile, infos: Sequence[zipfile.ZipInfo]) -> list[tuple[str, bytes]]:
    entries: list[tuple[str, bytes]] = []
    actual_total = 0
    for info in infos:
        if info.is_dir():
            continue
        remaining = MAX_ZIP_DECOMPRESSED_BYTES - actual_total
        data = _read_bounded_entry(archive, info, remaining)
        actual_total += len(data)
        _check_compression_ratio(info.filename, len(data), info.compress_size)
        entries.append((info.filename, data))
    return entries


def _read_bounded_entry(archive: zipfile.ZipFile, info: zipfile.ZipInfo, max_bytes: int) -> bytes:
    buf = io.BytesIO()
    read_bytes = 0
    with archive.open(info, "r") as stream:
        while True:
            chunk = stream.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            read_bytes += len(chunk)
            if read_bytes > max_bytes:
                msg = f"archive entry {info.filename!r} exceeds {MAX_ZIP_DECOMPRESSED_BYTES}-byte decompressed limit"
                raise UnsafeArchiveError(msg)
            buf.write(chunk)
    return buf.getvalue()


def _is_playwright_trace_name(entry_name: str) -> bool:
    lower = entry_name.lower()
    return lower.endswith(".zip") and "trace" in PurePosixPath(lower).name


def _part_from_entry(
    entry_name: str,
    payload: bytes,
    magic: str | None,
    *,
    source_url: str,
    artifact_name: str,
) -> MediaPart | None:
    if not payload:
        return None
    if magic is not None and magic != "application/zip":
        return MediaPart(
            name=entry_name,
            mime_type=magic,
            data=payload,
            modality=_modality_for_mime(magic, entry_name, artifact_name=artifact_name),
            source_url=source_url,
        )
    if _has_text_extension(entry_name) and _is_utf8_text(payload):
        trimmed, _ = trim_log_tail(payload.decode("utf-8", errors="replace"))
        return MediaPart(
            name=entry_name,
            mime_type="text/plain",
            data=trimmed.encode("utf-8"),
            modality="log",
            source_url=source_url,
        )
    return None


def _parse_trace_ndjson(
    entries: Sequence[tuple[str, bytes]],
) -> tuple[float | None, str, list[tuple[float, str]]]:
    frames: list[tuple[float, str]] = []
    actions: dict[str, str] = {}
    failure_ts: float | None = None
    failure_summary = ""

    for name, payload in entries:
        if not name.lower().endswith(".trace"):
            continue
        for raw_line in payload.decode("utf-8", errors="replace").splitlines():
            event = _parse_json_object(raw_line)
            if event is None:
                continue
            _record_trace_event(event, actions, frames)
            err_ts, err_summary = _extract_trace_error(event, actions)
            if err_summary:
                failure_ts = err_ts if err_ts is not None else failure_ts
                failure_summary = err_summary
    return failure_ts, failure_summary, frames


def _parse_json_object(line: str) -> dict[str, object] | None:
    stripped = line.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _record_trace_event(
    event: dict[str, object],
    actions: dict[str, str],
    frames: list[tuple[float, str]],
) -> None:
    event_type = event.get("type")
    if event_type in {"before", "action"}:
        call_id = event.get("callId")
        label = event.get("apiName") or event.get("title") or event.get("method")
        if isinstance(call_id, str) and isinstance(label, str) and label:
            actions[call_id] = label
    elif event_type == "screencast-frame":
        sha1 = event.get("sha1")
        ts = _as_float(event.get("timestamp"))
        if isinstance(sha1, str) and sha1:
            frames.append((ts if ts is not None else float(len(frames)), sha1))


def _extract_trace_error(event: dict[str, object], actions: dict[str, str]) -> tuple[float | None, str]:
    error_val = event.get("error")
    if event.get("type") == "error" and isinstance(event.get("message"), str):
        return _as_float(event.get("timestamp")), str(event["message"]).strip()
    if not error_val:
        return None, ""
    message = _error_message(error_val)
    if not message:
        return None, ""
    call_id = event.get("callId")
    action = actions.get(call_id, "") if isinstance(call_id, str) else ""
    summary = f"{action}: {message}" if action else message
    ts = _as_float(event.get("endTime")) or _as_float(event.get("timestamp")) or _as_float(event.get("startTime"))
    return ts, summary


def _error_message(error_val: object) -> str:
    if isinstance(error_val, str):
        return error_val.strip()
    if isinstance(error_val, dict):
        nested = error_val.get("error")
        if isinstance(nested, dict) and isinstance(nested.get("message"), str):
            return str(nested["message"]).strip()
        if isinstance(error_val.get("message"), str):
            return str(error_val["message"]).strip()
    return ""


def _pick_trace_frame_paths(
    entry_map: dict[str, bytes],
    frames: Sequence[tuple[float, str]],
    failure_ts: float | None,
) -> list[str]:
    resolved: list[tuple[float, str]] = []
    for ts, sha1 in frames:
        matched = _resolve_resource_path(entry_map, sha1)
        if matched is not None:
            resolved.append((ts, matched))

    if resolved:
        resolved.sort(key=lambda item: item[0])
        if failure_ts is not None:
            before_failure = [item for item in resolved if item[0] <= failure_ts]
            pool = before_failure or resolved
        else:
            pool = resolved
        return list(dict.fromkeys(path for _, path in pool[-MAX_TRACE_FRAMES:]))

    fallback = sorted(
        path
        for path, payload in entry_map.items()
        if path.startswith("resources/") and detect_magic_mime(payload) in _IMAGE_MIMES
    )
    return fallback[-MAX_TRACE_FRAMES:]


def _resolve_resource_path(entry_map: dict[str, bytes], sha1: str) -> str | None:
    direct = f"resources/{sha1}"
    if direct in entry_map:
        return direct
    if sha1 in entry_map:
        return sha1
    for path in entry_map:
        if path.startswith("resources/") and PurePosixPath(path).name == sha1:
            return path
    return None


def _modality_for_mime(mime_type: str, name: str, *, artifact_name: str = "") -> Modality:
    if mime_type == "video/webm":
        return "browser_video"
    if mime_type in _IMAGE_MIMES:
        return "screenshot"
    if mime_type == "image/gif":
        return "terminal_video"
    if mime_type == "video/mp4":
        return _classify_mp4_modality(name, artifact_name)
    return "log"


def _classify_mp4_modality(name: str, artifact_name: str = "") -> Modality:
    combined = f"{artifact_name}/{name}".lower()
    if any(hint in combined for hint in _BROWSER_HINTS):
        return "browser_video"
    if any(hint in combined for hint in _TERMINAL_HINTS):
        return "terminal_video"
    return "terminal_video"


def _has_text_extension(name: str) -> bool:
    return PurePosixPath(name.lower()).suffix in _TEXT_EXTENSIONS


def _looks_like_text_log(data: bytes, name: str, *, prefer_fallback: bool) -> bool:
    if not data or b"\x00" in data:
        return False
    if _has_text_extension(name):
        return _is_utf8_text(data)
    if prefer_fallback and b"\n" not in data and b"\x1b[" not in data:
        return False
    return _is_utf8_text(data)


def _is_utf8_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if not text:
        return True
    non_printable = sum(1 for ch in text if ord(ch) < _MIN_PRINTABLE_ASCII and ch not in "\n\r\t\x1b")
    return (non_printable / len(text)) <= _NON_PRINTABLE_RATIO


def _dedupe_parts(parts: Iterable[MediaPart]) -> list[MediaPart]:
    seen: set[tuple[str, str, int, bytes]] = set()
    unique: list[MediaPart] = []
    for part in parts:
        key = (part.name, part.mime_type, len(part.data), part.data[:64])
        if key not in seen:
            seen.add(key)
            unique.append(part)
    return unique


def _media_sort_key(part: MediaPart) -> tuple[int, str]:
    lower = part.name.lower()
    priority = 0 if "fail" in lower or "error" in lower else 1
    return (priority, lower)


def _screenshot_sort_key(part: MediaPart) -> tuple[int, str]:
    lower = part.name.lower()
    if "test-failed" in lower or "-diff." in lower or "-actual." in lower:
        rank = 0
    elif "fail" in lower or "screenshot" in lower:
        rank = 1
    else:
        rank = 2
    return (rank, lower)


def _merge_logs(logs: Sequence[MediaPart]) -> tuple[MediaPart | None, bool]:
    if not logs:
        return None, False
    ordered = sorted(logs, key=lambda part: part.name.lower())
    chunks = [
        f"--- {part.name} ---\n{part.data.decode('utf-8', errors='replace').strip()}"
        for part in ordered
        if part.data.strip()
    ]
    if not chunks:
        return None, False
    combined = "\n\n".join(chunks)
    trimmed, was_trimmed = trim_log_tail(combined, MAX_LOG_CHARS)
    first = ordered[0]
    return (
        MediaPart(
            name=first.name if len(ordered) == 1 else "failed-logs.txt",
            mime_type="text/plain",
            data=trimmed.encode("utf-8"),
            modality="log",
            source_url=first.source_url,
        ),
        was_trimmed or len(ordered) > 1,
    )


def _check_tokens(check: str) -> list[str]:
    folded = _MATRIX_SUFFIX.sub("", check).lower()
    raw_tokens = re.split(r"[^a-z0-9]+", folded)
    return [tok for tok in raw_tokens if len(tok) >= 2 and tok not in {"ci", "run", "job"}]  # noqa: PLR2004


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None
