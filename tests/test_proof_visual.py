import functools
import io
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pytest

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
from pit_crew.managed_agent import UnsupportedPartError, ask_multimodal
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


def test_video_commands_hold_the_last_frame_for_gemini() -> None:
    agg, ffmpeg = record.video_commands("r.cast", "out/r.mp4")
    assert agg[0] == "agg"
    assert agg[-2:] == ["r.cast", "out/r.gif"]
    assert agg[agg.index("--last-frame-duration") + 1] == str(record.LAST_FRAME_SECONDS)
    assert ffmpeg[0] == "ffmpeg"
    assert ffmpeg[-1] == "out/r.mp4"
    assert "yuv420p" in ffmpeg


# Gemini 4 Neon


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


def test_neon_gets_both_recordings_as_video_and_a_json_schema() -> None:
    client = FakeClient(FakeModels(json.dumps(asdict(SAME))))
    ask = functools.partial(ask_multimodal, client=client)

    assert compare_failures(b"base", b"cand", "CI/Unit tests", ask) == SAME
    request = client.models.calls[0]
    assert request["model"] == MODEL == "gemini-4-neon"
    contents = request["contents"]
    assert isinstance(contents, list)
    videos = [(p.inline_data.data, p.inline_data.mime_type) for p in contents if p.inline_data]
    assert videos == [(b"base", "video/mp4"), (b"cand", "video/mp4")]
    assert "CI/Unit tests" in contents[0].text
    config = request["config"]
    assert getattr(config, "response_mime_type", None) == "application/json"
    assert getattr(config, "response_json_schema", None) == SCHEMA


def test_ask_multimodal_returns_empty_text_when_the_model_says_nothing() -> None:
    assert ask_multimodal("gemini-4-neon", [("text", "hi")], SCHEMA, client=FakeClient(FakeModels(None))) == ""


@pytest.mark.parametrize("part", [("text", b"bytes"), ("video/mp4", "text")])
def test_ask_multimodal_refuses_a_mismatched_part(part: tuple[str, str | bytes]) -> None:
    with pytest.raises(UnsupportedPartError):
        ask_multimodal("gemini-4-neon", [part], SCHEMA, client=FakeClient(FakeModels("{}")))


def test_recordings_too_large_to_send_inline_are_refused() -> None:
    def never(_model: str, _parts: Sequence[Part], _schema: Mapping[str, object]) -> str:
        raise AssertionError

    with pytest.raises(RecordingsTooLargeError, match="inline limit"):
        compare_failures(b"x" * MAX_INLINE_BYTES, b"x", "CI/test", never)


@pytest.mark.parametrize("answer", ["not json", "[]", '{"same": "yes"}', '{"same": true}', "", None])
def test_an_answer_off_the_schema_is_refused(answer: str | None) -> None:
    with pytest.raises(UnreadableAnswerError):
        parse_match(answer)


def test_only_matching_failures_count_and_neon_errors_fail_closed() -> None:
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
            replay("cccc"),  # Neon unreachable
            replay("dddd", cand=False),  # candidate passed: missed, Neon not asked
            replay("eeee", base=False),  # baseline misses it too: not a bug
        ],
        load=str.encode,
        compare=compare,
    )
    assert [r.status for r in replays] == ["caught", "different failure", "unverified", "missed", "not a bug"]
    assert replays[2].note.startswith("not verified")
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
