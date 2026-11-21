"""Record a CI check as a terminal video, so the proof shows the bug failing.

A replay job wraps its check:

    python record.py --cast replay.cast --video replay.mp4 -- bun test

The check's output goes to a pseudo-terminal, so it prints exactly what it prints in CI,
colours included. Its stdin stays the job's own: a terminal there would never send input
or end of file, so a check that reads stdin would hang until the job timed out, and a
tool that saw one might stop to prompt. Output still streams to the job log, and the
wrapper exits with the check's own exit code, so recording never changes whether the job
passes. The video is rendered with `agg` (cast to GIF) and `ffmpeg` (GIF to MP4); if
either is missing the job keeps its result and the replay is left unverified.

Standard library only, so a replay job can run this file without installing pit-crew.
"""

import argparse
import codecs
import fcntl
import json
import os
import pty
import re
import select
import shlex
import struct
import subprocess
import sys
import tempfile
import termios
import time
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import BinaryIO, TextIO

WIDTH, HEIGHT = 120, 36
# Gemini samples video at one frame a second; hold the last screen, where test runners
# print their failure summary, long enough to be read several times.
LAST_FRAME_SECONDS = 5
IDLE_SECONDS = 2
_SIGNAL_EXIT_BASE = 128  # a shell reports death by signal N as exit code 128 + N
_POLL_SECONDS = 0.1  # how often to check whether the check has exited
# After the check exits, how long to keep reading what it already wrote. A background
# process it started can hold the terminal open for as long as it likes; CI moves on when
# the check exits, so the recording does too.
_DRAIN_SECONDS = 1.0
SECRET_ENV_VARS_KEY = "PIT_CREW_SECRET_ENV_VARS"  # noqa: S105  env var name, not a secret
_MASK_REPLACEMENT = "***"
_MIN_AUTO_SECRET_LEN = 4
_TRIVIAL_ENV_VALUES = frozenset({"true", "false", "none", "null"})
_SENSITIVE_ENV_RE = re.compile(
    r"(?:^|_)(?:TOKEN|SECRET|PASS(?:WORD|WD)?|API_?KEY|PRIVATE_?KEY|ACCESS_?KEY"
    r"|AUTH(?:_?KEY|_?TOKEN)?|CREDENTIAL|BEARER|COOKIE|SIGNING_?KEY|ENCRYPTION_?KEY"
    r"|CONNECTION_?STRING|DSN)(?:$|_)",
    re.IGNORECASE,
)
_ADD_MASK_RE = re.compile(r"(?:^|[\r\n])(?:\x1b\[[0-9;]*[A-Za-z]|[ \t])*::add-mask::([^\r\n]+)")
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"(?i)authorization\s*:\s*(?:bearer|basic)\s+(\S{8,})"),
)


def record(argv: list[str], cast_path: str | Path, echo: BinaryIO | None = None) -> int:
    """Run argv in a pseudo-terminal, write a secret-scrubbed asciicast v2 recording, and return its exit code."""
    out = echo if echo is not None else sys.stdout.buffer
    masks = _collect_env_secrets(os.environ)
    env = {**os.environ, "TERM": "xterm-256color", "COLUMNS": str(WIDTH), "LINES": str(HEIGHT)}
    env.pop(SECRET_ENV_VARS_KEY, None)
    path = Path(cast_path)
    scrub_needed = False
    try:
        # Opened first, so a recording that can't be written fails before the check starts.
        with path.open("w") as cast:
            header = {"version": 2, "width": WIDTH, "height": HEIGHT, "timestamp": int(time.time())}
            cast.write(json.dumps({**header, "command": shlex.join(argv), "env": {"TERM": env["TERM"]}}) + "\n")
            scrub_needed = True
            master, slave = pty.openpty()
            try:
                start = time.monotonic()
                proc = _start(argv, slave, env)
                try:
                    _copy(master, proc, out, cast, start, masks=masks)
                except BaseException:
                    proc.kill()  # the recording broke: stop the check rather than leave it running unseen
                    proc.wait()
                    raise
            finally:
                os.close(master)
        code = proc.wait()
    finally:
        if scrub_needed and path.exists():
            _scrub_cast_file(path, masks)
    return _SIGNAL_EXIT_BASE - code if code < 0 else code


def video_commands(cast_path: str, video_path: str, gif: str) -> list[list[str]]:
    """Return the commands that turn a recording into an MP4 Gemini can watch, by way of a GIF."""
    agg = ["agg", "--idle-time-limit", str(IDLE_SECONDS), "--last-frame-duration", str(LAST_FRAME_SECONDS)]
    # H.264 needs even dimensions and yuv420p to play everywhere, including in Gemini.
    ffmpeg = ["ffmpeg", "-y", "-loglevel", "error", "-i", gif, "-movflags", "+faststart", "-pix_fmt", "yuv420p"]
    return [[*agg, cast_path, gif], [*ffmpeg, "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", video_path]]


def render_video(cast_path: str, video_path: str) -> None:
    """Render a recording to MP4. Raises OSError or CalledProcessError if agg or ffmpeg fails."""
    # The GIF is only a step on the way, so it goes somewhere it can't collide with the video.
    with tempfile.TemporaryDirectory(prefix="pit-crew-") as tmp:
        for command in video_commands(cast_path, video_path, str(Path(tmp) / "replay.gif")):
            subprocess.run(command, check=True)  # noqa: S603  fixed commands built by video_commands


def main(args: list[str] | None = None) -> int:
    """Run the check given after --, record it, optionally render the video, and return its exit code."""
    parser = argparse.ArgumentParser(description="Run a CI check and record it as a terminal video.")
    parser.add_argument("--cast", required=True, help="asciicast v2 recording to write")
    parser.add_argument("--video", help="MP4 to render from the recording (needs agg and ffmpeg)")
    parser.add_argument(
        "--video-on-failure",
        action="store_true",
        help="only render --video when the check exits non-zero",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="the check, after --")
    parsed = parser.parse_args(args)
    command: list[str] = parsed.command[1:] if parsed.command[:1] == ["--"] else parsed.command
    if not command:
        parser.error("give the check to run after --")

    code = record(command, parsed.cast)
    if parsed.video and (code != 0 or not parsed.video_on_failure):
        try:
            render_video(parsed.cast, parsed.video)
        except (OSError, subprocess.CalledProcessError) as error:
            sys.stderr.write(f"pit-crew: no video, so this replay stays unverified: {error}\n")
    return code


def _start(argv: list[str], slave: int, env: dict[str, str]) -> "subprocess.Popen[bytes]":
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", HEIGHT, WIDTH, 0, 0))
        return subprocess.Popen(  # noqa: S603  running the check it is given is this tool's job
            argv, stdout=slave, stderr=slave, env=env, start_new_session=True
        )
    finally:
        os.close(slave)  # the check has its own copy; ours would hold the terminal open after it exits


def _copy(  # noqa: PLR0913  internal stream loop passing recording handles and secret masks
    master: int,
    proc: "subprocess.Popen[bytes]",
    out: BinaryIO,
    cast: TextIO,
    start: float,
    *,
    masks: set[str],
) -> None:
    """Stream raw check output to the job log while writing secret-masked events to the recording."""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    for data in _chunks(master, proc):
        out.write(data)
        out.flush()
        text = decoder.decode(data)
        _register_add_masks(text, masks)
        _event(cast, start, _redact_text(text, masks))
    tail = decoder.decode(b"", final=True)  # a character cut off by the check exiting
    _register_add_masks(tail, masks)
    _event(cast, start, _redact_text(tail, masks))


def _collect_env_secrets(environ: Mapping[str, str]) -> set[str]:
    masks: set[str] = set()
    explicit_raw = environ.get(SECRET_ENV_VARS_KEY, "")
    explicit_names = {name.strip() for name in explicit_raw.split(",") if name.strip()}
    for name in explicit_names:
        value = environ.get(name, "")
        _add_secret_value(masks, value, min_len=1, expand_json=True)
    for name, value in environ.items():
        if name == SECRET_ENV_VARS_KEY or name in explicit_names:
            continue
        if _SENSITIVE_ENV_RE.search(name):
            _add_secret_value(masks, value, min_len=_MIN_AUTO_SECRET_LEN, expand_json=False)
    return masks


def _add_secret_value(masks: set[str], value: str, *, min_len: int, expand_json: bool) -> None:
    if not value or not value.strip():
        return
    if expand_json and value.lstrip().startswith("{"):
        _extract_json_secrets(masks, value)
    _add_secret_lines(masks, value, min_len=min_len)


def _extract_json_secrets(masks: set[str], raw_json: str) -> None:
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError:
        return
    if not isinstance(parsed, dict):
        return
    for item in parsed.values():
        if isinstance(item, str):
            _add_secret_lines(masks, item, min_len=_MIN_AUTO_SECRET_LEN)


def _add_secret_lines(masks: set[str], value: str, *, min_len: int) -> None:
    candidates = [value, *value.splitlines()] if ("\n" in value or "\r" in value) else [value]
    for candidate in candidates:
        if len(candidate) < min_len or not candidate.strip():
            continue
        if min_len >= _MIN_AUTO_SECRET_LEN and candidate.strip().lower() in _TRIVIAL_ENV_VALUES:
            continue
        masks.add(candidate)


def _unescape_workflow_command(value: str) -> str:
    return value.replace("%0D", "\r").replace("%0d", "\r").replace("%0A", "\n").replace("%0a", "\n").replace("%25", "%")


def _register_add_masks(text: str, masks: set[str]) -> None:
    for match in _ADD_MASK_RE.finditer(text):
        raw = match.group(1)
        _add_secret_lines(masks, raw, min_len=1)
        unescaped = _unescape_workflow_command(raw)
        if unescaped != raw:
            _add_secret_lines(masks, unescaped, min_len=1)


def _find_secret_spans(text: str, masks: Iterable[str]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = [(m.start(1), m.end(1)) for m in _ADD_MASK_RE.finditer(text)]
    mask_list = sorted({m for m in masks if m}, key=len, reverse=True)
    if mask_list:
        literal_re = re.compile("|".join(re.escape(item) for item in mask_list))
        spans.extend((m.start(), m.end()) for m in literal_re.finditer(text))
    for pattern in _SECRET_PATTERNS:
        group_idx = 1 if pattern.groups else 0
        spans.extend((m.start(group_idx), m.end(group_idx)) for m in pattern.finditer(text))
    return _merge_spans(spans)


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not spans:
        return []
    ordered = sorted(spans)
    merged: list[tuple[int, int]] = [ordered[0]]
    for start, end in ordered[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _redact_text(text: str, masks: set[str]) -> str:
    if not text:
        return text
    spans = _find_secret_spans(text, masks)
    if not spans:
        return text
    out: list[str] = []
    pos = 0
    for start, end in spans:
        out.append(text[pos:start])
        out.append(_MASK_REPLACEMENT)
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _scrub_cast_file(cast_path: Path, masks: set[str]) -> None:
    lines = cast_path.read_text(encoding="utf-8").splitlines()
    if len(lines) <= 1:
        return
    header = lines[0]
    events = [json.loads(line) for line in lines[1:]]
    boundaries: list[tuple[int, int, int]] = []
    chunks: list[str] = []
    offset = 0
    for idx, event in enumerate(events):
        if isinstance(event, list) and len(event) == 3 and event[1] == "o" and isinstance(event[2], str):  # noqa: PLR2004
            text = event[2]
            boundaries.append((offset, offset + len(text), idx))
            chunks.append(text)
            offset += len(text)
    combined = "".join(chunks)
    if not combined:
        return
    _register_add_masks(combined, masks)
    spans = _find_secret_spans(combined, masks)
    if not spans:
        return
    _apply_spans_to_events(events, boundaries, spans)
    cast_path.write_text("\n".join([header, *(json.dumps(ev) for ev in events)]) + "\n", encoding="utf-8")


def _apply_spans_to_events(
    events: list[object],
    boundaries: list[tuple[int, int, int]],
    spans: list[tuple[int, int]],
) -> None:
    span_idx = 0
    for ev_start, ev_end, idx in boundaries:
        while span_idx < len(spans) and spans[span_idx][1] <= ev_start:
            span_idx += 1
        if span_idx >= len(spans) or spans[span_idx][0] >= ev_end:
            continue
        event = events[idx]
        assert isinstance(event, list)  # noqa: S101  populated only from validated 3-element list events
        event[2] = _redact_event_slice(str(event[2]), ev_start, ev_end, spans, span_idx)


def _redact_event_slice(
    text: str,
    ev_start: int,
    ev_end: int,
    spans: list[tuple[int, int]],
    start_span_idx: int,
) -> str:
    pieces: list[str] = []
    cursor = ev_start
    idx = start_span_idx
    while idx < len(spans) and spans[idx][0] < ev_end:
        lo, hi = spans[idx]
        if lo > cursor:
            pieces.append(text[cursor - ev_start : lo - ev_start])
        if lo >= ev_start:
            pieces.append(_MASK_REPLACEMENT)
        cursor = max(cursor, min(hi, ev_end))
        idx += 1
    if cursor < ev_end:
        pieces.append(text[cursor - ev_start :])
    return "".join(pieces)


def _event(cast: TextIO, start: float, text: str) -> None:
    if text:
        cast.write(json.dumps([round(time.monotonic() - start, 6), "o", text]) + "\n")


def _chunks(master: int, proc: "subprocess.Popen[bytes]") -> Iterator[bytes]:
    """Yield the check's output until the terminal closes, or for at most _DRAIN_SECONDS after the check exits."""
    deadline = None
    while True:
        if deadline is None and proc.poll() is not None:
            deadline = time.monotonic() + _DRAIN_SECONDS
        timeout = _POLL_SECONDS if deadline is None else deadline - time.monotonic()
        if timeout <= 0:
            return  # the check has exited and the drain time is up, however much is still coming
        ready, _, _ = select.select([master], [], [], timeout)
        if not ready:
            if deadline is None:
                continue
            return
        try:
            data = os.read(master, 65536)
        except OSError:  # Linux raises EIO once the check closes the terminal
            return
        if not data:  # macOS returns end of file instead
            return
        yield data


if __name__ == "__main__":
    sys.exit(main())
