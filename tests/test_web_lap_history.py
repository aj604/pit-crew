import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pit_crew.contracts import Candidate, Evidence, Hypothesis, Verdict
from pit_crew.ledger import InMemoryLedger
from pit_crew.server import DEFAULT_BRAND_DIR, create_app

WEB_DIR = Path(__file__).resolve().parents[1] / "src" / "pit_crew" / "web"
APP_JS, LAP_HISTORY_JS, STYLES_CSS = WEB_DIR / "app.js", WEB_DIR / "lap_history.js", WEB_DIR / "styles.css"
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


def test_lap_history_asset_tokens_and_styles() -> None:
    resp = TestClient(create_app(InMemoryLedger())).get("/web/lap_history.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]

    defined = set(re.findall(r"(--pc-[\w-]+)\s*:", (DEFAULT_BRAND_DIR / "tokens.css").read_text()))
    for path in (LAP_HISTORY_JS, APP_JS, STYLES_CSS):
        source = path.read_text()
        assert AD_HOC_COLOR.findall(source) == [], f"ad-hoc color in {path.name}"
        assert set(re.findall(r"var\((--pc-[\w-]+)\)", source)) <= defined

    css = STYLES_CSS.read_text()
    assert "/* === Lap History (#51) === */" in css
    assert re.search(r"\.lap-history__verdict--keep\s*\{[^}]*var\(--pc-drs-green\)", css)
    assert re.search(r"\.lap-history__verdict--reject\s*\{[^}]*var\(--pc-f1-red\)", css)
    assert re.search(r"\.lap-history__verdict--pending\s*\{", css)
    for token in ("soft", "medium", "hard", "hypersoft", "inter"):
        assert f"var(--pc-compound-{token})" in css
    assert "var(--pc-ink-muted)" in css
    assert ".lap-history__cell::before" in css


_VIEW_MODEL_SCRIPT = """
const lh = await import(process.argv[1]);
const records = {
  hypotheses: [
    { id: "h1", workflow_path: ".github/workflows/ci.yml", job: "test", step: "Install deps" },
    { id: "h2", workflow_path: ".github/workflows/ci.yml", job: "lint", step: "" },
    { id: "h3", workflow_path: ".github/workflows/ci.yml", job: "build", step: "Compile" },
  ],
  evidence: [
    { candidate_id: "c1", bugs_caught: 4, bugs_total: 4 },
    { candidate_id: "c2", bugs_caught: 3, bugs_total: 4 },
    { candidate_id: "c3", bugs_caught: 0, bugs_total: 0 },
  ],
};
const attempts = [
  { hypothesis_id: "h1", skill: "dep-cache", description: "d1", candidate_id: "c1", summary: "Cache wheels",
    rated: true, keep: true, speedup: 1.85, p_value: 0.004, pr_url: "https://github.com/aj604/pit-crew/pull/51" },
  { hypothesis_id: "h2", skill: "shard-by-timing", description: "d2", candidate_id: "c2", summary: "",
    rated: true, keep: false, speedup: 0, p_value: 0.24, pr_url: "javascript:alert(1)" },
  { hypothesis_id: "h3", skill: "affected-tests", description: "d3", candidate_id: "c3", summary: null,
    rated: false, keep: true, speedup: null, p_value: null, pr_url: "http://example.local/pr/custom" },
  { hypothesis_id: "x", skill: "dedupe-jobs", description: "", candidate_id: null, summary: null,
    rated: true, keep: null, speedup: 1.25, p_value: 0.0002, pr_url: "data:text/html,hi" },
  { hypothesis_id: "x", skill: "parallelize-dag", description: "DAG", candidate_id: "no-ev",
    summary: "Run jobs concurrently", rated: false, keep: null },
  { hypothesis_id: "x", skill: "custom-skill", description: "Fallback", candidate_id: null,
    rated: true, keep: false, speedup: 1.1, p_value: 0.05, pr_url: "not a url" },
];
console.log(JSON.stringify({
  rows: lh.buildLapHistoryRows(attempts, records, 0),
  empty: lh.buildLapHistoryRows([], null, null),
  nulls: lh.buildLapHistoryRows(null, undefined, null),
}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_lap_history_view_model_maps_attempts_records_compounds_and_pr_urls() -> None:
    assert NODE is not None
    subprocess.run([NODE, "--check", str(LAP_HISTORY_JS)], check=True, capture_output=True)  # noqa: S603
    out = _node(_VIEW_MODEL_SCRIPT, LAP_HISTORY_JS.as_uri())
    rows = out["rows"]

    assert [r["attemptNumber"] for r in rows] == [1, 2, 3, 4, 5, 6]
    assert [r["compoundToken"] for r in rows] == [
        "var(--pc-compound-soft)", "var(--pc-compound-medium)", "var(--pc-compound-hard)",
        "var(--pc-compound-hypersoft)", "var(--pc-compound-inter)", "var(--pc-ink-muted)",
    ]  # fmt: skip
    assert [r["verdict"] for r in rows] == ["KEEP", "REJECT", "PENDING", "PENDING", "PENDING", "REJECT"]
    assert (rows[0]["target"], rows[0]["summary"], rows[0]["speedup"], rows[0]["pValue"]) == (
        ".github/workflows/ci.yml · test · Install deps", "Cache wheels", "1.85x", "0.004",
    )  # fmt: skip
    assert (rows[0]["bugsCaught"], rows[0]["bugsStatus"], rows[0]["prLabel"], rows[0]["selected"]) == (
        "4/4", "all", "PR #51", True,
    )  # fmt: skip
    assert (rows[1]["target"], rows[1]["summary"], rows[1]["speedup"], rows[1]["pValue"]) == (
        ".github/workflows/ci.yml · lint", "d2", "0.00x", "0.240",
    )  # fmt: skip
    assert (rows[1]["bugsCaught"], rows[1]["bugsStatus"], rows[1]["prUrl"]) == ("3/4", "missed", None)
    assert (rows[2]["speedup"], rows[2]["pValue"], rows[2]["bugsCaught"], rows[2]["prLabel"]) == (
        "—", "—", "0/0", "View PR",
    )  # fmt: skip
    assert (rows[3]["summary"], rows[3]["pValue"], rows[3]["bugsCaught"], rows[3]["prUrl"]) == (
        "—", "<0.001", "—", None,
    )  # fmt: skip
    assert (rows[4]["bugsCaught"], rows[5]["prUrl"], out["empty"], out["nulls"]) == ("—", None, [], [])


_INTERACTIVE_SCRIPT = """
const fakeNode = (tag = "div", extra = {}) => {
  const ls = {};
  return {
    tagName: tag.toUpperCase(), className: "", hidden: false, textContent: "", title: "", value: "",
    href: "", target: "", rel: "", tabIndex: -1, disabled: false, dataset: {}, attrs: {}, style: {},
    kids: [], parentNode: null,
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener(t, fn) { (ls[t] ??= []).push(fn); },
    dispatchEvent(event) {
      const ev = event._ev ? event : {
        _ev: true, _stopped: false, target: this, defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this._stopped = true; }, ...event,
      };
      for (const fn of ls[ev.type] ?? []) fn(ev);
      if (!ev._stopped && this.parentNode) this.parentNode.dispatchEvent(ev);
      return ev;
    },
    replaceChildren(...kids) { this.kids = []; this.append(...kids); },
    append(...kids) { for (const k of kids) { k.parentNode = this; this.kids.push(k); } },
    closest(s) {
      for (let c = this; c; c = c.parentNode) if (c.tagName.toLowerCase() === s.toLowerCase()) return c;
      return null;
    },
    contains(t) { return t === this || this.kids.some((k) => k.contains(t)); },
    focus() { if (this._doc) this._doc.activeElement = this; },
    querySelectorAll(sel) {
      if (sel === "[data-mode]") return this.modeButtons ?? [];
      const out = [];
      const walk = (n) => { for (const k of n.kids) {
        if (sel.startsWith(".") ? k.className.split(/\\s+/).includes(sel.slice(1))
            : k.tagName.toLowerCase() === sel.toLowerCase()) out.push(k);
        walk(k);
      }};
      walk(this);
      return out;
    },
    click() { return this.disabled ? null : this.dispatchEvent({ type: "click" }); },
    ...extra,
  };
};
const fakeDoc = (withLap = true) => {
  const live = fakeNode("button", { dataset: { mode: "live" } });
  const demo = fakeNode("button", { dataset: { mode: "demo" } });
  const ids = {
    "repo-input": fakeNode("input"), "repo-options": fakeNode("datalist"), "base-sha-value": fakeNode("code"),
    "sla-median": fakeNode("span"), "sla-p95": fakeNode("span"), "sla-badge": fakeNode("span", { hidden: true }),
    "mode-toggle": fakeNode("div", { modeButtons: [live, demo] }), "run-pit-stop": fakeNode("button"),
    "demo-switcher": fakeNode("section"), ...(withLap ? { "lap-history": fakeNode("section") } : {}),
  };
  const doc = fakeNode("document", {
    readyState: "complete", activeElement: null,
    getElementById: (id) => ids[id] ?? null, createElement: (t) => fakeNode(t, { _doc: doc }),
  });
  return { doc, ids };
};
const textOf = (n) => (n.kids.length ? n.kids.map(textOf).join("") : n.textContent);
const app = await import(process.argv[1]);
const d = fakeDoc(true);
const pw = app.bootPitWall(d.doc, "");
const slot = d.ids["lap-history"];
const rows = () => pw.lapHistory.getRows().map((tr) => {
  const tds = tr.querySelectorAll("td"), link = tr.querySelectorAll("a")[0] ?? null;
  const cmp = tr.querySelectorAll(".lap-history__compound")[0], ring = tr.querySelectorAll(".lap-history__ring")[0];
  const targetEl = tds[2].querySelectorAll(".lap-history__target")[0] ?? null;
  const summaryEl = tds[2].querySelectorAll(".lap-history__summary")[0] ?? null;
  const verd = tr.querySelectorAll(".lap-history__verdict")[0], bugs = tr.querySelectorAll(".lap-history__bugs")[0];
  return {
    sel: tr.attrs["aria-selected"], selCls: tr.className.includes("lap-history__row--selected"),
    labels: tds.map((td) => td.dataset.label),
    n: tds[0].textContent, skill: cmp.dataset.skill, tok: cmp.dataset.compoundToken, ring: ring.style.borderColor,
    code: ring.textContent, targetTxt: targetEl?.textContent ?? null, summaryTxt: summaryEl?.textContent ?? null,
    spd: tds[3].textContent, p: tds[4].textContent, bugs: bugs.textContent,
    bugsSt: bugs.dataset.status, v: verd.textContent, vCls: verd.className,
    pr: link ? link.textContent : textOf(tds[7]), href: link?.attrs.href ?? null,
    target: link?.attrs.target ?? null, rel: link?.attrs.rel ?? null,
  };
});
const hdr = () => [d.ids["base-sha-value"].textContent, d.ids["sla-median"].textContent,
  d.ids["sla-p95"].textContent, d.ids["sla-badge"].hidden];

const headers = slot.querySelectorAll("th").map((th) => th.textContent);
const demoKeep = rows();
pw.store.selectScenario("unsafe_reject"); const demoReject = rows();
pw.store.reset(); const demoPending = rows();
pw.store.setMode("live");
const liveEmpty = [slot.querySelectorAll("table").length, slot.querySelectorAll(".lap-history__count")[0]?.textContent,
  slot.querySelectorAll(".lap-history__empty")[0]?.textContent];
pw.store.setLiveData(JSON.parse(process.argv[2]));
const liveInit = { rows: rows(), hdr: hdr() };
pw.lapHistory.getRows()[0].click();
const click0 = { idx: pw.store.getState().selectedAttemptIndex, hdr: hdr(), sel: rows().map((r) => [r.sel, r.selCls]) };
const r1 = pw.lapHistory.getRows()[1]; r1.focus();
const k1 = r1.dispatchEvent({ type: "keydown", key: "Enter" });
const key1 = { idx: pw.store.getState().selectedAttemptIndex, prev: k1.defaultPrevented,
  focus: d.doc.activeElement?.dataset?.attemptIndex, hdr: hdr() };
pw.lapHistory.getRows()[2].dispatchEvent({ type: "keydown", key: " " });
const key2 = { idx: pw.store.getState().selectedAttemptIndex, hdr: hdr() };
const prLink = pw.lapHistory.getRows()[0].querySelectorAll("a")[0];
const prEv = prLink.click();
const prClick = { idx: pw.store.getState().selectedAttemptIndex, prev: prEv.defaultPrevented };
const prKeyEv = prLink.dispatchEvent({ type: "keydown", key: "Enter" });
const prKey = { idx: pw.store.getState().selectedAttemptIndex, prev: prKeyEv.defaultPrevented };
const missing = app.bootPitWall(fakeDoc(false).doc, "").lapHistory === null;
const helper = {
  nullState: app.statusForHeader(null),
  noAttempt: app.statusForHeader({ status: { base_sha: "abc", sla_met: true }, selectedAttempt: null }),
  kept: app.statusForHeader({
    status: { base_sha: "old" },
    selectedAttempt: {
      rated: true, keep: true, base_sha: "old", head_sha: "new1111", median_seconds: 50, sla_met: true,
    },
  }),
  rejected: app.statusForHeader({
    status: { base_sha: "new1111", sla_met: true },
    selectedAttempt: {
      rated: true, keep: false, base_sha: "base222", head_sha: "cand333", p95_seconds: 130, sla_met: false,
    },
  }),
  pending: app.statusForHeader({
    status: { base_sha: "base222", sla_met: true, latest_verdict: { median_seconds: 40 } },
    selectedAttempt: { rated: false, base_sha: "pend444" },
  }),
  solo: app.statusForHeader({
    status: null,
    selectedAttempt: { rated: true, keep: true, head_sha: "solo555", median_seconds: 41, sla_met: true },
  }),
};
console.log(JSON.stringify({
  headers, demoKeep, demoReject, demoPending, liveEmpty, liveInit, click0, key1, key2, prClick, prKey, missing, helper,
}));
"""


def _build_live_ledger_payload() -> dict[str, Any]:
    repo, ledger = "aj604/pit-crew", InMemoryLedger()
    sha1, sha2, sha3 = "1111111" + "a" * 33, "aaaaaaa" + "1" * 33, "bbbbbbb" + "2" * 33
    for rec in (
        Hypothesis(id="h1", repo=repo, description="Cache uv", skill="dep-cache", expected_savings_seconds=42.0,
                   base_sha=sha1, workflow_path=".github/workflows/ci.yml", job="test", step="Install"),
        Candidate(id="c1", hypothesis_id="h1", branch="pit-crew/1", summary="Enable uv cache", repo=repo,
                  base_sha=sha1, head_sha=sha2),
        Evidence(id="e1", candidate_id="c1", baseline_seconds=(90.0,) * 5, candidate_seconds=(45.0,) * 5,
                 bugs_total=4, bugs_caught=4),
        Verdict(id="v1", candidate_id="c1", keep=True, speedup=2.0, p_value=0.004, reason="fast",
                median_seconds=45.0, p95_seconds=46.8, sla_met=True),
        Hypothesis(id="h2", repo=repo, description="Shard", skill="shard-by-timing", expected_savings_seconds=15.0,
                   base_sha=sha2, workflow_path=".github/workflows/ci.yml", job="test", step="Pytest"),
        Candidate(id="c2", hypothesis_id="h2", branch="pit-crew/2", summary="2 shards", repo=repo,
                  base_sha=sha2, head_sha=sha3),
        Evidence(id="e2", candidate_id="c2", baseline_seconds=(70.0,) * 5, candidate_seconds=(69.0,) * 5,
                 bugs_total=4, bugs_caught=3),
        Verdict(id="v2", candidate_id="c2", keep=False, speedup=1.03, p_value=0.22, reason="missed bug",
                median_seconds=69.0, p95_seconds=70.8, sla_met=False),
        Hypothesis(id="h3", repo=repo, description="Parallelize jobs", skill="parallelize-dag",
                   expected_savings_seconds=20.0, base_sha=sha2, workflow_path=".github/workflows/ci.yml", job="lint"),
    ):  # fmt: skip
        ledger.add(rec)
    client = TestClient(create_app(ledger))
    attempts = client.get(f"/api/repos/{repo}/attempts").json()
    attempts[0]["pr_url"], attempts[1]["pr_url"] = "https://github.com/aj604/pit-crew/pull/99", "javascript:alert(1)"
    return {
        "status": client.get(f"/api/repos/{repo}/status").json(),
        "attempts": attempts,
        "records": client.get(f"/api/repos/{repo}/records").json(),
    }


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_lap_history_renders_selects_syncs_header_and_handles_empty() -> None:
    out = _node(_INTERACTIVE_SCRIPT, APP_JS.as_uri(), json.dumps(_build_live_ledger_payload()))

    assert out["headers"] == [
        "Attempt #", "Skill", "Target / Summary", "Speedup", "p-value", "Bugs Caught", "Verdict", "PR",
    ]  # fmt: skip
    keep, rej, pend = out["demoKeep"][0], out["demoReject"][0], out["demoPending"][0]
    assert (keep["n"], keep["skill"], keep["tok"], keep["ring"], keep["code"]) == (
        "1", "dep-cache", "var(--pc-compound-soft)", "var(--pc-compound-soft)", "S",
    )  # fmt: skip
    assert keep["labels"] == out["headers"]
    assert (keep["targetTxt"], keep["summaryTxt"]) == (
        ".github/workflows/ci.yml · test · Install dependencies",
        "Cache pip downloads keyed on requirements.txt.",
    )
    assert (keep["spd"], keep["p"], keep["bugs"], keep["bugsSt"], keep["v"], keep["pr"]) == (
        "1.85x", "0.004", "4/4", "all", "KEEP", "—",
    )  # fmt: skip
    assert "lap-history__verdict--keep" in keep["vCls"]
    assert (rej["skill"], rej["tok"], rej["bugs"], rej["bugsSt"], rej["v"]) == (
        "affected-tests", "var(--pc-compound-hard)", "3/4", "missed", "REJECT",
    )  # fmt: skip
    assert (rej["targetTxt"], rej["summaryTxt"]) == (
        ".github/workflows/ci.yml · test · Run tests",
        "Run only tests whose imports touch changed files.",
    )
    assert "lap-history__verdict--reject" in rej["vCls"]
    assert (pend["spd"], pend["p"], pend["bugs"], pend["v"]) == ("—", "—", "—", "PENDING")
    assert (pend["targetTxt"], pend["summaryTxt"]) == (
        ".github/workflows/ci.yml · test · Run tests",
        "Full pytest run dominates the test job; only run tests affected by the diff.",
    )
    assert "lap-history__verdict--pending" in pend["vCls"]

    assert out["liveEmpty"][:2] == [0, "0 laps"]
    assert "No laps recorded" in out["liveEmpty"][2]
    live_rows = out["liveInit"]["rows"]
    assert [r["v"] for r in live_rows] == ["KEEP", "REJECT", "PENDING"]
    assert [r["labels"] for r in live_rows] == [out["headers"]] * 3
    assert [(r["targetTxt"], r["summaryTxt"]) for r in live_rows] == [
        (".github/workflows/ci.yml · test · Install", "Enable uv cache"),
        (".github/workflows/ci.yml · test · Pytest", "2 shards"),
        (".github/workflows/ci.yml · lint", "Parallelize jobs"),
    ]
    assert (live_rows[0]["pr"], live_rows[0]["href"], live_rows[0]["target"], live_rows[0]["rel"]) == (
        "PR #99", "https://github.com/aj604/pit-crew/pull/99", "_blank", "noopener noreferrer",
    )  # fmt: skip
    assert (live_rows[1]["pr"], live_rows[1]["href"]) == ("—", None)
    assert out["liveInit"]["hdr"] == ["aaaaaaa", "--", "--", True]
    assert out["click0"] == {
        "idx": 0, "hdr": ["aaaaaaa", "45.0", "46.8", False],
        "sel": [["true", True], ["false", False], ["false", False]],
    }  # fmt: skip
    assert out["key1"] == {"idx": 1, "prev": True, "focus": "1", "hdr": ["aaaaaaa", "69.0", "70.8", True]}
    assert out["key2"] == {"idx": 2, "hdr": ["aaaaaaa", "--", "--", True]}
    assert out["prClick"] == {"idx": 2, "prev": False}
    assert out["prKey"] == {"idx": 2, "prev": False}
    assert out["missing"] is True

    h = out["helper"]
    assert (h["nullState"], h["noAttempt"]) == (None, {"base_sha": "abc", "sla_met": True})
    assert (h["kept"]["base_sha"], h["kept"]["sla_met"], h["kept"]["latest_verdict"]["median_seconds"]) == (
        "new1111", True, 50,
    )  # fmt: skip
    assert (h["rejected"]["base_sha"], h["rejected"]["sla_met"], h["rejected"]["latest_verdict"]["p95_seconds"]) == (
        "base222", False, 130,
    )  # fmt: skip
    assert (h["pending"]["base_sha"], h["pending"]["sla_met"], h["pending"]["latest_verdict"]) == (
        "pend444", False, None,
    )  # fmt: skip
    assert (h["solo"]["base_sha"], h["solo"]["sla_met"]) == ("solo555", True)
