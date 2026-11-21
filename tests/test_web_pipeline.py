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
APP_JS, PIPELINE_JS, STYLES_CSS = WEB_DIR / "app.js", WEB_DIR / "pipeline.js", WEB_DIR / "styles.css"
NODE = shutil.which("node")
AD_HOC_COLOR = re.compile(r"(?<![\w&])#[0-9a-fA-F]{3,8}\b(?![\w-])|\b(?:rgba?|hsla?)\(", re.IGNORECASE)


def _node(script: str, *args: str) -> dict[str, Any]:
    assert NODE is not None
    res = subprocess.run(  # noqa: S603
        [NODE, "--input-type=module", "-e", script, *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    out: dict[str, Any] = json.loads(res.stdout)
    return out


def test_pipeline_js_is_served_and_has_no_ad_hoc_colors() -> None:
    client = TestClient(create_app(InMemoryLedger()))
    response = client.get("/web/pipeline.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert AD_HOC_COLOR.findall(PIPELINE_JS.read_text()) == []


def test_pipeline_styles_use_brand_tokens_compound_rings_and_file_viewer() -> None:
    css = STYLES_CSS.read_text()
    section = css[css.index("/* === Sector Pipeline Strip (#49) === */") :]
    defined = set(re.findall(r"(--pc-[\w-]+)\s*:", (DEFAULT_BRAND_DIR / "tokens.css").read_text()))
    used = set(re.findall(r"var\((--pc-[\w-]+)\)", section))

    assert used <= defined
    assert AD_HOC_COLOR.findall(section) == []
    required_tokens = (
        "--pc-sphere-cyan", "--pc-sector-purple", "--pc-vegas-gold", "--pc-vegas-magenta",
        "--pc-drs-green", "--pc-f1-red", "--pc-compound-soft", "--pc-compound-medium",
        "--pc-compound-hard", "--pc-compound-hypersoft", "--pc-compound-inter",
        "--pc-ink-muted", "--pc-font-mono", "--pc-bg-void",
    )  # fmt: skip
    for token in required_tokens:
        assert f"var({token})" in section
    assert re.search(r"\.pipeline-files__code\s*\{[^}]*var\(--pc-bg-void\)", section)
    assert re.search(r"\.pipeline-files__code\s*\{[^}]*var\(--pc-font-mono\)", section)
    assert re.search(r"\.pipeline-trust-wall\s*\{[^}]*var\(--pc-f1-red\)", section)
    assert "@media (max-width: 1100px)" in section


_VIEW_MODEL_SCRIPT = """
const p = await import(process.argv[1]);
const skills = ["dep-cache", "shard-by-timing", "affected-tests", "dedupe-jobs", "parallelize-dag", "other", null];
const compounds = Object.fromEntries(skills.map((s) => [String(s), p.compoundForSkill(s)]));
const joinView = p.pipelineViewFromState({
  mode: "live", stageIndex: 0, selectedAttempt: null,
  records: {
    hypotheses: [{ id: "h1", workflow_path: ".github/workflows/ci.yml", job: "test", step: "",
      bucket: "running", expected_savings_seconds: 12.5, confidence: null, skill: "parallelize-dag" }],
    candidates: [{ id: "c1", hypothesis_id: "h1", branch: "pit-crew/dag", summary: "Split jobs",
      changed_files: [], files: { "b.yml": "b", "a.yml": "a" } }],
    replay_plans: [{ id: "p1", candidate_id: "c1", bug_shas: ["aaa", "bbb"], cases: [] }],
    evidence: [{ id: "e1", candidate_id: "c1", baseline_seconds: [10, 11], candidate_seconds: [6, 7],
      bugs_caught: 2, bugs_total: 2 }],
    verdicts: [{ id: "v1", candidate_id: "c1", keep: false, speedup: 0, p_value: null, reason: "short-circuit" }],
  },
});
const multiView = p.pipelineViewFromState({
  mode: "live", stageIndex: 0,
  selectedAttempt: { hypothesis_id: "h1", candidate_id: "c1", skill: "shard-by-timing" },
  records: {
    hypotheses: [
      { id: "h1", workflow_path: "w1.yml", job: "j1", step: "s1", bucket: "running",
        expected_savings_seconds: 20, confidence: 0.8, skill: "shard-by-timing" },
      { id: "h2", workflow_path: "w2.yml", job: "j2", step: "s2", bucket: "setup",
        expected_savings_seconds: 5, confidence: 0.5, skill: "dep-cache" },
    ],
    candidates: [
      { id: "c1", hypothesis_id: "h1", branch: "b1", summary: "first",
        changed_files: ["w1.yml"], files: { "w1.yml": "first-yaml" } },
      { id: "c2", hypothesis_id: "h2", branch: "b2", summary: "second",
        changed_files: ["w2.yml"], files: { "w2.yml": "second-yaml" } },
    ],
    replay_plans: [
      { id: "p1", candidate_id: "c1", bug_shas: [],
        cases: [{ kind: "commit" }, { kind: "mutation" }, { kind: "mutation" }] },
      { id: "p2", candidate_id: "c2", bug_shas: ["x"], cases: [{ kind: "commit" }] },
    ],
    evidence: [], verdicts: [],
  },
});
const emptyLive = p.pipelineViewFromState({
  mode: "live", stageIndex: 4, selectedAttempt: null,
  records: { hypotheses: [], candidates: [], replay_plans: [], evidence: [], verdicts: [] },
});
const pvals = [null, 0.0004, 0.004, 0.24].map(p.formatPValue);
console.log(JSON.stringify({ compounds, joinView, multiView, emptyLive, pvals }));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_pipeline_view_model_compound_rings_and_edge_cases() -> None:
    assert NODE is not None
    subprocess.run([NODE, "--check", str(PIPELINE_JS)], check=True, capture_output=True)  # noqa: S603
    out = _node(_VIEW_MODEL_SCRIPT, PIPELINE_JS.as_uri())

    assert {k: v["token"] for k, v in out["compounds"].items()} == {
        "dep-cache": "--pc-compound-soft",
        "shard-by-timing": "--pc-compound-medium",
        "affected-tests": "--pc-compound-hard",
        "dedupe-jobs": "--pc-compound-hypersoft",
        "parallelize-dag": "--pc-compound-inter",
        "other": "--pc-ink-muted",
        "null": "--pc-ink-muted",
    }
    jv = out["joinView"]
    assert [s["state"] for s in jv["stages"]] == ["done"] * 5
    assert jv["trustWall"] == {"title": "TRUST WALL", "label": "AGENT PROPOSES | CODE JUDGES", "crossed": True}
    assert (jv["stages"][0]["data"]["step"], jv["stages"][0]["data"]["confidence"]) == ("(job-level)", "--")
    assert jv["stages"][0]["data"]["expectedSavings"] == "-12.5s"
    assert jv["stages"][1]["data"]["skill"] == "parallelize-dag"
    assert jv["stages"][1]["data"]["compound"] == {"code": "I", "label": "Inter", "token": "--pc-compound-inter"}
    assert [f["path"] for f in jv["stages"][1]["data"]["files"]] == ["a.yml", "b.yml"]
    assert jv["stages"][2]["data"] == {"commitCount": 2, "mutationCount": 0, "totalCount": 2}
    assert (jv["stages"][4]["data"]["pValue"], jv["stages"][4]["data"]["flag"]) == ("n/a", "REJECT")

    mv = out["multiView"]
    assert [s["state"] for s in mv["stages"]] == ["done", "done", "current", "pending", "pending"]
    assert mv["trustWall"]["crossed"] is False
    assert (mv["stages"][0]["data"]["workflowPath"], mv["stages"][1]["data"]["branch"]) == ("w1.yml", "b1")
    assert mv["stages"][1]["data"]["files"] == [{"path": "w1.yml", "content": "first-yaml"}]
    assert mv["stages"][2]["data"] == {"commitCount": 1, "mutationCount": 2, "totalCount": 3}

    assert [s["state"] for s in out["emptyLive"]["stages"]] == ["pending"] * 5
    assert out["emptyLive"]["trustWall"]["crossed"] is False
    assert out["pvals"] == ["n/a", "<0.001", "0.004", "0.24"]


_DOM_PLAYBACK_SCRIPT = """
const fakeNode = (tag = "div", extra = {}) => {
  const listeners = {};
  return {
    tagName: tag.toUpperCase(), className: "", hidden: false, open: false, textContent: "",
    title: "", value: "", disabled: false, dataset: {}, attrs: {}, kids: [],
    setAttribute(n, v) { this.attrs[n] = v; },
    addEventListener(t, fn) { (listeners[t] ??= []).push(fn); },
    dispatchEvent(e) { for (const fn of listeners[e.type] ?? []) fn(e); return true; },
    replaceChildren(...kids) { this.kids = kids; },
    append(...kids) { this.kids.push(...kids); },
    querySelectorAll(s) { return s === "[data-mode]" ? (this.modeButtons ?? []) : []; },
    click() { if (!this.disabled) this.dispatchEvent({ type: "click" }); },
    ...extra,
  };
};
const allText = (n) => [n.textContent, ...n.kids.map(allText)].filter(Boolean).join(" ");
const findClass = (n, c) => [
  ...((n.className || "").split(/\\s+/).includes(c) ? [n] : []), ...n.kids.flatMap((k) => findClass(k, c)),
];
const live = fakeNode("button", { dataset: { mode: "live" } });
const demo = fakeNode("button", { dataset: { mode: "demo" } });
const ids = {
  "repo-input": fakeNode("input"), "repo-options": fakeNode("datalist"), "base-sha-value": fakeNode("code"),
  "sla-median": fakeNode("span"), "sla-p95": fakeNode("span"), "sla-badge": fakeNode("span", { hidden: true }),
  "mode-toggle": fakeNode("div", { modeButtons: [live, demo] }), "run-pit-stop": fakeNode("button"),
  "demo-switcher": fakeNode("section"), "sector-pipeline": fakeNode("section"),
};
const doc = fakeNode("document", {
  readyState: "complete", getElementById: (id) => ids[id] ?? null, createElement: (t) => fakeNode(t),
});
const app = await import(process.argv[1]);
const pw = app.bootPitWall(doc, "");
const { pipeline: pl, switcher: sw } = pw;
const snap = () => {
  const codeNode = findClass(pl.stages.optimizer, "pipeline-files__code")[0];
  return {
    states: Object.fromEntries(Object.entries(pl.stages).map(([k, v]) => [k, v.dataset.state])),
    current: Object.entries(pl.stages).filter(([, v]) => v.attrs["aria-current"] === "step").map(([k]) => k),
    crossed: pl.trustWall.dataset.crossed, verdict: pl.stages.rater.dataset.verdict,
    raterText: allText(pl.stages.rater), ciText: allText(pl.stages.ci),
    proofText: allText(pl.stages.proof), anaText: allText(pl.stages.analysis),
    compound: findClass(pl.stages.optimizer, "compound-badge")[0]?.dataset ?? null,
    viewerOpen: pl.fileViewer?.open ?? null, codeText: codeNode ? allText(codeNode) : null,
    pips: findClass(pl.stages.ci, "pipeline-interleave__pip").length,
  };
};
const scenarios = {};
for (const btn of sw.scenarioButtons) {
  btn.click(); sw.resetButton.click();
  const stages = [snap()];
  for (let i = 0; i < 4; i += 1) { sw.stepButton.click(); stages.push(snap()); }
  scenarios[btn.dataset.scenario] = stages;
}
pw.store.selectScenario("proven_keep");
sw.playButton.click(); const playStart = snap().states; sw.playButton.click();
sw.stepButton.click(); const collapsed = pl.fileViewer.open;
pl.fileViewer.open = true; pl.fileViewer.dispatchEvent({ type: "toggle" });
sw.stepButton.click(); const staysOpen = pl.fileViewer.open;
pw.store.selectScenario("unsafe_reject"); const resetsOnSwitch = pl.fileViewer.open;
live.click(); const liveEmpty = snap();
pw.store.setLiveData({
  status: { repo: "owner/repo", active: false, base_sha: "abc1234", sla_met: false, latest_verdict: null },
  attempts: [
    { hypothesis_id: "h1", candidate_id: "c1", skill: "dep-cache" },
    { hypothesis_id: "h2", candidate_id: "c2", skill: "affected-tests" },
  ],
  records: {
    hypotheses: [
      { id: "h1", workflow_path: "a.yml", job: "t1", step: "i", bucket: "setup",
        expected_savings_seconds: 15, confidence: 0.9, skill: "dep-cache" },
      { id: "h2", workflow_path: "b.yml", job: "t2", step: "t", bucket: "running",
        expected_savings_seconds: 30, confidence: 0.6, skill: "affected-tests" },
    ],
    candidates: [
      { id: "c1", hypothesis_id: "h1", branch: "b1", summary: "s1",
        changed_files: ["a.yml"], files: { "a.yml": "FILE_A" } },
      { id: "c2", hypothesis_id: "h2", branch: "b2", summary: "s2",
        changed_files: ["b.yml"], files: { "b.yml": "FILE_B" } },
    ],
    replay_plans: [], evidence: [], verdicts: [],
  },
});
const live2 = snap(); pw.store.selectAttempt(0); const live1 = snap();
console.log(JSON.stringify({
  wallRole: pl.trustWall.attrs.role, wallText: allText(pl.trustWall), scenarios, playStart,
  collapsed, staysOpen, resetsOnSwitch, liveEmpty, live2, live1,
}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_sector_pipeline_dom_playback_and_file_viewer() -> None:
    out = _node(_DOM_PLAYBACK_SCRIPT, APP_JS.as_uri())

    assert out["wallRole"] == "separator"
    assert "TRUST WALL" in out["wallText"]
    assert "AGENT PROPOSES | CODE JUDGES" in out["wallText"]

    expected = {
        "proven_keep": ("keep", "KEEP", "1.85x", "0.004", "4 / 4", "dep-cache", "--pc-compound-soft"),
        "unsafe_reject": ("reject", "REJECT", "1.92x", "0.004", "3 / 4", "affected-tests", "--pc-compound-hard"),
        "noise_reject": ("reject", "REJECT", "1.03x", "0.24", "4 / 4", "dedupe-jobs", "--pc-compound-hypersoft"),
    }
    for scenario_id, (verdict_attr, flag, speedup, pval, bugs, skill, token) in expected.items():
        stages = out["scenarios"][scenario_id]
        assert len(stages) == 5
        assert stages[0]["states"] == {
            "analysis": "current", "optimizer": "pending", "proof": "pending", "ci": "pending", "rater": "pending"
        }  # fmt: skip
        assert (stages[0]["current"], stages[0]["crossed"], stages[0]["codeText"]) == (["analysis"], "false", None)
        assert ".github/workflows/ci.yml" in stages[0]["anaText"]

        assert (stages[1]["states"]["analysis"], stages[1]["states"]["optimizer"]) == ("done", "current")
        assert stages[1]["compound"] == {"skill": skill, "compoundToken": token}
        assert stages[1]["viewerOpen"] is False
        assert "name: CI" in stages[1]["codeText"]

        assert (stages[2]["states"]["proof"], stages[2]["crossed"]) == ("current", "false")
        assert all(tok in stages[2]["proofText"] for tok in ("2", "Commit SHAs", "Mutations"))

        assert (stages[3]["states"]["ci"], stages[3]["crossed"], stages[3]["pips"]) == ("current", "true", 10)
        assert bugs in stages[3]["ciText"]
        assert stages[3]["verdict"] == "pending"

        assert stages[4]["states"] == {
            "analysis": "done", "optimizer": "done", "proof": "done", "ci": "done", "rater": "current"
        }  # fmt: skip
        assert stages[4]["verdict"] == verdict_attr
        assert all(tok in stages[4]["raterText"] for tok in (flag, speedup, pval))

    assert (out["playStart"]["analysis"], out["playStart"]["optimizer"]) == ("current", "pending")
    assert (out["collapsed"], out["staysOpen"], out["resetsOnSwitch"]) == (False, True, False)
    assert out["liveEmpty"]["states"] == dict.fromkeys(("analysis", "optimizer", "proof", "ci", "rater"), "pending")
    assert (out["live2"]["codeText"], out["live2"]["compound"]["skill"]) == ("FILE_B", "affected-tests")
    assert (out["live1"]["codeText"], out["live1"]["compound"]["skill"]) == ("FILE_A", "dep-cache")
