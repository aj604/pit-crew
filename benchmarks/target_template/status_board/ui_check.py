"""Headless UI check: render the board from a fixture and assert on its DOM.

Run ``python -m status_board.ui_check``. On failure it writes a diagnostic SVG snapshot of
what rendered, with the failed assertions, to ``ui-snapshots/ui-check.svg`` and exits 1.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser
from pathlib import Path

from status_board.model import InvalidServiceError, Service, Status, parse_service
from status_board.render import render_board

DEFAULT_FIXTURE = Path("fixtures/board.json")
DEFAULT_SNAPSHOT_DIR = Path("ui-snapshots")
SNAPSHOT_NAME = "ui-check.svg"
_COLORS = {"up": "#1f883d", "degraded": "#bf8700", "down": "#cf222e"}


@dataclass
class Dom:
    """The parts of the rendered page the check asserts on."""

    title: str = ""
    banner: dict[str, str] = field(default_factory=dict)
    cards: list[dict[str, str]] = field(default_factory=list)


class _DomParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.dom = Dom()
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        classes = values.get("class", "").split()
        if tag == "title":
            self._in_title = True
        elif tag == "header" and "banner" in classes:
            self.dom.banner = values
        elif tag == "section" and "card" in classes:
            self.dom.cards.append(values)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.dom.title += data


def parse_dom(html: str) -> Dom:
    """Parse the rendered page into the elements the check cares about."""
    parser = _DomParser()
    parser.feed(html)
    parser.close()
    return parser.dom


def check(dom: Dom, title: str, services: Sequence[Service], expect: dict[str, object]) -> list[str]:
    """Every assertion that failed; empty when the page is right."""
    problems = []
    if dom.title != title:
        problems.append(f"title is {dom.title!r}, expected {title!r}")
    if dom.banner.get("data-overall") != expect.get("overall"):
        problems.append(f"banner shows {dom.banner.get('data-overall')!r}, expected {expect.get('overall')!r}")
    if len(dom.cards) != expect.get("cards"):
        problems.append(f"{len(dom.cards)} cards rendered, expected {expect.get('cards')}")
    names = [card.get("data-service") for card in dom.cards]
    if names != [service.name for service in services]:
        problems.append(f"cards are {names}, expected {[s.name for s in services]}")
    for card in dom.cards:
        status = card.get("data-status", "")
        if status not in set(Status) or f"status-{status}" not in card.get("class", "").split():
            problems.append(f"card {card.get('data-service')!r} has inconsistent status {status!r}")
    return problems


def snapshot_svg(dom: Dom, problems: Sequence[str]) -> str:
    """A picture of what rendered (banner and cards) above the failed assertions."""
    width, card_w, card_h = 640, 140, 56
    rows = (len(dom.cards) + 3) // 4
    height = 80 + rows * (card_h + 12) + 24 * len(problems) + 24
    overall = dom.banner.get("data-overall", "")
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" font-family="sans-serif">',
        f'<rect width="{width}" height="{height}" fill="#f6f7f9"/>',
        f'<rect width="{width}" height="48" fill="{_COLORS.get(overall, "#57606a")}"/>',
        f'<text x="16" y="30" fill="#fff" font-size="18">{_text(dom.title)} - {_text(overall or "no banner")}</text>',
    ]
    for i, card in enumerate(dom.cards):
        x, y = 16 + (i % 4) * (card_w + 12), 64 + (i // 4) * (card_h + 12)
        color = _COLORS.get(card.get("data-status", ""), "#57606a")
        parts.append(f'<rect x="{x}" y="{y}" width="{card_w}" height="{card_h}" fill="#fff" stroke="{color}"/>')
        label = _text(f"{card.get('data-service', '?')}: {card.get('data-status', '?')}")
        parts.append(f'<text x="{x + 8}" y="{y + 32}" font-size="14">{label}</text>')
    top = 80 + rows * (card_h + 12)
    for i, problem in enumerate(problems):
        parts.append(f'<text x="16" y="{top + 24 * i}" fill="#cf222e" font-size="13">{_text(problem)}</text>')
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def _text(value: str) -> str:
    return escape(value, quote=False)


def run(fixture: Path, snapshot_dir: Path) -> list[str]:
    """Render the fixture, check the page, and write a snapshot when anything failed."""
    data = json.loads(fixture.read_text(encoding="utf-8"))
    title = str(data.get("title", "Service status"))
    expect = data.get("expect", {})
    try:
        services = [parse_service(raw) for raw in data.get("services", [])]
    except InvalidServiceError as exc:
        services, problems = [], [str(exc)]
    else:
        problems = []
    dom = parse_dom(render_board(services, title))
    problems += check(dom, title, services, expect)
    if problems:
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        (snapshot_dir / SNAPSHOT_NAME).write_text(snapshot_svg(dom, problems), encoding="utf-8")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point; returns the exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT_DIR)
    args = parser.parse_args(argv)
    problems = run(args.fixture, args.snapshot_dir)
    if problems:
        sys.stderr.write("ui-check failed:\n" + "".join(f"  - {p}\n" for p in problems))
        sys.stderr.write(f"snapshot written to {args.snapshot_dir / SNAPSHOT_NAME}\n")
        return 1
    sys.stdout.write(f"ui-check passed: {args.fixture}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
