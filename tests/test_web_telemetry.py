import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pit_crew.ledger import InMemoryLedger
from pit_crew.server import DEFAULT_BRAND_DIR, create_app

WEB_DIR = Path(__file__).resolve().parents[1] / "src" / "pit_crew" / "web"
APP_JS = WEB_DIR / "app.js"
TELEMETRY_JS = WEB_DIR / "telemetry.js"
STYLES_CSS = WEB_DIR / "styles.css"
NODE = shutil.which("node")
AD_HOC_COLOR = re.compile(r"(?<![\w&])#[0-9a-fA-F]{3,8}\b(?![\w-])|\b(?:rgba?|hsla?)\(", re.IGNORECASE)


def _node(script: str, *args: str) -> dict[str, Any]:
    assert NODE is not None
    result = subprocess.run(  # noqa: S603
        [NODE, "--input-type=module", "-e", script, *args], check=True, capture_output=True, text=True, timeout=60
    )
    out: dict[str, Any] = json.loads(result.stdout)
    return out


def test_telemetry_asset_is_served() -> None:
    response = TestClient(create_app(InMemoryLedger())).get("/web/telemetry.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "createTelemetryCenter" in response.text


def test_telemetry_assets_use_only_defined_brand_tokens() -> None:
    telemetry_src, styles_src = TELEMETRY_JS.read_text(), STYLES_CSS.read_text()
    defined = set(re.findall(r"(--pc-[\w-]+)\s*:", (DEFAULT_BRAND_DIR / "tokens.css").read_text()))
    assert AD_HOC_COLOR.findall(telemetry_src) == []
    assert AD_HOC_COLOR.findall(styles_src) == []
    assert set(re.findall(r"var\((--pc-[\w-]+)\)", styles_src + telemetry_src)) <= defined


def test_telemetry_styles_enforce_brand_tokens_and_responsive_rules() -> None:
    css = STYLES_CSS.read_text()
    assert "/* === Telemetry Center (#50) === */" in css
    assert re.search(
        r"\.telemetry-summary__value\s*\{[^}]*var\(--pc-font-mono\)[^}]*font-variant-numeric:\s*tabular-nums", css
    )
    assert re.search(r"\.bug-status--caught\s*\{[^}]*var\(--pc-drs-green\)", css)
    assert re.search(r"\.bug-status--missed\s*\{[^}]*var\(--pc-f1-red\)", css)
    assert re.search(r"\.timing-chart__ref-line--sla-target\s*\{[^}]*var\(--pc-vegas-gold\)", css)
    assert re.search(r"\.timing-chart__cold-band\s*\{[^}]*var\(--pc-vegas-gold\)", css)
    telemetry_css = css.split("/* === Telemetry Center (#50) === */", maxsplit=1)[1]
    assert "@media (max-width: 640px)" in telemetry_css
    assert ".telemetry-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }" in telemetry_css


_PURE_SCRIPT = """
const t = await import(process.argv[1]);
const d = await import(process.argv[2]);
const models = Object.fromEntries(d.DEMO_SCENARIO_IDS.map((id) => {
  const snap = d.scenarioSnapshotAtStage(id, 4);
  const { evidence, verdict } = t.resolveAttemptTelemetry({ ...snap, selectedAttempt: snap.attempts[0] });
  const chart = t.buildTimingChartModel(evidence, verdict);
  const matrix = t.buildBugMatrixModel(evidence);
  return [id, {
    plot: chart.plot, yMax: chart.yMax, summary: chart.summary, runCount: chart.runCount,
    coldFlags: chart.runs.map((r) => r.isCold),
    runs: chart.runs,
    evidence: { baseline: evidence.baseline_seconds, candidate: evidence.candidate_seconds },
    refs: chart.referenceLines.map((r) => ({ id: r.id, label: r.label, value: r.value, y: r.y, labelY: r.labelY })),
    matrix: { total: matrix.total, caught: matrix.caught, missed: matrix.missed },
  }];
}));
const ciSnap = d.scenarioSnapshotAtStage("proven_keep", 3);
const ciChart = t.buildTimingChartModel(ciSnap.records.evidence[0], null);
const nullP = t.buildTimingChartModel(ciSnap.records.evidence[0], {
  keep: false, speedup: 0, p_value: null, median_seconds: 48.0, p95_seconds: 82.0, sla_met: true,
});
const multi = {
  selectedAttempt: { candidate_id: "c1" },
  records: {
    evidence: [{ candidate_id: "c1", baseline_seconds: [100], candidate_seconds: [50] },
               { candidate_id: "c2", baseline_seconds: [90], candidate_seconds: [80] }],
    verdicts: [{ candidate_id: "c1", keep: true, speedup: 2.0, p_value: 0.0004, median_seconds: 50, p95_seconds: 50 }],
  },
};
const unequalBaseLong = t.buildTimingChartModel({ baseline_seconds: [100, 90, 80], candidate_seconds: [50] }, null);
const unequalCandLong = t.buildTimingChartModel({ baseline_seconds: [95], candidate_seconds: [60, 50, 45] }, null);
const highN = t.buildTimingChartModel(
  { baseline_seconds: Array(30).fill(90), candidate_seconds: Array(30).fill(50) },
  null,
);
const proof = [
  { sha: "abcdef1234567890", status: "caught" },
  { commit_sha: "9876543210abcdef", status: "missed", baseline_failed: false, run_url: "javascript:alert(1)" },
  { kind: "mutation", ref: "app/tax.py", baseline_failed: true, caught: false, run_url: "http://ci.example/runs/7" },
  { kind: "mutation", ref: "app/stock.py", caught: true, run_url: "data:text/html,boom" },
].map(t.normalizeBugOutcome);
console.log(JSON.stringify({
  models, proof, unequalBaseLong, unequalCandLong, highN,
  medians: [t.computeMedian([5, 1, 9]), t.computeMedian([10, 20, 30, 40]), t.computeMedian([])],
  p95s: [t.computeP95([42]), t.computeP95([88, 48, 46, 58, 44]), t.computeP95([])],
  urls: [
    "https://github.com/a/b", "http://localhost:8000/r", "javascript:alert(1)",
    "data:text/html,x", "/rel", "https://]", "http://example.com:bad", null,
  ].map(t.isSafeExternalUrl),
  ciSummary: ciChart.summary, nullPValue: nullP.summary.pValue,
  empty: { hasData: t.buildTimingChartModel(null, null).hasData, summary: t.buildTimingChartModel(null, null).summary },
  resolved: [
    t.resolveAttemptTelemetry(multi).evidence.candidate_id,
    t.resolveAttemptTelemetry({ selectedAttempt: null, records: multi.records }).evidence.candidate_id,
    t.resolveAttemptTelemetry({ records: multi.records }).evidence.candidate_id,
  ],
  resolvedUnattempted: [
    t.resolveAttemptTelemetry({ selectedAttempt: { candidate_id: null, rated: false }, records: multi.records }),
    t.resolveAttemptTelemetry({ selectedAttempt: { rated: false }, records: multi.records }),
    t.resolveAttemptTelemetry({ selectedAttempt: { candidate_id: "   " }, records: multi.records }),
    t.resolveAttemptTelemetry({ selectedAttempt: { candidate_id: "missing" }, records: multi.records }),
  ],
  smallP: t.formatPValue(0.0004),
}));
"""


def _assert_bar_geometry(bar: dict[str, Any], plot: dict[str, float]) -> None:
    plot_bottom = plot["y"] + plot["height"]
    assert plot["y"] <= bar["y"] <= plot_bottom
    assert bar["height"] > 0
    assert round(bar["y"] + bar["height"]) == plot_bottom
    assert plot["x"] <= bar["x"]
    assert bar["x"] + bar["width"] <= plot["x"] + plot["width"]


def _assert_monotonic_bars(points: list[dict[str, Any]]) -> None:
    for a in points:
        for b in points:
            if a["seconds"] > b["seconds"]:
                assert a["y"] < b["y"]
                assert a["height"] > b["height"]


def _assert_scenario_model(
    m: dict[str, Any],
    spec: tuple[str, str, str, str, float, float, int, int],
) -> None:
    sp, pv, med, p95, base_med, cand_med, caught, missed = spec
    plot = m["plot"]
    plot_bottom = plot["y"] + plot["height"]
    assert (m["summary"]["speedup"], m["summary"]["pValue"], m["summary"]["median"], m["summary"]["p95"]) == (
        sp, pv, med, p95,
    )  # fmt: skip
    assert (m["runCount"], m["coldFlags"]) == (5, [True, False, False, False, False])

    points: list[dict[str, Any]] = []
    for idx, run in enumerate(m["runs"]):
        base, cand = run["baseline"], run["candidate"]
        assert (base["seconds"], cand["seconds"]) == (m["evidence"]["baseline"][idx], m["evidence"]["candidate"][idx])
        _assert_bar_geometry(base, plot)
        _assert_bar_geometry(cand, plot)
        assert base["x"] + base["width"] < cand["x"]
        points.extend([base, cand])
    _assert_monotonic_bars(points)

    refs = {r["id"]: r for r in m["refs"]}
    assert set(refs) == {"baseline-median", "candidate-median", "sla-target"}
    assert (refs["baseline-median"]["value"], refs["candidate-median"]["value"], refs["sla-target"]["value"]) == (
        base_med, cand_med, 60,
    )  # fmt: skip
    assert all(plot["y"] <= ref["y"] <= plot_bottom for ref in m["refs"])
    assert refs["baseline-median"]["y"] < refs["candidate-median"]["y"]
    assert refs["baseline-median"]["label"] == f"BASE {base_med:.1f}s"
    assert refs["candidate-median"]["label"] == f"CAND {cand_med:.1f}s"
    assert refs["sla-target"]["label"] == "SLA 60s"
    ys = sorted(r["labelY"] for r in m["refs"])
    assert all(ys[i + 1] - ys[i] >= 13.9 for i in range(len(ys) - 1))
    assert (m["matrix"]["total"], m["matrix"]["caught"], m["matrix"]["missed"]) == (4, caught, missed)


def _assert_edge_chart_models(out: dict[str, Any]) -> None:
    base_long = out["unequalBaseLong"]
    assert base_long["runCount"] == 3
    assert [r["baseline"]["seconds"] for r in base_long["runs"]] == [100, 90, 80]
    assert [r["candidate"]["seconds"] if r["candidate"] else None for r in base_long["runs"]] == [50, None, None]
    for r in base_long["runs"]:
        _assert_bar_geometry(r["baseline"], base_long["plot"])
        if r["candidate"] is not None:
            _assert_bar_geometry(r["candidate"], base_long["plot"])

    cand_long = out["unequalCandLong"]
    assert cand_long["runCount"] == 3
    assert [r["baseline"]["seconds"] if r["baseline"] else None for r in cand_long["runs"]] == [95, None, None]
    assert [r["candidate"]["seconds"] for r in cand_long["runs"]] == [60, 50, 45]
    for r in cand_long["runs"]:
        if r["baseline"] is not None:
            _assert_bar_geometry(r["baseline"], cand_long["plot"])
        _assert_bar_geometry(r["candidate"], cand_long["plot"])

    high_n = out["highN"]
    assert high_n["runCount"] == 30
    for idx, r in enumerate(high_n["runs"]):
        _assert_bar_geometry(r["baseline"], high_n["plot"])
        _assert_bar_geometry(r["candidate"], high_n["plot"])
        assert r["baseline"]["x"] + r["baseline"]["width"] < r["candidate"]["x"]
        if idx + 1 < len(high_n["runs"]):
            assert r["candidate"]["x"] + r["candidate"]["width"] < high_n["runs"][idx + 1]["baseline"]["x"]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_pure_chart_geometry_medians_and_bug_normalization() -> None:
    assert NODE is not None
    subprocess.run([NODE, "--check", str(TELEMETRY_JS)], check=True, capture_output=True)  # noqa: S603
    out = _node(_PURE_SCRIPT, TELEMETRY_JS.as_uri(), (WEB_DIR / "demo_scenarios.js").as_uri())

    assert out["medians"] == [5, 25, None]
    assert out["p95s"] == [42, 82.0, None]
    assert out["urls"] == [True, True, False, False, False, False, False, False]
    assert (out["smallP"], out["nullPValue"], out["resolved"]) == ("<0.001", "\u2014", ["c1", "c2", "c2"])
    assert out["resolvedUnattempted"] == [
        {"candidateId": None, "evidence": None, "verdict": None},
        {"candidateId": None, "evidence": None, "verdict": None},
        {"candidateId": None, "evidence": None, "verdict": None},
        {"candidateId": "missing", "evidence": None, "verdict": None},
    ]
    assert out["empty"]["hasData"] is False
    assert [out["empty"]["summary"][k] for k in ("speedup", "pValue", "median", "p95")] == ["\u2014"] * 4
    assert [out["ciSummary"][k] for k in ("speedup", "pValue", "median", "p95")] == [
        "1.85x",
        "\u2014",
        "48.0s",
        "82.0s",
    ]

    expected = {
        "proven_keep": ("1.85x", "0.004", "48.0s", "82.0s", 88.8, 48.0, 4, 0),
        "unsafe_reject": ("1.92x", "0.004", "62.5s", "72.4s", 120.0, 62.5, 3, 1),
        "noise_reject": ("1.03x", "0.24", "100.0s", "114.8s", 103.0, 100.0, 4, 0),
    }
    for sid, spec in expected.items():
        _assert_scenario_model(out["models"][sid], spec)
    _assert_edge_chart_models(out)

    p = out["proof"]
    assert (p[0]["kind"], p[0]["displayRef"], p[0]["baselineLabel"], p[0]["statusLabel"], p[0]["runUrl"]) == (
        "commit", "abcdef1", "\u2014", "CAUGHT", None,
    )  # fmt: skip
    assert (p[1]["displayRef"], p[1]["baselineLabel"], p[1]["statusLabel"], p[1]["runUrl"]) == (
        "9876543", "PASS", "MISSED", None,
    )  # fmt: skip
    assert (p[2]["displayRef"], p[2]["statusLabel"], p[2]["runUrl"]) == (
        "app/tax.py", "MISSED", "http://ci.example/runs/7",
    )  # fmt: skip
    assert (p[3]["statusLabel"], p[3]["runUrl"]) == ("CAUGHT", None)


_DOM_SCRIPT = """
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
    click() { if (!this.disabled) this.dispatchEvent({ type: "click" }); },
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
const inspect = (slot) => {
  const svg = walk(slot, (n) => n.tag === "svg")[0] ?? null;
  const metrics = Object.fromEntries(
    walk(slot, (n) => n.tag === "dd" && n.dataset.metric).map((n) => [n.dataset.metric, [n.textContent, n.className]]),
  );
  const rows = walk(slot, (n) => n.tag === "tr" && n.className.includes("bug-matrix__row")).map((tr) => {
    const link = walk(tr, (n) => n.tag === "a")[0] ?? null;
    const status = walk(tr, (n) => n.className.includes("bug-status"))[0];
    return {
      kind: tr.dataset.kind, ref: walk(tr, (n) => n.tag === "code")[0].textContent,
      base: walk(tr, (n) => n.className.includes("bug-baseline"))[0].textContent,
      status: status.textContent, statusClass: status.className,
      href: link?.attrs.href ?? null, target: link?.attrs.target ?? null, rel: link?.attrs.rel ?? null,
    };
  });
  const badge = walk(slot, (n) => n.className.includes("bug-matrix__summary"))[0];
  const bars = svg
    ? walk(svg, (n) => n.tag === "rect" && n.attrs["data-series"]).map((n) => ({
        series: n.attrs["data-series"], run: Number(n.attrs["data-run"]), seconds: Number(n.attrs["data-seconds"]),
        x: Number(n.attrs.x), y: Number(n.attrs.y), width: Number(n.attrs.width), height: Number(n.attrs.height),
        title: walk(n, (c) => c.tag === "title")[0]?.textContent ?? null,
      }))
    : [];
  const dotList = svg
    ? walk(svg, (n) => n.tag === "circle" && n.attrs["data-series"]).map((n) => ({
        series: n.attrs["data-series"], run: Number(n.attrs["data-run"]),
        cx: Number(n.attrs.cx), cy: Number(n.attrs.cy), r: Number(n.attrs.r),
      }))
    : [];
  const refLines = svg
    ? walk(svg, (n) => n.tag === "line" && n.attrs["data-ref-line"]).map((n) => ({
        id: n.attrs["data-ref-line"], value: Number(n.attrs["data-value"]),
        x1: Number(n.attrs.x1), x2: Number(n.attrs.x2), y1: Number(n.attrs.y1), y2: Number(n.attrs.y2),
      }))
    : [];
  const refLabels = svg
    ? walk(svg, (n) => n.tag === "text" && n.attrs["data-ref-label"]).map((n) => ({
        id: n.attrs["data-ref-label"], x: Number(n.attrs.x), y: Number(n.attrs.y), text: n.textContent,
      }))
    : [];
  const runGroups = svg
    ? walk(svg, (n) => n.tag === "g" && n.attrs["data-run"]).map((n) => Number(n.attrs["data-run"]))
    : [];
  return {
    panels: slot.kids.length, svgNs: svg?.ns ?? null,
    runGroups, bars, dotList,
    baseBars: bars.filter((b) => b.series === "baseline").length,
    candBars: bars.filter((b) => b.series === "candidate").length,
    dots: dotList.length,
    cold: svg ? walk(svg, (n) => n.attrs["data-cold-run"] === "true").length : 0,
    refs: refLines.map((r) => r.id),
    refLines, refLabels,
    metrics, rows, badge: [badge?.textContent ?? null, badge?.dataset.state ?? null],
    empties: walk(slot, (n) => n.className.includes("telemetry-panel__empty")).length,
  };
};
const app = await import(process.argv[1]);
const tel = await import(process.argv[2]);
const demo = await import(process.argv[3]);
const d = fakeDoc();
const pw = app.bootPitWall(d.doc, "");
const slot = d.ids["telemetry-center"];
const modelFor = (id) => {
  const snap = demo.scenarioSnapshotAtStage(id, 4);
  const { evidence, verdict } = tel.resolveAttemptTelemetry({ ...snap, selectedAttempt: snap.attempts[0] });
  const chart = tel.buildTimingChartModel(evidence, verdict);
  return {
    plot: chart.plot, refs: chart.referenceLines,
    baseline: evidence.baseline_seconds, candidate: evidence.candidate_seconds,
  };
};
const proven = inspect(slot);
const provenModel = modelFor("proven_keep");
pw.store.selectScenario("unsafe_reject"); const unsafe = inspect(slot); const unsafeModel = modelFor("unsafe_reject");
pw.store.selectScenario("noise_reject"); const noise = inspect(slot); const noiseModel = modelFor("noise_reject");
pw.store.selectScenario("proven_keep");
const early = [0, 1, 2].map((i) => { pw.store.setStage(i); return inspect(slot); });
pw.store.setStage(3); const ci = inspect(slot);
pw.store.setMode("live"); const liveEmpty = inspect(slot);
pw.store.setLiveData({
  attempts: [
    { candidate_id: "a", rated: true },
    { candidate_id: "b", rated: false },
    { candidate_id: null, rated: false },
  ],
  records: {
    evidence: [
      {
        candidate_id: "a", baseline_seconds: [100, 95], candidate_seconds: [55, 50], bugs_total: 1, bugs_caught: 0,
        bug_outcomes: [{
          kind: "commit", ref: "1234567890abcdef", baseline_failed: true, caught: false, run_url: "javascript:alert(1)",
        }],
      },
      {
        candidate_id: "b", baseline_seconds: [100, 95, 90], candidate_seconds: [55],
        bugs_total: 0, bugs_caught: 0, bug_outcomes: [],
      },
    ],
    verdicts: [{
      candidate_id: "a", keep: false, speedup: 1.86, p_value: null,
      median_seconds: 52.5, p95_seconds: 54.8, sla_met: true,
    }],
  },
});
const liveAutoUnattempted = inspect(slot);
pw.store.selectAttempt(1); const liveB = inspect(slot);
pw.store.selectAttempt(0); const liveA = inspect(slot);
pw.store.selectAttempt(2); const liveBackToUnattempted = inspect(slot);
console.log(JSON.stringify({
  handle: Boolean(pw.telemetry), nullSlot: tel.createTelemetryCenter(d.doc, null, pw.store),
  proven, provenModel, unsafe, unsafeModel, noise, noiseModel, early, ci, liveEmpty,
  liveAutoUnattempted, liveB, liveA, liveBackToUnattempted,
}));
"""


def _assert_rendered_svg_geometry(snap: dict[str, Any], model: dict[str, Any]) -> None:
    plot = model["plot"]
    plot_bottom = plot["y"] + plot["height"]
    base_bars = [b for b in snap["bars"] if b["series"] == "baseline"]
    cand_bars = [b for b in snap["bars"] if b["series"] == "candidate"]
    assert [b["seconds"] for b in base_bars] == model["baseline"]
    assert [b["seconds"] for b in cand_bars] == model["candidate"]
    assert [b["run"] for b in base_bars] == list(range(1, len(model["baseline"]) + 1))
    assert [b["run"] for b in cand_bars] == list(range(1, len(model["candidate"]) + 1))

    dots_by_key = {(d["series"], d["run"]): d for d in snap["dotList"]}
    assert len(dots_by_key) == len(snap["bars"])
    for bar in snap["bars"]:
        _assert_bar_geometry(bar, plot)
        dot = dots_by_key[(bar["series"], bar["run"])]
        assert dot["cy"] == bar["y"]
        assert abs(dot["cx"] - (bar["x"] + bar["width"] / 2)) <= 0.02
        assert bar["series"] in (bar["title"] or "")

    for a in snap["bars"]:
        for b in snap["bars"]:
            if a["seconds"] > b["seconds"]:
                assert a["y"] < b["y"]
                assert a["height"] > b["height"]

    model_refs = {r["id"]: r for r in model["refs"]}
    dom_refs = {r["id"]: r for r in snap["refLines"]}
    dom_labels = {lbl["id"]: lbl for lbl in snap["refLabels"]}
    assert set(dom_refs) == set(model_refs) == set(dom_labels)
    for ref_id, mref in model_refs.items():
        line = dom_refs[ref_id]
        label = dom_labels[ref_id]
        assert line["x1"] == plot["x"]
        assert line["x2"] == plot["x"] + plot["width"]
        assert line["y1"] == line["y2"] == mref["y"]
        assert plot["y"] <= line["y1"] <= plot_bottom
        assert label["text"] == mref["label"]
        assert label["x"] > plot["x"] + plot["width"]
    assert dom_refs["baseline-median"]["y1"] < dom_refs["candidate-median"]["y1"]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_telemetry_center_dom_across_demo_scenarios_stages_and_live_mode() -> None:
    out = _node(_DOM_SCRIPT, APP_JS.as_uri(), TELEMETRY_JS.as_uri(), (WEB_DIR / "demo_scenarios.js").as_uri())
    assert (out["handle"], out["nullSlot"]) == (True, None)

    proven = out["proven"]
    assert (proven["panels"], proven["svgNs"], proven["baseBars"], proven["candBars"]) == (
        2, "http://www.w3.org/2000/svg", 5, 5,
    )  # fmt: skip
    assert (proven["dots"], proven["cold"]) == (10, 1)
    assert set(proven["refs"]) == {"baseline-median", "candidate-median", "sla-target"}
    _assert_rendered_svg_geometry(proven, out["provenModel"])
    assert {k: v[0] for k, v in proven["metrics"].items()} == {
        "speedup": "1.85x", "p_value": "0.004", "median": "48.0s", "p95": "82.0s",
    }  # fmt: skip
    assert all("telemetry" in v[1] for v in proven["metrics"].values())
    assert proven["badge"] == ["4/4 CAUGHT", "caught"]
    assert [r["kind"] for r in proven["rows"]] == ["commit", "commit", "mutation", "mutation"]
    assert (len(proven["rows"][0]["ref"]), proven["rows"][2]["ref"]) == (7, "app/tax.py")
    assert all(
        r["base"] == "FAIL"
        and r["status"] == "CAUGHT"
        and "bug-status--caught" in r["statusClass"]
        and r["target"] == "_blank"
        and r["rel"] == "noopener noreferrer"
        and r["href"].startswith("https://")
        for r in proven["rows"]
    )

    unsafe = out["unsafe"]
    _assert_rendered_svg_geometry(unsafe, out["unsafeModel"])
    assert {k: v[0] for k, v in unsafe["metrics"].items()} == {
        "speedup": "1.92x", "p_value": "0.004", "median": "62.5s", "p95": "72.4s",
    }  # fmt: skip
    assert unsafe["badge"] == ["3/4 CAUGHT \u00b7 1 MISSED", "missed"]
    missed = [r for r in unsafe["rows"] if r["status"] == "MISSED"]
    assert len(missed) == 1
    assert missed[0]["ref"] == "app/tax.py"
    assert "bug-status--missed" in missed[0]["statusClass"]

    _assert_rendered_svg_geometry(out["noise"], out["noiseModel"])
    assert {k: v[0] for k, v in out["noise"]["metrics"].items()} == {
        "speedup": "1.03x", "p_value": "0.24", "median": "100.0s", "p95": "114.8s",
    }  # fmt: skip
    assert out["noise"]["badge"] == ["4/4 CAUGHT", "caught"]

    for snap in out["early"]:
        assert (snap["svgNs"], snap["empties"]) == (None, 2)
        assert all(v[0] == "\u2014" for v in snap["metrics"].values())

    ci, live_a, live_b = out["ci"], out["liveA"], out["liveB"]
    assert (ci["svgNs"], ci["metrics"]["median"][0], ci["metrics"]["p_value"][0], len(ci["rows"])) == (
        "http://www.w3.org/2000/svg", "48.0s", "\u2014", 4,
    )  # fmt: skip
    assert out["liveEmpty"]["empties"] == 2
    for unattempted in (out["liveAutoUnattempted"], out["liveBackToUnattempted"]):
        assert (unattempted["svgNs"], unattempted["empties"], unattempted["rows"], unattempted["badge"]) == (
            None, 2, [], ["Awaiting CI", "pending"],
        )  # fmt: skip
        assert all(v[0] == "\u2014" for v in unattempted["metrics"].values())

    assert (live_b["runGroups"], live_b["baseBars"], live_b["candBars"], live_b["dots"]) == ([1, 2, 3], 3, 1, 4)
    assert [(b["series"], b["run"], b["seconds"]) for b in live_b["bars"]] == [
        ("baseline", 1, 100),
        ("candidate", 1, 55),
        ("baseline", 2, 95),
        ("baseline", 3, 90),
    ]
    assert [(d["series"], d["run"]) for d in live_b["dotList"]] == [
        ("baseline", 1),
        ("candidate", 1),
        ("baseline", 2),
        ("baseline", 3),
    ]
    for bar in live_b["bars"]:
        _assert_bar_geometry(bar, out["provenModel"]["plot"])
    assert (live_b["rows"], live_b["empties"]) == ([], 1)

    assert (live_a["baseBars"], live_a["candBars"], live_a["metrics"]["p_value"][0], live_a["rows"][0]["href"]) == (
        2, 2, "\u2014", None,
    )  # fmt: skip
    assert live_a["rows"][0]["status"] == "MISSED"
