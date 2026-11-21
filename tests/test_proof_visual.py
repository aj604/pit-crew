import functools
import io
import json
import subprocess
import sys
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


def test_record_reports_death_by_signal_like_a_shell(tmp_path: Path) -> None:
    script = "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"
    assert record.record([sys.executable, "-c", script], tmp_path / "r.cast", io.BytesIO()) == 128 + 15


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
