// Lap History panel (#51): clickable attempt table backed by the shared Pit Wall store.
// Plain ES module, no build step; colours come only from /brand/tokens.css (--pc-*).

const EMPTY = "—";

/** Pirelli-style compound ring token and short code for each Optimizer playbook skill. */
export const COMPOUND_BY_SKILL = Object.freeze({
  "dep-cache": Object.freeze({ code: "S", label: "Soft", token: "var(--pc-compound-soft)" }),
  "shard-by-timing": Object.freeze({ code: "M", label: "Medium", token: "var(--pc-compound-medium)" }),
  "affected-tests": Object.freeze({ code: "H", label: "Hard", token: "var(--pc-compound-hard)" }),
  "dedupe-jobs": Object.freeze({ code: "HS", label: "Hypersoft", token: "var(--pc-compound-hypersoft)" }),
  "parallelize-dag": Object.freeze({ code: "I", label: "Inter", token: "var(--pc-compound-inter)" }),
});
const FALLBACK_COMPOUND = Object.freeze({ code: "?", label: "Custom", token: "var(--pc-ink-muted)" });
const COLUMNS = Object.freeze([
  ["attempt", "Attempt #"], ["skill", "Skill"], ["summary", "Target / Summary"], ["speedup", "Speedup"],
  ["pValue", "p-value"], ["bugs", "Bugs Caught"], ["verdict", "Verdict"], ["pr", "PR"],
]);

/** Return the compound ring descriptor for `skill`, falling back to muted ink. */
export function compoundForSkill(skill) {
  return typeof skill === "string" && Object.hasOwn(COMPOUND_BY_SKILL, skill)
    ? COMPOUND_BY_SKILL[skill]
    : FALLBACK_COMPOUND;
}

/** Derive the display verdict (`KEEP`, `REJECT`, or `PENDING`) from `rated` and `keep`. */
export function verdictForAttempt(attempt) {
  if (!attempt?.rated) return "PENDING";
  if (attempt.keep === true) return "KEEP";
  if (attempt.keep === false) return "REJECT";
  return "PENDING";
}

/** Format a numeric speedup multiplier (e.g. `1.85x`), or `—` when unavailable. */
export function formatSpeedup(speedup) {
  return typeof speedup === "number" && Number.isFinite(speedup) ? `${speedup.toFixed(2)}x` : EMPTY;
}

/** Format a permutation-test p-value for tabular display, or `—` when unavailable. */
export function formatPValue(pValue) {
  if (typeof pValue !== "number" || !Number.isFinite(pValue)) return EMPTY;
  return pValue > 0 && pValue < 0.001 ? "<0.001" : pValue.toFixed(3);
}

/** Format bugs caught (`caught/total`) from an Evidence record, or `—` when absent. */
export function formatBugsCaught(evidence) {
  if (!evidence || typeof evidence.bugs_caught !== "number" || typeof evidence.bugs_total !== "number"
      || !Number.isFinite(evidence.bugs_caught) || !Number.isFinite(evidence.bugs_total)) {
    return { text: EMPTY, status: "unknown" };
  }
  const allCaught = evidence.bugs_total > 0 && evidence.bugs_caught === evidence.bugs_total;
  return { text: `${evidence.bugs_caught}/${evidence.bugs_total}`, status: allCaught ? "all" : "missed" };
}

/** Return a normalized `http:`/`https:` URL for an attempt's PR link, or `null` if missing/unsafe. */
export function safePrUrl(url) {
  if (typeof url !== "string" || !url.trim()) return null;
  try {
    const parsed = new URL(url.trim());
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.href : null;
  } catch {
    return null;
  }
}

const prLinkLabel = (url) => {
  const match = /\/pull\/(\d+)(?:[/?#]|$)/.exec(url);
  return match ? `PR #${match[1]}` : "View PR";
};

const formatTarget = (hyp) =>
  hyp ? [hyp.workflow_path, hyp.job, hyp.step].map((p) => (typeof p === "string" ? p.trim() : "")).filter(Boolean).join(" · ") : "";

const resolveSummary = (attempt) =>
  [attempt?.summary, attempt?.description].find((v) => typeof v === "string" && v.trim().length > 0)?.trim() ?? EMPTY;

/** Pure: build Lap History row view models from `attempts` joined with `records.hypotheses` and `records.evidence`. */
export function buildLapHistoryRows(attempts, records, selectedAttemptIndex = null) {
  const list = Array.isArray(attempts) ? attempts : [];
  const hypById = new Map((Array.isArray(records?.hypotheses) ? records.hypotheses : []).filter((h) => h?.id).map((h) => [h.id, h]));
  const evByCand = new Map((Array.isArray(records?.evidence) ? records.evidence : []).filter((e) => e?.candidate_id).map((e) => [e.candidate_id, e]));

  return list.map((attempt, index) => {
    const skill = typeof attempt?.skill === "string" && attempt.skill.trim() ? attempt.skill.trim() : "";
    const compound = compoundForSkill(skill);
    const bugs = formatBugsCaught(typeof attempt?.candidate_id === "string" ? evByCand.get(attempt.candidate_id) : null);
    const prUrl = safePrUrl(attempt?.pr_url);
    return {
      index, attemptNumber: index + 1, skill: skill || EMPTY,
      compoundCode: compound.code, compoundLabel: compound.label, compoundToken: compound.token,
      target: formatTarget(typeof attempt?.hypothesis_id === "string" ? hypById.get(attempt.hypothesis_id) : null),
      summary: resolveSummary(attempt), speedup: formatSpeedup(attempt?.speedup), pValue: formatPValue(attempt?.p_value),
      bugsCaught: bugs.text, bugsStatus: bugs.status, verdict: verdictForAttempt(attempt),
      prUrl, prLabel: prUrl ? prLinkLabel(prUrl) : EMPTY, selected: index === selectedAttemptIndex,
    };
  });
}

const isLinkTarget = (target) =>
  Boolean(target && (typeof target.closest === "function" ? target.closest("a") : String(target.tagName ?? "").toLowerCase() === "a"));

/** Render the interactive Lap History table into `slot` and keep it synchronized with `store`. */
export function createLapHistory(root, slot, store) {
  if (!slot) return null;
  const make = (tag, className = "", text = "") => {
    const node = root.createElement(tag);
    if (className) node.className = className;
    if (text !== "") node.textContent = text;
    return node;
  };
  let rowNodes = [];
  const selectIndex = (index) => {
    if (index >= 0 && index < store.getState().attempts.length) store.selectAttempt(index);
  };

  const buildRow = (row) => {
    const tr = make("tr", row.selected ? "lap-history__row lap-history__row--selected" : "lap-history__row");
    tr.dataset.attemptIndex = String(row.index);
    tr.dataset.verdict = row.verdict.toLowerCase();
    tr.setAttribute("aria-selected", String(row.selected));
    tr.tabIndex = 0;
    tr.addEventListener("click", (event) => {
      if (isLinkTarget(event?.target)) return;
      selectIndex(row.index);
    });
    tr.addEventListener("keydown", (event) => {
      if (isLinkTarget(event?.target)) return;
      if (event?.key === "Enter" || event?.key === " ") {
        event.preventDefault?.();
        selectIndex(row.index);
      } else if (event?.key === "ArrowDown" && row.index + 1 < rowNodes.length) {
        event.preventDefault?.();
        rowNodes[row.index + 1]?.focus?.();
      } else if (event?.key === "ArrowUp" && row.index > 0) {
        event.preventDefault?.();
        rowNodes[row.index - 1]?.focus?.();
      }
    });

    const cell = (colIndex, className, content) => {
      const td = make("td", `lap-history__cell ${className}`);
      td.dataset.label = COLUMNS[colIndex][1];
      if (typeof content === "string") td.textContent = content;
      else if (Array.isArray(content)) td.append(...content);
      else td.append(content);
      return td;
    };

    const badge = make("span", "lap-history__compound");
    badge.dataset.skill = row.skill;
    badge.dataset.compoundToken = row.compoundToken;
    badge.title = `${row.compoundLabel} compound (${row.skill})`;
    const ring = make("span", "lap-history__ring telemetry", row.compoundCode);
    ring.setAttribute("aria-hidden", "true");
    if (ring.style) ring.style.borderColor = row.compoundToken;
    badge.append(ring, make("span", "lap-history__skill-name telemetry", row.skill));

    const summaryParts = row.target
      ? [make("span", "lap-history__target telemetry", row.target), make("span", "lap-history__summary", row.summary)]
      : [make("span", "lap-history__summary", row.summary)];
    const bugsSpan = make("span", "lap-history__bugs telemetry", row.bugsCaught);
    bugsSpan.dataset.status = row.bugsStatus;
    const verdictBadge = make("span", `lap-history__verdict lap-history__verdict--${row.verdict.toLowerCase()}`, row.verdict);
    verdictBadge.dataset.verdict = row.verdict;

    let prContent = make("span", "lap-history__empty-cell telemetry", EMPTY);
    if (row.prUrl) {
      const link = make("a", "lap-history__pr-link telemetry", row.prLabel);
      link.href = row.prUrl;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.setAttribute("href", row.prUrl);
      link.setAttribute("target", "_blank");
      link.setAttribute("rel", "noopener noreferrer");
      link.setAttribute("aria-label", `Open pull request ${row.prLabel} in new tab`);
      link.addEventListener("click", (event) => event?.stopPropagation?.());
      prContent = link;
    }

    tr.append(
      cell(0, "lap-history__cell--attempt telemetry", String(row.attemptNumber)),
      cell(1, "lap-history__cell--skill", badge),
      cell(2, "lap-history__cell--summary", summaryParts),
      cell(3, "lap-history__cell--metric telemetry", row.speedup),
      cell(4, "lap-history__cell--metric telemetry", row.pValue),
      cell(5, "lap-history__cell--metric", bugsSpan),
      cell(6, "lap-history__cell--verdict", verdictBadge),
      cell(7, "lap-history__cell--pr", prContent),
    );
    return tr;
  };

  const render = (state) => {
    const focusedIndex =
      typeof slot.contains === "function" && root.activeElement && slot.contains(root.activeElement)
        ? root.activeElement.dataset?.attemptIndex
        : undefined;
    const rows = buildLapHistoryRows(state?.attempts, state?.records, state?.selectedAttemptIndex ?? null);
    const panel = make("div", "lap-history");
    const header = make("div", "lap-history__header");
    header.append(
      make("h2", "lap-history__title", "Lap History"),
      make("span", "lap-history__count telemetry", `${rows.length} ${rows.length === 1 ? "lap" : "laps"}`),
    );
    panel.append(header);
    if (rows.length === 0) {
      rowNodes = [];
      panel.append(make("p", "lap-history__empty", "No laps recorded yet. Run a Pit Stop or switch to Demo Mode to inspect telemetry."));
      slot.replaceChildren(panel);
      return;
    }
    const wrap = make("div", "lap-history__table-wrap");
    const table = make("table", "lap-history__table");
    const thead = make("thead", "lap-history__head");
    const headRow = make("tr");
    for (const [key, label] of COLUMNS) {
      const th = make("th", `lap-history__th lap-history__th--${key}`, label);
      th.setAttribute("scope", "col");
      headRow.append(th);
    }
    thead.append(headRow);
    const tbody = make("tbody", "lap-history__body");
    rowNodes = rows.map(buildRow);
    tbody.append(...rowNodes);
    table.append(thead, tbody);
    wrap.append(table);
    panel.append(wrap);
    slot.replaceChildren(panel);
    if (focusedIndex !== undefined) rowNodes[Number(focusedIndex)]?.focus?.();
  };

  render(store.getState());
  const unsubscribe = store.subscribe(render);
  return { slot, render, getRows: () => rowNodes, destroy: unsubscribe };
}
