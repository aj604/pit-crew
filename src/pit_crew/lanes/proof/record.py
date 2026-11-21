"""Record a CI check as a terminal video, so the proof shows the bug failing.

A replay job wraps its check:

    python record.py --cast replay.cast --video replay.mp4 -- bun test

The check runs in a pseudo-terminal, so it prints exactly what it prints in CI, colours
included. Output still streams to the job log, and the wrapper exits with the check's own
exit code, so recording never changes whether the job passes. The video is rendered with
`agg` (cast to GIF) and `ffmpeg` (GIF to MP4); if either is missing the job keeps its
result and the replay is left unverified.

Standard library only, so a replay job can run this file without installing pit-crew.
"""

import argparse
import codecs
import fcntl
import json
import os
import pty
import shlex
import struct
import subprocess
import sys
import termios
import time
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

WIDTH, HEIGHT = 120, 36
# Gemini samples video at one frame a second; hold the last screen, where test runners
# print their failure summary, long enough to be read several times.
LAST_FRAME_SECONDS = 5
IDLE_SECONDS = 2
_SIGNAL_EXIT_BASE = 128  # a shell reports death by signal N as exit code 128 + N


def record(argv: list[str], cast_path: str | Path, echo: BinaryIO | None = None) -> int:
    """Run argv in a pseudo-terminal, write an asciicast v2 recording, and return its exit code."""
    out = echo if echo is not None else sys.stdout.buffer
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", HEIGHT, WIDTH, 0, 0))
    env = {**os.environ, "TERM": "xterm-256color", "COLUMNS": str(WIDTH), "LINES": str(HEIGHT)}
    start = time.monotonic()
    proc = subprocess.Popen(  # noqa: S603  running the check it is given is this tool's job
        argv, stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True
    )
    os.close(slave)

    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    with Path(cast_path).open("w") as cast:
        header = {"version": 2, "width": WIDTH, "height": HEIGHT, "timestamp": int(time.time())}
        cast.write(json.dumps({**header, "command": shlex.join(argv), "env": {"TERM": env["TERM"]}}) + "\n")
        for data in _chunks(master):
            out.write(data)
            out.flush()
            if text := decoder.decode(data):
                cast.write(json.dumps([round(time.monotonic() - start, 6), "o", text]) + "\n")
    os.close(master)
    code = proc.wait()
    return _SIGNAL_EXIT_BASE - code if code < 0 else code


def video_commands(cast_path: str, video_path: str) -> list[list[str]]:
    """Return the commands that turn a recording into an MP4 Gemini can watch."""
    gif = str(Path(video_path).with_suffix(".gif"))
    agg = ["agg", "--idle-time-limit", str(IDLE_SECONDS), "--last-frame-duration", str(LAST_FRAME_SECONDS)]
    # H.264 needs even dimensions and yuv420p to play everywhere, including in Gemini.
    ffmpeg = ["ffmpeg", "-y", "-loglevel", "error", "-i", gif, "-movflags", "+faststart", "-pix_fmt", "yuv420p"]
    return [[*agg, cast_path, gif], [*ffmpeg, "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", video_path]]


def render_video(cast_path: str, video_path: str) -> None:
    """Render a recording to MP4. Raises OSError or CalledProcessError if agg or ffmpeg fails."""
    for command in video_commands(cast_path, video_path):
        subprocess.run(command, check=True)  # noqa: S603  fixed commands built by video_commands


def main(args: list[str] | None = None) -> int:
    """Run the check given after --, record it, optionally render the video, and return its exit code."""
    parser = argparse.ArgumentParser(description="Run a CI check and record it as a terminal video.")
    parser.add_argument("--cast", required=True, help="asciicast v2 recording to write")
    parser.add_argument("--video", help="MP4 to render from the recording (needs agg and ffmpeg)")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="the check, after --")
    parsed = parser.parse_args(args)
    command: list[str] = parsed.command[1:] if parsed.command[:1] == ["--"] else parsed.command
    if not command:
        parser.error("give the check to run after --")

    code = record(command, parsed.cast)
    if parsed.video:
        try:
            render_video(parsed.cast, parsed.video)
        except (OSError, subprocess.CalledProcessError) as error:
            sys.stderr.write(f"pit-crew: no video, so this replay stays unverified: {error}\n")
    return code


def _chunks(master: int) -> Iterator[bytes]:
    while True:
        try:
            data = os.read(master, 65536)
        except OSError:  # Linux raises EIO once the check closes the terminal
            return
        if not data:  # macOS returns end of file instead
            return
        yield data


if __name__ == "__main__":
    sys.exit(main())
