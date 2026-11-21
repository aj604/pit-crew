import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pit_crew.ledger import InMemoryLedger
from pit_crew.server import DEFAULT_BRAND_DIR, DEFAULT_WEB_DIR, create_app

WEB_DIR = Path(__file__).resolve().parents[1] / "src" / "pit_crew" / "web"
APP_JS = WEB_DIR / "app.js"
NODE = shutil.which("node")
# A `#` hex literal not preceded by a word char or `&` (so `#repo-input` ids and `&#...;` don't count as
# id selectors), plus functional colour notations that would bypass the brand tokens.
AD_HOC_COLOR = re.compile(r"(?<![\w&])#[0-9a-fA-F]{3,8}\b(?![\w-])|\b(?:rgba?|hsla?)\(", re.IGNORECASE)


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(InMemoryLedger()))


def test_default_dirs_point_at_repo_assets() -> None:
    assert DEFAULT_WEB_DIR == WEB_DIR
    assert (DEFAULT_BRAND_DIR / "tokens.css").is_file()


def test_index_serves_html_shell(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    assert 'href="/brand/tokens.css"' in html
    assert 'href="/web/styles.css"' in html
    assert re.search(r'<script\s+type="module"\s+src="/web/app\.js"\s*>', html)
    assert '<link rel="icon" type="image/svg+xml" href="/brand/favicon.svg" />' in html


def test_index_loads_brand_fonts_with_swap(client: TestClient) -> None:
    html = client.get("/").text
    fonts = re.search(r'href="(https://fonts\.googleapis\.com/css2\?[^"]+)"', html)

    assert fonts is not None
    href = fonts.group(1)
    for family in ("Chakra+Petch", "Titillium+Web", "JetBrains+Mono"):
        assert f"family={family}" in href
    assert "display=swap" in href
    # Tokens stylesheet loads after the font link so its fallback stacks apply offline.
    assert html.index(href) < html.index("/brand/tokens.css") < html.index("/web/styles.css")


def test_header_bar_renders_controls(client: TestClient) -> None:
    html = client.get("/").text

    assert 'src="/brand/logo-lockup-dark.svg"' in html
    assert re.search(r'id="repo-input"[^>]*', html)
    assert 'placeholder="owner/name"' in html
    assert 'list="repo-options"' in html
    assert '<datalist id="repo-options">' in html
    assert 'id="base-sha-chip"' in html
    assert 'aria-label="SLA Target Meter"' in html
    assert "s / 60s" in html
    assert "s / 120s" in html
    assert re.search(r'<span class="sla-meter__badge" id="sla-badge" hidden>', html)
    assert 'data-mode="live"' in html
    assert 'data-mode="demo"' in html
    assert re.search(r'<button[^>]*id="run-pit-stop"[^>]*>Run Pit Stop</button>', html)


def test_index_provides_downstream_mount_points(client: TestClient) -> None:
    html = client.get("/").text

    for slot in ("demo-switcher", "sector-pipeline", "telemetry-center", "lap-history"):
        assert re.search(rf'<section id="{slot}"[^>]*></section>', html)


@pytest.mark.parametrize(
    ("path", "media_type"),
    [
        ("/brand/tokens.css", "text/css"),
        ("/brand/logo-lockup-dark.svg", "image/svg+xml"),
        ("/brand/favicon.svg", "image/svg+xml"),
        ("/web/styles.css", "text/css"),
        ("/web/app.js", "javascript"),
    ],
)
def test_static_assets_are_served(client: TestClient, path: str, media_type: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert media_type in response.headers["content-type"]


def test_missing_brand_dir_still_serves_shell(tmp_path: Path) -> None:
    client = TestClient(create_app(InMemoryLedger(), brand_dir=tmp_path / "missing"))

    assert client.get("/").status_code == 200
    assert client.get("/web/app.js").status_code == 200
    assert client.get("/brand/tokens.css").status_code == 404


def test_custom_dirs_are_mounted(tmp_path: Path) -> None:
    brand = tmp_path / "brand"
    web = tmp_path / "web"
    brand.mkdir()
    web.mkdir()
    (brand / "tokens.css").write_text(":root {}")
    (web / "index.html").write_text("<!doctype html><title>custom</title>")
    client = TestClient(create_app(InMemoryLedger(), brand_dir=brand, web_dir=web))

    assert "custom" in client.get("/").text
    assert client.get("/brand/tokens.css").text == ":root {}"


@pytest.mark.parametrize("name", ["styles.css", "index.html"])
def test_shell_uses_no_ad_hoc_colors(name: str) -> None:
    source = (WEB_DIR / name).read_text()

    assert AD_HOC_COLOR.findall(source) == []


def test_color_check_catches_literals() -> None:
    assert AD_HOC_COLOR.search("color: #FF1E00;")
    assert AD_HOC_COLOR.search("background: rgb(0 0 0);")
    assert not AD_HOC_COLOR.search("#repo-input { color: var(--pc-ink-highvis); }")


def test_styles_only_reference_defined_tokens() -> None:
    defined = set(re.findall(r"(--pc-[\w-]+)\s*:", (DEFAULT_BRAND_DIR / "tokens.css").read_text()))
    used = set(re.findall(r"var\((--pc-[\w-]+)\)", (WEB_DIR / "styles.css").read_text()))

    assert used
    assert used <= defined


def test_styles_use_brand_fonts_and_tabular_nums() -> None:
    css = (WEB_DIR / "styles.css").read_text()

    for token in ("--pc-font-display", "--pc-font-sans", "--pc-font-mono"):
        assert f"var({token})" in css
    assert re.search(r"\.telemetry\s*\{[^}]*font-variant-numeric:\s*tabular-nums", css)
    assert re.search(r"\[hidden\]\s*\{[^}]*display:\s*none", css)
    assert re.search(r"\.sla-meter__badge\s*\{[^}]*var\(--pc-drs-green\)", css)
    assert "@media (max-width: 640px)" in css


def test_telemetry_readouts_use_tabular_class(client: TestClient) -> None:
    html = client.get("/").text

    assert re.search(r'id="base-sha-value"', html)
    assert html.count("telemetry") >= 3


def test_app_js_ids_exist_in_index() -> None:
    js = APP_JS.read_text()
    html = (WEB_DIR / "index.html").read_text()
    ids = set(re.findall(r'el\("([\w-]+)"\)', js))

    assert ids
    for element_id in ids:
        assert f'id="{element_id}"' in html


_NODE_SCRIPT = """
const m = await import(process.argv[1]);
const status = {
  repo: "aj604/pit-crew", active: true, base_sha: "0123456789abcdef", sla_met: true,
  latest_verdict: { median_seconds: 42.25, p95_seconds: 118 },
};
console.log(JSON.stringify({
  view: m.headerViewFromStatus(status),
  empty: m.headerViewFromStatus(null),
  slugs: [m.isRepoSlug("owner/name"), m.isRepoSlug("owner"), m.isRepoSlug("a/b/c"), m.isRepoSlug(3)],
  targets: [m.SLA_MEDIAN_TARGET_SECONDS, m.SLA_P95_TARGET_SECONDS],
  modes: m.MODES,
}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_app_js_is_valid_module_with_header_helpers() -> None:
    assert NODE is not None
    subprocess.run([NODE, "--check", str(APP_JS)], check=True, capture_output=True)  # noqa: S603
    result = subprocess.run(  # noqa: S603
        [NODE, "--input-type=module", "-e", _NODE_SCRIPT, APP_JS.as_uri()],
        check=True,
        capture_output=True,
        text=True,
    )

    out = json.loads(result.stdout)
    assert out["view"] == {
        "repo": "aj604/pit-crew",
        "sha": "0123456",
        "median": "42.3",
        "p95": "118.0",
        "slaMet": True,
        "active": True,
    }
    assert out["empty"] == {"repo": "", "sha": "-------", "median": "--", "p95": "--", "slaMet": False, "active": False}
    assert out["slugs"] == [True, False, False, False]
    assert out["targets"] == [60, 120]
    assert out["modes"] == ["live", "demo"]


# A minimal fake DOM: enough of `getElementById`, attributes and listeners to drive `createHeaderBar`
# and the module's boot code under node, without jsdom.
_NODE_DOM_SCRIPT = """
const fakeNode = (extra = {}) => {
  const listeners = {};
  return {
    hidden: false, textContent: "", title: "", value: "", disabled: false, dataset: {}, attrs: {}, kids: [],
    setAttribute(name, value) { this.attrs[name] = value; },
    addEventListener(type, fn) { (listeners[type] ??= []).push(fn); },
    dispatchEvent(event) { for (const fn of listeners[event.type] ?? []) fn(event); return true; },
    replaceChildren(...kids) { this.kids = kids; },
    querySelectorAll(selector) { return selector === "[data-mode]" ? (this.modeButtons ?? []) : []; },
    click() { this.dispatchEvent({ type: "click" }); },
    ...extra,
  };
};
const fakeDocument = (readyState) => {
  const live = fakeNode({ dataset: { mode: "live" } });
  const demo = fakeNode({ dataset: { mode: "demo" } });
  const ids = {
    "repo-input": fakeNode(), "repo-options": fakeNode(), "base-sha-value": fakeNode(),
    "sla-median": fakeNode(), "sla-p95": fakeNode(), "sla-badge": fakeNode({ hidden: true }),
    "mode-toggle": fakeNode({ modeButtons: [live, demo] }), "run-pit-stop": fakeNode(),
  };
  const doc = fakeNode({ readyState, getElementById: (id) => ids[id] ?? null, createElement: () => ({}) });
  return { doc, ids, live, demo };
};
const pressed = (d) => [d.live.attrs["aria-pressed"], d.demo.attrs["aria-pressed"]];
const url = process.argv[1];
globalThis.window = globalThis;

const ready = fakeDocument("complete");
globalThis.document = ready.doc;
const m = await import(url);
const header = window.pitWall?.header;
const bootBound = header?.runButton === ready.ids["run-pit-stop"];
const ids = ready.ids;

header.applyStatus({
  repo: "aj604/pit-crew", active: true, base_sha: "0123456789abcdef", sla_met: true,
  latest_verdict: { median_seconds: 42.25, p95_seconds: 118 },
});
const met = {
  badgeHidden: ids["sla-badge"].hidden, repo: ids["repo-input"].value, sha: ids["base-sha-value"].textContent,
  shaTitle: ids["base-sha-value"].title, median: ids["sla-median"].textContent, p95: ids["sla-p95"].textContent,
  disabled: ids["run-pit-stop"].disabled, busy: ids["run-pit-stop"].attrs["aria-busy"],
  label: ids["run-pit-stop"].textContent,
};
header.applyStatus({ sla_met: false });
const missed = {
  badgeHidden: ids["sla-badge"].hidden, disabled: ids["run-pit-stop"].disabled,
  busy: ids["run-pit-stop"].attrs["aria-busy"], label: ids["run-pit-stop"].textContent,
};

const modeEvents = [];
ids["mode-toggle"].addEventListener("pitwall:mode", (event) => modeEvents.push(event.detail.mode));
const initialMode = header.mode;
ready.live.click();
const afterLive = { mode: header.mode, pressed: pressed(ready) };
ready.demo.click();
const afterDemo = { mode: header.mode, pressed: pressed(ready) };

const loading = fakeDocument("loading");
globalThis.document = loading.doc;
delete globalThis.pitWall;
await import(url + "?loading");
const boundBeforeReady = "pitWall" in globalThis;
loading.doc.dispatchEvent({ type: "DOMContentLoaded" });
const boundAfterReady = window.pitWall?.header?.runButton === loading.ids["run-pit-stop"];

console.log(JSON.stringify({
  bootBound, met, missed, initialMode, afterLive, afterDemo, modeEvents, boundBeforeReady, boundAfterReady,
}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_header_bar_binds_dom_badge_toggle_and_run_button() -> None:
    assert NODE is not None
    result = subprocess.run(  # noqa: S603
        [NODE, "--input-type=module", "-e", _NODE_DOM_SCRIPT, APP_JS.as_uri()],
        check=True,
        capture_output=True,
        text=True,
    )

    out = json.loads(result.stdout)
    assert out["bootBound"] is True
    assert out["met"] == {
        "badgeHidden": False,
        "repo": "aj604/pit-crew",
        "sha": "0123456",
        "shaTitle": "0123456789abcdef",
        "median": "42.3",
        "p95": "118.0",
        "disabled": True,
        "busy": "true",
        "label": "Pit Stop Running",
    }
    assert out["missed"] == {"badgeHidden": True, "disabled": False, "busy": "false", "label": "Run Pit Stop"}
    assert out["initialMode"] == "demo"
    assert out["afterLive"] == {"mode": "live", "pressed": ["true", "false"]}
    assert out["afterDemo"] == {"mode": "demo", "pressed": ["false", "true"]}
    assert out["modeEvents"] == ["live", "demo"]
    assert out["boundBeforeReady"] is False
    assert out["boundAfterReady"] is True
