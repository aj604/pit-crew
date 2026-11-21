"""The visual proof attached to a candidate's PR: a card image and the PR body around it.

Every number comes from the rater's Evidence and Verdict, never rescored here, so the
picture cannot disagree with the decision. The card is SVG with its own background, so it
reads the same on GitHub's light and dark themes.
"""

import re
from collections.abc import Sequence
from statistics import median
from xml.sax.saxutils import escape

from pit_crew.contracts import Evidence, Verdict
from pit_crew.lanes.proof.verify import MODEL, Replay

WIDTH = 860
ROW = 30
MAX_PR_VISUAL_PAIRS = 3
_TOP = 116  # y of the table header
_BRANCH_CHARS = 56  # fits the 18px title left of a right-aligned REJECT
_STYLE = """
text { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 14px; fill: #e6edf3; }
.title { font-size: 18px; font-weight: bold; }
.big { font-size: 22px; font-weight: bold; }
.small { font-size: 13px; fill: #8b949e; }
.head { font-size: 12px; fill: #8b949e; }
.good { fill: #3fb950; font-weight: bold; }
.bad { fill: #f85149; font-weight: bold; }
.warn { fill: #d29922; font-weight: bold; }
.muted { fill: #8b949e; }
.red { fill: #f85149; }
.badge-text { font-size: 11px; font-weight: bold; }
.badge-video { fill: #58a6ff; }
.badge-visual { fill: #3fb950; }
.badge-trace { fill: #d29922; }
.badge-term { fill: #a371f7; }
.badge-log { fill: #8b949e; }
"""
# Markdown that could turn quoted CI output into links, images, HTML or formatting.
_MARKDOWN = re.compile(r"([\\`*_\[\]<>|~!])")
_STATUS = {"caught": "good", "missed": "bad", "different failure": "bad", "unverified": "warn", "not a bug": "muted"}
_COLUMNS = ((28, "bug"), (116, "check"), (320, "modality"), (440, "baseline"), (550, "candidate"), (670, "result"))
_MODALITY_BADGES: dict[str, tuple[str, str]] = {
    "browser_video": ("VIDEO", "badge-video"),
    "screenshot": ("SCREENSHOT", "badge-visual"),
    "trace_frame": ("TRACE", "badge-trace"),
    "terminal_video": ("TERMINAL", "badge-term"),
    "log": ("LOG", "badge-log"),
}


def render_card(branch: str, evidence: Evidence, verdict: Verdict, replays: Sequence[Replay]) -> str:
    """Return the proof card as SVG: CI time before and after, each bug's replays, and the verdict."""
    rows = max(len(replays), 1)
    height = _TOP + ROW * (rows + 1) + 76
    verdict_class = "good" if verdict.keep else "bad"
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" viewBox="0 0 {WIDTH} {height}">',
        f"<style>{_STYLE}</style>",
        f'<rect width="{WIDTH}" height="{height}" rx="12" fill="#0d1117"/>',
        _text(24, 36, _clip(branch, _BRANCH_CHARS), "title"),
        _text(WIDTH - 24, 36, "KEEP" if verdict.keep else "REJECT", f"title {verdict_class}", "end"),
        _text(
            24,
            76,
            f"CI time  {_median(evidence.baseline_seconds)}  ->  {_median(evidence.candidate_seconds)}",
            "big",
        ),
        _text(WIDTH - 24, 76, _speedup(verdict), f"big {verdict_class if verdict.keep else 'muted'}", "end"),
        _text(24, 100, _timing(evidence, verdict), "small"),
        f'<rect x="16" y="{_TOP}" width="{WIDTH - 32}" height="{ROW}" rx="6" fill="#161b22"/>',
        *(_text(x, _TOP + 20, label, "head") for x, label in _COLUMNS),
    ]
    y = _TOP + ROW
    for replay in replays:
        y += ROW
        out += _row(replay, y)
    if not replays:
        y += ROW
        out.append(_text(28, y - 10, "no bugs replayed", "bad"))

    compared = [r.match for r in replays if r.match is not None]
    matched = sum(m.same for m in compared)
    out += [
        _text(24, y + 30, _clip(verdict.reason, 88), ""),
        _text(24, y + 52, f"{MODEL} watched {len(compared)} pairs of failure recordings: {matched} matched", "head"),
        "</svg>",
    ]
    return "\n".join(out)


def pr_body(  # noqa: PLR0913  optional preview/media options preserve legacy positional callers
    branch: str,
    evidence: Evidence,
    verdict: Verdict,
    replays: Sequence[Replay],
    card_url: str,
    *,
    preview_base_url: str = "",
    media_base_url: str = "",
    media_requires_auth: bool = False,
) -> str:
    """Return the markdown PR body: the card, replay table, visual comparisons, and trust rule."""
    rows = [
        f"| Bug | Check | Modality | Baseline | Candidate | Result | Recordings | What {MODEL} saw |",
        "|---|---|---|---|---|---|---|---|",
        *(
            f"| `{r.sha[:7]}` | {_cell(r.check)} | {_modality_md(r.modality)} "
            f"| {_outcome(failed=r.baseline_failed)} | {_outcome(failed=r.candidate_failed)} "
            f"| **{r.status}** "
            f"| {_recordings(r, media_base_url=media_base_url, media_requires_auth=media_requires_auth)} "
            f"| {_cell(_seen_summary(r))} |"
            for r in replays
        ),
    ]
    before, after = _median(evidence.baseline_seconds), _median(evidence.candidate_seconds)
    sections = [
        f"## Pit Crew: `{branch}`",
        "",
        f"![Proof card for {branch}]({card_url})",
        "",
        f"**{verdict.reason}.** Median CI time {before} -> {after}; {_timing(evidence, verdict)}.",
        "",
        *rows,
    ]
    visual_section = _visual_comparison_section(replays, preview_base_url=preview_base_url)
    if visual_section:
        sections.extend(["", *visual_section])
    sections.extend(
        [
            "",
            (
                f"Exit codes decide whether a run failed. {MODEL} only confirms that the baseline and candidate "
                "fail the same way, so it can turn a catch into a miss but never a miss into a catch."
            ),
        ]
    )
    return "\n".join(sections)


def _row(replay: Replay, y: int) -> list[str]:
    return [
        _text(28, y - 10, replay.sha[:7], ""),
        _text(116, y - 10, _clip(replay.check, 22), ""),
        *_modality_badge(320, y - 10, replay.modality),
        _run(440, y - 10, failed=replay.baseline_failed),
        _run(550, y - 10, failed=replay.candidate_failed),
        _text(670, y - 10, replay.status, _STATUS[replay.status]),
        f'<line x1="16" y1="{y}" x2="{WIDTH - 16}" y2="{y}" stroke="#30363d"/>',
    ]


def _modality_badge(x: int, y: int, modality: str) -> list[str]:
    badge = _MODALITY_BADGES.get(modality)
    if badge is None:
        return [_text(x, y, "-", "muted")]
    label, css = badge
    pill_w = max(48, len(label) * 8 + 14)
    return [
        f'<rect x="{x}" y="{y - 14}" width="{pill_w}" height="18" rx="4" fill="#161b22" stroke="#30363d"/>',
        _text(x + 7, y - 1, label, f"badge-text {css}"),
    ]


def _modality_md(modality: str) -> str:
    badge = _MODALITY_BADGES.get(modality)
    return f"`{badge[0]}`" if badge is not None else "-"


def _seen_summary(replay: Replay) -> str:
    if replay.match is None:
        return replay.note
    if replay.match.same:
        if replay.match.visual_summary:
            return f"{replay.match.candidate_failure} — {replay.match.visual_summary}"
        return replay.match.candidate_failure
    if replay.match.visual_summary:
        return f"{replay.match.reason} ({replay.match.visual_summary})"
    return replay.match.reason


def _visual_comparison_section(replays: Sequence[Replay], *, preview_base_url: str) -> list[str]:
    paired = [r for r in replays if r.baseline_preview_path and r.candidate_preview_path][:MAX_PR_VISUAL_PAIRS]
    if not paired:
        return []
    lines = ["### Visual Failure Evidence (Baseline vs. Candidate)"]
    for r in paired:
        base_url = _resolve_preview_url(r.baseline_preview_path, preview_base_url)
        cand_url = _resolve_preview_url(r.candidate_preview_path, preview_base_url)
        label = _MODALITY_BADGES.get(r.modality, ("VISUAL", ""))[0]
        base_alt = escape(f"Baseline failure for {r.sha[:7]} ({r.check})", {'"': "&quot;"})
        cand_alt = escape(f"Candidate failure for {r.sha[:7]} ({r.check})", {'"': "&quot;"})
        lines.extend(
            [
                "",
                f"#### `{r.sha[:7]}` — {_cell(r.check)} (`{label}`)",
                "",
                "| Baseline | Candidate |",
                "|---|---|",
                (
                    f'| <img src="{escape(base_url, {'"': "&quot;"})}" alt="{base_alt}" width="360" /> '
                    f'| <img src="{escape(cand_url, {'"': "&quot;"})}" alt="{cand_alt}" width="360" /> |'
                ),
            ]
        )
        if r.match and r.match.visual_summary:
            lines.append(f"\n> **{MODEL} visual check:** {r.match.visual_summary}")
    return lines


def _resolve_preview_url(path: str, preview_base_url: str) -> str:
    if path.startswith(("https://", "http://")):
        return path
    if preview_base_url:
        return f"{preview_base_url.rstrip('/')}/{path.lstrip('/')}"
    return path


def _speedup(verdict: Verdict) -> str:
    # The rater scores speed only once every bug is caught; until then there is no p-value.
    return "not scored" if verdict.p_value is None else f"{verdict.speedup:.2f}x"


def _timing(evidence: Evidence, verdict: Verdict) -> str:
    runs = min(len(evidence.baseline_seconds), len(evidence.candidate_seconds))
    parts = [f"median of {runs} runs per side"]
    if verdict.p_value is not None:
        parts += [f"p = {verdict.p_value:.3f}", f"p95 {_duration(verdict.p95_seconds)}"]
        parts.append("SLA met" if verdict.sla_met else "SLA not met")
    return ", ".join(parts)


def _text(x: int, y: int, s: str, css: str, anchor: str = "start") -> str:
    cls = f' class="{css}"' if css else ""
    return f'<text x="{x}" y="{y}" text-anchor="{anchor}"{cls}>{escape(s)}</text>'


def _run(x: int, y: int, *, failed: bool) -> str:
    # Red means the check went red on the bug, which is what we want to see on both sides.
    return _text(x, y, "x failed" if failed else "o passed", "red" if failed else "muted")


def _median(seconds: Sequence[float]) -> str:
    # The rater rejects a candidate with no runs; the card still has to say so.
    return _duration(median(seconds)) if seconds else "no runs"


def _resolve_recording_link(  # noqa: PLR0913
    video_url: str,
    artifact_urls: tuple[str, ...],
    fallback_video: str,
    *,
    side: str,
    media_base_url: str,
    media_requires_auth: bool = False,
) -> tuple[str, str]:
    cleaned_video = video_url.strip()
    base = media_base_url.strip().rstrip("/")
    if cleaned_video.startswith(("https://", "http://")):
        if not (media_requires_auth and base and cleaned_video.startswith(f"{base}/media/")):
            label = f"{side}.mp4" if cleaned_video.lower().endswith(".mp4") else side
            return label, cleaned_video
    elif not media_requires_auth and cleaned_video.startswith("/media/") and base.startswith(("https://", "http://")):
        label = f"{side}.mp4" if cleaned_video.lower().endswith(".mp4") else side
        return label, f"{base}{cleaned_video}"
    fallback = artifact_urls[0] if artifact_urls else fallback_video
    return side, fallback


def _recordings(replay: Replay, *, media_base_url: str = "", media_requires_auth: bool = False) -> str:
    # Prefer playable public MP4 URLs when unauthenticated; otherwise fall back to GitHub artifact/run URLs.
    entries = (
        _resolve_recording_link(
            replay.baseline_video_url,
            replay.baseline_artifact_urls,
            replay.baseline_video,
            side="baseline",
            media_base_url=media_base_url,
            media_requires_auth=media_requires_auth,
        ),
        _resolve_recording_link(
            replay.candidate_video_url,
            replay.candidate_artifact_urls,
            replay.candidate_video,
            side="candidate",
            media_base_url=media_base_url,
            media_requires_auth=media_requires_auth,
        ),
    )
    return " / ".join(f"[{label}]({url})" for label, url in entries if url) or "none"


def _duration(seconds: float) -> str:
    minutes, secs = divmod(round(seconds), 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 3] + "..."


def _outcome(*, failed: bool) -> str:
    return "failed" if failed else "passed"


def _cell(s: str) -> str:
    """Return text for a table cell, as plain text: it can quote CI output, which anyone's commit can print."""
    text = _MARKDOWN.sub(r"\\\1", " ".join(s.splitlines()))
    return text.replace("@", "@\u2060")  # a word joiner after @ stops it pinging a user or team
