import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pit_crew.contracts import BugCase, Candidate, Evidence, Hypothesis, ReplayPlan, Verdict
from pit_crew.ledger import InMemoryLedger
from pit_crew.rater import rate
from pit_crew.server import DEFAULT_BRAND_DIR, DEFAULT_WEB_DIR, create_app

WEB_DIR = Path(__file__).resolve().parents[1] / "src" / "pit_crew" / "web"
APP_JS = WEB_DIR / "app.js"
DEMO_JS = WEB_DIR / "demo_scenarios.js"
STORE_JS = WEB_DIR / "store.js"
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
        ("/web/demo_scenarios.js", "javascript"),
        ("/web/store.js", "javascript"),
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


@pytest.mark.parametrize("name", ["styles.css", "index.html", "app.js", "demo_scenarios.js", "store.js"])
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
_FAKE_DOM = """
const fakeNode = (extra = {}) => {
  const listeners = {};
  return {
    hidden: false, textContent: "", title: "", value: "", disabled: false, dataset: {}, attrs: {}, kids: [],
    setAttribute(name, value) { this.attrs[name] = value; },
    addEventListener(type, fn) { (listeners[type] ??= []).push(fn); },
    dispatchEvent(event) { for (const fn of listeners[event.type] ?? []) fn(event); return true; },
    replaceChildren(...kids) { this.kids = kids; },
    append(...kids) { this.kids.push(...kids); },
    querySelectorAll(selector) { return selector === "[data-mode]" ? (this.modeButtons ?? []) : []; },
    click() { if (!this.disabled) this.dispatchEvent({ type: "click" }); },
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
    "demo-switcher": fakeNode(),
  };
  const doc = fakeNode({ readyState, getElementById: (id) => ids[id] ?? null, createElement: () => fakeNode() });
  return { doc, ids, live, demo };
};
const pressed = (d) => [d.live.attrs["aria-pressed"], d.demo.attrs["aria-pressed"]];
"""

_NODE_DOM_SCRIPT = (
    _FAKE_DOM
    + """
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
)


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


def _node(script: str, *args: str) -> dict[str, Any]:
    assert NODE is not None
    result = subprocess.run(  # noqa: S603
        [NODE, "--input-type=module", "-e", script, *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    out: dict[str, Any] = json.loads(result.stdout)
    return out


_DEMO_DUMP_SCRIPT = """
const d = await import(process.argv[1]);
const fails = (fn) => { try { fn(); return null; } catch (error) { return error.message; } };
console.log(JSON.stringify({
  ids: d.DEMO_SCENARIO_IDS, stages: d.DEMO_STAGES, labels: d.DEMO_STAGE_LABELS,
  titles: d.DEMO_SCENARIO_IDS.map((id) => d.getScenario(id).title),
  snapshots: Object.fromEntries(d.DEMO_SCENARIO_IDS.map((id) => [id, d.DEMO_STAGES.map((_, i) =>
    d.scenarioSnapshotAtStage(id, i))])),
  errors: [fails(() => d.getScenario("nope")), fails(() => d.scenarioSnapshotAtStage("proven_keep", 5)),
    fails(() => d.scenarioSnapshotAtStage("proven_keep", -1))],
}));
"""

# scenario -> (keep, speedup, p_value, bugs_caught, median, p95, sla_met)
DEMO_TARGETS = {
    "unsafe_reject": (False, 1.92, 0.004, 3, 62.5, 72.4, False),
    "noise_reject": (False, 1.03, 0.24, 4, 100.0, 114.8, False),
    "proven_keep": (True, 1.85, 0.004, 4, 48.0, 82.0, True),
}


@pytest.fixture(scope="module")
def demo_dump() -> dict[str, Any]:
    if NODE is None:
        pytest.skip("node is not installed")
    return _node(_DEMO_DUMP_SCRIPT, DEMO_JS.as_uri())


def _ledger_from_records(records: dict[str, list[dict[str, Any]]]) -> InMemoryLedger:
    ledger = InMemoryLedger()
    for h in records["hypotheses"]:
        ledger.add(Hypothesis(**h))
    for c in records["candidates"]:
        ledger.add(Candidate(**{**c, "changed_files": tuple(c["changed_files"])}))
    for p in records["replay_plans"]:
        cases = tuple(BugCase(**{**case, "expected_checks": tuple(case["expected_checks"])}) for case in p["cases"])
        ledger.add(ReplayPlan(p["candidate_id"], tuple(p["bug_shas"]), id=p["id"], cases=cases))
    for e in records["evidence"]:
        ledger.add(_evidence(e))
    for v in records["verdicts"]:
        ledger.add(Verdict(**v))
    return ledger


def _evidence(e: dict[str, Any]) -> Evidence:
    tuples = ("baseline_seconds", "candidate_seconds", "run_urls", "bug_outcomes")
    return Evidence(**{**e, **{key: tuple(e[key]) for key in tuples}})


def test_demo_scenarios_list_stages_and_reject_unknowns(demo_dump: dict[str, Any]) -> None:
    assert demo_dump["ids"] == ["unsafe_reject", "noise_reject", "proven_keep"]
    assert demo_dump["stages"] == ["analysis", "optimizer", "proof", "ci", "rater"]
    assert demo_dump["labels"] == ["Analysis", "Optimizer", "Proof", "CI", "Rater"]
    assert demo_dump["titles"] == ["Fast but Unsafe", "Within Noise", "Proven Speedup"]
    assert demo_dump["errors"] == [
        "Unknown demo scenario: nope",
        "Demo stage index out of range: 5",
        "Demo stage index out of range: -1",
    ]


@pytest.mark.parametrize("scenario", list(DEMO_TARGETS))
def test_demo_scenario_hits_targets_and_matches_rater(demo_dump: dict[str, Any], scenario: str) -> None:
    keep, speedup, p_value, caught, median, p95, sla_met = DEMO_TARGETS[scenario]
    records = demo_dump["snapshots"][scenario][-1]["records"]
    verdict, evidence = records["verdicts"][0], records["evidence"][0]

    assert (verdict["keep"], verdict["speedup"], verdict["p_value"]) == (keep, speedup, p_value)
    assert (verdict["median_seconds"], verdict["p95_seconds"], verdict["sla_met"]) == (median, p95, sla_met)
    assert (evidence["bugs_caught"], evidence["bugs_total"]) == (caught, 4)
    assert len(evidence["baseline_seconds"]) >= 5
    assert len(evidence["candidate_seconds"]) >= 5
    assert [o["caught"] for o in evidence["bug_outcomes"]].count(False) == 4 - caught
    assert {o["kind"] for o in evidence["bug_outcomes"]} == {"commit", "mutation"}
    assert all(set(o) == {"kind", "ref", "baseline_failed", "caught", "run_url"} for o in evidence["bug_outcomes"])
    assert all(content.startswith("name: CI") for content in records["candidates"][0]["files"].values())

    rated = rate(_evidence(evidence))
    assert (rated.median_seconds, rated.sla_met) == (median, sla_met)
    assert rated.p95_seconds == pytest.approx(p95)
    if caught < 4:
        # rater short-circuits on a missed bug; the demo shows the speed it would have measured.
        assert (rated.keep, rated.reason) == (False, f"caught {caught}/4 bugs")
        rated = rate(_evidence({**evidence, "bugs_caught": 4}))
    else:
        assert (rated.keep, rated.reason) == (keep, verdict["reason"])
    assert round(rated.speedup, 2) == speedup
    assert rated.p_value is not None
    assert round(rated.p_value, 3 if p_value < 0.01 else 2) == p_value


@pytest.mark.parametrize("scenario", list(DEMO_TARGETS))
def test_demo_snapshots_match_live_api_shapes(demo_dump: dict[str, Any], scenario: str) -> None:
    stages = demo_dump["snapshots"][scenario]
    base = stages[0]["status"]["base_sha"]

    for index, snap in enumerate(stages):
        client = TestClient(create_app(_ledger_from_records(snap["records"])))
        api = "/api/repos/pit-crew-demo/checkout-api"

        assert client.get(f"{api}/records").json() == snap["records"]
        assert client.get(f"{api}/attempts").json() == snap["attempts"]
        assert client.get(f"{api}/status").json() == {**snap["status"], "active": False}
        assert snap["status"]["active"] is (index < 4)
        counts = [len(snap["records"][k]) for k in ("candidates", "replay_plans", "evidence", "verdicts")]
        assert counts == [int(index >= n) for n in range(1, 5)]
        if index < 4:
            assert (snap["status"]["base_sha"], snap["status"]["latest_verdict"]) == (base, None)

    final = stages[-1]["status"]
    head = stages[-1]["records"]["candidates"][0]["head_sha"]
    assert final["base_sha"] == (head if DEMO_TARGETS[scenario][0] else base)


_STORE_SCRIPT = """
const s = await import(process.argv[1]);
const timers = [];
const timer = {
  setInterval(fn, ms) { timers.push({ fn, ms, cleared: false }); return timers.length - 1; },
  clearInterval(id) { timers[id].cleared = true; },
};
const pick = (st) => [st.mode, st.scenarioId, st.stageIndex, st.stage, st.isPlaying];
const fails = (fn) => { try { fn(); return null; } catch (error) { return error.message; } };
const boots = ["", "?repo=owner/name", "?repo=owner/name&mode=demo", "?mode=demo", "?repo=bad", "?mode=live"]
  .map(s.resolveInitialBoot);
const store = s.createPitWallStore({ timer, intervalMs: 500 });
const seen = [];
const unsubscribe = store.subscribe((st) => seen.push(st.stage));
const log = { initial: pick(store.getState()), keys: Object.keys(store.getState()).sort() };
const init = store.getState();
log.initialData = [init.status.sla_met, init.attempts.length, init.records.verdicts.length,
  init.selectedAttemptIndex, init.selectedAttempt.candidate_id === init.records.candidates[0].id];
store.reset(); log.reset = pick(store.getState());
store.step(); store.step(); log.stepped = pick(store.getState());
store.setStage(3); store.step(); store.step(); log.capped = pick(store.getState());
store.play(); log.play = [...pick(store.getState()), timers.length, timers[0].ms];
store.play(); log.playTwice = timers.length;
timers[0].fn(); timers[0].fn(); log.ticked = pick(store.getState());
timers[0].fn(); timers[0].fn(); log.finished = [...pick(store.getState()), timers[0].cleared];
store.play(); store.pause(); log.paused = [...pick(store.getState()), timers[1].cleared];
store.selectScenario("unsafe_reject"); log.scenario = [...pick(store.getState()),
  store.getState().status.latest_verdict.speedup];
store.play(); store.selectScenario("noise_reject"); log.scenarioStops = [store.getState().isPlaying, timers[2].cleared];
store.selectAttempt(0); log.selected = store.getState().selectedAttemptIndex;
log.errors = [fails(() => store.selectAttempt(1)), fails(() => store.selectScenario("x")),
  fails(() => store.setStage(5)), fails(() => store.setMode("x"))];
store.play(); store.setMode("live"); const live = store.getState();
log.live = [...pick(live), timers[3].cleared, live.status, live.attempts, live.selectedAttempt, live.records];
store.step(); store.reset(); store.play();
log.liveIgnores = [live.stageIndex === store.getState().stageIndex, timers.length];
store.setRepo("owner/name"); store.setFetchError("boom"); log.fetchError = store.getState().fetchError;
store.setLiveData({ status: { repo: "owner/name" }, attempts: [{ id: 1 }, { id: 2 }] });
const fed = store.getState();
log.fed = [fed.repo, fed.status, fed.attempts.length, fed.selectedAttemptIndex, fed.selectedAttempt, fed.fetchError,
  fed.records.verdicts];
store.selectAttempt(0); log.feedSelect = store.getState().selectedAttempt;
store.selectAttempt(1); store.setLiveData({ attempts: [{ id: 3 }] });
log.clamped = store.getState().selectedAttemptIndex;
store.setFetchError(); log.clearedError = store.getState().fetchError;
store.setMode("demo"); log.backToDemo = [...pick(store.getState()), store.getState().attempts.length];
store.play(); store.step(); store.reset();
log.resetStops = [...pick(store.getState()), timers.at(-1).cleared];
store.step(); store.step(); store.play();
log.resumeMid = pick(store.getState());
timers.at(-1).fn(); log.resumeTick = pick(store.getState());
store.setStage(1); log.stageWhilePlaying = [...pick(store.getState()), timers.at(-1).cleared];
store.setStage(4); log.stageEndStops = [...pick(store.getState()), timers.at(-1).cleared];
unsubscribe(); const before = seen.length; store.reset(); log.unsubscribed = seen.length === before;
log.seenStart = seen.slice(0, 4);
console.log(JSON.stringify({ boots, log }));
"""


STATE_KEYS = (
    "mode repo scenarioId stageIndex stage isPlaying status attempts records selectedAttemptIndex selectedAttempt"
    " fetchError"
)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_boot_and_store_transitions() -> None:
    out = _node(_STORE_SCRIPT, STORE_JS.as_uri())
    demo = {"mode": "demo", "repo": "", "scenarioId": "proven_keep"}
    live = {"mode": "live", "repo": "owner/name", "scenarioId": "proven_keep"}
    assert out["boots"] == [demo, live, {**demo, "repo": "owner/name"}, demo, demo, demo]

    log = out["log"]
    assert log["initial"] == ["demo", "proven_keep", 4, "rater", False]
    assert log["keys"] == sorted(STATE_KEYS.split())
    assert log["initialData"] == [True, 1, 1, 0, True]
    assert log["reset"] == ["demo", "proven_keep", 0, "analysis", False]
    assert log["stepped"] == ["demo", "proven_keep", 2, "proof", False]
    assert log["capped"] == ["demo", "proven_keep", 4, "rater", False]
    assert log["play"] == ["demo", "proven_keep", 0, "analysis", True, 1, 500]
    assert log["playTwice"] == 1
    assert log["ticked"] == ["demo", "proven_keep", 2, "proof", True]
    assert log["finished"] == ["demo", "proven_keep", 4, "rater", False, True]
    assert log["paused"] == ["demo", "proven_keep", 0, "analysis", False, True]
    assert log["scenario"] == ["demo", "unsafe_reject", 4, "rater", False, 1.92]
    assert log["scenarioStops"] == [False, True]
    assert log["selected"] == 0
    assert log["errors"] == [
        "Attempt index out of range: 1",
        "Unknown demo scenario: x",
        "Demo stage index out of range: 5",
        "Unknown Pit Wall mode: x",
    ]
    empty_records: dict[str, list[Any]] = {
        k: [] for k in ("hypotheses", "candidates", "replay_plans", "evidence", "verdicts")
    }
    assert log["live"] == ["live", "noise_reject", 0, "analysis", False, True, None, [], None, empty_records]
    assert log["liveIgnores"] == [True, 4]
    assert log["fetchError"] == "boom"
    assert log["fed"] == ["owner/name", {"repo": "owner/name"}, 2, 1, {"id": 2}, None, []]
    assert log["feedSelect"] == {"id": 1}
    assert log["clamped"] == 0
    assert log["clearedError"] is None
    assert log["backToDemo"] == ["demo", "noise_reject", 0, "analysis", False, 1]
    assert log["resetStops"] == ["demo", "noise_reject", 0, "analysis", False, True]
    assert log["resumeMid"] == ["demo", "noise_reject", 2, "proof", True]
    assert log["resumeTick"] == ["demo", "noise_reject", 3, "ci", True]
    assert log["stageWhilePlaying"] == ["demo", "noise_reject", 1, "optimizer", True, False]
    assert log["stageEndStops"] == ["demo", "noise_reject", 4, "rater", False, True]
    assert log["unsubscribed"] is True
    assert log["seenStart"] == ["analysis", "optimizer", "proof", "ci"]


_SWITCHER_SCRIPT = (
    _FAKE_DOM
    + """
const m = await import(process.argv[1]);
const view = (d, pw) => ({
  hdr: [d.ids["base-sha-value"].textContent, d.ids["sla-median"].textContent, d.ids["sla-p95"].textContent,
    d.ids["sla-badge"].hidden, d.ids["run-pit-stop"].disabled, d.ids["run-pit-stop"].attrs["aria-busy"],
    d.ids["run-pit-stop"].textContent],
  ui: [d.ids["demo-switcher"].hidden, pw.store.getState().stage,
    pw.switcher.stageItems.map((n) => `${n.dataset.state}:${n.attrs["aria-current"]}`).join(","),
    pw.switcher.scenarioButtons.filter((n) => n.attrs["aria-pressed"] === "true").map((n) => n.textContent).join(),
    pw.switcher.playButton.textContent, pw.switcher.playButton.attrs["aria-pressed"],
    pw.switcher.stepButton.disabled, pw.switcher.resetButton.disabled],
  repo: d.ids["repo-input"].value,
});
const d = fakeDocument("complete");
const pw = m.bootPitWall(d.doc, "");
const sw = pw.switcher;
const out = { boot: view(d, pw), labels: sw.stageItems.map((n) => n.textContent),
  slotKids: d.ids["demo-switcher"].kids.length };
sw.resetButton.click(); out.analysis = view(d, pw);
d.ids["run-pit-stop"].click();
out.runAfterReset = [pw.store.getState().stage, pw.store.getState().isPlaying, d.ids["run-pit-stop"].disabled];
sw.playButton.click();
const walk = [];
for (let i = 0; i < 4; i += 1) { sw.stepButton.click(); walk.push(view(d, pw)); }
out.walk = walk;
sw.resetButton.click(); sw.stepButton.click();
d.ids["run-pit-stop"].click(); out.runMidStage = [pw.store.getState().stage, pw.store.getState().isPlaying];
sw.playButton.click();
sw.scenarioButtons[0].click(); out.unsafe = view(d, pw);
sw.playButton.click(); out.playing = view(d, pw);
sw.playButton.click(); out.paused = view(d, pw);
d.ids["run-pit-stop"].click(); out.runPlays = pw.store.getState().isPlaying; pw.store.pause();
d.live.click(); out.live = view(d, pw);
d.ids["repo-input"].value = "my-org/my-repo";
d.demo.click(); out.demo = view(d, pw);
d.live.click(); out.liveRestored = d.ids["repo-input"].value;
const l = fakeDocument("complete");
const lpw = m.bootPitWall(l.doc, "?repo=owner/name");
out.liveBoot = { ...view(l, lpw), mode: lpw.store.getState().mode, pressed: pressed(l) };
l.ids["run-pit-stop"].click(); out.liveRunIgnored = lpw.store.getState().isPlaying;
l.ids["repo-input"].value = "draft/repo";
let repoWrites = 0;
const origSetRepo = lpw.header.setRepo;
lpw.header.setRepo = (v) => { repoWrites += 1; origSetRepo(v); };
lpw.store.setLiveData({
  status: { repo: "owner/name", active: true, base_sha: "abcdef012345" }, attempts: [{ id: 1 }],
});
out.liveActive = { hdr: view(l, lpw).hdr, repo: l.ids["repo-input"].value, writes: repoWrites };
lpw.store.setFetchError("offline"); lpw.store.selectAttempt(0);
out.liveUnchanged = [l.ids["repo-input"].value, repoWrites];
lpw.store.setRepo("next/repo");
out.liveSetRepo = [l.ids["repo-input"].value, repoWrites];
l.ids["repo-input"].value = "";
l.demo.click(); const demoRepo = l.ids["repo-input"].value;
l.live.click(); out.liveBlank = [demoRepo, l.ids["repo-input"].value];
l.demo.click(); lpw.store.setRepo("from-demo/repo");
l.live.click(); out.liveFromDemoSetRepo = l.ids["repo-input"].value;
console.log(JSON.stringify(out));
"""
)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_demo_switcher_drives_header_through_stages() -> None:
    out = _node(_SWITCHER_SCRIPT, APP_JS.as_uri())
    idle = [False, "false", "Run Pit Stop"]
    running = [True, "true", "Pit Stop Running"]
    kept = ["e9b17d3", "48.0", "82.0", False, *idle]
    pending = ["4f1c9e2", "--", "--", True, *idle]
    rater = "done:false,done:false,done:false,done:false,current:step"
    analysis = "current:step,pending:false,pending:false,pending:false,pending:false"
    assert out["boot"]["hdr"] == kept
    assert out["boot"]["ui"] == [False, "rater", rater, "Proven Speedup", "Play", "false", True, False]
    assert out["boot"]["repo"] == "pit-crew-demo/checkout-api"
    assert out["labels"] == ["Analysis", "Optimizer", "Proof", "CI", "Rater"]
    assert out["slotKids"] == 4
    assert out["analysis"]["hdr"] == pending
    assert out["analysis"]["ui"] == [False, "analysis", analysis, "Proven Speedup", "Play", "false", False, True]
    assert out["runAfterReset"] == ["analysis", True, True]
    assert [w["ui"][1] for w in out["walk"]] == ["optimizer", "proof", "ci", "rater"]
    assert [w["ui"][2].count("done:false") for w in out["walk"]] == [1, 2, 3, 4]
    assert [w["hdr"] for w in out["walk"]] == [pending, pending, pending, kept]
    assert out["runMidStage"] == ["optimizer", True]
    assert out["unsafe"]["ui"][3] == "Fast but Unsafe"
    assert out["unsafe"]["hdr"] == ["4f1c9e2", "62.5", "72.4", True, *idle]
    assert out["playing"]["ui"][1:] == [
        "analysis", analysis, "Fast but Unsafe", "Pause", "true", False, False
    ]  # fmt: skip
    assert out["playing"]["hdr"] == ["4f1c9e2", "--", "--", True, *running]
    assert out["paused"]["ui"][4:6] == ["Play", "false"]
    assert out["paused"]["hdr"] == pending
    assert out["runPlays"] is True
    assert (out["live"]["ui"][0], out["live"]["hdr"], out["live"]["repo"]) == (
        True,
        ["-------", "--", "--", True, *idle],
        "",
    )
    assert (out["demo"]["ui"][0], out["demo"]["ui"][3], out["demo"]["repo"]) == (
        False,
        "Fast but Unsafe",
        "pit-crew-demo/checkout-api",
    )
    assert out["liveRestored"] == "my-org/my-repo"
    assert (out["liveBoot"]["mode"], out["liveBoot"]["ui"][0], out["liveBoot"]["repo"]) == ("live", True, "owner/name")
    assert out["liveBoot"]["pressed"] == ["true", "false"]
    assert out["liveRunIgnored"] is False
    assert out["liveActive"] == {"hdr": ["abcdef0", "--", "--", True, *running], "repo": "draft/repo", "writes": 0}
    assert out["liveUnchanged"] == ["draft/repo", 0]
    assert out["liveSetRepo"] == ["next/repo", 1]
    assert out["liveBlank"] == ["pit-crew-demo/checkout-api", ""]
    assert out["liveFromDemoSetRepo"] == "from-demo/repo"
    assert '<section id="demo-switcher" class="pit-wall__slot demo-switcher"' in (WEB_DIR / "index.html").read_text()
