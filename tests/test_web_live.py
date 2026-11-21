import contextlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from pit_crew.contracts import Candidate, Evidence, Hypothesis, ReplayPlan, Verdict
from pit_crew.ledger import InMemoryLedger
from pit_crew.server import DEFAULT_BRAND_DIR, create_app

WEB_DIR = Path(__file__).resolve().parents[1] / "src" / "pit_crew" / "web"
APP_JS, LIVE_JS, STYLES_CSS = WEB_DIR / "app.js", WEB_DIR / "live_poller.js", WEB_DIR / "styles.css"
NODE = shutil.which("node")
CHROME = next(
    (p for p in (shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chromium-browser"),
                 "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome") if p and Path(p).is_file()),
    None,
)  # fmt: skip
AD_HOC_COLOR = re.compile(r"(?<![\w&])#[0-9a-fA-F]{3,8}\b(?![\w-])|\b(?:rgba?|hsla?)\(", re.IGNORECASE)


class _BlockingDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.started, self.release, self.finished = threading.Event(), threading.Event(), threading.Event()

    def _run(self, kind: str, repo: str) -> None:
        self.calls.append((kind, repo))
        self.started.set()
        self.release.wait(timeout=5)
        self.finished.set()

    def run_cycle(self, repo: str) -> None:
        self._run("run_cycle", repo)

    def run_until_sla(self, repo: str) -> None:
        self._run("run_until_sla", repo)


def _seed_ledger(repo: str = "aj604/pit-crew") -> InMemoryLedger:
    ledger = InMemoryLedger()
    sha1, sha2 = "1111111" + "a" * 33, "e9b17d3" + "b" * 33
    for rec in (
        Hypothesis(
            id="h1", repo=repo, description="Cache deps", skill="dep-cache", expected_savings_seconds=40.0,
            base_sha=sha1, workflow_path=".github/workflows/ci.yml", job="test", step="Install",
        ),
        Candidate(
            id="c1", hypothesis_id="h1", branch="pit-crew/1", summary="Enable uv cache",
            repo=repo, base_sha=sha1, head_sha=sha2,
        ),
        ReplayPlan(id="p1", candidate_id="c1", bug_shas=("deadbeef",)),
        Evidence(
            id="e1", candidate_id="c1", baseline_seconds=(90.0,) * 5,
            candidate_seconds=(48.0,) * 5, bugs_total=4, bugs_caught=4,
        ),
        Verdict(
            id="v1", candidate_id="c1", keep=True, speedup=1.88, p_value=0.004,
            reason="proven speedup", median_seconds=48.0, p95_seconds=82.0, sla_met=True,
        ),
    ):  # fmt: skip
        ledger.add(rec)
    return ledger


def _node(script: str, *args: str) -> dict[str, Any]:
    assert NODE is not None
    res = subprocess.run(  # noqa: S603
        [NODE, "--input-type=module", "-e", script, *args],
        check=True, capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    out: dict[str, Any] = json.loads(res.stdout)
    return out


def test_live_poller_asset_tokens_and_styles() -> None:
    resp = TestClient(create_app(InMemoryLedger())).get("/web/live_poller.js")
    assert (resp.status_code, "javascript" in resp.headers["content-type"]) == (200, True)
    defined = set(re.findall(r"(--pc-[\w-]+)\s*:", (DEFAULT_BRAND_DIR / "tokens.css").read_text()))
    for path in (LIVE_JS, APP_JS, STYLES_CSS):
        source = path.read_text()
        assert AD_HOC_COLOR.findall(source) == [], f"ad-hoc color in {path.name}"
        assert set(re.findall(r"var\((--pc-[\w-]+)\)", source)) <= defined
    css = STYLES_CSS.read_text()
    assert "/* === Live Mode & Fetch Resilience (#53) === */" in css
    assert re.search(r"\.live-status-pill\s*\{[^}]*var\(--pc-vegas-gold\)", css)
    assert re.search(r'\.run-button\[aria-busy="true"\]::before\s*\{[^}]*animation:', css)


def test_live_api_endpoints_and_trigger_statuses() -> None:
    repo, dispatcher = "aj604/pit-crew", _BlockingDispatcher()
    with TestClient(create_app(_seed_ledger(repo), dispatcher=dispatcher)) as client:
        assert (client.get("/").status_code, client.get("/web/live_poller.js").status_code) == (200, 200)
        st = client.get(f"/api/repos/{repo}/status").json()
        attempts = client.get(f"/api/repos/{repo}/attempts").json()
        records = client.get(f"/api/repos/{repo}/records").json()
        assert (st["repo"], st["active"], st["sla_met"], st["latest_verdict"]["median_seconds"]) == (
            repo, False, True, 48.0,
        )  # fmt: skip
        assert st["base_sha"].startswith("e9b17d3")
        assert len(attempts) == 1
        assert set(records) == {"hypotheses", "candidates", "replay_plans", "evidence", "verdicts"}

        started = client.post("/triggers/cycle", json={"repo": repo, "until_sla": False})
        assert (started.status_code, started.json()) == (202, {"status": "started", "repo": repo})
        assert dispatcher.started.wait(timeout=5)
        assert client.get(f"/api/repos/{repo}/status").json()["active"] is True

        conflict = client.post("/triggers/cycle", json={"repo": repo, "until_sla": False})
        assert conflict.status_code == 409
        assert f"Cycle already active for {repo}" in conflict.json()["detail"]
        dispatcher.release.set()
        assert dispatcher.finished.wait(timeout=5)

    with TestClient(create_app(_seed_ledger(repo), dispatcher=None)) as no_disp:
        unavail = no_disp.post("/triggers/cycle", json={"repo": repo, "until_sla": False})
        assert (unavail.status_code, unavail.json()["detail"]) == (503, "Dispatcher is not configured")


_NODE_HARNESS = """
const flush = async (n = 8) => { for (let i = 0; i < n; i += 1) await Promise.resolve(); };
const jr = (status, body) => ({ ok: status >= 200 && status < 300, status, async json() { return body; } });
const makeTimer = () => {
  let seq = 1; const active = new Map(), scheduled = [];
  return {
    scheduled, active,
    setTimeout(fn, ms) { const id = seq++; active.set(id, { id, fn, ms }); scheduled.push(ms); return id; },
    clearTimeout(id) { active.delete(id); },
    fireNext() {
      const e = active.values().next().value;
      if (!e) return false;
      active.delete(e.id); e.fn(); return true;
    },
  };
};
const fakeNode = (tag = "div", extra = {}) => {
  const ls = {};
  return {
    tagName: tag.toUpperCase(), id: "", className: "", hidden: false, textContent: "", title: "", value: "",
    disabled: false, dataset: {}, attrs: {}, style: {}, kids: [], parentNode: null, removed: false,
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener(t, fn) { (ls[t] ??= []).push(fn); },
    removeEventListener(t, fn) { if (ls[t]) ls[t] = ls[t].filter((f) => f !== fn); },
    dispatchEvent(e) {
      const ev = e instanceof Event ? e : { target: this, preventDefault() {}, stopPropagation() {}, ...e };
      for (const fn of [...(ls[ev.type] ?? [])]) fn(ev);
      return ev;
    },
    replaceChildren(...kids) { this.kids = []; this.append(...kids); },
    append(...kids) { for (const k of kids) { k.parentNode = this; this.kids.push(k); } },
    remove() { const p = this.parentNode; this.removed = true; if (p) p.kids = p.kids.filter((k) => k !== this); },
    querySelectorAll(sel) {
      if (sel === "[data-mode]") return this.modeButtons ?? [];
      const out = [], cls = sel[0] === ".", target = cls ? sel.slice(1) : sel.toUpperCase();
      const walk = (n) => { for (const k of n.kids) {
        if (cls ? k.className.split(/\\s+/).includes(target) : k.tagName === target) out.push(k);
        walk(k);
      } };
      walk(this); return out;
    },
    click() { if (!this.disabled) this.dispatchEvent({ type: "click" }); },
    ...extra,
  };
};
const fakeDoc = () => {
  const modeBtn = (mode) => fakeNode("button", { dataset: { mode } });
  const live = modeBtn("live"), demo = modeBtn("demo");
  const ids = {
    "header-bar": fakeNode("header"), "pit-wall": fakeNode("main"), "repo-input": fakeNode("input"),
    "repo-options": fakeNode("datalist"), "base-sha-value": fakeNode("code"), "sla-median": fakeNode("span"),
    "sla-p95": fakeNode("span"), "sla-badge": fakeNode("span", { hidden: true }),
    "mode-toggle": fakeNode("div", { modeButtons: [live, demo] }), "run-pit-stop": fakeNode("button"),
    "demo-switcher": fakeNode("section"), "lap-history": fakeNode("section"),
  };
  const doc = fakeNode("document", { readyState: "complete", hidden: false,
    getElementById: (id) => ids[id] ?? null, createElement: (t) => fakeNode(t) });
  return { doc, ids, live, demo };
};
const pillText = (p) => p.querySelectorAll(".live-status-pill__text")[0]?.textContent ?? "";
const btnOf = (d) => [d.ids["run-pit-stop"].disabled, d.ids["run-pit-stop"].attrs["aria-busy"],
  d.ids["run-pit-stop"].textContent];
"""

_LIVE_NODE_SCRIPT = (
    _NODE_HARNESS
    + """
const app = await import(process.argv[1]), lp = await import(process.argv[2]), seed = JSON.parse(process.argv[3]);
const helpers = {
  intervals: [
    lp.LIVE_POLL_ACTIVE_MS, lp.LIVE_POLL_IDLE_MS, lp.pollIntervalForStatus({ active: true }),
    lp.pollIntervalForStatus({ active: false }), lp.pollIntervalForStatus(null),
    lp.pollIntervalForStatus({ active: false }, true),
  ],
  paths: [lp.repoApiPaths("My.Org/Pit-Crew"), lp.repoApiPaths("bad"), lp.repoApiPaths("a/b/c")],
  detail: [lp.extractErrorDetail({ detail: "  oops  " }), lp.extractErrorDetail({}), lp.extractErrorDetail(null)],
  validRecs: [
    lp.isValidRecordsPayload(seed.records), lp.isValidRecordsPayload({ hypotheses: [] }),
    lp.isValidRecordsPayload(null),
  ],
};

const d1 = fakeDoc(), t1 = makeTimer(), calls1 = [];
let active1 = true, fail1 = null;
const fetch1 = async (url, init = {}) => {
  calls1.push([init.method ?? "GET", url]);
  if (fail1 === "network") throw new Error("socket hang up");
  if (fail1 === "500" && url.endsWith("/attempts")) return jr(500, { detail: "db locked" });
  if (url.endsWith("/status")) return jr(200, { ...seed.status, active: active1 });
  return jr(200, url.endsWith("/attempts") ? seed.attempts : seed.records);
};
const pw1 = app.bootPitWall(d1.doc, "?repo=aj604/pit-crew", { live: { fetch: fetch1, timer: t1, doc: d1.doc } });
await flush();
const p1 = pw1.livePoller.statusPill;
const hydrated = {
  urls: calls1.slice(0, 3), sha: d1.ids["base-sha-value"].textContent, median: d1.ids["sla-median"].textContent,
  p95: d1.ids["sla-p95"].textContent, slaHidden: d1.ids["sla-badge"].hidden, laps: pw1.lapHistory.getRows().length,
  btn: btnOf(d1), pillMounted: d1.ids["header-bar"].kids.includes(p1), pillHidden: p1.hidden,
  pillRole: [p1.attrs.role, p1.attrs["aria-live"]], delayActive: t1.scheduled.at(-1),
};
active1 = false; t1.fireNext(); await flush();
const idleState = { delayIdle: t1.scheduled.at(-1), btn: btnOf(d1) };
fail1 = "500"; t1.fireNext(); await flush();
const onHttpErr = { pillHidden: p1.hidden, pillText: pillText(p1), sha: d1.ids["base-sha-value"].textContent,
  laps: pw1.lapHistory.getRows().length, attempts: pw1.store.getState().attempts.length };
fail1 = "network"; t1.fireNext(); await flush();
const onNetErr = { pillHidden: p1.hidden, pillText: pillText(p1), laps: pw1.lapHistory.getRows().length };
fail1 = null; t1.fireNext(); await flush();
const recovered = { pillHidden: p1.hidden, fetchError: pw1.store.getState().fetchError };
d1.doc.hidden = true; d1.doc.dispatchEvent({ type: "visibilitychange" });
const hiddenPaused = t1.active.size, callsBeforeShow = calls1.length;
d1.doc.hidden = false; d1.doc.dispatchEvent({ type: "visibilitychange" }); await flush();
const resumed = { newCalls: calls1.length - callsBeforeShow, activeTimers: t1.active.size };
fail1 = "network"; t1.fireNext(); await flush();
const errBeforeDemo = p1.hidden;
pw1.store.setMode("demo");
const inDemo = { activeTimers: t1.active.size, pillHidden: p1.hidden };
pw1.livePoller.destroy();
const afterDestroy = { destroyed: pw1.livePoller.getState().destroyed, pillRemoved: p1.removed };

const d2 = fakeDoc(), t2 = makeTimer(), pending = [];
const pw2 = app.bootPitWall(d2.doc, "?repo=org/first", {
  live: { fetch: (url) => new Promise((resolve) => pending.push({ url, resolve })), timer: t2, doc: d2.doc },
});
const b1 = pending.splice(0, 3);
d2.ids["repo-input"].value = "org/second";
d2.ids["repo-input"].dispatchEvent({ type: "keydown", key: "Enter" });
const b2 = pending.splice(0, 3);
b2[0].resolve(jr(200, { ...seed.status, repo: "org/second", base_sha: "2222222aaaa", active: false }));
b2[1].resolve(jr(200, [{ ...seed.attempts[0], head_sha: "2222222aaaa" }]));
b2[2].resolve(jr(200, seed.records));
await flush();
b1[0].resolve(jr(200, { ...seed.status, repo: "org/first", base_sha: "9999999zzzz", active: true }));
b1[1].resolve(jr(200, [])); b1[2].resolve(jr(200, seed.records));
await flush();
const staleRepo = {
  repo: pw2.store.getState().repo, statusRepo: pw2.store.getState().status?.repo,
  statusSha: pw2.store.getState().status?.base_sha, sha: d2.ids["base-sha-value"].textContent,
  attempts: pw2.store.getState().attempts.length,
};
pw2.livePoller.refresh();
const b3 = pending.splice(0, 3);
pw2.store.setMode("demo");
for (const req of b3) req.resolve(jr(500, { detail: "late" }));
await flush();
const staleDemo = { error: pw2.store.getState().fetchError, timers: t2.active.size };
pw2.livePoller.destroy();

const d3 = fakeDoc(), t3 = makeTimer(), postLog = [];
let postFn = null, active3 = false;
const fetch3 = async (url, init = {}) => {
  if (init.method === "POST") { postLog.push({ url, body: JSON.parse(init.body) }); return await postFn(); }
  if (url.endsWith("/status")) return jr(200, { ...seed.status, repo: "Aj604/Pit-Crew", active: active3 });
  return jr(200, url.endsWith("/attempts") ? seed.attempts : seed.records);
};
const pw3 = app.bootPitWall(d3.doc, "?repo=Aj604/Pit-Crew", { live: { fetch: fetch3, timer: t3, doc: d3.doc } });
await flush();
const p3 = pw3.livePoller.statusPill;
let resolvePost;
postFn = () => new Promise((r) => { resolvePost = r; });
d3.ids["run-pit-stop"].click(); d3.ids["run-pit-stop"].click();
const inFlight = { posts: postLog.length, btn: btnOf(d3) };
active3 = true; resolvePost(jr(202, { status: "started", repo: "Aj604/Pit-Crew" })); await flush();
const after202 = { body: postLog[0], btn: btnOf(d3).slice(0, 2), delay: t3.scheduled.at(-1), pillHidden: p3.hidden };
active3 = false; t3.fireNext(); await flush();
postFn = async () => { active3 = true; return jr(409, { detail: "Cycle already active for Aj604/Pit-Crew" }); };
d3.ids["run-pit-stop"].click(); await flush();
const after409 = { btn: btnOf(d3).slice(0, 2), pillHidden: p3.hidden, pillText: pillText(p3),
  delay: t3.scheduled.at(-1), laps: pw3.lapHistory.getRows().length };
active3 = false; t3.fireNext(); await flush();
const after409Clear = { pillHidden: p3.hidden, disabled: d3.ids["run-pit-stop"].disabled, delay: t3.scheduled.at(-1) };
postFn = async () => jr(503, { detail: "Dispatcher is not configured" });
d3.ids["run-pit-stop"].click(); await flush();
const after503 = {
  btn: btnOf(d3).slice(0, 2), pillHidden: p3.hidden, pillText: pillText(p3), laps: pw3.lapHistory.getRows().length,
};
postFn = async () => { throw new Error("connection reset"); };
d3.ids["run-pit-stop"].click(); await flush();
const afterPostNetErr = {
  btn: btnOf(d3).slice(0, 2), pillHidden: p3.hidden, pillText: pillText(p3), laps: pw3.lapHistory.getRows().length,
};
pw3.livePoller.destroy();

const d4 = fakeDoc(), t4 = makeTimer(), calls4 = [];
const fetch4 = async (url) => {
  calls4.push(url);
  if (url.endsWith("/status")) return jr(200, { ...seed.status, repo: "my-org/synced", active: false });
  return jr(200, url.endsWith("/attempts") ? seed.attempts : seed.records);
};
const pw4 = app.bootPitWall(d4.doc, "", { live: { fetch: fetch4, timer: t4, doc: d4.doc } });
d4.live.click(); d4.ids["run-pit-stop"].click();
const emptyRunErr = pillText(pw4.livePoller.statusPill);
d4.ids["repo-input"].value = "bad-slug"; d4.ids["repo-input"].dispatchEvent({ type: "change" });
const invalidSlugErr = pillText(pw4.livePoller.statusPill);
d4.ids["repo-input"].value = "my-org/synced"; d4.demo.click(); d4.live.click(); await flush();
const syncedFromInput = { repo: pw4.store.getState().repo, fetched: calls4.slice(0, 3), emptyRunErr, invalidSlugErr };
pw4.livePoller.destroy();

console.log(JSON.stringify({
  helpers, hydrated, idleState, onHttpErr, onNetErr, recovered, hiddenPaused, resumed, errBeforeDemo, inDemo,
  afterDestroy, staleRepo, staleDemo, inFlight, after202, after409, after409Clear, after503, afterPostNetErr,
  syncedFromInput,
}));
"""
)


_LIVE_EDGE_NODE_SCRIPT = (
    _NODE_HARNESS
    + """
const app = await import(process.argv[1]), seed = JSON.parse(process.argv[2]);
const idleGet = (url, repoOverride = null) => {
  const statusBody = { ...seed.status, ...(repoOverride ? { repo: repoOverride } : {}), active: false };
  if (url.endsWith("/status")) return jr(200, statusBody);
  return jr(200, url.endsWith("/attempts") ? seed.attempts : seed.records);
};

// 1) Stale POST resolves before readJsonSafe after repo switch + new trigger
const dA = fakeDoc(), tA = makeTimer(), postResolversA = [];
const pwA = app.bootPitWall(dA.doc, "?repo=org/first", {
  live: {
    fetch: (url, init = {}) => init.method === "POST"
      ? new Promise((resolve, reject) => postResolversA.push({ resolve, reject }))
      : Promise.resolve(idleGet(url)),
    timer: tA, doc: dA.doc,
  },
});
await flush();
dA.ids["run-pit-stop"].click();
dA.ids["repo-input"].value = "org/second";
dA.ids["repo-input"].dispatchEvent({ type: "change" });
await flush();
dA.ids["run-pit-stop"].click();
let staleJsonCalled = false;
postResolversA[0].resolve({
  ok: false, status: 503,
  async json() { staleJsonCalled = true; return { detail: "stale repo A 503" }; },
});
await flush();
const stalePostBeforeJson = {
  staleJsonCalled,
  inFlight: pwA.livePoller.getState().triggerInFlight,
  btn: btnOf(dA),
  error: pwA.store.getState().fetchError,
  pillHidden: pwA.livePoller.statusPill.hidden,
};
postResolversA[1].resolve(jr(202, { status: "started", repo: "org/second" }));
await flush();
pwA.livePoller.destroy();

// 2) Stale POST rejects in catch after repo switch + new trigger
const dB = fakeDoc(), tB = makeTimer(), postResolversB = [];
const pwB = app.bootPitWall(dB.doc, "?repo=org/first", {
  live: {
    fetch: (url, init = {}) => init.method === "POST"
      ? new Promise((resolve, reject) => postResolversB.push({ resolve, reject }))
      : Promise.resolve(idleGet(url)),
    timer: tB, doc: dB.doc,
  },
});
await flush();
dB.ids["run-pit-stop"].click();
pwB.store.setRepo("org/second");
await flush();
dB.ids["run-pit-stop"].click();
postResolversB[0].reject(new Error("stale network drop"));
await flush();
const stalePostReject = {
  inFlight: pwB.livePoller.getState().triggerInFlight,
  btn: btnOf(dB),
  error: pwB.store.getState().fetchError,
  pillHidden: pwB.livePoller.statusPill.hidden,
};
postResolversB[1].resolve(jr(202, { status: "started", repo: "org/second" }));
await flush();
pwB.livePoller.destroy();

// 3a) Mode switch to Demo while response.json() is pending
const dC1 = fakeDoc(), tC1 = makeTimer();
let resolveJsonC1;
const pwC1 = app.bootPitWall(dC1.doc, "?repo=org/first", {
  live: {
    fetch: (url, init = {}) => init.method === "POST"
      ? Promise.resolve({ ok: false, status: 503, json: () => new Promise((r) => { resolveJsonC1 = r; }) })
      : Promise.resolve(idleGet(url)),
    timer: tC1, doc: dC1.doc,
  },
});
await flush();
dC1.ids["run-pit-stop"].click();
await flush();
pwC1.store.setMode("demo");
resolveJsonC1({ detail: "late 503" });
await flush();
const staleJsonDemo = {
  mode: pwC1.store.getState().mode,
  error: pwC1.store.getState().fetchError,
  pillHidden: pwC1.livePoller.statusPill.hidden,
};
pwC1.livePoller.destroy();

// 3b) Repo switch + new trigger while Repo A's response.json() is pending
const dC2 = fakeDoc(), tC2 = makeTimer();
let resolveJsonC2, postCountC2 = 0, resolvePostB2;
const pwC2 = app.bootPitWall(dC2.doc, "?repo=org/first", {
  live: {
    fetch: (url, init = {}) => {
      if (init.method === "POST") {
        postCountC2 += 1;
        if (postCountC2 === 1) {
          return Promise.resolve({ ok: false, status: 503, json: () => new Promise((r) => { resolveJsonC2 = r; }) });
        }
        return new Promise((r) => { resolvePostB2 = r; });
      }
      return Promise.resolve(idleGet(url));
    },
    timer: tC2, doc: dC2.doc,
  },
});
await flush();
dC2.ids["run-pit-stop"].click();
await flush();
pwC2.store.setRepo("org/second");
await flush();
dC2.ids["run-pit-stop"].click();
resolveJsonC2({ detail: "late repo A 503" });
await flush();
const staleJsonRepo = {
  inFlight: pwC2.livePoller.getState().triggerInFlight,
  btn: btnOf(dC2),
  error: pwC2.store.getState().fetchError,
  pillHidden: pwC2.livePoller.statusPill.hidden,
};
resolvePostB2(jr(202, { status: "started", repo: "org/second" }));
await flush();
pwC2.livePoller.destroy();

// 4) resetCycle() runs before syncRunButton(s) on repoChanged (for both triggerInFlight and assumedActive)
const dD1 = fakeDoc(), tD1 = makeTimer();
const pwD1 = app.bootPitWall(dD1.doc, "?repo=org/first", {
  live: {
    fetch: (url, init = {}) => init.method === "POST" ? new Promise(() => {}) : Promise.resolve(idleGet(url)),
    timer: tD1, doc: dD1.doc,
  },
});
await flush();
dD1.ids["run-pit-stop"].click();
const beforeClearBtn = btnOf(dD1);
dD1.ids["repo-input"].value = "";
dD1.ids["repo-input"].dispatchEvent({ type: "change" });
const afterClearInFlight = {
  beforeClearBtn,
  btn: btnOf(dD1),
  inFlight: pwD1.livePoller.getState().triggerInFlight,
  repo: pwD1.store.getState().repo,
  activeTimers: tD1.active.size,
};
pwD1.livePoller.destroy();

const dD2 = fakeDoc(), tD2 = makeTimer();
let failGetsD2 = false;
const pwD2 = app.bootPitWall(dD2.doc, "?repo=org/first", {
  live: {
    fetch: async (url, init = {}) => {
      if (init.method === "POST") { failGetsD2 = true; return jr(202, { status: "started", repo: "org/first" }); }
      if (failGetsD2) return jr(500, { detail: "transient" });
      return idleGet(url);
    },
    timer: tD2, doc: dD2.doc,
  },
});
await flush();
dD2.ids["run-pit-stop"].click();
await flush();
const beforeSwitchAssumed = { btn: btnOf(dD2), assumedActive: pwD2.livePoller.getState().assumedActive };
dD2.doc.hidden = true;
pwD2.store.setRepo("org/other");
const afterSwitchAssumed = {
  beforeSwitchAssumed,
  btn: btnOf(dD2),
  assumedActive: pwD2.livePoller.getState().assumedActive,
};
pwD2.livePoller.destroy();

// 5) 409 immediately followed by idle status (active: false) clears pill and re-enables button
const dE = fakeDoc(), tE = makeTimer();
const pwE = app.bootPitWall(dE.doc, "?repo=org/first", {
  live: {
    fetch: async (url, init = {}) => init.method === "POST"
      ? jr(409, { detail: "Cycle already active for org/first" })
      : idleGet(url, "org/first"),
    timer: tE, doc: dE.doc,
  },
});
await flush();
dE.ids["run-pit-stop"].click();
await flush();
const conflictImmediateIdle = {
  pillHidden: pwE.livePoller.statusPill.hidden,
  error: pwE.store.getState().fetchError,
  btn: btnOf(dE),
  delay: tE.scheduled.at(-1),
};
pwE.livePoller.destroy();

// 6) repoInput change with valid slug + unchanged change (no refetch) + Enter on unchanged slug (refreshes)
const dF = fakeDoc(), tF = makeTimer(), callsF = [];
const pwF = app.bootPitWall(dF.doc, "?repo=org/first", {
  live: {
    fetch: async (url) => { callsF.push(url); return idleGet(url); },
    timer: tF, doc: dF.doc,
  },
});
await flush();
const callsAfterInit = callsF.length;
dF.ids["repo-input"].value = "org/from-change";
dF.ids["repo-input"].dispatchEvent({ type: "change" });
await flush();
const afterValidChange = {
  repo: pwF.store.getState().repo,
  newUrls: callsF.slice(callsAfterInit),
};
const callsBeforeUnchangedChange = callsF.length;
dF.ids["repo-input"].dispatchEvent({ type: "change" });
await flush();
const unchangedChangeNewCalls = callsF.length - callsBeforeUnchangedChange;
let enterPrevented = false;
const callsBeforeEnter = callsF.length;
dF.ids["repo-input"].dispatchEvent({ type: "keydown", key: "Enter", preventDefault() { enterPrevented = true; } });
await flush();
const afterEnterUnchanged = {
  enterPrevented,
  newUrls: callsF.slice(callsBeforeEnter),
};
pwF.livePoller.destroy();

console.log(JSON.stringify({
  stalePostBeforeJson, stalePostReject, staleJsonDemo, staleJsonRepo,
  afterClearInFlight, afterSwitchAssumed, conflictImmediateIdle,
  afterValidChange, unchangedChangeNewCalls, afterEnterUnchanged,
}));
"""
)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_live_poller_hydration_polling_resilience_and_trigger() -> None:
    assert NODE is not None
    subprocess.run([NODE, "--check", str(LIVE_JS)], check=True, capture_output=True)  # noqa: S603
    client = TestClient(create_app(_seed_ledger("aj604/pit-crew")))
    seed = {k: client.get(f"/api/repos/aj604/pit-crew/{k}").json() for k in ("status", "attempts", "records")}
    out = _node(_LIVE_NODE_SCRIPT, APP_JS.as_uri(), LIVE_JS.as_uri(), json.dumps(seed))

    assert out["helpers"]["intervals"] == [3000, 15000, 3000, 15000, 15000, 3000]
    assert out["helpers"]["paths"] == [
        {
            "repo": "My.Org/Pit-Crew",
            **{k: f"/api/repos/My.Org/Pit-Crew/{k}" for k in ("status", "attempts", "records")},
        },
        None,
        None,
    ]
    assert (out["helpers"]["detail"], out["helpers"]["validRecs"]) == (["oops", "", ""], [True, False, False])
    assert out["hydrated"] == {
        "urls": [["GET", f"/api/repos/aj604/pit-crew/{k}"] for k in ("status", "attempts", "records")],
        "sha": "e9b17d3", "median": "48.0", "p95": "82.0", "slaHidden": False, "laps": 1,
        "btn": [True, "true", "Pit Stop Running"], "pillMounted": True, "pillHidden": True,
        "pillRole": ["status", "polite"], "delayActive": 3000,
    }  # fmt: skip
    assert out["idleState"] == {"delayIdle": 15000, "btn": [False, "false", "Run Pit Stop"]}
    assert (out["onHttpErr"]["pillHidden"], "db locked" in out["onHttpErr"]["pillText"]) == (False, True)
    assert (out["onHttpErr"]["sha"], out["onHttpErr"]["laps"], out["onHttpErr"]["attempts"]) == ("e9b17d3", 1, 1)
    net_err = out["onNetErr"]
    assert (net_err["pillHidden"], "socket hang up" in net_err["pillText"], net_err["laps"]) == (False, True, 1)
    assert out["recovered"] == {"pillHidden": True, "fetchError": None}
    assert (out["hiddenPaused"], out["resumed"]) == (0, {"newCalls": 3, "activeTimers": 1})
    assert (out["errBeforeDemo"], out["inDemo"], out["afterDestroy"]) == (
        False, {"activeTimers": 0, "pillHidden": True}, {"destroyed": True, "pillRemoved": True},
    )  # fmt: skip
    assert out["staleRepo"] == {
        "repo": "org/second", "statusRepo": "org/second", "statusSha": "2222222aaaa", "sha": "2222222", "attempts": 1,
    }  # fmt: skip
    assert out["staleDemo"] == {"error": None, "timers": 0}
    assert out["inFlight"] == {"posts": 1, "btn": [True, "true", "Pit Stop Running"]}
    assert out["after202"] == {
        "body": {"url": "/triggers/cycle", "body": {"repo": "Aj604/Pit-Crew", "until_sla": False}},
        "btn": [True, "true"], "delay": 3000, "pillHidden": True,
    }  # fmt: skip
    assert out["after409"] == {
        "btn": [True, "true"], "pillHidden": False,
        "pillText": "Cycle already active for Aj604/Pit-Crew", "delay": 3000, "laps": 1,
    }  # fmt: skip
    assert out["after409Clear"] == {"pillHidden": True, "disabled": False, "delay": 15000}
    assert out["after503"] == {
        "btn": [False, "false"], "pillHidden": False, "pillText": "Dispatcher is not configured", "laps": 1,
    }  # fmt: skip
    assert (out["afterPostNetErr"]["btn"], out["afterPostNetErr"]["pillHidden"], out["afterPostNetErr"]["laps"]) == (
        [False, "false"], False, 1,
    )  # fmt: skip
    assert "connection reset" in out["afterPostNetErr"]["pillText"]
    assert out["syncedFromInput"]["repo"] == "my-org/synced"
    assert out["syncedFromInput"]["fetched"] == [
        f"/api/repos/my-org/synced/{k}" for k in ("status", "attempts", "records")
    ]
    assert "owner/name" in out["syncedFromInput"]["emptyRunErr"]
    assert "owner/name" in out["syncedFromInput"]["invalidSlugErr"]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_live_poller_stale_post_preserve_notice_and_repo_input() -> None:
    client = TestClient(create_app(_seed_ledger("aj604/pit-crew")))
    seed = {k: client.get(f"/api/repos/aj604/pit-crew/{k}").json() for k in ("status", "attempts", "records")}
    out = _node(_LIVE_EDGE_NODE_SCRIPT, APP_JS.as_uri(), json.dumps(seed))

    assert out["stalePostBeforeJson"] == {
        "staleJsonCalled": False,
        "inFlight": True,
        "btn": [True, "true", "Pit Stop Running"],
        "error": None,
        "pillHidden": True,
    }
    assert out["stalePostReject"] == {
        "inFlight": True,
        "btn": [True, "true", "Pit Stop Running"],
        "error": None,
        "pillHidden": True,
    }
    assert out["staleJsonDemo"] == {"mode": "demo", "error": None, "pillHidden": True}
    assert out["staleJsonRepo"] == {
        "inFlight": True,
        "btn": [True, "true", "Pit Stop Running"],
        "error": None,
        "pillHidden": True,
    }
    assert out["afterClearInFlight"] == {
        "beforeClearBtn": [True, "true", "Pit Stop Running"],
        "btn": [False, "false", "Run Pit Stop"],
        "inFlight": False,
        "repo": "",
        "activeTimers": 0,
    }
    assert out["afterSwitchAssumed"] == {
        "beforeSwitchAssumed": {"btn": [True, "true", "Pit Stop Running"], "assumedActive": True},
        "btn": [False, "false", "Run Pit Stop"],
        "assumedActive": False,
    }
    assert out["conflictImmediateIdle"] == {
        "pillHidden": True,
        "error": None,
        "btn": [False, "false", "Run Pit Stop"],
        "delay": 15000,
    }
    assert out["afterValidChange"] == {
        "repo": "org/from-change",
        "newUrls": [f"/api/repos/org/from-change/{k}" for k in ("status", "attempts", "records")],
    }
    assert out["unchangedChangeNewCalls"] == 0
    assert out["afterEnterUnchanged"] == {
        "enterPrevented": True,
        "newUrls": [f"/api/repos/org/from-change/{k}" for k in ("status", "attempts", "records")],
    }


_CDP_VERIFY_SCRIPT = """
const [wsUrl, baseUrl] = process.argv.slice(1);
const ws = new WebSocket(wsUrl);
await new Promise((res, rej) => {
  ws.addEventListener("open", res, { once: true }); ws.addEventListener("error", rej, { once: true });
});
let seq = 0; const inflight = new Map(), errors = [];
ws.addEventListener("message", (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && inflight.has(m.id)) {
    const { res, rej } = inflight.get(m.id); inflight.delete(m.id);
    if (m.error) rej(new Error(m.error.message)); else res(m.result);
  } else if (m.method === "Runtime.exceptionThrown") errors.push(m.params.exceptionDetails?.text ?? "exception");
  else if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error")
    errors.push(m.params.args.map((a) => a.value ?? a.description ?? "").join(" "));
});
const send = (method, params = {}) => new Promise((res, rej) => {
  const id = ++seq; inflight.set(id, { res, rej }); ws.send(JSON.stringify({ id, method, params }));
});
const evalJs = async (expression) => {
  const o = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (o.exceptionDetails) throw new Error(o.exceptionDetails.text);
  return o.result.value;
};
const waitFor = async (expr) => {
  for (const end = Date.now() + 8000; Date.now() < end; ) {
    if (await evalJs(expr)) return true;
    await new Promise((r) => setTimeout(r, 50));
  }
  return false;
};
await send("Page.enable"); await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: `${baseUrl}/?repo=aj604/pit-crew` });
await waitFor(`document.getElementById("base-sha-value")?.textContent === "e9b17d3"`);
const desktop = await evalJs(`(() => { const g = (i) => document.getElementById(i), de = document.documentElement;
  return { sha: g("base-sha-value").textContent, median: g("sla-median").textContent, p95: g("sla-p95").textContent,
    slaHidden: g("sla-badge").hidden, laps: document.querySelectorAll("#lap-history tbody tr").length,
    pillHidden: g("live-status-pill").hidden, overflow: de.scrollWidth > de.clientWidth }; })()`);
await evalJs(`document.getElementById("run-pit-stop").click()`);
await waitFor(`window.pitWall?.store?.getState()?.status?.active === true`);
const triggered = await evalJs(`(() => { const b = document.getElementById("run-pit-stop"); return {
  disabled: b.disabled, busy: b.getAttribute("aria-busy"), label: b.textContent,
  spinnerAnim: getComputedStyle(b, "::before").animationName,
}; })()`);
await evalJs(`fetch("/__test/fail-api", { method: "POST" }).then(() => window.pitWall.livePoller.refresh())`);
await waitFor(`document.getElementById("live-status-pill")?.hidden === false`);
await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
await new Promise((r) => setTimeout(r, 100));
const mobile = await evalJs(`(() => { const p = document.getElementById("live-status-pill"); return {
  pillHidden: p.hidden, pillText: p.textContent, pillColor: getComputedStyle(p).color,
  laps: document.querySelectorAll("#lap-history tbody tr").length,
  sha: document.getElementById("base-sha-value").textContent,
  overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
}; })()`);
ws.close();
console.log(JSON.stringify({ desktop, triggered, mobile, errors }));
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _terminate_process_group(proc: subprocess.Popen[bytes], *, timeout: float = 5.0) -> None:
    if os.name == "posix":
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGTERM)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=timeout)


def _run_cdp_against(app_port: int) -> dict[str, Any]:
    assert CHROME is not None
    cdp_port = _free_port()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as user_data:
        proc = subprocess.Popen(  # noqa: S603
            [CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
             f"--remote-debugging-port={cdp_port}", f"--user-data-dir={user_data}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )  # fmt: skip
        try:
            ws_url, deadline = "", time.monotonic() + 10
            while time.monotonic() < deadline and not ws_url:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/list", timeout=1) as resp:
                        targets = json.loads(resp.read().decode())
                    page = next((t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")), None)
                    ws_url = str(page["webSocketDebuggerUrl"]) if page else ""
                except OSError:
                    time.sleep(0.1)
            assert ws_url, "Chrome CDP endpoint did not start"
            return _node(_CDP_VERIFY_SCRIPT, ws_url, f"http://127.0.0.1:{app_port}")
        finally:
            _terminate_process_group(proc)


def test_terminate_process_group_falls_back_to_sigkill() -> None:
    script = (
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(30)"
    )
    with subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    ) as proc:
        try:
            assert proc.stdout is not None
            assert proc.stdout.readline().strip() == b"ready"
            _terminate_process_group(proc, timeout=0.1)
            assert proc.poll() is not None
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


@pytest.mark.skipif(NODE is None or CHROME is None, reason="node or Chrome is not installed")
def test_live_mode_browser_verification_desktop_and_mobile() -> None:
    dispatcher, fail_api = _BlockingDispatcher(), threading.Event()
    app = create_app(_seed_ledger("aj604/pit-crew"), dispatcher=dispatcher)

    @app.post("/__test/fail-api")
    def _enable_fail_api() -> dict[str, bool]:
        fail_api.set()
        return {"failing": True}

    @app.middleware("http")
    async def _fault_injector(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if fail_api.is_set() and request.url.path.startswith("/api/repos/"):
            return JSONResponse({"detail": "Upstream telemetry down"}, status_code=503)
        return await call_next(request)

    app_port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=app_port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        out = _run_cdp_against(app_port)
    finally:
        dispatcher.release.set()
        server.should_exit = True
        thread.join(timeout=5)

    assert out["errors"] == []
    assert out["desktop"] == {
        "sha": "e9b17d3", "median": "48.0", "p95": "82.0", "slaHidden": False,
        "laps": 1, "pillHidden": True, "overflow": False,
    }  # fmt: skip
    assert out["triggered"] == {
        "disabled": True, "busy": "true", "label": "Pit Stop Running", "spinnerAnim": "pc-spin",
    }  # fmt: skip
    assert dispatcher.calls == [("run_cycle", "aj604/pit-crew")]
    mob = out["mobile"]
    assert (mob["pillHidden"], "Upstream telemetry down" in mob["pillText"], mob["overflow"]) == (False, True, False)
    assert (mob["pillColor"], mob["laps"], mob["sha"]) == ("rgb(255, 208, 0)", 1, "e9b17d3")
