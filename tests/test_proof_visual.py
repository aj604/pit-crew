import functools
import io
import json
import os
import pty
import select
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import override

import pytest
from google import genai

from pit_crew.contracts import Evidence, Verdict
from pit_crew.lanes.proof import record
from pit_crew.lanes.proof.card import pr_body, render_card
from pit_crew.lanes.proof.verify import (
    MAX_INLINE_BYTES,
    MODEL,
    SCHEMA,
    FailureMatch,
    Part,
    RecordingsTooLargeError,
    Replay,
    UnreadableAnswerError,
    compare_failures,
    parse_match,
    tally,
    verify_replays,
)
from pit_crew.managed_agent import MODELS, UnsupportedPartError, ask_multimodal
from pit_crew.rater import rate

SAME = FailureMatch(
    same=True,
    baseline_failure="expected 3, got 2",
    candidate_failure="expected 3, got 2",
    reason="Same assertion in the same test.",
)
OTHER = FailureMatch(
    same=False,
    baseline_failure="expected 3, got 2",
    candidate_failure="ENOTFOUND registry.npmjs.org",
    reason="Candidate failed to install.",
)
SVG_TEXT = "{http://www.w3.org/2000/svg}text"


def replay(
    sha: str = "b2b2b2b",
    check: str = "CI/Unit tests",
    *,
    base: bool = True,
    cand: bool = True,
    match: FailureMatch | None = None,
) -> Replay:
    return Replay(sha, check, base, cand, f"{sha}-base.mp4", f"{sha}-cand.mp4", match)


def svg_texts(svg: str) -> list[str]:
    root = ET.fromstring(svg)  # noqa: S314  parses SVG this test just rendered
    return [t.text or "" for t in root.iter(SVG_TEXT)]


# Recording


def test_record_keeps_the_checks_exit_code_and_output(tmp_path: Path) -> None:
    cast = tmp_path / "replay.cast"
    echo = io.BytesIO()
    script = "print('FAIL test_sum: expected 3, got 2'); raise SystemExit(3)"
    assert record.record([sys.executable, "-c", script], cast, echo) == 3

    header, *events = [json.loads(line) for line in cast.read_text().splitlines()]
    assert header["version"] == 2
    assert header["width"] == record.WIDTH
    assert "expected 3, got 2" in "".join(text for _, kind, text in events if kind == "o")
    assert b"expected 3, got 2" in echo.getvalue()  # still streams to the job log


def test_record_scrubs_chunk_split_and_late_add_mask_secrets_from_cast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cast = tmp_path / "secret.cast"
    echo = io.BytesIO()
    monkeypatch.setenv("CUSTOM_API_TOKEN", "env-tok-secret-999")
    monkeypatch.setenv("ALL_SECRETS_JSON", json.dumps({"nested": "json-secret-888"}))
    monkeypatch.setenv(record.SECRET_ENV_VARS_KEY, "ALL_SECRETS_JSON")
    script = (
        "import sys, time\n"
        "sys.stdout.write('before=late-secret-777 split=env-tok-'); sys.stdout.flush()\n"
        "time.sleep(0.05)\n"
        "sys.stdout.write('secret-999 json=json-secret-888\\n'); sys.stdout.flush()\n"
        "sys.stdout.write('::add-mask::late-sec'); sys.stdout.flush()\n"
        "time.sleep(0.05)\n"
        "sys.stdout.write('ret-777\\n'); sys.stdout.flush()\n"
        "raise SystemExit(1)\n"
    )
    assert record.record([sys.executable, "-c", script], cast, echo) == 1
    _, *events = [json.loads(line) for line in cast.read_text(encoding="utf-8").splitlines()]
    recorded = "".join(text for _, kind, text in events if kind == "o")
    for secret in ("env-tok-secret-999", "json-secret-888", "late-secret-777", "ret-777"):
        assert secret not in recorded
    assert "before=***" in recorded
    assert "split=***" in recorded
    assert "json=***" in recorded
    assert "::add-mask::***" in recorded
    # Raw output still echoed to stdout for GitHub Actions runner processing.
    assert b"::add-mask::late-secret-777" in echo.getvalue()


def test_record_persists_masks_file_across_steps_with_private_permissions(tmp_path: Path) -> None:
    masks_file = tmp_path / "masks" / "job.masks"
    step1_cast = tmp_path / "step1.cast"
    step2_cast = tmp_path / "step2.cast"
    step1 = (
        "import sys, time\n"
        "sys.stdout.write('::add-mask::cross-step-'); sys.stdout.flush()\n"
        "time.sleep(0.05)\n"
        "sys.stdout.write('secret-42\\n'); sys.stdout.flush()\n"
    )
    step2 = (
        "import sys, time\n"
        "sys.stdout.write('caf\\xe9 leaked=cross-step-'); sys.stdout.flush()\n"
        "time.sleep(0.05)\n"
        "sys.stdout.write('secret-42\\n'); sys.stdout.flush()\n"
        "raise SystemExit(2)\n"
    )
    assert record.record([sys.executable, "-c", step1], step1_cast, io.BytesIO(), masks_file=masks_file) == 0
    assert masks_file.is_file()
    assert (masks_file.stat().st_mode & 0o777) == 0o600
    assert (masks_file.parent.stat().st_mode & 0o777) == 0o700
    # Masks file stores only salted HMAC-SHA256 digests, never plaintext secrets.
    masks_raw = masks_file.read_text(encoding="utf-8")
    assert "cross-step-secret-42" not in masks_raw
    assert '"salt"' in masks_raw

    assert (
        record.main(
            [
                "--cast",
                str(step2_cast),
                "--masks-file",
                str(masks_file),
                "--",
                sys.executable,
                "-c",
                step2,
            ]
        )
        == 2
    )
    _, *events = [json.loads(line) for line in step2_cast.read_text(encoding="utf-8").splitlines()]
    recorded = "".join(text for _, kind, text in events if kind == "o")
    assert "cross-step-secret-42" not in recorded
    assert "caf\xe9 leaked=***" in recorded


def test_record_preserves_short_trivial_large_and_many_explicit_masks_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    masks_file = tmp_path / "masks" / "job.masks"
    large_secret = "PEM-" + ("Z" * 5000) + "-END"

    # 1. Short (<4 chars), trivial ("true"), >4 KiB, and >256 explicit masks all survive into Step 2.
    monkeypatch.setenv("LARGE_EXPLICIT", large_secret)
    monkeypatch.setenv(record.SECRET_ENV_VARS_KEY, "LARGE_EXPLICIT")
    step1_script = (
        "print('::add-mask::q9')\n"
        "print('::add-mask::true')\n"
        "for i in range(270):\n"
        "    print(f'::add-mask::many-secret-{i:03d}')\n"
    )
    assert (
        record.record([sys.executable, "-c", step1_script], tmp_path / "s1.cast", io.BytesIO(), masks_file=masks_file)
        == 0
    )
    raw_masks = masks_file.read_text(encoding="utf-8")
    for leaked in ("q9", "many-secret-269", large_secret):
        assert leaked not in raw_masks

    monkeypatch.delenv("LARGE_EXPLICIT", raising=False)
    monkeypatch.delenv(record.SECRET_ENV_VARS_KEY, raising=False)
    step2_cast = tmp_path / "s2.cast"
    step2_script = f"print('short=q9 flag=true tail=many-secret-269 big={large_secret}')\nraise SystemExit(3)\n"
    assert record.record([sys.executable, "-c", step2_script], step2_cast, io.BytesIO(), masks_file=masks_file) == 3
    _, *events = [json.loads(line) for line in step2_cast.read_text(encoding="utf-8").splitlines()]
    recorded = "".join(text for _, kind, text in events if kind == "o")
    assert "q9" not in recorded
    assert "true" not in recorded
    assert "many-secret-269" not in recorded
    assert large_secret not in recorded
    assert "short=*** flag=*** tail=*** big=***" in recorded

    # 2. Verbose output exceeding exact hash scan budget fails closed: deletes .cast and skips video rendering.
    rendered: list[str] = []
    monkeypatch.setattr(record, "render_video", lambda c, v: rendered.append(f"{c}->{v}"))
    monkeypatch.setattr(record, "_MAX_HASH_WINDOW_CHECKS", 50)
    verbose_cast = tmp_path / "verbose.cast"
    verbose_video = tmp_path / "verbose.mp4"
    code = record.main(
        [
            "--cast",
            str(verbose_cast),
            "--video",
            str(verbose_video),
            "--masks-file",
            str(masks_file),
            "--",
            sys.executable,
            "-c",
            "print('x' * 200 + 'many-secret-269'); raise SystemExit(4)",
        ]
    )
    assert code == 4
    assert not verbose_cast.exists()
    assert rendered == []
    assert "redaction incomplete" in capsys.readouterr().err

    # 3. Corrupted masks file also fails closed without producing .cast or .mp4.
    monkeypatch.setattr(record, "_MAX_HASH_WINDOW_CHECKS", 5_000_000)
    masks_file.write_text("not-valid-json\n", encoding="utf-8")
    corrupt_cast = tmp_path / "corrupt.cast"
    corrupt_code = record.main(
        [
            "--cast",
            str(corrupt_cast),
            "--video",
            str(tmp_path / "corrupt.mp4"),
            "--masks-file",
            str(masks_file),
            "--",
            sys.executable,
            "-c",
            "print('secret'); raise SystemExit(5)",
        ]
    )
    assert corrupt_code == 5
    assert not corrupt_cast.exists()
    assert rendered == []


def test_record_reports_death_by_signal_like_a_shell(tmp_path: Path) -> None:
    script = "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"
    assert record.record([sys.executable, "-c", script], tmp_path / "r.cast", io.BytesIO()) == 128 + 15


def test_record_stops_when_the_check_exits_even_if_its_background_process_does_not(tmp_path: Path) -> None:
    # The sleep keeps the terminal open; CI moves on when the check exits, so the recording must too.
    pid = tmp_path / "pid"
    cast = tmp_path / "r.cast"
    started = time.monotonic()
    try:
        code = record.record(["sh", "-c", f"sleep 30 & echo $! > {pid}; echo done; exit 4"], cast, io.BytesIO())
    finally:
        if pid.exists():
            os.kill(int(pid.read_text()), signal.SIGKILL)
    assert code == 4
    assert time.monotonic() - started < 10
    assert "done" in cast.read_text()


def test_record_stops_at_the_drain_deadline_while_output_keeps_coming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # As when a background process writes faster than the recorder reads: output is always waiting.
    real_openpty, real_read, real_select = pty.openpty, os.read, select.select
    terminals: list[int] = []
    reads = 0

    def openpty() -> tuple[int, int]:
        master, slave = real_openpty()
        terminals.append(master)
        return master, slave

    def read(fd: int, size: int) -> bytes:
        nonlocal reads
        if fd not in terminals:
            return real_read(fd, size)
        reads += 1
        if reads > 200_000:
            raise AssertionError("still recording long after the drain deadline")  # noqa: EM101, TRY003
        return b"spam\n"

    def ready(read_fds: list[int], *rest: object) -> tuple[list[int], list[int], list[int]]:
        return (read_fds, [], []) if read_fds == terminals else real_select(read_fds, *rest)  # type: ignore[arg-type]

    monkeypatch.setattr(pty, "openpty", openpty)
    monkeypatch.setattr(os, "read", read)
    monkeypatch.setattr(select, "select", ready)
    monkeypatch.setattr(record, "_DRAIN_SECONDS", 0.05)
    assert record.record(["true"], tmp_path / "r.cast", io.BytesIO()) == 0
    assert reads


def test_a_check_that_reads_stdin_gets_the_jobs_own(tmp_path: Path) -> None:
    # Given the terminal as stdin, the check would wait forever for input; here it reads end of file.
    script = "import sys; print(f'read {len(sys.stdin.read())} bytes')"
    wrapper = [
        sys.executable,
        "-I",
        record.__file__,
        "--cast",
        str(tmp_path / "r.cast"),
        "--",
        sys.executable,
        "-c",
        script,
    ]
    done = subprocess.run(  # noqa: S603  runs this repo's recorder on a fixed command
        wrapper, stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False
    )
    assert done.returncode == 0
    assert b"read 0 bytes" in done.stdout


def test_record_keeps_a_character_cut_off_at_the_end(tmp_path: Path) -> None:
    cast = tmp_path / "r.cast"
    script = "import sys; sys.stdout.buffer.write(b'cut \\xe2\\x82')"  # first two bytes of the euro sign
    assert record.record([sys.executable, "-c", script], cast, io.BytesIO()) == 0
    _, *events = [json.loads(line) for line in cast.read_text().splitlines()]
    assert "".join(text for _, _, text in events).endswith("cut \ufffd")


def open_fds() -> int:
    return sum(1 for _ in Path("/dev/fd").iterdir())


def test_record_closes_the_terminal_when_the_check_cannot_start(tmp_path: Path) -> None:
    before = open_fds()
    with pytest.raises(FileNotFoundError):
        record.record([str(tmp_path / "no-such-check")], tmp_path / "r.cast", io.BytesIO())
    assert open_fds() == before


def test_record_fails_before_starting_the_check_if_it_cannot_write(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    before = open_fds()
    with pytest.raises(FileNotFoundError):
        record.record(["touch", str(marker)], tmp_path / "missing" / "r.cast", io.BytesIO())
    assert not marker.exists()
    assert open_fds() == before


def test_record_stops_the_check_if_the_recording_breaks(tmp_path: Path) -> None:
    class BrokenLog(io.BytesIO):
        @override
        def write(self, _data: object) -> int:
            raise OSError(28, "No space left on device")

    before = open_fds()
    with pytest.raises(OSError, match="No space"):
        record.record(
            [sys.executable, "-c", "print('x'); import time; time.sleep(30)"], tmp_path / "r.cast", BrokenLog()
        )
    assert open_fds() == before


def test_record_cli_returns_the_checks_exit_code(tmp_path: Path, capfdbinary: pytest.CaptureFixture[bytes]) -> None:
    assert record.main(["--cast", str(tmp_path / "r.cast"), "--", sys.executable, "-c", "print('ok')"]) == 0
    assert b"ok" in capfdbinary.readouterr().out


def test_record_cli_needs_a_command(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        record.main(["--cast", str(tmp_path / "r.cast"), "--"])


def test_record_cli_renders_the_video_after_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[list[str]] = []
    monkeypatch.setattr(record, "record", lambda *_: 1)
    monkeypatch.setattr(subprocess, "run", lambda command, **_: ran.append(command))
    assert record.main(["--cast", "r.cast", "--video", "r.mp4", "--", "false"]) == 1
    assert [command[0] for command in ran] == ["agg", "ffmpeg"]


def test_record_cli_video_on_failure_skips_passing_steps_and_renders_failing_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", lambda command, **_: ran.append(command))

    monkeypatch.setattr(record, "record", lambda *_: 0)
    assert record.main(["--cast", "r.cast", "--video", "r.mp4", "--video-on-failure", "--", "true"]) == 0
    assert ran == []

    monkeypatch.setattr(record, "record", lambda *_: 2)
    assert record.main(["--cast", "r.cast", "--video", "r.mp4", "--video-on-failure", "--", "false"]) == 2
    assert [command[0] for command in ran] == ["agg", "ffmpeg"]


def test_a_missing_video_tool_keeps_the_result_and_says_why(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_agg(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError(2, "No such file", "agg")

    monkeypatch.setattr(subprocess, "run", no_agg)
    monkeypatch.setattr(record, "record", lambda *_: 1)
    assert record.main(["--cast", "r.cast", "--video", "r.mp4", "--", "false"]) == 1
    assert "unverified" in capsys.readouterr().err


def test_render_video_leaves_no_gif_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[Path] = []

    def run(command: list[str], **_: object) -> None:
        made.append(Path(command[-1]))
        made[-1].write_bytes(b"frame")

    monkeypatch.setattr(subprocess, "run", run)
    record.render_video("r.cast", str(tmp_path / "r.gif"))  # even when the video is itself called .gif
    gif, video = made
    assert gif != video
    assert not gif.exists()
    assert video.read_bytes() == b"frame"


def test_video_commands_hold_the_last_frame_for_gemini() -> None:
    agg, ffmpeg = record.video_commands("r.cast", "out/r.mp4", "tmp/r.gif")
    assert agg[0] == "agg"
    assert agg[-2:] == ["r.cast", "tmp/r.gif"]
    assert agg[agg.index("--last-frame-duration") + 1] == str(record.LAST_FRAME_SECONDS)
    assert ffmpeg[0] == "ffmpeg"
    assert ffmpeg[-1] == "out/r.mp4"
    assert "yuv420p" in ffmpeg


# Gemini 4 Carbon


@dataclass
class FakeResponse:
    text: str | None


@dataclass
class FakeModels:
    answer: str | None
    calls: list[dict[str, object]] = field(default_factory=list)

    def generate_content(self, **kwargs: object) -> FakeResponse:
        self.calls.append(kwargs)
        return FakeResponse(self.answer)


@dataclass
class FakeClient:
    models: FakeModels


def test_carbon_gets_both_recordings_as_video_and_a_json_schema() -> None:
    client = FakeClient(FakeModels(json.dumps(asdict(SAME))))
    ask = functools.partial(ask_multimodal, client=client)

    assert compare_failures(b"base", b"cand", "CI/Unit tests", ask) == SAME
    request = client.models.calls[0]
    assert request["model"] == MODEL == MODELS["proof"] == "gemini-4-carbon"
    contents = request["contents"]
    assert isinstance(contents, list)
    videos = [(p.inline_data.data, p.inline_data.mime_type) for p in contents if p.inline_data]
    assert videos == [(b"base", "video/mp4"), (b"cand", "video/mp4")]
    assert "CI/Unit tests" in contents[0].text
    config = request["config"]
    assert getattr(config, "response_mime_type", None) == "application/json"
    assert getattr(config, "response_json_schema", None) == SCHEMA


def test_ask_multimodal_returns_empty_text_when_the_model_says_nothing() -> None:
    assert ask_multimodal(MODEL, [("text", "hi")], SCHEMA, client=FakeClient(FakeModels(None))) == ""


def test_ask_multimodal_uses_the_same_api_version_as_the_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[dict[str, object]] = []

    def client(**kwargs: object) -> FakeClient:
        made.append(kwargs)
        return FakeClient(FakeModels("{}"))

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(genai, "Client", client)
    assert ask_multimodal(MODEL, [("text", "hi")], SCHEMA) == "{}"
    assert made == [{"api_key": "test-key", "http_options": {"api_version": "v1beta"}}]


@pytest.mark.parametrize("part", [("text", b"bytes"), ("video/mp4", "text")])
def test_ask_multimodal_refuses_a_mismatched_part(part: tuple[str, str | bytes]) -> None:
    with pytest.raises(UnsupportedPartError):
        ask_multimodal(MODEL, [part], SCHEMA, client=FakeClient(FakeModels("{}")))


def test_recordings_too_large_to_send_inline_are_refused() -> None:
    def never(_model: str, _parts: Sequence[Part], _schema: Mapping[str, object]) -> str:
        raise AssertionError

    with pytest.raises(RecordingsTooLargeError, match="inline limit"):
        compare_failures(b"x" * MAX_INLINE_BYTES, b"x", "CI/test", never)
    # Media goes base64-encoded, so recordings at three quarters of the limit already fill it.
    quarter = b"x" * (MAX_INLINE_BYTES * 3 // 8)
    with pytest.raises(RecordingsTooLargeError, match="inline limit"):
        compare_failures(quarter, quarter, "CI/test", never)


@pytest.mark.parametrize("answer", ["not json", "[]", '{"same": "yes"}', '{"same": true}', "", None])
def test_an_answer_off_the_schema_is_refused(answer: str | None) -> None:
    with pytest.raises(UnreadableAnswerError):
        parse_match(answer)


def test_only_matching_failures_count_and_carbon_errors_fail_closed() -> None:
    def compare(base: bytes, _cand: bytes, _check: str) -> FailureMatch:
        if base == b"aaaa-base.mp4":
            return SAME
        if base == b"bbbb-base.mp4":
            return OTHER
        raise TimeoutError

    replays = verify_replays(
        [
            replay("aaaa"),  # same failure on both sides
            replay("bbbb"),  # candidate failed for an unrelated reason
            replay("cccc"),  # Carbon unreachable
            replay("dddd", cand=False),  # candidate passed: missed, Carbon not asked
            replay("eeee", base=False),  # baseline misses it too: not a bug
        ],
        load=str.encode,
        compare=compare,
    )
    assert [r.status for r in replays] == ["caught", "different failure", "unverified", "missed", "not a bug"]
    assert replays[2].note == "not verified: TimeoutError"  # named even though it has no message
    assert tally(replays) == (4, 1)
    outcomes = [r.as_outcome() for r in replays]
    assert outcomes[0]["same_failure"] is True
    assert outcomes[2]["same_failure"] is None
    assert outcomes[1]["seen"] == "Candidate failed to install."


# Card and PR body


@pytest.fixture
def proof() -> tuple[Evidence, Verdict, list[Replay]]:
    replays = [replay("b2b2b2b", match=SAME), replay("d4d4d4d", "CI/lint <fast>", match=SAME)]
    total, caught = tally(replays)
    evidence = Evidence(
        "c1",
        (250.0, 252.0, 248.0, 251.0, 249.0),
        (58.0, 60.0, 56.0, 59.0, 57.0),
        total,
        caught,
        bug_outcomes=tuple(r.as_outcome() for r in replays),
    )
    return evidence, rate(evidence), replays


def test_card_is_valid_svg_showing_the_raters_numbers(proof: tuple[Evidence, Verdict, list[Replay]]) -> None:
    evidence, verdict, replays = proof
    texts = svg_texts(render_card("pc/cache-deps", evidence, verdict, replays))

    assert "KEEP" in texts
    assert "CI time  4m10s  ->  58s" in texts
    assert f"{verdict.speedup:.2f}x" in texts
    assert any("SLA met" in t for t in texts)
    assert "CI/lint <fast>" in texts  # escaped in the markup, readable in the image
    assert texts.count("caught") == 2
    assert f"{MODEL} watched 2 pairs of failure recordings: 2 matched" in texts


def test_card_for_a_rejected_candidate_says_so(proof: tuple[Evidence, Verdict, list[Replay]]) -> None:
    evidence, _, replays = proof
    replays = [replays[0], replay("d4d4d4d", cand=False)]
    total, caught = tally(replays)
    evidence = Evidence("c1", evidence.baseline_seconds, evidence.candidate_seconds, total, caught)
    texts = svg_texts(render_card("pc/x", evidence, rate(evidence), replays))
    assert "REJECT" in texts
    assert "missed" in texts
    assert "caught 1/2 bugs" in texts
    assert "not scored" in texts


def test_card_with_no_replays_says_so(proof: tuple[Evidence, Verdict, list[Replay]]) -> None:
    evidence, _, _ = proof
    empty = Evidence("c1", evidence.baseline_seconds, evidence.candidate_seconds, 0, 0)
    assert "no bugs replayed" in svg_texts(render_card("pc/x", empty, rate(empty), []))


def test_card_and_pr_body_survive_a_candidate_with_no_runs(proof: tuple[Evidence, Verdict, list[Replay]]) -> None:
    evidence, _, replays = proof
    empty = Evidence("c1", evidence.baseline_seconds, (), evidence.bugs_total, evidence.bugs_caught)
    verdict = rate(empty)
    assert not verdict.keep
    assert "CI time  4m10s  ->  no runs" in svg_texts(render_card("pc/x", empty, verdict, replays))
    assert "Median CI time 4m10s -> no runs" in pr_body("pc/x", empty, verdict, replays, "card.svg")


def test_a_long_branch_name_is_clipped_clear_of_the_verdict(proof: tuple[Evidence, Verdict, list[Replay]]) -> None:
    evidence, verdict, replays = proof
    title = svg_texts(render_card("pc/" + "x" * 100, evidence, verdict, replays))[0]
    assert len(title) <= 56
    assert title.endswith("...")


def test_pr_body_quotes_ci_output_as_plain_text(proof: tuple[Evidence, Verdict, list[Replay]]) -> None:
    # Carbon quotes what the recordings show, and any commit can make CI print anything.
    evidence, verdict, _ = proof
    loud = replace(OTHER, reason="printed @octo-org/admins, [click](https://evil.example) <img src=x>")
    body = pr_body("pc/x", evidence, verdict, [replay("aaaa", match=loud)], "card.svg")
    assert "@octo-org" not in body
    assert "@\u2060octo-org/admins" in body
    assert "[click](" not in body
    assert "\\[click\\](https://evil.example)" in body
    assert "\\<img src=x\\>" in body  # escaped, so GitHub shows it as text


def test_pr_body_says_when_a_replay_has_no_recording(proof: tuple[Evidence, Verdict, list[Replay]]) -> None:
    evidence, verdict, _ = proof
    no_video = replace(replay("aaaa"), baseline_video="", candidate_video="")
    half = replace(replay("bbbb"), candidate_video="")
    body = pr_body("pc/x", evidence, verdict, [no_video, half], "card.svg")
    assert "]()" not in body
    assert "| none |" in body
    assert "| [baseline](bbbb-base.mp4) |" in body
    assert f"What {MODEL} saw" in body


def test_pr_body_links_the_card_and_every_recording(proof: tuple[Evidence, Verdict, list[Replay]]) -> None:
    evidence, verdict, replays = proof
    replays = [*replays, replay("cccc", match=OTHER)]
    body = pr_body("pc/cache-deps", evidence, verdict, replays, "https://example.com/card.svg")
    assert "![Proof card for pc/cache-deps](https://example.com/card.svg)" in body
    assert "[baseline](b2b2b2b-base.mp4) / [candidate](b2b2b2b-cand.mp4)" in body
    assert "| **caught** |" in body
    assert "| **different failure** |" in body
    assert "Candidate failed to install." in body
    assert "never a miss into a catch" in body
