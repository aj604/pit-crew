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
import select
import shlex
import struct
import subprocess
import sys
import tempfile
import termios
import time
from collections.abc import Iterator
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


def record(argv: list[str], cast_path: str | Path, echo: BinaryIO | None = None) -> int:
    """Run argv in a pseudo-terminal, write an asciicast v2 recording, and return its exit code."""
    out = echo if echo is not None else sys.stdout.buffer
    env = {**os.environ, "TERM": "xterm-256color", "COLUMNS": str(WIDTH), "LINES": str(HEIGHT)}
    # Opened first, so a recording that can't be written fails before the check starts.
    with Path(cast_path).open("w") as cast:
        header = {"version": 2, "width": WIDTH, "height": HEIGHT, "timestamp": int(time.time())}
        cast.write(json.dumps({**header, "command": shlex.join(argv), "env": {"TERM": env["TERM"]}}) + "\n")
        master, slave = pty.openpty()
        try:
            start = time.monotonic()
            proc = _start(argv, slave, env)
            try:
                _copy(master, proc, out, cast, start)
            except BaseException:
                proc.kill()  # the recording broke: stop the check rather than leave it running unseen
                proc.wait()
                raise
        finally:
            os.close(master)
    code = proc.wait()
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


def _copy(master: int, proc: "subprocess.Popen[bytes]", out: BinaryIO, cast: TextIO, start: float) -> None:
    """Stream the check's output to the job log and to the recording until the check is done with it."""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    for data in _chunks(master, proc):
        out.write(data)
        out.flush()
        _event(cast, start, decoder.decode(data))
    _event(cast, start, decoder.decode(b"", final=True))  # a character cut off by the check exiting


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
