// Sector Pipeline Strip (#49): 5-stage horizontal strip + glowing Trust Wall divider.
// Plain ES module, no build step. Subscribes to store.js and renders into #sector-pipeline.

export const TRUST_WALL_TITLE = "TRUST WALL";
export const TRUST_WALL_LABEL = "AGENT PROPOSES | CODE JUDGES";

export const PIPELINE_STAGES = Object.freeze([
  Object.freeze({ id: "analysis", code: "ANA", label: "Analysis", zone: "agent" }),
  Object.freeze({ id: "optimizer", code: "OPT", label: "Optimizer", zone: "agent" }),
  Object.freeze({ id: "proof", code: "PRF", label: "Proof", zone: "agent" }),
  Object.freeze({ id: "ci", code: "DSP", label: "Fork CI", zone: "judge" }),
  Object.freeze({ id: "rater", code: "RAT", label: "Rater", zone: "judge" }),
]);

export const COMPOUND_RINGS = Object.freeze({
  "dep-cache": Object.freeze({ code: "S", label: "Soft", token: "--pc-compound-soft" }),
  "shard-by-timing": Object.freeze({ code: "M", label: "Medium", token: "--pc-compound-medium" }),
  "affected-tests": Object.freeze({ code: "H", label: "Hard", token: "--pc-compound-hard" }),
  "dedupe-jobs": Object.freeze({ code: "HS", label: "Hypersoft", token: "--pc-compound-hypersoft" }),
  "parallelize-dag": Object.freeze({ code: "I", label: "Inter", token: "--pc-compound-inter" }),
});
export const FALLBACK_COMPOUND = Object.freeze({ code: "?", label: "Custom", token: "--pc-ink-muted" });

const EMPTY = "--";
const MAX_INTERLEAVE_PAIRS = 6;
const isNum = (v) => typeof v === "number" && Number.isFinite(v);

/** Return the compound ring descriptor for an Optimizer playbook skill. */
export const compoundForSkill = (skill) => (typeof skill === "string" && COMPOUND_RINGS[skill]) || FALLBACK_COMPOUND;

/** Format expected savings seconds as a telemetry delta (e.g. `-40.0s`). */
export const formatSavings = (s) => (!isNum(s) ? EMPTY : s > 0 ? `-${s.toFixed(1)}s` : `${Math.abs(s).toFixed(1)}s`);

/** Format a 0..1 confidence score as a whole percentage. */
export const formatConfidence = (c) => (isNum(c) ? `${Math.round(c * 100)}%` : EMPTY);

/** Format a speedup multiplier (e.g. `1.85x`). */
export const formatSpeedup = (x) => (isNum(x) ? `${x.toFixed(2)}x` : EMPTY);

/** Format a permutation test p-value, handling null cleanly. */
export function formatPValue(p) {
  if (!isNum(p)) return "n/a";
  return p < 0.001 ? "<0.001" : p < 0.01 ? p.toFixed(3) : p.toFixed(2);
}

/** Resolve the active or selected attempt's linked records from store state. */
export function resolveAttemptRecords(state) {
  const rec = state?.records ?? {};
  const list = (k) => (Array.isArray(rec[k]) ? rec[k] : []);
  const hypotheses = list("hypotheses");
  const candidates = list("candidates");
  const attempt = state?.selectedAttempt ?? null;
  const hasIds = Boolean(attempt?.hypothesis_id || attempt?.candidate_id);

  const candidate = hasIds
    ? attempt.candidate_id ? (candidates.find((c) => c.id === attempt.candidate_id) ?? null) : null
    : (candidates.at(-1) ?? null);
  const hypId = attempt?.hypothesis_id ?? candidate?.hypothesis_id ?? null;
  const hypothesis = hypId
    ? (hypotheses.find((h) => h.id === hypId) ?? (hasIds ? null : (hypotheses.at(-1) ?? null)))
    : (hypotheses.at(-1) ?? null);
  const candId = attempt?.candidate_id ?? candidate?.id ?? null;
  const byCand = (k) => {
    const items = list(k);
    return candId ? (items.find((x) => x.candidate_id === candId) ?? null) : hasIds ? null : (items.at(-1) ?? null);
  };
  return {
    hypothesis, candidate, replayPlan: byCand("replay_plans"), evidence: byCand("evidence"),
    verdict: byCand("verdicts"), skill: hypothesis?.skill || attempt?.skill || "",
  };
}

function stageStates(mode, stageIndex, present) {
  const last = present.lastIndexOf(true);
  return PIPELINE_STAGES.map((_, i) => {
    if (mode === "demo") return i < stageIndex ? "done" : i === stageIndex ? "current" : "pending";
    if (last === -1) return "pending";
    if (present[4] || i < last) return present[i] ? "done" : "pending";
    return i === last ? "current" : "pending";
  });
}

/** Pure: compute the view-model for the 5-stage Sector Pipeline Strip and Trust Wall. */
export function pipelineViewFromState(state) {
  const mode = state?.mode === "live" ? "live" : "demo";
  const stageIndex = Number.isInteger(state?.stageIndex) ? state.stageIndex : 0;
  const { hypothesis, candidate, replayPlan, evidence, verdict, skill } = resolveAttemptRecords(state);
  const states = stageStates(mode, stageIndex, [hypothesis, candidate, replayPlan, evidence, verdict].map(Boolean));

  const analysis = hypothesis && {
    workflowPath: hypothesis.workflow_path || EMPTY, job: hypothesis.job || EMPTY,
    step: hypothesis.step || "(job-level)", bucket: hypothesis.bucket || EMPTY,
    expectedSavings: formatSavings(hypothesis.expected_savings_seconds),
    confidence: formatConfidence(hypothesis.confidence), description: hypothesis.description || "",
  };

  const files = candidate?.files && typeof candidate.files === "object"
    ? Object.entries(candidate.files).sort(([a], [b]) => a.localeCompare(b)).map(([path, content]) => ({ path, content: String(content ?? "") }))
    : [];
  const changedFiles = Array.isArray(candidate?.changed_files) && candidate.changed_files.length > 0
    ? [...candidate.changed_files] : files.map((f) => f.path);
  const optimizer = candidate && {
    candidateId: candidate.id || "", skill: skill || "unknown", compound: compoundForSkill(skill),
    branch: candidate.branch || EMPTY, changedFiles, summary: candidate.summary || "", files,
  };

  const cases = Array.isArray(replayPlan?.cases) ? replayPlan.cases : [];
  const bugShas = Array.isArray(replayPlan?.bug_shas) ? replayPlan.bug_shas : [];
  const commitCount = Math.max(bugShas.length, cases.filter((c) => c?.kind === "commit").length);
  const mutationCount = cases.filter((c) => c?.kind === "mutation").length;
  const proof = replayPlan && { commitCount, mutationCount, totalCount: commitCount + mutationCount };

  const baselineRuns = Array.isArray(evidence?.baseline_seconds) ? evidence.baseline_seconds.length : 0;
  const candidateRuns = Array.isArray(evidence?.candidate_seconds) ? evidence.candidate_seconds.length : 0;
  const bugsCaught = isNum(evidence?.bugs_caught) ? evidence.bugs_caught : 0;
  const bugsTotal = isNum(evidence?.bugs_total) ? evidence.bugs_total : 0;
  const ci = evidence && {
    baselineRuns, candidateRuns, totalRuns: baselineRuns + candidateRuns,
    bugsCaught, bugsTotal, allBugsCaught: bugsTotal > 0 && bugsCaught >= bugsTotal,
  };

  const rater = verdict && {
    keep: verdict.keep === true, flag: verdict.keep === true ? "KEEP" : "REJECT",
    speedup: formatSpeedup(verdict.speedup), pValue: formatPValue(verdict.p_value),
    reason: verdict.reason || "", slaMet: verdict.sla_met === true,
  };

  const dataList = [analysis || null, optimizer || null, proof || null, ci || null, rater || null];
  return {
    mode, stageIndex,
    trustWall: { title: TRUST_WALL_TITLE, label: TRUST_WALL_LABEL, crossed: states[3] !== "pending" || states[4] !== "pending" },
    stages: PIPELINE_STAGES.map((meta, i) => ({ ...meta, state: states[i], data: dataList[i] })),
  };
}

const el = (root, tag, className, text = "") => Object.assign(root.createElement(tag), { className, textContent: text });

function kvList(root, rows) {
  const dl = el(root, "dl", "pipeline-kv");
  for (const [label, value, opts = {}] of rows) {
    const row = el(root, "div", "pipeline-kv__row");
    const dd = el(root, "dd", opts.mono ? "pipeline-kv__value telemetry" : "pipeline-kv__value", value);
    if (opts.accent) dd.dataset.accent = opts.accent;
    row.append(el(root, "dt", "pipeline-kv__label", label), dd);
    dl.append(row);
  }
  return dl;
}

function renderOptimizer(root, data, isExpanded, onToggle) {
  const badge = el(root, "div", "compound-badge");
  Object.assign(badge.dataset, { skill: data.skill, compoundToken: data.compound.token });
  const ring = el(root, "span", "compound-badge__ring telemetry", data.compound.code);
  ring.setAttribute("aria-hidden", "true");
  badge.append(ring, el(root, "span", "compound-badge__skill telemetry", data.skill), el(root, "span", "compound-badge__tier", data.compound.label));

  const details = el(root, "details", "pipeline-files");
  details.open = isExpanded;
  details.dataset.candidateId = data.candidateId;
  const list = el(root, "div", "pipeline-files__list");
  if (data.files.length === 0) {
    list.append(el(root, "p", "pipeline-files__empty", "No file contents recorded."));
  } else {
    for (const entry of data.files) {
      const item = el(root, "div", "pipeline-files__entry");
      const pre = el(root, "pre", "pipeline-files__code");
      pre.append(el(root, "code", "", entry.content));
      item.append(el(root, "div", "pipeline-files__path telemetry", entry.path), pre);
      list.append(item);
    }
  }
  const n = data.files.length;
  details.append(el(root, "summary", "pipeline-files__toggle", n === 1 ? "Candidate file (1)" : `Candidate files (${n})`), list);
  details.addEventListener("toggle", () => onToggle(details.open));

  const nodes = [
    badge,
    kvList(root, [
      ["Branch", data.branch, { mono: true }],
      ["Changed", data.changedFiles.length > 0 ? data.changedFiles.join(", ") : EMPTY, { mono: true }],
    ]),
  ];
  if (data.summary) nodes.push(el(root, "p", "pipeline-stage__summary", data.summary));
  nodes.push(details);
  return { nodes, fileViewer: details };
}

function renderStageNodes(root, { id, data }, fileOpts) {
  if (id === "analysis") {
    return {
      nodes: [kvList(root, [
        ["Workflow", data.workflowPath, { mono: true }], ["Job", data.job, { mono: true }], ["Step", data.step],
        ["Bucket", data.bucket, { mono: true }], ["Est. Saving", data.expectedSavings, { mono: true, accent: "cyan" }],
        ["Confidence", data.confidence, { mono: true }],
      ])],
    };
  }
  if (id === "optimizer") return renderOptimizer(root, data, fileOpts.isExpanded, fileOpts.onToggle);
  if (id === "proof") {
    const counts = el(root, "div", "pipeline-proof-counts");
    for (const [val, lbl] of [
      [data.commitCount, data.commitCount === 1 ? "Commit SHA" : "Commit SHAs"],
      [data.mutationCount, data.mutationCount === 1 ? "Mutation" : "Mutations"],
    ]) {
      const box = el(root, "div", "pipeline-metric");
      box.append(el(root, "span", "pipeline-metric__value telemetry", String(val)), el(root, "span", "pipeline-metric__label", lbl));
      counts.append(box);
    }
    return { nodes: [counts, kvList(root, [["Replay Suite", `${data.totalCount} cases`, { mono: true }]])] };
  }
  if (id === "ci") {
    const track = el(root, "div", "pipeline-interleave");
    track.setAttribute("aria-label", `Interleaved timing runs: ${data.baselineRuns} baseline, ${data.candidateRuns} candidate`);
    const pairs = Math.min(Math.max(data.baselineRuns, data.candidateRuns), MAX_INTERLEAVE_PAIRS);
    for (let i = 0; i < pairs; i += 1) {
      if (i < data.baselineRuns) track.append(el(root, "span", "pipeline-interleave__pip", "B"));
      if (i < data.candidateRuns) {
        const pip = el(root, "span", "pipeline-interleave__pip", "C");
        pip.dataset.arm = "candidate";
        track.append(pip);
      }
    }
    return {
      nodes: [track, kvList(root, [
        ["Interleaved", `${data.baselineRuns}B · ${data.candidateRuns}C (${data.totalRuns})`, { mono: true }],
        ["Bugs Caught", `${data.bugsCaught} / ${data.bugsTotal}`, { mono: true, accent: data.allBugsCaught ? "green" : "red" }],
      ])],
    };
  }
  const flag = el(root, "div", "pipeline-verdict-flag", data.flag);
  flag.dataset.verdict = data.keep ? "keep" : "reject";
  const nodes = [
    flag,
    kvList(root, [["Speedup", data.speedup, { mono: true, accent: data.keep ? "green" : "red" }], ["p-value", data.pValue, { mono: true }]]),
  ];
  if (data.reason) nodes.push(el(root, "p", "pipeline-stage__summary", data.reason));
  return { nodes };
}

/** Render the 5-stage Sector Pipeline Strip and Trust Wall into `slot` and subscribe to `store`. */
export function createSectorPipeline(root, slot, store) {
  const strip = el(root, "div", "sector-pipeline");
  const zones = el(root, "div", "sector-pipeline__zones");
  zones.append(
    el(root, "span", "sector-pipeline__zone sector-pipeline__zone--agent", "Agent Proposes"),
    el(root, "span", "sector-pipeline__zone sector-pipeline__zone--judge", "Deterministic Code Judges"),
  );
  const track = el(root, "div", "sector-pipeline__track");
  const stages = {};
  const refs = {};
  const buildCard = (meta) => {
    const card = el(root, "article", "pipeline-stage");
    Object.assign(card.dataset, { stage: meta.id, zone: meta.zone, state: "pending" });
    const head = el(root, "header", "pipeline-stage__head");
    const badge = el(root, "span", "pipeline-stage__status telemetry", "Pending");
    head.append(el(root, "span", "pipeline-stage__code telemetry", meta.code), el(root, "h2", "pipeline-stage__title", meta.label), badge);
    const body = el(root, "div", "pipeline-stage__body");
    card.append(head, body);
    stages[meta.id] = card;
    refs[meta.id] = { card, badge, body };
    return card;
  };
  const trustWall = el(root, "div", "pipeline-trust-wall");
  trustWall.setAttribute("role", "separator");
  trustWall.setAttribute("aria-label", `${TRUST_WALL_TITLE}: ${TRUST_WALL_LABEL}`);
  trustWall.dataset.crossed = "false";
  trustWall.append(
    el(root, "span", "pipeline-trust-wall__title", TRUST_WALL_TITLE),
    el(root, "span", "pipeline-trust-wall__label telemetry", TRUST_WALL_LABEL),
  );
  track.append(buildCard(PIPELINE_STAGES[0]), buildCard(PIPELINE_STAGES[1]), buildCard(PIPELINE_STAGES[2]), trustWall, buildCard(PIPELINE_STAGES[3]), buildCard(PIPELINE_STAGES[4]));
  strip.append(zones, track);
  slot.replaceChildren(strip);

  let expandedCandidateId = null;
  let lastCandidateId = null;
  const handle = { slot, stages, trustWall, fileViewer: null, render: null, destroy: null };

  const render = (state) => {
    const view = pipelineViewFromState(state);
    trustWall.dataset.crossed = String(view.trustWall.crossed);
    const candId = view.stages[1].data?.candidateId ?? null;
    if (candId !== lastCandidateId) {
      expandedCandidateId = null;
      lastCandidateId = candId;
    }
    handle.fileViewer = null;
    for (const stage of view.stages) {
      const ref = refs[stage.id];
      ref.card.dataset.state = stage.state;
      ref.card.setAttribute("aria-current", stage.state === "current" ? "step" : "false");
      ref.badge.textContent = stage.id === "rater" && stage.data ? stage.data.flag : stage.state === "done" ? "Done" : stage.state === "current" ? "Active" : "Pending";
      if (stage.id === "rater") ref.card.dataset.verdict = stage.data ? (stage.data.keep ? "keep" : "reject") : "pending";
      if (!stage.data) {
        ref.body.replaceChildren(el(root, "p", "pipeline-stage__empty", `Awaiting ${stage.label}`));
        continue;
      }
      const rendered = renderStageNodes(root, stage, {
        isExpanded: Boolean(candId && expandedCandidateId === candId),
        onToggle(open) { expandedCandidateId = open ? candId : null; },
      });
      if (rendered.fileViewer) handle.fileViewer = rendered.fileViewer;
      ref.body.replaceChildren(...rendered.nodes);
    }
  };
  handle.render = render;
  render(store.getState());
  handle.destroy = store.subscribe(render);
  return handle;
}
