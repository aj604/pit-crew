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

WIDTH = 760
ROW = 30
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
"""
# Markdown that could turn quoted CI output into links, images, HTML or formatting.
_MARKDOWN = re.compile(r"([\\`*_\[\]<>|~!])")
_STATUS = {"caught": "good", "missed": "bad", "different failure": "bad", "unverified": "warn", "not a bug": "muted"}
_COLUMNS = ((28, "bug"), (120, "check"), (340, "baseline"), (450, "candidate"), (570, "result"))


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
        _text(24, y + 30, _clip(verdict.reason, 80), ""),
        _text(24, y + 52, f"{MODEL} watched {len(compared)} pairs of failure recordings: {matched} matched", "head"),
        "</svg>",
    ]
    return "\n".join(out)


def pr_body(branch: str, evidence: Evidence, verdict: Verdict, replays: Sequence[Replay], card_url: str) -> str:
    """Return the markdown PR body: the card, the verdict, and every replay with links to its recordings."""
    rows = [
        f"| Bug | Check | Baseline | Candidate | Result | Recordings | What {MODEL} saw |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in replays:
        seen = r.match.candidate_failure if r.match and r.match.same else (r.match.reason if r.match else r.note)
        rows.append(
            f"| `{r.sha[:7]}` | {_cell(r.check)} | {_outcome(failed=r.baseline_failed)} "
            f"| {_outcome(failed=r.candidate_failed)} | **{r.status}** "
            f"| {_recordings(r)} | {_cell(seen)} |"
        )
    before, after = _median(evidence.baseline_seconds), _median(evidence.candidate_seconds)
    return "\n".join(
        [
            f"## Pit Crew: `{branch}`",
            "",
            f"![Proof card for {branch}]({card_url})",
            "",
            f"**{verdict.reason}.** Median CI time {before} -> {after}; {_timing(evidence, verdict)}.",
            "",
            *rows,
            "",
            (
                f"Exit codes decide whether a run failed. {MODEL} only confirms that the baseline and candidate "
                "fail the same way, so it can turn a catch into a miss but never a miss into a catch."
            ),
        ]
    )


def _row(replay: Replay, y: int) -> list[str]:
    return [
        _text(28, y - 10, replay.sha[:7], ""),
        _text(120, y - 10, _clip(replay.check, 25), ""),
        _run(340, y - 10, failed=replay.baseline_failed),
        _run(450, y - 10, failed=replay.candidate_failed),
        _text(570, y - 10, replay.status, _STATUS[replay.status]),
        f'<line x1="16" y1="{y}" x2="{WIDTH - 16}" y2="{y}" stroke="#30363d"/>',
    ]


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


def _recordings(replay: Replay) -> str:
    # A replay without agg or ffmpeg has no video; say so rather than link to nothing.
    videos = (("baseline", replay.baseline_video), ("candidate", replay.candidate_video))
    return " / ".join(f"[{side}]({url})" for side, url in videos if url) or "none"


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
