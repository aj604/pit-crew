"""The visual proof attached to a candidate's PR: a card image and the PR body around it.

Every number comes from the rater's Evidence and Verdict, never recomputed here, so the
picture cannot disagree with the decision. The card is SVG with its own background, so it
reads the same on GitHub's light and dark themes.
"""

from statistics import median
from xml.sax.saxutils import escape

from pit_crew.contracts import Evidence, Verdict
from pit_crew.lanes.proof.verify import MODEL, Replay

WIDTH = 760
ROW = 30
_BG, _PANEL, _TEXT, _MUTED, _LINE = "#0d1117", "#161b22", "#e6edf3", "#8b949e", "#30363d"
_GOOD, _BAD, _WARN = "#3fb950", "#f85149", "#d29922"
_STATUS = {"caught": _GOOD, "missed": _BAD, "different failure": _BAD, "unverified": _WARN, "not a bug": _MUTED}


def render_card(branch: str, evidence: Evidence, verdict: Verdict, replays: list[Replay]) -> str:
    before, after = median(evidence.baseline_seconds), median(evidence.candidate_seconds)
    runs = min(len(evidence.baseline_seconds), len(evidence.candidate_seconds))
    # The rater scores speed only after the bugs pass, so a missing p-value means speed was never scored.
    scored = verdict.p_value is not None
    timing = f"median of {runs} runs per side" + (f", p = {verdict.p_value:.3f}" if scored else ", speed not scored")
    compared = [r for r in replays if r.match is not None]
    matched = sum(r.match.same for r in compared if r.match)
    height = 146 + ROW * max(len(replays), 1) + 76

    out = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" viewBox="0 0 {WIDTH} {height}" '
            'font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,monospace">'
        ),
        f'<rect width="{WIDTH}" height="{height}" rx="12" fill="{_BG}"/>',
        _text(24, 36, branch, 18, _TEXT, weight="bold"),
        _text(WIDTH - 24, 36, "KEEP" if verdict.keep else "REJECT", 16, _GOOD if verdict.keep else _BAD, "end", "bold"),
        _text(24, 76, f"CI time  {_duration(before)}  ->  {_duration(after)}", 22, _TEXT, weight="bold"),
        _text(WIDTH - 24, 76, f"{verdict.speedup:.2f}x" if scored else "–", 22, _GOOD if verdict.keep else _MUTED,
              "end", "bold"),
        _text(24, 100, timing, 13, _MUTED),
        f'<rect x="16" y="116" width="{WIDTH - 32}" height="{ROW}" rx="6" fill="{_PANEL}"/>',
    ]
    columns = [(28, "bug"), (120, "check"), (340, "baseline"), (450, "candidate"), (570, "result")]
    out += [_text(x, 136, label, 12, _MUTED) for x, label in columns]

    y = 116 + ROW
    for replay in replays:
        y += ROW
        out += [
            _text(28, y - 10, replay.sha[:7], 14, _TEXT),
            _text(120, y - 10, _clip(replay.check, 25), 14, _TEXT),
            _run(340, y - 10, replay.baseline_failed),
            _run(450, y - 10, replay.candidate_failed),
            _text(570, y - 10, replay.status, 14, _STATUS[replay.status], weight="bold"),
            f'<line x1="16" y1="{y}" x2="{WIDTH - 16}" y2="{y}" stroke="{_LINE}"/>',
        ]
    if not replays:
        y += ROW
        out.append(_text(28, y - 10, "no bugs replayed", 14, _BAD))

    out += [
        _text(24, y + 30, _clip(verdict.reason, 80), 13, _TEXT),
        _text(24, y + 52, f"{MODEL} watched {len(compared)} pairs of failure recordings: {matched} matched", 12, _MUTED),
        "</svg>",
    ]
    return "\n".join(out)


def pr_body(branch: str, evidence: Evidence, verdict: Verdict, replays: list[Replay], card_url: str) -> str:
    before, after = median(evidence.baseline_seconds), median(evidence.candidate_seconds)
    p = "n/a" if verdict.p_value is None else f"{verdict.p_value:.3f}"
    rows = [
        "| Bug | Check | Baseline | Candidate | Result | Recordings | What Gemini 4 Neon saw |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in replays:
        seen = r.match.candidate_failure if r.match and r.match.same else (r.match.reason if r.match else r.note)
        rows.append(
            f"| `{r.sha[:7]}` | {_cell(r.check)} | {_outcome(r.baseline_failed)} | {_outcome(r.candidate_failed)} "
            f"| **{r.status}** | [baseline]({r.baseline_video}) · [candidate]({r.candidate_video}) | {_cell(seen)} |"
        )
    return "\n".join(
        [
            f"## Pit Crew: `{branch}`",
            "",
            f"![Proof card for {branch}]({card_url})",
            "",
            f"**{verdict.reason}.** Median CI time {_duration(before)} → {_duration(after)}, p = {p}.",
            "",
            *rows,
            "",
            (
                f"Exit codes decide whether a run failed. {MODEL} only confirms that the baseline and candidate fail "
                "the same way, so it can turn a catch into a miss but never a miss into a catch."
            ),
        ]
    )


def _text(x: float, y: float, s: str, size: int, fill: str, anchor: str = "start", weight: str = "normal") -> str:
    return (
        f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" text-anchor="{anchor}" '
        f'font-weight="{weight}">{escape(s)}</text>'
    )


def _run(x: float, y: float, failed: bool) -> str:
    # Red means the check went red on the bug, which is what we want to see on both sides.
    return _text(x, y, "x failed" if failed else "o passed", 14, _BAD if failed else _MUTED)


def _duration(seconds: float) -> str:
    minutes, secs = divmod(round(seconds), 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _outcome(failed: bool) -> str:
    return "failed" if failed else "passed"


def _cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")
