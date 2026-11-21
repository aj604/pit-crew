import io
import json
import sys
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from pit_crew.contracts import Evidence, Verdict
from pit_crew.lanes.proof import record
from pit_crew.lanes.proof.card import pr_body, render_card
from pit_crew.lanes.proof.verify import (
    MODEL,
    FailureMatch,
    Replay,
    compare_failures,
    parse_match,
    tally,
    verify_replays,
)
from pit_crew.rater import rate

SAME = FailureMatch(True, "expected 3, got 2", "expected 3, got 2", "Same assertion in the same test.")
OTHER = FailureMatch(False, "expected 3, got 2", "ENOTFOUND registry.npmjs.org", "Candidate failed to install.")


def replay(sha="b2b2b2b", check="CI/Unit tests", base=True, cand=True, match=None) -> Replay:
    return Replay(sha, check, base, cand, f"{sha}-base.mp4", f"{sha}-cand.mp4", match)


# Recording


def test_record_keeps_the_checks_exit_code_and_output(tmp_path):
    cast = tmp_path / "replay.cast"
    echo = io.BytesIO()
    script = "print('FAIL test_sum: expected 3, got 2'); raise SystemExit(3)"
    assert record.record([sys.executable, "-c", script], str(cast), echo) == 3

    header, *events = [json.loads(line) for line in cast.read_text().splitlines()]
    assert header["version"] == 2 and header["width"] == record.WIDTH
    assert "expected 3, got 2" in "".join(text for _, kind, text in events if kind == "o")
    assert b"expected 3, got 2" in echo.getvalue()  # still streams to the job log


def test_record_cli_returns_the_checks_exit_code(tmp_path, capfdbinary):
    code = record.main(["--cast", str(tmp_path / "r.cast"), "--", sys.executable, "-c", "raise SystemExit(0)"])
    assert code == 0


def test_a_missing_video_tool_keeps_the_result_and_says_why(tmp_path, monkeypatch, capsys):
    def no_agg(*_args, **_kwargs):
        raise FileNotFoundError("agg")

    monkeypatch.setattr(record.subprocess, "run", no_agg)
    monkeypatch.setattr(record, "record", lambda *_: 1)
    assert record.main(["--cast", "r.cast", "--video", "r.mp4", "--", "false"]) == 1
    assert "unverified" in capsys.readouterr().err


def test_video_commands_hold_the_last_frame_for_gemini():
    agg, ffmpeg = record.video_commands("r.cast", "out/r.mp4")
    assert agg[0] == "agg" and agg[-2:] == ["r.cast", "out/r.gif"]
    assert agg[agg.index("--last-frame-duration") + 1] == str(record.LAST_FRAME_SECONDS)
    assert ffmpeg[0] == "ffmpeg" and ffmpeg[-1] == "out/r.mp4" and "yuv420p" in ffmpeg


# Gemini 4 Neon


class FakeModels:
    def __init__(self, answer: str):
        self.answer, self.calls = answer, []

    def generate_content(self, **request):
        self.calls.append(request)
        return SimpleNamespace(text=self.answer)


def test_neon_gets_both_recordings_as_video_and_a_json_schema():
    models = FakeModels(json.dumps(SAME.__dict__))
    match = compare_failures(b"base", b"cand", "CI/Unit tests", client=SimpleNamespace(models=models))

    assert match == SAME
    request = models.calls[0]
    assert request["model"] == MODEL == "gemini-4-neon"
    videos = [p.inline_data for p in request["contents"] if p.inline_data]
    assert [(v.data, v.mime_type) for v in videos] == [(b"base", "video/mp4"), (b"cand", "video/mp4")]
    assert "CI/Unit tests" in request["contents"][0].text
    assert request["config"].response_mime_type == "application/json"


def test_recordings_too_large_to_send_inline_are_refused():
    with pytest.raises(ValueError, match="too large"):
        compare_failures(b"x" * (20 * 1024 * 1024), b"x", "CI/test", client=object())


@pytest.mark.parametrize("answer", ["not json", '{"same": "yes"}', '{"same": true}', ""])
def test_an_answer_off_the_schema_is_refused(answer):
    with pytest.raises(ValueError):
        parse_match(answer)


def test_only_matching_failures_count_and_neon_errors_fail_closed():
    def compare(base: bytes, cand: bytes, check: str) -> FailureMatch:
        if base == b"aaaa-base.mp4":
            return SAME
        if base == b"bbbb-base.mp4":
            return OTHER
        raise TimeoutError("Gemini did not answer")

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
    assert "Gemini did not answer" in replays[2].note
    assert tally(replays) == (4, 1)


# Card and PR body


@pytest.fixture
def proof() -> tuple[Evidence, Verdict, list[Replay]]:
    replays = [replay("b2b2b2b", match=SAME), replay("d4d4d4d", "CI/lint <fast>", match=SAME)]
    total, caught = tally(replays)
    evidence = Evidence("c1", (250.0, 252.0, 248.0, 251.0, 249.0), (108.0, 110.0, 106.0, 109.0, 107.0), total, caught)
    return evidence, rate(evidence), replays


def test_card_is_valid_svg_showing_the_raters_numbers(proof):
    evidence, verdict, replays = proof
    svg = render_card("pc/cache-deps", evidence, verdict, replays)
    texts = [t.text for t in ElementTree.fromstring(svg).iter("{http://www.w3.org/2000/svg}text")]

    assert "KEEP" in texts and "CI time  4m10s  ->  1m48s" in texts
    assert f"{verdict.speedup:.2f}x" in texts
    assert "CI/lint <fast>" in texts  # escaped in the markup, readable in the image
    assert texts.count("caught") == 2
    assert f"{MODEL} watched 2 pairs of failure recordings: 2 matched" in texts


def test_card_for_a_rejected_candidate_says_so(proof):
    evidence, _, replays = proof
    replays = [replays[0], replay("d4d4d4d", cand=False)]
    total, caught = tally(replays)
    evidence = Evidence("c1", evidence.baseline_seconds, evidence.candidate_seconds, total, caught)
    svg = render_card("pc/x", evidence, rate(evidence), replays)
    texts = [t.text for t in ElementTree.fromstring(svg).iter("{http://www.w3.org/2000/svg}text")]
    assert "REJECT" in texts and "missed" in texts and "caught 1/2 bugs" in texts


def test_pr_body_links_the_card_and_every_recording(proof):
    evidence, verdict, replays = proof
    body = pr_body("pc/cache-deps", evidence, verdict, replays, "https://example.com/card.svg")
    assert "![Proof card for pc/cache-deps](https://example.com/card.svg)" in body
    assert "[baseline](b2b2b2b-base.mp4) · [candidate](b2b2b2b-cand.mp4)" in body
    assert "| **caught** |" in body and "expected 3, got 2" in body
    assert "never a miss into a catch" in body
