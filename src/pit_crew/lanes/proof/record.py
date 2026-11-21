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
from typing import BinaryIO

WIDTH, HEIGHT = 120, 36
# Gemini samples video at one frame a second; hold the last screen, where test runners
# print their failure summary, long enough to be read several times.
LAST_FRAME_SECONDS = 5
IDLE_SECONDS = 2


def record(argv: list[str], cast_path: str, echo: BinaryIO | None = None) -> int:
    """Run argv in a pseudo-terminal, write an asciicast v2 recording, return its exit code."""
    echo = echo if echo is not None else sys.stdout.buffer
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", HEIGHT, WIDTH, 0, 0))
    env = {**os.environ, "TERM": "xterm-256color", "COLUMNS": str(WIDTH), "LINES": str(HEIGHT)}
    start = time.monotonic()
    proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
    os.close(slave)

    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    with open(cast_path, "w") as cast:
        header = {"version": 2, "width": WIDTH, "height": HEIGHT, "timestamp": int(time.time())}
        cast.write(json.dumps({**header, "command": shlex.join(argv), "env": {"TERM": env["TERM"]}}) + "\n")
        while True:
            try:
                data = os.read(master, 65536)
            except OSError:  # Linux raises EIO once the check closes the terminal
                break
            if not data:  # macOS returns end of file instead
                break
            echo.write(data)
            echo.flush()
            if text := decoder.decode(data):
                cast.write(json.dumps([round(time.monotonic() - start, 6), "o", text]) + "\n")
    os.close(master)
    code = proc.wait()
    return 128 - code if code < 0 else code  # killed by a signal: the shell's 128+N


def video_commands(cast_path: str, video_path: str) -> list[list[str]]:
    """The commands that turn a recording into an MP4 Gemini can watch."""
    gif = os.path.splitext(video_path)[0] + ".gif"
    return [
        ["agg", "--idle-time-limit", str(IDLE_SECONDS), "--last-frame-duration", str(LAST_FRAME_SECONDS),
         cast_path, gif],
        # H.264 needs even dimensions and yuv420p to play everywhere, including in Gemini.
        ["ffmpeg", "-y", "-loglevel", "error", "-i", gif, "-movflags", "+faststart", "-pix_fmt", "yuv420p",
         "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", video_path],
    ]


def render_video(cast_path: str, video_path: str) -> None:
    for command in video_commands(cast_path, video_path):
        subprocess.run(command, check=True)


def main(args: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a CI check and record it as a terminal video.")
    parser.add_argument("--cast", required=True, help="asciicast v2 recording to write")
    parser.add_argument("--video", help="MP4 to render from the recording (needs agg and ffmpeg)")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="the check, after --")
    parsed = parser.parse_args(args)
    command = parsed.command[1:] if parsed.command[:1] == ["--"] else parsed.command
    if not command:
        parser.error("give the check to run after --")

    code = record(command, parsed.cast)
    if parsed.video:
        try:
            render_video(parsed.cast, parsed.video)
        except (OSError, subprocess.CalledProcessError) as error:
            print(f"pit-crew: no video, so this replay stays unverified: {error}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
