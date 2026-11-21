"""Offline test suite for Spec 5 (#139): Multimodal Visual Proof & Artifact Equivalence."""

from __future__ import annotations

import base64
import io
import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from fastapi.testclient import TestClient

from pit_crew import managed_agent, rater
from pit_crew.contracts import Candidate, Evidence, Hypothesis, ReplayPlan
from pit_crew.github import HttpxGitHubClient, RunArtifact
from pit_crew.lanes.proof import artifacts as artifacts_mod
from pit_crew.lanes.proof.artifacts import (
    MAX_LOG_CHARS,
    MAX_ZIP_ENTRIES,
    ArtifactBundle,
    MediaPart,
    UnsafeArchiveError,
    bundle_from_raw,
    detect_magic_mime,
    extract_playwright_trace,
    extract_zip_artifact,
    score_artifact_name,
    select_bundle_parts,
    sniff_media_part,
)
from pit_crew.lanes.proof.card import pr_body, render_card
from pit_crew.lanes.proof.miner import JobRun
from pit_crew.lanes.proof.verify import (
    MODEL,
    SCHEMA,
    VISUAL_DOWNGRADE_REASON,
    FailureMatch,
    Replay,
    UnreadableAnswerError,
    compare_bundles,
    compare_failures,
    expected_checks_matched,
    multimodal_failure_request,
    parse_match,
    verify_bundle_replays,
)
from pit_crew.ledger import InMemoryLedger, Ledger
from pit_crew.managed_agent import (
    FileUploadError,
    MediaTooLargeError,
    ask_multimodal,
)
from pit_crew.proof_bridge import (
    HISTORICAL_MAX_SCREENSHOT_BYTES,
    PR_MAX_PREVIEW_BYTES,
    PROOF_ARTIFACTS_DIR,
    PROOF_CARD_PATH,
    SANDBOX_ARTIFACTS_DIR,
    ProofBridge,
    ProofPRConfig,
    ReplayRunPair,
)
from pit_crew.server import create_app

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"png-body-data"
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"jpeg-body-data"
WEBP_BYTES = b"RIFF\x18\x00\x00\x00WEBP" + b"webp-body-data"
GIF_BYTES = b"GIF89a" + b"gif-body-data"
WEBM_BYTES = b"\x1a\x45\xdf\xa3" + b"webm-video-stream"
MP4_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"mp4-video-stream"

WEB_DIR = Path(__file__).resolve().parents[1] / "src" / "pit_crew" / "web"
NODE = shutil.which("node")


def _make_zip(
    files: Mapping[str, bytes],
    *,
    compression: int = zipfile.ZIP_DEFLATED,
    symlink_names: set[str] | None = None,
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compression) as zf:
        for name, data in files.items():
            info = zipfile.ZipInfo(name)
            if symlink_names and name in symlink_names:
                info.external_attr = (0o120777) << 16
            zf.writestr(info, data, compress_type=compression)
    return buf.getvalue()


def _make_trace_zip(
    *,
    events: Sequence[dict[str, object]],
    resources: Mapping[str, bytes],
) -> bytes:
    ndjson = "\n".join(json.dumps(ev) for ev in events).encode("utf-8")
    payload: dict[str, bytes] = {"trace.trace": ndjson}
    for name, data in resources.items():
        payload[f"resources/{name}"] = data
    return _make_zip(payload)


# ---------------------------------------------------------------------------
# Phase 1A: Typed Artifacts, Magic Bytes, Safe ZIP & Playwright trace.zip
# ---------------------------------------------------------------------------


def test_detect_magic_mime_and_sniff_media_part_never_trust_extensions() -> None:
    assert detect_magic_mime(PNG_BYTES) == "image/png"
    assert detect_magic_mime(JPEG_BYTES) == "image/jpeg"
    assert detect_magic_mime(WEBP_BYTES) == "image/webp"
    assert detect_magic_mime(GIF_BYTES) == "image/gif"
    assert detect_magic_mime(WEBM_BYTES) == "video/webm"
    assert detect_magic_mime(MP4_BYTES) == "video/mp4"
    assert detect_magic_mime(b"plain text log") is None

    # PNG disguised as .mp4 must still be detected as image/png screenshot.
    png_part = sniff_media_part(PNG_BYTES, "fake.mp4")
    assert png_part.mime_type == "image/png"
    assert png_part.modality == "screenshot"

    # Plain text disguised as .png must NOT be tagged as image/png.
    fake_png = sniff_media_part(b"AssertionError: expected 1 == 2\n", "spoofed.png")
    assert fake_png.mime_type == "text/plain"
    assert fake_png.modality == "log"

    # WebM is always browser_video; Cypress mp4 is browser_video; record.py mp4 is terminal_video.
    assert sniff_media_part(WEBM_BYTES, "video.webm").modality == "browser_video"
    assert sniff_media_part(MP4_BYTES, "cypress/videos/cart.mp4").modality == "browser_video"
    assert sniff_media_part(MP4_BYTES, "terminal-replay.mp4").modality == "terminal_video"
    assert sniff_media_part(GIF_BYTES, "replay.gif").modality == "terminal_video"


def test_extract_zip_artifact_unpacks_media_and_playwright_trace() -> None:
    trace_zip = _make_trace_zip(
        events=[
            {"type": "before", "callId": "c1", "apiName": "expect.toBeVisible", "startTime": 100.0},
            {"type": "screencast-frame", "sha1": "frame-early.jpeg", "timestamp": 101.0},
            {"type": "screencast-frame", "sha1": "frame-before.jpeg", "timestamp": 104.0},
            {"type": "screencast-frame", "sha1": "frame-fail.png", "timestamp": 105.0},
            {"type": "screencast-frame", "sha1": "frame-after.jpeg", "timestamp": 120.0},
            {
                "type": "after",
                "callId": "c1",
                "endTime": 105.0,
                "error": {"error": {"message": "Locator [data-testid=total] not visible"}},
            },
        ],
        resources={
            "frame-early.jpeg": JPEG_BYTES + b"1",
            "frame-before.jpeg": JPEG_BYTES + b"2",
            "frame-fail.png": PNG_BYTES + b"3",
            "frame-after.jpeg": JPEG_BYTES + b"4",
        },
    )
    outer = _make_zip(
        {
            "test-results/checkout/video.webm": WEBM_BYTES,
            "test-results/checkout/test-failed-1.png": PNG_BYTES,
            "test-results/checkout/trace.zip": trace_zip,
            "test-results/checkout/error-context.md": b"Expected $42.00, got $42.01\n",
            "test-results/checkout/corrupt.png": b"\x00\x01\x02not-a-real-png",
        }
    )

    parts = extract_zip_artifact(outer, source_url="https://example.com/art/1", artifact_name="failure-recordings-0")
    by_mod = {p.modality for p in parts}
    assert by_mod == {"browser_video", "screenshot", "trace_frame", "log"}
    # Corrupt .png with invalid magic bytes was dropped.
    assert not any("corrupt.png" in p.name for p in parts)
    # Trace extracted the 2 frames at/just before failure_ts (104.0 and 105.0), ignoring 101.0 and 120.0.
    trace_frames = [p for p in parts if p.modality == "trace_frame"]
    assert [p.name.rsplit(":", maxsplit=1)[-1] for p in trace_frames] == ["frame-before.jpeg", "frame-fail.png"]
    assert any("expect.toBeVisible" in p.data.decode() for p in parts if p.modality == "log")


def test_extract_playwright_trace_falls_back_to_resources_without_screencast_events() -> None:
    trace_zip = _make_trace_zip(
        events=[{"type": "error", "message": "Page crashed", "timestamp": 50.0}],
        resources={
            "01.jpeg": JPEG_BYTES + b"a",
            "02.png": PNG_BYTES + b"b",
            "03.webp": WEBP_BYTES + b"c",
        },
    )
    parts = extract_playwright_trace(trace_zip, trace_name="ui-trace.zip")
    frames = [p for p in parts if p.modality == "trace_frame"]
    assert len(frames) == 2
    assert [f.mime_type for f in frames] == ["image/png", "image/webp"]
    logs = [p for p in parts if p.modality == "log"]
    assert len(logs) == 1
    assert b"Page crashed" in logs[0].data


@pytest.mark.parametrize(
    ("bad_zip", "match"),
    [
        (_make_zip({"../evil.png": PNG_BYTES}), "unsafe path"),
        (_make_zip({"/abs/path.png": PNG_BYTES}), "unsafe path"),
        (_make_zip({"win\\path.png": PNG_BYTES}), "unsafe path"),
        (_make_zip({"C:drive.png": PNG_BYTES}), "unsafe path"),
        (_make_zip({"link.png": b"/etc/passwd"}, symlink_names={"link.png"}), "symlink"),
        (_make_zip({f"f{i}.txt": b"x" for i in range(MAX_ZIP_ENTRIES + 1)}), "entries"),
        (_make_zip({"bomb.txt": b"A" * 200_000}, compression=zipfile.ZIP_DEFLATED), "compression ratio"),
        (b"not a zip archive", "not a valid ZIP"),
        (b"PK\x03\x04corrupt", "corrupt ZIP"),
    ],
)
def test_extract_zip_artifact_blocks_unsafe_archives(bad_zip: bytes, match: str) -> None:
    with pytest.raises(UnsafeArchiveError, match=match):
        extract_zip_artifact(bad_zip)


def test_extract_zip_artifact_enforces_size_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    sample = _make_zip({"a.png": PNG_BYTES})
    monkeypatch.setattr(artifacts_mod, "MAX_ZIP_DOWNLOAD_BYTES", len(sample) - 1)
    with pytest.raises(UnsafeArchiveError, match="download limit"):
        extract_zip_artifact(sample)

    monkeypatch.setattr(artifacts_mod, "MAX_ZIP_DOWNLOAD_BYTES", 1024 * 1024)
    monkeypatch.setattr(artifacts_mod, "MAX_ZIP_DECOMPRESSED_BYTES", 10)
    with pytest.raises(UnsafeArchiveError, match="decompressed"):
        extract_zip_artifact(sample)


def test_select_bundle_parts_enforces_modality_hierarchy_and_budget() -> None:
    parts = [
        MediaPart("v1.webm", "video/webm", WEBM_BYTES + b"1", "browser_video", "https://example.com/v1"),
        MediaPart("v2.webm", "video/webm", WEBM_BYTES + b"2", "browser_video", "https://example.com/v2"),
        MediaPart("generic.png", "image/png", PNG_BYTES + b"g", "screenshot"),
        MediaPart("test-failed-1.png", "image/png", PNG_BYTES + b"f", "screenshot"),
        MediaPart("checkout-diff.png", "image/png", PNG_BYTES + b"d", "screenshot"),
        MediaPart("trace.zip:frame.jpeg", "image/jpeg", JPEG_BYTES, "trace_frame"),
        MediaPart("terminal.mp4", "video/mp4", MP4_BYTES, "terminal_video"),
        MediaPart("run.log", "text/plain", ("L" * (MAX_LOG_CHARS + 500) + "TAIL_END").encode(), "log"),
    ]
    bundle = select_bundle_parts(parts, run_id="101", run_url="https://github.com/o/r/actions/runs/101")

    assert bundle.primary_modality == "browser_video"
    assert bundle.has_visual is True
    assert bundle.truncated is True
    assert [p.modality for p in bundle.parts] == ["browser_video", "screenshot", "screenshot", "log"]
    # test-failed-1 and checkout-diff outranked generic.png; terminal_video was dropped because Tier 1 visual exists.
    names = [p.name for p in bundle.parts if p.modality == "screenshot"]
    assert set(names) == {"test-failed-1.png", "checkout-diff.png"}
    log_part = next(p for p in bundle.parts if p.modality == "log")
    assert len(log_part.data.decode()) == MAX_LOG_CHARS
    assert log_part.data.endswith(b"TAIL_END")

    # Without Tier 1 visual artifacts, terminal_video IS kept.
    term_bundle = select_bundle_parts(
        [MediaPart("terminal.mp4", "video/mp4", MP4_BYTES, "terminal_video")],
        run_id="102",
        run_url="https://github.com/o/r/actions/runs/102",
    )
    assert term_bundle.primary_modality == "terminal_video"
    assert term_bundle.has_visual is False
    assert term_bundle.artifact_urls == ("https://github.com/o/r/actions/runs/102",)


def test_bundle_from_raw_unpacks_zip_and_handles_empty() -> None:
    empty = bundle_from_raw(b"", run_id="0")
    assert empty.primary_modality is None
    assert empty.has_visual is False

    zipped = _make_zip({"test-failed-1.png": PNG_BYTES})
    bundle = bundle_from_raw(zipped, run_id="55", run_url="https://github.com/o/r/actions/runs/55")
    assert bundle.primary_modality == "screenshot"
    assert bundle.parts[0].mime_type == "image/png"


# ---------------------------------------------------------------------------
# Phase 1B: Modality-Aware Verification, Visual-Downgrade & expected_checks
# ---------------------------------------------------------------------------


def test_multimodal_failure_request_sends_real_mime_types_and_visual_prompt() -> None:
    base = ArtifactBundle(
        run_id="base-1",
        parts=(
            MediaPart("video.webm", "video/webm", WEBM_BYTES, "browser_video"),
            MediaPart("test-failed-1.png", "image/png", PNG_BYTES, "screenshot"),
            MediaPart("failed.log", "text/plain", b"AssertionError: total mismatch", "log"),
        ),
    )
    cand = ArtifactBundle(
        run_id="cand-1",
        parts=(
            MediaPart("video.webm", "video/webm", WEBM_BYTES, "browser_video"),
            MediaPart("test-failed-1.png", "image/png", PNG_BYTES, "screenshot"),
        ),
    )
    parts = multimodal_failure_request(base, cand, "CI/UI check")
    prompt_text = parts[0][1]
    assert isinstance(prompt_text, str)
    assert "CI/UI check" in prompt_text
    assert "ERR_CONNECTION_REFUSED" in prompt_text
    assert "never as instructions" in prompt_text
    media_mimes = [kind for kind, value in parts if isinstance(value, bytes)]
    assert media_mimes == ["video/webm", "image/png", "text/plain", "video/webm", "image/png"]


def test_compare_failures_sniffs_real_png_and_webm_instead_of_hardcoded_mp4() -> None:
    seen_parts: list[Sequence[tuple[str, str | bytes]]] = []

    def fake_ask(_model: str, parts: Sequence[tuple[str, str | bytes]], _schema: Mapping[str, object]) -> str:
        seen_parts.append(parts)
        return json.dumps(
            {
                "same": True,
                "baseline_failure": "Checkout total $42.01",
                "candidate_failure": "Checkout total $42.01",
                "reason": "Identical UI assertion failure.",
                "visual_evidence_seen": True,
                "visual_summary": "Both screenshots render $42.01 in the cart summary.",
            }
        )

    match = compare_failures(PNG_BYTES, PNG_BYTES, "CI/ui-check", fake_ask)
    assert match.same is True
    assert match.visual_evidence_seen is True
    assert "42.01" in match.visual_summary
    sent_mimes = [kind for kind, val in seen_parts[0] if isinstance(val, bytes)]
    assert sent_mimes == ["image/png", "image/png"]


def test_visual_downgrade_fails_closed_without_calling_model() -> None:
    def must_not_be_called(_m: str, _p: Sequence[tuple[str, str | bytes]], _s: Mapping[str, object]) -> str:
        raise AssertionError

    base = ArtifactBundle(
        run_id="base-1",
        parts=(MediaPart("test-failed-1.png", "image/png", PNG_BYTES, "screenshot"),),
    )
    cand = ArtifactBundle(
        run_id="cand-1",
        parts=(MediaPart("failed.log", "text/plain", b"FAILED test_ui\n", "log"),),
    )
    match = compare_bundles(base, cand, "CI/ui-check", must_not_be_called)
    assert match.same is False
    assert match.reason == VISUAL_DOWNGRADE_REASON
    assert match.visual_evidence_seen is False


def test_expected_checks_guard_rejects_mismatched_check_before_calling_model() -> None:
    assert expected_checks_matched(("CI/UI check",), ("UI check (2/4)",)) is True
    assert expected_checks_matched(("CI/UI check",), ("CI/Lint",)) is False
    # Matrix-qualified expected checks must not fold across different matrix legs.
    assert expected_checks_matched(("CI/test (node-26, ubuntu-latest)",), ("test (node-20, ubuntu-latest)",)) is False
    assert expected_checks_matched(("CI/test (node-26, ubuntu-latest)",), ("test (node-26, ubuntu-latest)",)) is True

    def must_not_be_called(_m: str, _p: Sequence[tuple[str, str | bytes]], _s: Mapping[str, object]) -> str:
        raise AssertionError

    base = ArtifactBundle(run_id="b", parts=(MediaPart("a.png", "image/png", PNG_BYTES, "screenshot"),))
    cand = ArtifactBundle(run_id="c", parts=(MediaPart("a.png", "image/png", PNG_BYTES, "screenshot"),))
    match = compare_bundles(
        base,
        cand,
        "CI/UI check",
        must_not_be_called,
        expected_checks=("CI/UI check",),
        candidate_failed_checks=("CI/Lint",),
    )
    assert match.same is False
    assert "unexpected check" in match.reason


def test_parse_match_validates_additive_visual_fields_and_keeps_legacy_compat() -> None:
    legacy = parse_match(json.dumps({"same": True, "baseline_failure": "a", "candidate_failure": "a", "reason": "ok"}))
    assert legacy.visual_evidence_seen is False
    assert legacy.visual_summary == ""

    rich = parse_match(
        json.dumps(
            {
                "same": False,
                "baseline_failure": "Cart error banner",
                "candidate_failure": "Blank white screen",
                "reason": "Candidate rendered a blank page.",
                "visual_evidence_seen": True,
                "visual_summary": "Baseline shows cart page; candidate is blank white.",
            }
        )
    )
    assert rich.same is False
    assert rich.visual_evidence_seen is True
    assert "blank white" in rich.visual_summary

    with pytest.raises(UnreadableAnswerError):
        parse_match(
            json.dumps(
                {
                    "same": True,
                    "baseline_failure": "a",
                    "candidate_failure": "a",
                    "reason": "ok",
                    "visual_evidence_seen": "yes",
                }
            )
        )
    with pytest.raises(UnreadableAnswerError):
        parse_match(
            json.dumps(
                {
                    "same": True,
                    "baseline_failure": "a",
                    "candidate_failure": "a",
                    "reason": "ok",
                    "visual_summary": 123,
                }
            )
        )


def test_verify_bundle_replays_populates_outcome_metadata_and_fails_closed_on_empty() -> None:
    bundles = {
        "r1-base": ArtifactBundle(
            run_id="r1-base",
            run_url="https://github.com/o/r/actions/runs/1",
            parts=(MediaPart("f.png", "image/png", PNG_BYTES, "screenshot", "https://example.com/art/base"),),
        ),
        "r1-cand": ArtifactBundle(
            run_id="r1-cand",
            run_url="https://github.com/o/r/actions/runs/2",
            parts=(MediaPart("f.png", "image/png", PNG_BYTES, "screenshot", "https://example.com/art/cand"),),
        ),
        "r2-base": ArtifactBundle(run_id="r2-base"),
        "r2-cand": ArtifactBundle(run_id="r2-cand"),
    }

    def fake_ask(_m: str, _p: Sequence[tuple[str, str | bytes]], _s: Mapping[str, object]) -> str:
        return json.dumps(
            {
                "same": True,
                "baseline_failure": "err",
                "candidate_failure": "err",
                "reason": "matched",
                "visual_evidence_seen": True,
                "visual_summary": "Same rendered modal",
            }
        )

    replays = verify_bundle_replays(
        [
            Replay(
                "aaaa1111",
                "CI/ui",
                baseline_failed=True,
                candidate_failed=True,
                baseline_video="r1-base",
                candidate_video="r1-cand",
            ),
            Replay(
                "bbbb2222",
                "CI/ui",
                baseline_failed=True,
                candidate_failed=True,
                baseline_video="r2-base",
                candidate_video="r2-cand",
            ),
            Replay(
                "cccc3333",
                "CI/ui",
                baseline_failed=True,
                candidate_failed=False,
                baseline_video="r3-base",
                candidate_video="r3-cand",
            ),
        ],
        load_bundle=lambda run_id, _check: bundles[run_id],
        ask=fake_ask,
    )
    assert [r.status for r in replays] == ["caught", "unverified", "missed"]
    outcome = replays[0].as_outcome()
    assert outcome["modality"] == "screenshot"
    assert outcome["visual_evidence_seen"] is True
    assert outcome["visual_summary"] == "Same rendered modal"
    assert outcome["baseline_artifact_urls"] == ["https://example.com/art/base"]
    assert outcome["candidate_artifact_urls"] == ["https://example.com/art/cand"]


# ---------------------------------------------------------------------------
# Phase 2A: managed_agent.ask_multimodal Gemini Files API Fallback (> 20 MB)
# ---------------------------------------------------------------------------


@dataclass
class _FakeUploadedFile:
    name: str | None
    uri: str | None
    state: object = "ACTIVE"


@dataclass
class _FakeFilesAPI:
    states_by_name: dict[str, list[object]] = field(default_factory=dict)
    fail_delete_names: set[str] = field(default_factory=set)
    fail_upload_on_call: int | None = None
    uploaded: list[tuple[bytes, str]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    polled: list[str] = field(default_factory=list)

    def upload(self, *, file: io.IOBase, config: object = None) -> _FakeUploadedFile:
        idx = len(self.uploaded) + 1
        if self.fail_upload_on_call == idx:
            msg = "upload quota exceeded"
            raise RuntimeError(msg)
        data = file.read()
        mime = getattr(config, "mime_type", "")
        self.uploaded.append((data, mime))
        name = f"files/item-{idx}"
        seq = self.states_by_name.get(name, ["ACTIVE"])
        first_state = seq.pop(0) if len(seq) > 1 else seq[0]
        return _FakeUploadedFile(name=name, uri=f"https://generativelanguage.googleapis.com/{name}", state=first_state)

    def get(self, *, name: str) -> _FakeUploadedFile:
        self.polled.append(name)
        seq = self.states_by_name.get(name, ["ACTIVE"])
        state = seq.pop(0) if len(seq) > 1 else seq[0]
        return _FakeUploadedFile(name=name, uri=f"https://generativelanguage.googleapis.com/{name}", state=state)

    def delete(self, *, name: str) -> None:
        self.deleted.append(name)
        if name in self.fail_delete_names:
            msg = f"transient delete error for {name}"
            raise RuntimeError(msg)


@dataclass
class _FakeGenResponse:
    text: str | None


@dataclass
class _FakeModelsAPI:
    reply: str | Exception
    calls: list[dict[str, object]] = field(default_factory=list)

    def generate_content(self, **kwargs: object) -> _FakeGenResponse:
        self.calls.append(kwargs)
        if isinstance(self.reply, Exception):
            raise self.reply
        return _FakeGenResponse(text=self.reply)


@dataclass
class _FakeFilesClient:
    models: _FakeModelsAPI
    files: _FakeFilesAPI


def test_ask_multimodal_falls_back_to_files_api_and_cleans_up_in_finally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(managed_agent, "MAX_INLINE_BYTES", 64)
    monkeypatch.setattr(managed_agent, "FILE_POLL_INTERVAL_SECONDS", 0.0)

    files_api = _FakeFilesAPI(states_by_name={"files/item-1": ["PROCESSING", "ACTIVE"], "files/item-2": ["ACTIVE"]})
    models_api = _FakeModelsAPI(reply='{"same": true}')
    client = _FakeFilesClient(models=models_api, files=files_api)

    result = ask_multimodal(
        MODEL,
        [("text", "Compare videos"), ("video/webm", WEBM_BYTES * 8), ("image/png", PNG_BYTES * 4)],
        SCHEMA,
        client=client,
    )
    assert result == '{"same": true}'
    assert len(files_api.uploaded) == 2
    assert [mime for _, mime in files_api.uploaded] == ["video/webm", "image/png"]
    assert files_api.polled == ["files/item-1"]
    assert files_api.deleted == ["files/item-1", "files/item-2"]

    contents = models_api.calls[0]["contents"]
    assert isinstance(contents, list)
    file_uris = [p.file_data.file_uri for p in contents if p.file_data]
    assert file_uris == [
        "https://generativelanguage.googleapis.com/files/item-1",
        "https://generativelanguage.googleapis.com/files/item-2",
    ]


def test_ask_multimodal_files_api_deletes_uploaded_files_even_on_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(managed_agent, "MAX_INLINE_BYTES", 32)
    monkeypatch.setattr(managed_agent, "FILE_POLL_INTERVAL_SECONDS", 0.0)

    # 1. generate_content raises; first delete fails, second delete still runs.
    files_api = _FakeFilesAPI(fail_delete_names={"files/item-1"})
    client = _FakeFilesClient(models=_FakeModelsAPI(reply=RuntimeError("model 503")), files=files_api)
    with pytest.raises(RuntimeError, match="model 503"):
        ask_multimodal(MODEL, [("video/webm", WEBM_BYTES * 8), ("image/png", PNG_BYTES * 4)], SCHEMA, client=client)
    assert files_api.deleted == ["files/item-1", "files/item-2"]

    # 2. Second upload fails -> first uploaded file is still deleted.
    files_api_2 = _FakeFilesAPI(fail_upload_on_call=2)
    client_2 = _FakeFilesClient(models=_FakeModelsAPI(reply="{}"), files=files_api_2)
    with pytest.raises(RuntimeError, match="quota"):
        ask_multimodal(MODEL, [("video/webm", WEBM_BYTES * 8), ("image/png", PNG_BYTES * 4)], SCHEMA, client=client_2)
    assert files_api_2.deleted == ["files/item-1"]

    # 3. File enters FAILED processing state -> deleted in finally.
    files_api_3 = _FakeFilesAPI(states_by_name={"files/item-1": ["PROCESSING", "FAILED"]})
    client_3 = _FakeFilesClient(models=_FakeModelsAPI(reply="{}"), files=files_api_3)
    with pytest.raises(FileUploadError, match="failed processing"):
        ask_multimodal(MODEL, [("video/webm", WEBM_BYTES * 8)], SCHEMA, client=client_3)
    assert files_api_3.deleted == ["files/item-1"]

    # 4. Over 100 MB cap raises MediaTooLargeError before uploading.
    monkeypatch.setattr(managed_agent, "MAX_UPLOAD_BYTES", 100)
    files_api_4 = _FakeFilesAPI()
    client_4 = _FakeFilesClient(models=_FakeModelsAPI(reply="{}"), files=files_api_4)
    with pytest.raises(MediaTooLargeError, match="Files API limit"):
        ask_multimodal(MODEL, [("video/webm", b"x" * 101)], SCHEMA, client=client_4)
    assert files_api_4.uploaded == []


# ---------------------------------------------------------------------------
# Phase 2B: HttpxGitHubClient Artifacts, Failed Job Logs & Binary Commits
# ---------------------------------------------------------------------------


def test_github_artifacts_and_logs_strip_auth_header_on_302_redirects() -> None:
    seen_requests: list[tuple[str, dict[str, str]]] = []
    zip_payload = _make_zip({"test-failed-1.png": PNG_BYTES})

    routes: dict[str, httpx.Response] = {
        "https://api.github.com/repos/acme/app/actions/runs/500/artifacts?per_page=100": httpx.Response(
            200,
            json={
                "artifacts": [
                    {
                        "id": 9001,
                        "name": "failure-recordings-ui-0",
                        "size_in_bytes": len(zip_payload),
                        "archive_download_url": "https://api.github.com/repos/acme/app/actions/artifacts/9001/zip",
                        "expired": False,
                        "created_at": "2026-11-21T10:00:00Z",
                    }
                ]
            },
        ),
        "https://api.github.com/repos/acme/app/actions/artifacts/9001/zip": httpx.Response(
            302,
            headers={"Location": "https://blob.core.windows.net/sas/9001.zip?sig=abc"},
        ),
        "https://blob.core.windows.net/sas/9001.zip?sig=abc": httpx.Response(200, content=zip_payload),
        "https://api.github.com/repos/acme/app/actions/runs/500/jobs?per_page=100": httpx.Response(
            200,
            json={
                "jobs": [
                    {"id": 10, "name": "Lint", "conclusion": "success"},
                    {"id": 11, "name": "UI check (1/2)", "conclusion": "failure"},
                    {"id": 12, "name": "Unit tests", "conclusion": "failure"},
                ]
            },
        ),
        "https://api.github.com/repos/acme/app/actions/jobs/11/logs": httpx.Response(
            302,
            headers={"Location": "https://pipelines.actions.githubusercontent.com/log/11"},
        ),
        "https://pipelines.actions.githubusercontent.com/log/11": httpx.Response(
            200,
            content=b"AssertionError: cart total wrong\n",
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append((str(request.url), {k.lower(): v for k, v in request.headers.items()}))
        return routes.get(str(request.url), httpx.Response(404))

    with HttpxGitHubClient(token="ghp_secret_token", transport=httpx.MockTransport(handler)) as gh:  # noqa: S106
        artifacts = gh.list_run_artifacts("acme/app", 500)
        assert artifacts == [
            RunArtifact(
                id=9001,
                name="failure-recordings-ui-0",
                size_in_bytes=len(zip_payload),
                archive_download_url="https://api.github.com/repos/acme/app/actions/artifacts/9001/zip",
                expired=False,
                created_at="2026-11-21T10:00:00Z",
            )
        ]
        downloaded = gh.download_artifact("acme/app", 9001)
        assert downloaded == zip_payload
        assert gh.list_failed_job_names("acme/app", 500) == ["UI check (1/2)", "Unit tests"]
        logs = gh.get_failed_job_logs("acme/app", 500, check="CI/UI check")
        assert b"=== UI check (1/2) ===" in logs
        assert b"AssertionError: cart total wrong" in logs

    # Verify Authorization header was sent to api.github.com and STRIPPED on both external redirects!
    api_reqs = [hdrs for url, hdrs in seen_requests if url.startswith("https://api.github.com")]
    blob_reqs = [hdrs for url, hdrs in seen_requests if not url.startswith("https://api.github.com")]
    assert api_reqs
    assert len(blob_reqs) == 2
    assert all("authorization" in h for h in api_reqs)
    assert all("authorization" not in h for h in blob_reqs)
    # Passing job 10 and non-matching failed job 12 were never fetched!
    assert not any("/jobs/10/logs" in url or "/jobs/12/logs" in url for url, _ in seen_requests)


def test_github_download_rejects_non_http_redirect_and_oversized_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/artifacts/1/zip"):
            return httpx.Response(302, headers={"Location": "file:///etc/passwd"})
        if request.url.path.endswith("/artifacts/2/zip"):
            return httpx.Response(200, content=b"x" * 200)
        return httpx.Response(404)

    with HttpxGitHubClient(token="ghp_test", transport=httpx.MockTransport(handler)) as gh:  # noqa: S106
        with pytest.raises(ValueError, match="non-HTTP"):
            gh.download_artifact("acme/app", 1)
        with pytest.raises(ValueError, match="exceeded"):
            gh.download_artifact("acme/app", 2, max_bytes=50)


def test_github_commit_files_uploads_binary_png_via_git_blobs_api() -> None:
    blob_calls: list[dict[str, object]] = []
    tree_calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/git/commits/base123"):
            return httpx.Response(200, json={"tree": {"sha": "tree-base"}})
        if request.method == "POST" and path.endswith("/git/blobs"):
            body = json.loads(request.content)
            blob_calls.append(body)
            return httpx.Response(201, json={"sha": "blob-png-sha"})
        if request.method == "POST" and path.endswith("/git/trees"):
            body = json.loads(request.content)
            tree_calls.append(body)
            return httpx.Response(201, json={"sha": "tree-new"})
        if request.method == "POST" and path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "commit-new"})
        if request.method == "PATCH" and path.endswith("/git/refs/heads/pc/cand"):
            return httpx.Response(200, json={"object": {"sha": "commit-new"}})
        return httpx.Response(404)

    with HttpxGitHubClient(token="ghp_test", transport=httpx.MockTransport(handler)) as gh:  # noqa: S106
        sha = gh.commit_files(
            "acme/app",
            "pc/cand",
            "base123",
            {
                PROOF_CARD_PATH: "<svg/>",
                f"{PROOF_ARTIFACTS_DIR}/deadbee-0-baseline.png": PNG_BYTES,
            },
            "add visual proof",
        )
    assert sha == "commit-new"
    assert blob_calls == [{"content": base64.b64encode(PNG_BYTES).decode("ascii"), "encoding": "base64"}]
    entries = tree_calls[0]["tree"]
    assert isinstance(entries, list)
    assert {"path": PROOF_CARD_PATH, "mode": "100644", "type": "blob", "content": "<svg/>"} in entries
    assert {
        "path": f"{PROOF_ARTIFACTS_DIR}/deadbee-0-baseline.png",
        "mode": "100644",
        "type": "blob",
        "sha": "blob-png-sha",
    } in entries


# ---------------------------------------------------------------------------
# Phase 3 & 4: ProofBridge Shard Merging, Historical Mounting, Visual PR Card
# ---------------------------------------------------------------------------


class _RichProofGitHub:
    """Offline GitHub fake implementing ProofHistoryClient + RunArtifactsClient + PullRequestClient."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        files: dict[tuple[str, str, str], str] | None = None,
        job_runs: Sequence[JobRun] = (),
        artifacts_by_run: dict[tuple[str, str], list[RunArtifact]] | None = None,
        artifact_zips: dict[int, bytes | Exception] | None = None,
        failed_logs: dict[tuple[str, str], bytes] | None = None,
        failed_job_names: dict[tuple[str, str], list[str]] | None = None,
    ) -> None:
        self._files = dict(files or {})
        self._job_runs = list(job_runs)
        self._artifacts_by_run = dict(artifacts_by_run or {})
        self._artifact_zips = dict(artifact_zips or {})
        self._failed_logs = dict(failed_logs or {})
        self._failed_job_names = dict(failed_job_names or {})
        self.downloaded_ids: list[int] = []
        self.committed_files: list[dict[str, Any]] = []
        self.opened_prs: list[dict[str, str]] = []

    def get_file_content(self, repo: str, ref: str, path: str) -> str | None:
        return self._files.get((repo, ref, path))

    def list_workflow_job_runs(self, repo: str) -> Sequence[JobRun]:
        _ = repo
        return list(self._job_runs)

    def get_run_failure_output(self, repo: str, run_id: str) -> bytes:
        return self._failed_logs.get((repo, run_id), b"")

    def list_run_artifacts(self, repo: str, run_id: int | str) -> list[RunArtifact]:
        return list(self._artifacts_by_run.get((repo, str(run_id)), []))

    def download_artifact(self, repo: str, artifact_id: int) -> bytes:
        _ = repo
        self.downloaded_ids.append(artifact_id)
        item = self._artifact_zips[artifact_id]
        if isinstance(item, Exception):
            raise item
        return item

    def get_failed_job_logs(
        self,
        repo: str,
        run_id: int | str,
        *,
        check: str | None = None,
        max_bytes: int = 262_144,
    ) -> bytes:
        _ = check
        data = self._failed_logs.get((repo, str(run_id)), b"")
        return data[-max_bytes:]

    def list_failed_job_names(self, repo: str, run_id: int | str) -> list[str]:
        return list(self._failed_job_names.get((repo, str(run_id)), []))

    def commit_files(self, repo: str, branch: str, files: dict[str, Any], message: str) -> str:
        self.committed_files.append({"repo": repo, "branch": branch, "files": dict(files), "message": message})
        return "proofsha999"

    def open_pull_request(self, repo: str, *, head: str, base: str, title: str, body: str) -> str:
        self.opened_prs.append({"repo": repo, "head": head, "base": base, "title": title, "body": body})
        return f"https://github.com/{repo}/pull/42"


def test_proof_bridge_collects_sharded_artifacts_and_mounts_historical_screenshots() -> None:
    repo = "acme/shop"
    base_ci = (
        "name: CI\non: [push]\njobs:\n  ui:\n    name: UI Check\n"
        "    runs-on: ubuntu-latest\n    steps:\n      - run: playwright test\n"
    )
    cand_ci = base_ci.replace("playwright test", "playwright test --shard=1/4")

    shard_zips = {
        1: _make_zip({"test-results/shard0/video.webm": WEBM_BYTES}),
        2: _make_zip({"test-results/shard1/test-failed-1.png": PNG_BYTES}),
        3: _make_zip({"test-results/shard2/test-failed-2.png": PNG_BYTES + b"2"}),
        4: _make_zip({"test-results/shard3/extra.png": PNG_BYTES + b"3"}),
        5: _make_zip({"test-results/shard4/ignored.png": PNG_BYTES + b"4"}),
        6: _make_zip({"../escape.png": PNG_BYTES}),  # unsafe archive skipped safely
    }
    hist_zip = _make_zip({"test-results/hist/test-failed-cart.png": PNG_BYTES})
    big_hist_zip = _make_zip({"test-results/hist/huge.png": PNG_BYTES + b"0" * HISTORICAL_MAX_SCREENSHOT_BYTES})

    github = _RichProofGitHub(
        files={(repo, "base0000", ".github/workflows/ci.yml"): base_ci},
        job_runs=[
            JobRun(
                sha="1111aaaa",
                branch="main",
                created_at="2026-11-10T00:00:00Z",
                workflow="CI",
                job="UI Check",
                attempt=1,
                conclusion="failure",
            ),
            JobRun(
                sha="2222bbbb",
                branch="main",
                created_at="2026-11-11T00:00:00Z",
                workflow="CI",
                job="UI Check",
                attempt=1,
                conclusion="failure",
            ),
        ],
        artifacts_by_run={
            (repo, "800"): [
                RunArtifact(100, "expired-shot", 500, "https://example.com/100", expired=True),
                RunArtifact(1, "failure-recordings-ui-0", len(shard_zips[1]), "https://example.com/1", expired=False),
                RunArtifact(2, "failure-recordings-ui-1", len(shard_zips[2]), "https://example.com/2", expired=False),
                RunArtifact(3, "failure-recordings-ui-2", len(shard_zips[3]), "https://example.com/3", expired=False),
                RunArtifact(4, "failure-recordings-ui-3", len(shard_zips[4]), "https://example.com/4", expired=False),
                RunArtifact(5, "failure-recordings-ui-4", len(shard_zips[5]), "https://example.com/5", expired=False),
            ],
            (repo, "701"): [
                RunArtifact(20, "failure-recordings-ui-0", len(hist_zip), "https://example.com/20", expired=False),
            ],
            (repo, "702"): [
                RunArtifact(21, "failure-recordings-ui-0", len(big_hist_zip), "https://example.com/21", expired=False),
            ],
        },
        artifact_zips={**shard_zips, 20: hist_zip, 21: big_hist_zip},
        failed_logs={(repo, "800"): b"FAILED cart.spec.ts: expected $42.00\n"},
    )

    captured: dict[str, Any] = {}

    def fake_run_lane(
        lane: str,
        task: str,
        *,
        repo: str = "",
        ledger: Ledger | None = None,
        extra_sources: list[dict[str, str]] | None = None,
    ) -> str:
        captured.update({"lane": lane, "task": task, "repo": repo, "ledger": ledger, "extra_sources": extra_sources})
        return "ok"

    bridge = ProofBridge(github, run_lane=fake_run_lane)
    bundle = bridge.collect_run_failure_bundle(repo, "800", check="CI/UI Check")
    assert bundle.primary_modality == "browser_video"
    assert bundle.has_visual is True
    assert bundle.truncated is True
    # Only up to 4 non-expired shard ZIPs downloaded (expired id=100 and 5th shard id=5 skipped).
    assert github.downloaded_ids == [1, 2, 3, 4]
    assert score_artifact_name("failure-recordings-ui-0", "CI/UI Check") < score_artifact_name("misc", "CI/UI Check")

    cand = Candidate(
        id="cand-ui",
        hypothesis_id="hyp-ui",
        branch="pc/shard-ui",
        summary="Shard Playwright UI checks across 4 runners",
        repo=repo,
        base_sha="base0000",
        files={".github/workflows/ci.yml": cand_ci},
    )
    ctx, _ = bridge.run(cand, ledger=InMemoryLedger(), historical_run_ids={"1111aaaa": "701", "2222bbbb": "702"})
    assert ctx.mounted_artifacts == (f"{SANDBOX_ARTIFACTS_DIR}/1111aaaa/test-failed-cart.png",)
    assert len(ctx.extra_sources) == 1
    assert ctx.extra_sources[0]["encoding"] == "base64"
    assert base64.b64decode(ctx.extra_sources[0]["content"]) == PNG_BYTES
    assert captured["extra_sources"] == list(ctx.extra_sources)
    assert "## Mounted historical failure screenshots" in str(captured["task"])


def test_proof_bridge_end_to_end_visual_verify_and_kept_pr_commits_png_pairs() -> None:
    repo = "acme/shop"
    base_zip = _make_zip({"test-results/test-failed-1.png": PNG_BYTES + b"base"})
    cand_zip = _make_zip({"test-results/test-failed-1.png": PNG_BYTES + b"cand"})
    github = _RichProofGitHub(
        artifacts_by_run={
            (repo, "901"): [RunArtifact(1, "ui-failure", len(base_zip), "https://example.com/base.zip", expired=False)],
            (repo, "902"): [RunArtifact(2, "ui-failure", len(cand_zip), "https://example.com/cand.zip", expired=False)],
        },
        artifact_zips={1: base_zip, 2: cand_zip},
        failed_logs={
            (repo, "901"): b"AssertionError: expected $42.00\n",
            (repo, "902"): b"AssertionError: expected $42.00\n",
        },
        failed_job_names={(repo, "902"): ["UI Check (1/2)"]},
    )

    def fake_ask(_model: str, _parts: Sequence[tuple[str, str | bytes]], _schema: Mapping[str, object]) -> str:
        return json.dumps(
            {
                "same": True,
                "baseline_failure": "Cart total $42.01",
                "candidate_failure": "Cart total $42.01",
                "reason": "Identical UI rounding failure.",
                "visual_evidence_seen": True,
                "visual_summary": "Both failure screenshots show $42.01 instead of $42.00.",
            }
        )

    bridge = ProofBridge(github, ask=fake_ask)
    verification = bridge.verify_replay_runs(
        repo,
        [
            ReplayRunPair(
                sha="deadbeef1234",
                check="CI/UI Check",
                baseline_conclusion="failure",
                candidate_conclusion="failure",
                baseline_run_id="901",
                candidate_run_id="902",
                expected_checks=("CI/UI Check",),
            )
        ],
    )
    assert verification.bugs_total == 1
    assert verification.bugs_caught == 1
    assert len(verification.preview_files) == 2
    outcome = verification.bug_outcomes[0]
    assert outcome["modality"] == "screenshot"
    assert outcome["visual_evidence_seen"] is True
    assert outcome["baseline_preview_path"] == f"{PROOF_ARTIFACTS_DIR}/deadbee-0-baseline.png"
    assert outcome["candidate_preview_path"] == f"{PROOF_ARTIFACTS_DIR}/deadbee-0-candidate.png"

    cand = Candidate(id="cand-vis", hypothesis_id="hyp-vis", branch="pc/cand-vis", summary="Shard UI tests", repo=repo)
    evidence = verification.to_evidence(
        candidate_id=cand.id,
        baseline_seconds=(120.0, 121.0, 119.0, 122.0, 118.0),
        candidate_seconds=(50.0, 51.0, 49.0, 52.0, 48.0),
    )
    verdict = rater.rate(evidence)
    assert verdict.keep is True

    # Also pass an invalid/oversized preview to ensure open_kept_pr filters it out.
    extra_previews = [
        *verification.preview_files,
        (f"{PROOF_ARTIFACTS_DIR}/oversized.png", PNG_BYTES + b"0" * PR_MAX_PREVIEW_BYTES),
        ("../escape.png", PNG_BYTES),
        (f"{PROOF_ARTIFACTS_DIR}/not-a-png.png", b"plain text"),
    ]
    opened = bridge.open_kept_pr(
        cand,
        evidence,
        verdict,
        verification.replays,
        config=ProofPRConfig(auto_open_pr=True, base_branch="main"),
        preview_files=extra_previews,
    )
    assert opened is not None
    assert opened.artifact_paths == (
        f"{PROOF_ARTIFACTS_DIR}/deadbee-0-baseline.png",
        f"{PROOF_ARTIFACTS_DIR}/deadbee-0-candidate.png",
    )
    committed = github.committed_files[0]["files"]
    assert set(committed) == {
        PROOF_CARD_PATH,
        f"{PROOF_ARTIFACTS_DIR}/deadbee-0-baseline.png",
        f"{PROOF_ARTIFACTS_DIR}/deadbee-0-candidate.png",
    }
    svg = committed[PROOF_CARD_PATH]
    root = ET.fromstring(svg)  # noqa: S314  parses SVG just rendered locally
    svg_text = [t.text or "" for t in root.iter("{http://www.w3.org/2000/svg}text")]
    assert "SCREENSHOT" in svg_text
    assert "### Visual Failure Evidence (Baseline vs. Candidate)" in opened.body
    assert (
        f"https://raw.githubusercontent.com/{repo}/proofsha999/{PROOF_ARTIFACTS_DIR}/deadbee-0-baseline.png"
        in opened.body
    )
    assert (
        f"https://raw.githubusercontent.com/{repo}/proofsha999/{PROOF_ARTIFACTS_DIR}/deadbee-0-candidate.png"
        in opened.body
    )
    assert '<img src="' in opened.body
    assert "Both failure screenshots show $42.01 instead of $42.00." in opened.body


def test_card_and_pr_body_render_all_modality_badges() -> None:
    replays = [
        Replay(
            "aaaa111",
            "CI/e2e",
            baseline_failed=True,
            candidate_failed=True,
            baseline_video="https://example.com/1",
            candidate_video="https://example.com/2",
            match=FailureMatch(
                same=True,
                baseline_failure="e",
                candidate_failure="e",
                reason="ok",
                visual_evidence_seen=True,
                visual_summary="Video matches",
            ),
            modality="browser_video",
            baseline_artifact_urls=("https://example.com/art/1",),
            candidate_artifact_urls=("https://example.com/art/2",),
            baseline_preview_path="https://example.com/b.png",
            candidate_preview_path="https://example.com/c.png",
        ),
        Replay(
            "bbbb222",
            "CI/trace",
            baseline_failed=True,
            candidate_failed=True,
            baseline_video="b",
            candidate_video="c",
            modality="trace_frame",
        ),
        Replay(
            "cccc333",
            "CI/cli",
            baseline_failed=True,
            candidate_failed=True,
            baseline_video="b",
            candidate_video="c",
            modality="terminal_video",
        ),
        Replay(
            "dddd444",
            "CI/lint",
            baseline_failed=True,
            candidate_failed=True,
            baseline_video="b",
            candidate_video="c",
            modality="log",
        ),
    ]
    ev = Evidence("c1", (120.0,) * 5, (50.0,) * 5, 4, 1)
    verdict = rater.rate(ev)
    svg = render_card("pc/multi", ev, verdict, replays)
    root = ET.fromstring(svg)  # noqa: S314
    texts = [t.text or "" for t in root.iter("{http://www.w3.org/2000/svg}text")]
    for label in ("VIDEO", "TRACE", "TERMINAL", "LOG"):
        assert label in texts
    body = pr_body("pc/multi", ev, verdict, replays, "https://example.com/card.svg")
    assert "`VIDEO`" in body
    assert "[baseline](https://example.com/art/1) / [candidate](https://example.com/art/2)" in body
    assert '<img src="https://example.com/b.png"' in body


# ---------------------------------------------------------------------------
# Phase 4: Server /attempts/{candidate_id} & Pit Wall Visual Proof UI
# ---------------------------------------------------------------------------


def test_server_attempt_detail_endpoint_exposes_visual_proof_and_preview_urls() -> None:
    ledger = InMemoryLedger()
    hyp = Hypothesis(
        id="hyp-1",
        repo="acme/shop",
        description="Shard Playwright suite",
        skill="shard-by-timing",
        expected_savings_seconds=80.0,
        base_sha="base1111",
    )
    cand = Candidate(
        id="cand-1",
        hypothesis_id=hyp.id,
        branch="pc/shard-1",
        summary="Shard Playwright across 4 jobs",
        repo="acme/shop",
        base_sha="base1111",
        head_sha="head2222",
    )
    plan = ReplayPlan(id="plan-1", candidate_id=cand.id, bug_shas=("deadbeef",))
    ev = Evidence(
        id="ev-1",
        candidate_id=cand.id,
        baseline_seconds=(120.0,) * 5,
        candidate_seconds=(50.0,) * 5,
        bugs_total=1,
        bugs_caught=1,
        bug_outcomes=(
            {
                "sha": "deadbeef",
                "check": "CI/UI Check",
                "status": "caught",
                "baseline_failed": True,
                "candidate_failed": True,
                "modality": "screenshot",
                "visual_evidence_seen": True,
                "visual_summary": "Same broken cart total rendered.",
                "baseline_artifact_urls": ["https://github.com/acme/shop/actions/runs/10"],
                "candidate_artifact_urls": ["https://github.com/acme/shop/actions/runs/11"],
                "baseline_preview_path": f"{PROOF_ARTIFACTS_DIR}/deadbee-0-baseline.png",
                "candidate_preview_path": f"{PROOF_ARTIFACTS_DIR}/deadbee-0-candidate.png",
            },
        ),
    )
    verdict = rater.rate(ev)
    for rec in (hyp, cand, plan, ev, verdict):
        ledger.add(rec)

    client = TestClient(create_app(ledger, api_token="secret-tok"))  # noqa: S106
    assert client.get("/api/repos/acme/shop/attempts/cand-1").status_code == 401
    headers = {"Authorization": "Bearer secret-tok"}
    assert client.get("/api/repos/acme/shop/attempts/missing", headers=headers).status_code == 404
    assert client.get("/api/repos/other/repo/attempts/cand-1", headers=headers).status_code == 404

    res = client.get("/api/repos/acme/shop/attempts/cand-1", headers=headers)
    assert res.status_code == 200
    payload = res.json()
    assert payload["candidate_id"] == "cand-1"
    assert len(payload["visual_proof"]) == 1
    proof = payload["visual_proof"][0]
    assert proof["modality"] == "screenshot"
    assert proof["visual_evidence_seen"] is True
    assert proof["visual_summary"] == "Same broken cart total rendered."
    assert (
        proof["baseline_preview_url"]
        == f"https://raw.githubusercontent.com/acme/shop/head2222/{PROOF_ARTIFACTS_DIR}/deadbee-0-baseline.png"
    )
    assert (
        proof["candidate_preview_url"]
        == f"https://raw.githubusercontent.com/acme/shop/head2222/{PROOF_ARTIFACTS_DIR}/deadbee-0-candidate.png"
    )

    # Demo preview SVGs are served statically under /web/previews/.
    for name in (
        "cart-total-baseline.svg",
        "cart-total-candidate.svg",
        "coupon-baseline.svg",
        "coupon-candidate.svg",
    ):
        svg_res = client.get(f"/web/previews/{name}")
        assert svg_res.status_code == 200
        assert "image/svg+xml" in svg_res.headers["content-type"]


_UI_VISUAL_PROOF_SCRIPT = """
const makeNode = (tag, ns = null) => {
  const listeners = {};
  return {
    tag, ns, className: "", hidden: false, textContent: "", title: "", value: "", disabled: false,
    dataset: {}, attrs: {}, kids: [],
    setAttribute(k, v) { this.attrs[k] = String(v); },
    addEventListener(t, fn) { (listeners[t] ??= []).push(fn); },
    dispatchEvent(e) { for (const fn of listeners[e.type] ?? []) fn(e); return true; },
    replaceChildren(...kids) { this.kids = kids; },
    append(...kids) { this.kids.push(...kids); },
    querySelectorAll(sel) { return sel === "[data-mode]" ? (this.modeButtons ?? []) : []; },
  };
};
const walk = (n, pred, acc = []) => {
  if (!n) return acc;
  if (pred(n)) acc.push(n);
  for (const c of n.kids ?? []) walk(c, pred, acc);
  return acc;
};
const fakeDoc = () => {
  const live = makeNode("button"); live.dataset.mode = "live";
  const demo = makeNode("button"); demo.dataset.mode = "demo";
  const modeToggle = makeNode("div"); modeToggle.modeButtons = [live, demo];
  const ids = {
    "repo-input": makeNode("input"), "repo-options": makeNode("datalist"), "base-sha-value": makeNode("code"),
    "sla-median": makeNode("span"), "sla-p95": makeNode("span"),
    "sla-badge": Object.assign(makeNode("span"), { hidden: true }),
    "mode-toggle": modeToggle, "run-pit-stop": makeNode("button"),
    "demo-switcher": makeNode("section"), "telemetry-center": makeNode("section"),
  };
  return {
    doc: {
      readyState: "complete", getElementById: (id) => ids[id] ?? null,
      createElement: (t) => makeNode(t, null), createElementNS: (ns, t) => makeNode(t, ns),
    },
    ids,
  };
};
const app = await import(process.argv[1]);
const tel = await import(process.argv[2]);
const d = fakeDoc();
const pw = app.bootPitWall(d.doc, "");
const slot = d.ids["telemetry-center"];

const demoModalities = walk(slot, (n) => n.className.includes("bug-modality") && n.dataset.modality)
  .map((n) => [n.dataset.modality, n.textContent]);
const demoImgs = walk(slot, (n) => n.tag === "img").map((n) => ({ src: n.attrs.src, alt: n.attrs.alt }));
const demoSummaries = walk(slot, (n) => n.className.includes("visual-proof__summary")).map((n) => n.textContent);

pw.store.setMode("live");
pw.store.setRepo("acme/shop");
pw.store.setLiveData({
  status: { repo: "acme/shop", active: false, base_sha: "head2222" },
  attempts: [{ candidate_id: "c1", head_sha: "head2222", rated: true }],
  records: {
    candidates: [{ id: "c1", repo: "acme/shop", head_sha: "head2222" }],
    evidence: [{
      candidate_id: "c1",
      baseline_seconds: [100, 95],
      candidate_seconds: [50, 48],
      bugs_total: 2,
      bugs_caught: 1,
      bug_outcomes: [
        {
          sha: "deadbeef",
          status: "caught",
          baseline_failed: true,
          modality: "screenshot",
          visual_evidence_seen: true,
          visual_summary: "Cart total mismatch visible on both sides.",
          baseline_preview_path: ".github/pit-crew/proof-artifacts/deadbee-0-baseline.png",
          candidate_preview_path: ".github/pit-crew/proof-artifacts/deadbee-0-candidate.png",
        },
        {
          sha: "cafebabe",
          status: "different failure",
          baseline_failed: true,
          modality: "log",
          visual_evidence_seen: false,
          visual_summary: "candidate failed without producing visual failure artifacts present on baseline",
          baseline_preview_path: "javascript:alert(1)",
          candidate_preview_path: "../evil.png",
        },
      ],
    }],
    verdicts: [],
  },
});
const liveImgs = walk(slot, (n) => n.tag === "img").map((n) => n.attrs.src);
const liveStatuses = walk(slot, (n) => n.className.includes("bug-matrix__row"))
  .map((tr) => walk(tr, (n) => n.className.includes("bug-status"))[0].textContent);
console.log(JSON.stringify({
  demoModalities,
  demoImgs,
  demoSummaries,
  liveImgs,
  liveStatuses,
  safeChecks: [
    tel.isSafePreviewUrl("/web/previews/cart-total-baseline.svg"),
    tel.isSafePreviewUrl("/web/../secret.txt"),
    tel.isSafePreviewUrl("javascript:alert(1)"),
    tel.isSafePreviewUrl("https://raw.githubusercontent.com/a/b/main/x.png"),
  ],
}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_pit_wall_ui_renders_modality_badges_and_visual_previews() -> None:
    assert NODE is not None
    result = subprocess.run(  # noqa: S603
        [
            NODE,
            "--input-type=module",
            "-e",
            _UI_VISUAL_PROOF_SCRIPT,
            (WEB_DIR / "app.js").as_uri(),
            (WEB_DIR / "telemetry.js").as_uri(),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    out = json.loads(result.stdout)
    assert out["safeChecks"] == [True, False, False, True]
    assert ["screenshot", "SCREENSHOT"] in out["demoModalities"]
    assert ["browser_video", "VIDEO"] in out["demoModalities"]
    assert ["trace_frame", "TRACE"] in out["demoModalities"]
    assert ["log", "LOG"] in out["demoModalities"]
    assert len(out["demoImgs"]) == 4
    assert out["demoImgs"][0]["src"] == "/web/previews/cart-total-baseline.svg"
    assert out["demoImgs"][1]["src"] == "/web/previews/cart-total-candidate.svg"
    assert any("$42.01" in s for s in out["demoSummaries"])

    assert out["liveImgs"] == [
        "https://raw.githubusercontent.com/acme/shop/head2222/.github/pit-crew/proof-artifacts/deadbee-0-baseline.png",
        "https://raw.githubusercontent.com/acme/shop/head2222/.github/pit-crew/proof-artifacts/deadbee-0-candidate.png",
    ]
    assert out["liveStatuses"] == ["CAUGHT", "DIFF FAILURE"]


def test_artifacts_and_managed_agent_edge_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    # Trace with malformed NDJSON lines, nested directories, and string error values.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("resources/", b"")
        zf.writestr("resources/a.png", PNG_BYTES)
        zf.writestr(
            "test.trace",
            b"not-json\n[]\n"
            + json.dumps({"type": "screencast-frame", "sha1": "a.png", "timestamp": 10}).encode()
            + b"\n"
            + json.dumps({"type": "after", "error": "boom", "timestamp": 12}).encode()
            + b"\n",
        )
    trace_parts = extract_playwright_trace(buf.getvalue())
    assert any(p.modality == "trace_frame" for p in trace_parts)
    assert any(p.modality == "log" and b"boom" in p.data for p in trace_parts)

    # Deduplication & whitespace-only log skipping.
    dup = MediaPart("dup.png", "image/png", PNG_BYTES, "screenshot")
    deduped = select_bundle_parts(
        [dup, dup, MediaPart("empty.log", "text/plain", b"   \n", "log")],
        run_id="99",
    )
    assert len(deduped.parts) == 1

    # Client without .files raises FileUploadError when > MAX_INLINE_BYTES.
    monkeypatch.setattr(managed_agent, "MAX_INLINE_BYTES", 8)
    monkeypatch.setattr(managed_agent, "FILE_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(managed_agent, "FILE_POLL_TIMEOUT_SECONDS", 0.0)

    @dataclass
    class _NoFilesClient:
        models: _FakeModelsAPI

    with pytest.raises(FileUploadError, match="does not expose a files API"):
        ask_multimodal(MODEL, [("video/webm", WEBM_BYTES * 4)], SCHEMA, client=_NoFilesClient(_FakeModelsAPI("{}")))

    # Polling timeout raises FileUploadError and still cleans up uploaded file.
    stuck = _FakeFilesAPI(states_by_name={"files/item-1": ["PROCESSING", "PROCESSING"]})
    with pytest.raises(FileUploadError, match="timed out"):
        ask_multimodal(
            MODEL,
            [("video/webm", WEBM_BYTES * 4)],
            SCHEMA,
            client=_FakeFilesClient(models=_FakeModelsAPI("{}"), files=stuck),
        )
    assert stuck.deleted == ["files/item-1"]


def test_github_redirect_edge_cases_and_historical_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/artifacts/10/zip"):
            return httpx.Response(302)  # missing Location
        if request.url.path.endswith("/artifacts/11/zip"):
            return httpx.Response(
                302, headers={"Location": "https://api.github.com/repos/acme/app/actions/artifacts/11/zip"}
            )
        if request.url.path.endswith("/runs/12/jobs"):
            return httpx.Response(200, json={"jobs": [{"id": 1, "name": "Lint", "conclusion": "success"}]})
        return httpx.Response(404)

    with HttpxGitHubClient(token="ghp_test", transport=httpx.MockTransport(handler)) as gh:  # noqa: S106
        with pytest.raises(ValueError, match="missing Location"):
            gh.download_artifact("acme/app", 10)
        with pytest.raises(ValueError, match="Too many redirects"):
            gh.download_artifact("acme/app", 11)
        assert gh.get_failed_job_logs("acme/app", 12) == b""

    # Historical artifact errors are strictly non-fatal in prepare_context.
    github = _RichProofGitHub(
        job_runs=[
            JobRun(
                sha="aaaa1111",
                branch="main",
                created_at="2026-11-10T00:00:00Z",
                workflow="CI",
                job="UI",
                attempt=1,
                conclusion="failure",
            )
        ],
        artifacts_by_run={("acme/app", "500"): [RunArtifact(1, "ui", 100, "https://example.com/1", expired=False)]},
        artifact_zips={1: OSError("expired 410")},
    )
    cand = Candidate(id="c1", hypothesis_id="h1", branch="pc/1", summary="s", repo="acme/app", changed_files=("a.py",))
    ctx = ProofBridge(github).prepare_context(cand, historical_run_ids={"aaaa1111": "500"})
    assert ctx.mounted_artifacts == ()
    assert ctx.extra_sources == ()
