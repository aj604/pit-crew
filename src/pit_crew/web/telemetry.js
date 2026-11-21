// Pit Wall Telemetry Center (#50): Interleaved Timing SVG chart & Bug Replay Matrix.
// Plain ES module, no build step. Colors come solely from CSS classes backed by /brand/tokens.css.

export const SLA_TARGET_SECONDS = 60;
export const CHART_VIEWBOX = Object.freeze({ width: 600, height: 280 });

const SVG_NS = "http://www.w3.org/2000/svg";
const EM_DASH = "\u2014";
const PLOT = Object.freeze({ x: 48, y: 24, width: 460, height: 208 });
const MIN_LABEL_GAP = 14;

const isNum = (v) => typeof v === "number" && Number.isFinite(v);
const isNonNeg = (v) => isNum(v) && v >= 0;
const round2 = (v) => Math.round(v * 100) / 100;

function findLast(items, pred) {
  for (let i = items.length - 1; i >= 0; i -= 1) if (pred(items[i])) return items[i];
  return null;
}

/** Pure: median of finite numbers, matching Python's `statistics.median`. */
export function computeMedian(values) {
  if (!Array.isArray(values)) return null;
  const s = values.filter(isNum).sort((a, b) => a - b);
  if (s.length === 0) return null;
  const m = Math.floor(s.length / 2);
  return s.length % 2 === 1 ? s[m] : (s[m - 1] + s[m]) / 2;
}

/** Pure: interpolated 95th percentile, matching `pit_crew.rater._p95`. */
export function computeP95(values) {
  if (!Array.isArray(values)) return null;
  const s = values.filter(isNum).sort((a, b) => a - b);
  if (s.length === 0) return null;
  if (s.length === 1) return s[0];
  const pos = 0.95 * (s.length - 1);
  const lo = Math.floor(pos);
  return s[lo] + (pos - lo) * (s[Math.min(lo + 1, s.length - 1)] - s[lo]);
}

/** Pure: allow only valid http(s) URLs for clickable external links. */
export function isSafeExternalUrl(url) {
  if (typeof url !== "string" || !url.trim()) return false;
  try {
    const { protocol } = new URL(url.trim());
    return protocol === "http:" || protocol === "https:";
  } catch {
    return false;
  }
}
export const formatMetricSeconds = (s) => (isNum(s) ? `${s.toFixed(1)}s` : EM_DASH);
export const formatSpeedup = (x) => (isNum(x) ? `${x.toFixed(2)}x` : EM_DASH);
export function formatPValue(p) {
  if (!isNum(p)) return EM_DASH;
  if (p > 0 && p < 0.001) return "<0.001";
  return p < 0.01 ? p.toFixed(3) : p.toFixed(2);
}

/** Pure: resolve selected attempt's Evidence and Verdict by candidate_id (falling back to latest only when no attempt is selected). */
export function resolveAttemptTelemetry(state) {
  const rec = state?.records ?? {};
  const selected = state?.selectedAttempt ?? null;
  if (selected !== null) {
    const cid = typeof selected.candidate_id === "string" && selected.candidate_id.trim() ? selected.candidate_id : null;
    if (!cid) return { candidateId: null, evidence: null, verdict: null };
    const byCid = (list) => (Array.isArray(list) ? findLast(list, (r) => r?.candidate_id === cid) : null);
    return { candidateId: cid, evidence: byCid(rec.evidence), verdict: byCid(rec.verdicts) };
  }
  const latest = (list) => (Array.isArray(list) ? (list.at(-1) ?? null) : null);
  return { candidateId: null, evidence: latest(rec.evidence), verdict: latest(rec.verdicts) };
}

/** Pure: normalize one `Evidence.bug_outcomes` entry across Demo and Proof-lane shapes. */
export function normalizeBugOutcome(outcome) {
  const item = outcome && typeof outcome === "object" ? outcome : {};
  const rawRef = String(item.ref ?? item.commit_sha ?? item.sha ?? "").trim();
  const kind =
    typeof item.kind === "string" && item.kind.trim()
      ? item.kind.trim().toLowerCase()
      : item.commit_sha || item.sha
        ? "commit"
        : "unknown";
  const baselineFailed = typeof item.baseline_failed === "boolean" ? item.baseline_failed : null;
  const caught =
    typeof item.caught === "boolean"
      ? item.caught
      : typeof item.status === "string" && item.status.trim().toLowerCase() === "caught";
  const rawUrl = typeof item.run_url === "string" ? item.run_url.trim() : "";
  return {
    kind, rawRef, displayRef: rawRef ? (kind === "commit" ? rawRef.slice(0, 7) : rawRef) : EM_DASH,
    baselineFailed, baselineLabel: baselineFailed === true ? "FAIL" : baselineFailed === false ? "PASS" : EM_DASH,
    caught, statusLabel: caught ? "CAUGHT" : "MISSED", runUrl: isSafeExternalUrl(rawUrl) ? rawUrl : null,
  };
}

function deconflictLabels(lines, minY, maxY) {
  const sorted = [...lines].sort((a, b) => a.y - b.y);
  for (let i = 0; i < sorted.length; i += 1) {
    sorted[i].labelY = round2(Math.max(sorted[i].y, (i === 0 ? minY - MIN_LABEL_GAP : sorted[i - 1].labelY) + MIN_LABEL_GAP));
  }
  for (let i = sorted.length - 1; i >= 0; i -= 1) {
    const limit = i === sorted.length - 1 ? maxY : sorted[i + 1].labelY - MIN_LABEL_GAP;
    if (sorted[i].labelY > limit) sorted[i].labelY = round2(Math.max(minY, limit));
  }
  return sorted;
}

/** Pure: compute SVG geometry, reference lines, and summary readouts for the Interleaved Timing chart. */
export function buildTimingChartModel(evidence, verdict) {
  const baseline = Array.isArray(evidence?.baseline_seconds) ? evidence.baseline_seconds.filter(isNonNeg) : [];
  const candidate = Array.isArray(evidence?.candidate_seconds) ? evidence.candidate_seconds.filter(isNonNeg) : [];
  const baselineMedian = computeMedian(baseline);
  const candidateMedian = (isNum(verdict?.median_seconds) ? verdict.median_seconds : null) ?? computeMedian(candidate);
  const p95 = (isNum(verdict?.p95_seconds) ? verdict.p95_seconds : null) ?? computeP95(candidate);
  const calcSpeedup = baselineMedian !== null && candidateMedian !== null && candidateMedian > 0 ? baselineMedian / candidateMedian : null;
  const speedup = (isNum(verdict?.speedup) ? verdict.speedup : null) ?? calcSpeedup;
  const pValue = isNum(verdict?.p_value) ? verdict.p_value : null;
  const summary = {
    speedup: formatSpeedup(speedup), pValue: formatPValue(pValue),
    median: formatMetricSeconds(candidateMedian), p95: formatMetricSeconds(p95),
    baselineMedian, candidateMedian, slaMet: verdict?.sla_met === true, keep: verdict ? verdict.keep === true : null,
  };

  const runCount = Math.max(baseline.length, candidate.length);
  if (runCount === 0) {
    return { hasData: false, runCount: 0, viewBox: CHART_VIEWBOX, plot: PLOT, yMax: 0, yTicks: [], runs: [], referenceLines: [], summary };
  }

  const ceiling = [SLA_TARGET_SECONDS, ...baseline, ...candidate, ...(baselineMedian !== null ? [baselineMedian] : []), ...(candidateMedian !== null ? [candidateMedian] : [])];
  const rawMax = Math.max(...ceiling);
  const targetStep = (rawMax * 1.1) / 4;
  const step = targetStep <= 20 ? 20 : targetStep <= 30 ? 30 : targetStep <= 40 ? 40 : Math.ceil(targetStep / 20) * 20;
  const yMax = Math.max(step * 3, Math.ceil((rawMax * 1.08) / step) * step);
  const yFor = (s) => round2(PLOT.y + PLOT.height - (s / yMax) * PLOT.height);
  const hFor = (s) => round2((s / yMax) * PLOT.height);
  const yTicks = [];
  for (let t = 0; t <= yMax; t += step) yTicks.push({ value: t, label: `${t}s`, y: yFor(t) });

  const slotW = PLOT.width / runCount;
  const barW = round2(Math.min(24, slotW * 0.28));
  const gap = round2(Math.min(8, slotW * 0.08));
  const pt = (val, x) => (val === null ? null : { seconds: val, x, y: yFor(val), width: barW, height: hFor(val), dotX: round2(x + barW / 2), dotY: yFor(val) });
  const runs = Array.from({ length: runCount }, (_, i) => {
    const slotX = PLOT.x + i * slotW;
    const centerX = slotX + slotW / 2;
    return {
      runNumber: i + 1, isCold: i === 0, slotX: round2(slotX), slotWidth: round2(slotW), centerX: round2(centerX),
      baseline: pt(baseline[i] ?? null, round2(centerX - gap / 2 - barW)),
      candidate: pt(candidate[i] ?? null, round2(centerX + gap / 2)),
    };
  });

  const makeRef = (id, label, value) => ({ id, label, value, y: yFor(value), labelY: yFor(value) });
  const refs = [makeRef("sla-target", `SLA ${SLA_TARGET_SECONDS}s`, SLA_TARGET_SECONDS)];
  if (baselineMedian !== null) refs.push(makeRef("baseline-median", `BASE ${formatMetricSeconds(baselineMedian)}`, baselineMedian));
  if (candidateMedian !== null) refs.push(makeRef("candidate-median", `CAND ${formatMetricSeconds(candidateMedian)}`, candidateMedian));
  return {
    hasData: true, runCount, viewBox: CHART_VIEWBOX, plot: PLOT, yMax, yTicks, runs,
    referenceLines: deconflictLabels(refs, PLOT.y + 10, PLOT.y + PLOT.height - 4), summary,
  };
}

/** Pure: compute Bug Replay Matrix view-model from `Evidence`. */
export function buildBugMatrixModel(evidence) {
  if (!evidence) return { hasEvidence: false, rows: [], total: 0, caught: 0, missed: 0 };
  const rows = (Array.isArray(evidence.bug_outcomes) ? evidence.bug_outcomes : []).map(normalizeBugOutcome);
  const total = isNonNeg(evidence.bugs_total) && evidence.bugs_total >= rows.length ? evidence.bugs_total : rows.length;
  const caught = rows.length > 0 ? rows.filter((r) => r.caught).length : isNonNeg(evidence.bugs_caught) ? evidence.bugs_caught : 0;
  return { hasEvidence: true, rows, total, caught, missed: Math.max(0, total - caught) };
}

function makeEl(root, tag, className = "", text = "", attrs = null) {
  const node = root.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  if (attrs) for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  return node;
}

function makeSvg(root, tag, attrs = {}, text = null) {
  const node = typeof root.createElementNS === "function" ? root.createElementNS(SVG_NS, tag) : root.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) node.setAttribute(k, String(v));
  if (text !== null) node.textContent = text;
  return node;
}

function renderTimingSvg(root, model) {
  const { viewBox, plot, yTicks, runs, referenceLines } = model;
  const svg = makeSvg(root, "svg", {
    class: "timing-chart__svg", viewBox: `0 0 ${viewBox.width} ${viewBox.height}`, role: "img",
    "aria-label": `Interleaved timing chart across ${model.runCount} runs, baseline median ${formatMetricSeconds(model.summary.baselineMedian)}, candidate median ${model.summary.median}, 60s SLA target`,
  });
  const grid = makeSvg(root, "g", { class: "timing-chart__grid" });
  for (const t of yTicks) {
    grid.append(
      makeSvg(root, "line", { class: "timing-chart__grid-line", x1: plot.x, x2: plot.x + plot.width, y1: t.y, y2: t.y }),
      makeSvg(root, "text", { class: "timing-chart__axis-label telemetry", x: plot.x - 8, y: t.y + 4, "text-anchor": "end" }, t.label),
    );
  }
  const runsGroup = makeSvg(root, "g", { class: "timing-chart__runs" });
  for (const run of runs) {
    const group = makeSvg(root, "g", {
      class: run.isCold ? "timing-chart__run timing-chart__run--cold" : "timing-chart__run",
      "data-run": run.runNumber, "data-cold": String(run.isCold),
    });
    if (run.isCold) {
      group.append(
        makeSvg(root, "rect", {
          class: "timing-chart__cold-band", "data-cold-run": "true",
          x: round2(run.slotX + 4), y: plot.y, width: round2(Math.max(12, run.slotWidth - 8)), height: plot.height, rx: 4,
        }),
        makeSvg(root, "text", { class: "timing-chart__cold-tag telemetry", x: run.centerX, y: plot.y - 7, "text-anchor": "middle" }, "COLD #1"),
      );
    }
    for (const [series, p] of [["baseline", run.baseline], ["candidate", run.candidate]]) {
      if (!p) continue;
      const bar = makeSvg(root, "rect", {
        class: `timing-chart__bar timing-chart__bar--${series}`, "data-series": series, "data-run": run.runNumber,
        "data-seconds": p.seconds, x: p.x, y: p.y, width: p.width, height: p.height, rx: 2,
      });
      bar.append(makeSvg(root, "title", {}, `Run ${run.runNumber} ${series}${run.isCold ? " (Cold)" : ""}: ${p.seconds.toFixed(1)}s`));
      group.append(bar, makeSvg(root, "circle", {
        class: `timing-chart__dot timing-chart__dot--${series}`, "data-series": series, "data-run": run.runNumber,
        cx: p.dotX, cy: p.dotY, r: 3.5,
      }));
    }
    group.append(makeSvg(root, "text", {
      class: run.isCold ? "timing-chart__run-label timing-chart__run-label--cold telemetry" : "timing-chart__run-label telemetry",
      x: run.centerX, y: plot.y + plot.height + 18, "text-anchor": "middle",
    }, `#${run.runNumber}`));
    runsGroup.append(group);
  }
  const refsGroup = makeSvg(root, "g", { class: "timing-chart__refs" });
  for (const ref of referenceLines) {
    refsGroup.append(
      makeSvg(root, "line", {
        class: `timing-chart__ref-line timing-chart__ref-line--${ref.id}`, "data-ref-line": ref.id, "data-value": ref.value,
        x1: plot.x, x2: plot.x + plot.width, y1: ref.y, y2: ref.y,
      }),
      makeSvg(root, "text", {
        class: `timing-chart__ref-label timing-chart__ref-label--${ref.id} telemetry`, "data-ref-label": ref.id,
        x: plot.x + plot.width + 6, y: ref.labelY + 3, "text-anchor": "start",
      }, ref.label),
    );
  }
  svg.append(grid, runsGroup, refsGroup);
  return svg;
}

function renderBugTable(root, model) {
  const wrap = makeEl(root, "div", "bug-matrix__table-wrap");
  const table = makeEl(root, "table", "bug-matrix__table");
  const headRow = makeEl(root, "tr");
  for (const label of ["Kind", "Ref", "Baseline", "Status", "CI Run"]) {
    headRow.append(makeEl(root, "th", "bug-matrix__th", label, { scope: "col" }));
  }
  const thead = makeEl(root, "thead");
  thead.append(headRow);
  const tbody = makeEl(root, "tbody");
  const cell = (child, cls = "") => {
    const td = makeEl(root, "td", cls ? `bug-matrix__cell ${cls}` : "bug-matrix__cell");
    td.append(child);
    return td;
  };
  for (const row of model.rows) {
    const tr = makeEl(root, "tr", "bug-matrix__row");
    tr.dataset.kind = row.kind;
    tr.dataset.caught = String(row.caught);
    const code = makeEl(root, "code", "bug-matrix__code telemetry", row.displayRef);
    if (row.rawRef) code.title = row.rawRef;
    const baseState = row.baselineFailed === true ? "failed" : row.baselineFailed === false ? "passed" : "unknown";
    const baseBadge = makeEl(root, "span", `bug-baseline bug-baseline--${baseState} telemetry`, row.baselineLabel);
    baseBadge.dataset.baselineFailed = row.baselineFailed === null ? "unknown" : String(row.baselineFailed);
    const statusBadge = makeEl(
      root, "span", row.caught ? "bug-status bug-status--caught telemetry" : "bug-status bug-status--missed telemetry", row.statusLabel,
    );
    statusBadge.dataset.caught = String(row.caught);
    const runNode = row.runUrl
      ? Object.assign(
          makeEl(root, "a", "bug-matrix__link telemetry", "Run \u2197", {
            href: row.runUrl, target: "_blank", rel: "noopener noreferrer", "aria-label": `Open CI run for ${row.displayRef}`,
          }),
          { title: row.runUrl },
        )
      : makeEl(root, "span", "bug-matrix__no-link telemetry", EM_DASH);
    tr.append(
      cell(makeEl(root, "span", `bug-kind bug-kind--${row.kind}`, row.kind)),
      cell(code, "bug-matrix__ref telemetry"),
      cell(baseBadge),
      cell(statusBadge),
      cell(runNode, "bug-matrix__run-cell"),
    );
    tbody.append(tr);
  }
  table.append(thead, tbody);
  wrap.append(table);
  return wrap;
}

function createPanelHeader(root, title, badgeClass, extra = null) {
  const header = makeEl(root, "header", "telemetry-panel__header");
  const badge = makeEl(root, "span", `${badgeClass} telemetry`, "Awaiting CI");
  const wrap = makeEl(root, "div", "telemetry-panel__title-wrap");
  wrap.append(makeEl(root, "h2", "telemetry-panel__title", title), badge);
  header.append(wrap);
  if (extra) header.append(extra);
  return { header, badge };
}

/** Mount the 2-column Telemetry Center into `slot` and keep it synchronized with `store`. */
export function createTelemetryCenter(root, slot, store) {
  if (!slot) return null;

  const legend = makeEl(root, "div", "timing-chart__legend", "", { "aria-label": "Chart legend" });
  for (const [k, label] of [["baseline", "Baseline"], ["candidate", "Candidate"], ["sla", "60s SLA / Cold #1"]]) {
    const entry = makeEl(root, "span", "timing-chart__legend-item");
    entry.append(makeEl(root, "span", `timing-chart__swatch timing-chart__swatch--${k}`, "", { "aria-hidden": "true" }), makeEl(root, "span", "", label));
    legend.append(entry);
  }
  const timingPanel = makeEl(root, "article", "telemetry-panel telemetry-panel--timing", "", { "aria-label": "Interleaved timing chart" });
  const { header: timingHeader, badge: timingMeta } = createPanelHeader(root, "Interleaved Timing", "telemetry-panel__meta", legend);
  const summaryGrid = makeEl(root, "dl", "telemetry-summary");
  const metricValues = {};
  for (const [key, label] of [["speedup", "Speedup"], ["p_value", "p-value"], ["median", "Median"], ["p95", "p95"]]) {
    const item = makeEl(root, "div", "telemetry-summary__item");
    const dd = makeEl(root, "dd", "telemetry-summary__value telemetry", EM_DASH);
    dd.dataset.metric = key;
    item.append(makeEl(root, "dt", "telemetry-summary__label", label), dd);
    summaryGrid.append(item);
    metricValues[key] = dd;
  }
  const chartHost = makeEl(root, "div", "timing-chart__host");
  timingPanel.append(timingHeader, summaryGrid, chartHost);

  const matrixPanel = makeEl(root, "article", "telemetry-panel telemetry-panel--bugs", "", { "aria-label": "Bug replay matrix" });
  const { header: matrixHeader, badge: matrixBadge } = createPanelHeader(root, "Bug Replay Matrix", "bug-matrix__summary");
  const matrixHost = makeEl(root, "div", "bug-matrix__host");
  matrixPanel.append(matrixHeader, matrixHost);
  slot.replaceChildren(timingPanel, matrixPanel);

  const render = (state) => {
    const { evidence, verdict } = resolveAttemptTelemetry(state);
    const chart = buildTimingChartModel(evidence, verdict);
    const matrix = buildBugMatrixModel(evidence);
    metricValues.speedup.textContent = chart.summary.speedup;
    metricValues.p_value.textContent = chart.summary.pValue;
    metricValues.median.textContent = chart.summary.median;
    metricValues.p95.textContent = chart.summary.p95;

    if (chart.hasData) {
      timingMeta.textContent = `N=${chart.runCount} pairs`;
      chartHost.replaceChildren(renderTimingSvg(root, chart));
    } else {
      timingMeta.textContent = state?.mode === "demo" ? `Stage: ${state.stage ?? "pending"}` : "No telemetry";
      const msg = state?.mode === "demo" ? "Awaiting interleaved CI runs from Fork CI stage." : "No interleaved timing evidence recorded for this attempt yet.";
      chartHost.replaceChildren(makeEl(root, "p", "telemetry-panel__empty", msg));
    }

    if (!matrix.hasEvidence) {
      matrixBadge.textContent = "Awaiting CI";
      matrixBadge.dataset.state = "pending";
      const msg = state?.mode === "demo" ? "Bug replay verification runs in the Fork CI stage." : "No bug replay outcomes recorded for this attempt yet.";
      matrixHost.replaceChildren(makeEl(root, "p", "telemetry-panel__empty", msg));
    } else if (matrix.rows.length === 0) {
      matrixBadge.textContent = "0 bugs";
      matrixBadge.dataset.state = "pending";
      matrixHost.replaceChildren(makeEl(root, "p", "telemetry-panel__empty", "No bug replay outcomes recorded."));
    } else {
      matrixBadge.textContent =
        matrix.missed > 0 ? `${matrix.caught}/${matrix.total} CAUGHT \u00b7 ${matrix.missed} MISSED` : `${matrix.caught}/${matrix.total} CAUGHT`;
      matrixBadge.dataset.state = matrix.missed > 0 ? "missed" : "caught";
      matrixHost.replaceChildren(renderBugTable(root, matrix));
    }
  };

  render(store.getState());
  const unsubscribe = store.subscribe(render);
  return { slot, timingPanel, matrixPanel, metricValues, chartHost, matrixHost, render, unsubscribe };
}
