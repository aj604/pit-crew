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
