// Offline Demo Mode data: three deterministic pit stops in the pinned Spec 1B JSON shapes
// (/status, /attempts, /records), so the Pit Wall runs with no GEMINI_API_KEY or GITHUB_TOKEN.
// Timings are chosen so pit_crew.rater.rate() reproduces each verdict's numbers (tests check this).

export const DEMO_STAGES = Object.freeze(["analysis", "optimizer", "proof", "ci", "rater"]);
export const DEMO_STAGE_LABELS = Object.freeze(["Analysis", "Optimizer", "Proof", "CI", "Rater"]);
export const DEMO_SCENARIO_IDS = Object.freeze(["unsafe_reject", "noise_reject", "proven_keep"]);
export const DEMO_REPO = "pit-crew-demo/checkout-api";

const BASE_SHA = "4f1c9e2a7b3d58e6c0a91f2d7e4b6a8c3d5e7f90";
const RUNS = "https://github.com/pit-crew-demo/checkout-api/actions/runs";
const WORKFLOW = ".github/workflows/ci.yml";
const BASELINE_STEPS = `      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install dependencies
        run: pip install -r requirements.txt
      - name: Run tests
        run: pytest -n auto`;

const workflow = (testJob) => `name: CI
on: [push, pull_request]
jobs:
${testJob}`;

const job = (name, steps) => `  ${name}:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
${steps}
`;

const bugCases = (prefix) => [
  { kind: "commit", reason: "historical red commit: cart total rounding", commit_sha: `${prefix}1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4`, mutated_files: {}, artifact_ref: null },
  { kind: "commit", reason: "historical red commit: coupon double-apply", commit_sha: `${prefix}9e8d7c6b5a49382716f5e4d3c2b1a09f8e7d6c5`, mutated_files: {}, artifact_ref: null },
  { kind: "mutation", reason: "flip tax rate boundary", commit_sha: null, mutated_files: { "app/tax.py": "RATE = 0.0\n" }, artifact_ref: "proof/tax-mutation.log" },
  { kind: "mutation", reason: "drop inventory check", commit_sha: null, mutated_files: { "app/stock.py": "def in_stock(sku):\n    return True\n" }, artifact_ref: "proof/stock-mutation.log" },
];

function buildScenario(spec) {
  const { id, n, title, hypothesis, candidate, timings, caught, verdict } = spec;
  const hypothesisId = `hyp-${n}000000000`.slice(0, 12);
  const candidateId = `cand-${n}0000000`.slice(0, 12);
  const cases = bugCases(String(n));
  const runUrl = (offset) => `${RUNS}/${9100 + n * 100 + offset}`;
  const bugOutcomes = cases.map((bug, i) => ({
    kind: bug.kind,
    ref: bug.kind === "commit" ? bug.commit_sha : Object.keys(bug.mutated_files)[0],
    baseline_failed: true,
    caught: caught[i],
    run_url: runUrl(20 + i),
  }));
  const files = { [WORKFLOW]: candidate.workflow };
  return Object.freeze({
    id,
    title,
    hypothesis: {
      id: hypothesisId, repo: DEMO_REPO, base_sha: BASE_SHA, workflow_path: WORKFLOW, job: "test", ...hypothesis,
    },
    candidate: {
      id: candidateId, hypothesis_id: hypothesisId, branch: `pit-crew/${hypothesis.skill}-${n}`, summary: candidate.summary,
      repo: DEMO_REPO, base_sha: BASE_SHA, head_sha: candidate.head_sha, files, changed_files: Object.keys(files),
    },
    replayPlan: {
      id: `plan-${n}0000000`.slice(0, 12), candidate_id: candidateId,
      bug_shas: cases.filter((c) => c.kind === "commit").map((c) => c.commit_sha), cases,
    },
    evidence: {
      id: `evid-${n}0000000`.slice(0, 12), candidate_id: candidateId, ...timings,
      bugs_total: cases.length, bugs_caught: caught.filter(Boolean).length,
      run_urls: timings.candidate_seconds.map((_, i) => runUrl(i)), bug_outcomes: bugOutcomes,
    },
    verdict: { id: `verd-${n}0000000`.slice(0, 12), candidate_id: candidateId, ...verdict },
  });
}

export const DEMO_SCENARIOS = Object.freeze({
  unsafe_reject: buildScenario({
    id: "unsafe_reject",
    n: 1,
    title: "Fast but Unsafe",
    hypothesis: {
      description: "Full pytest run dominates the test job; only run tests affected by the diff.",
      skill: "affected-tests", expected_savings_seconds: 57.5, step: "Run tests", bucket: "running",
      evidence: { step_p50_seconds: 96.0, max_saving_seconds: 70.0, runs_sampled: 30 }, confidence: 0.7, risk: "high",
    },
    candidate: {
      summary: "Run only tests whose imports touch changed files.",
      head_sha: "a7e3b91c04d2f6e8b5a3c1d9e7f2046b8a1c3e5d",
      workflow: workflow(job("test", BASELINE_STEPS.replace("pytest -n auto", "pytest -n auto --picked=first"))),
    },
    timings: {
      baseline_seconds: [131.0, 120.0, 121.5, 119.5, 118.5],
      candidate_seconds: [74.0, 62.5, 60.0, 66.0, 58.5],
    },
    caught: [true, true, false, true],
    // Real rater.rate() short-circuits on a missed bug (speedup 0, p null). The demo keeps the
    // measured 1.92x / p = 0.004 to show the point: faster, yet unsafe, so the Trust Wall blocks merge.
    verdict: {
      keep: false, speedup: 1.92, p_value: 0.004,
      reason: "1.92x faster yet unsafe: caught 3/4 bugs, Trust Wall blocks merge",
      median_seconds: 62.5, p95_seconds: 72.4, sla_met: false,
    },
  }),
  noise_reject: buildScenario({
    id: "noise_reject",
    n: 2,
    title: "Within Noise",
    hypothesis: {
      description: "lint and test jobs both install dependencies; dedupe into one job.",
      skill: "dedupe-jobs", expected_savings_seconds: 9.0, step: "", bucket: "setup",
      evidence: { step_p50_seconds: 11.0, max_saving_seconds: 12.0, runs_sampled: 30 }, confidence: 0.4, risk: "low",
    },
    candidate: {
      summary: "Fold the lint job into the test job so dependencies install once.",
      head_sha: "c2d4f6a8b0e1937d5c7b9a1e3f5d7092b4c6e8f1",
      workflow: workflow(job("test", `${BASELINE_STEPS}\n      - name: Lint\n        run: ruff check`)),
    },
    timings: {
      baseline_seconds: [121.0, 102.0, 103.0, 100.0, 109.0],
      candidate_seconds: [118.0, 94.0, 100.0, 100.0, 102.0],
    },
    caught: [true, true, true, true],
    verdict: {
      keep: false, speedup: 1.03, p_value: 0.24, reason: "speedup is within noise",
      median_seconds: 100.0, p95_seconds: 114.8, sla_met: false,
    },
  }),
  proven_keep: buildScenario({
    id: "proven_keep",
    n: 3,
    title: "Proven Speedup",
    hypothesis: {
      description: "Install dependencies re-downloads every wheel; cache pip between runs.",
      skill: "dep-cache", expected_savings_seconds: 40.0, step: "Install dependencies", bucket: "setup",
      evidence: { step_p50_seconds: 44.0, max_saving_seconds: 42.0, runs_sampled: 30 }, confidence: 0.85, risk: "low",
    },
    candidate: {
      summary: "Cache pip downloads keyed on requirements.txt.",
      head_sha: "e9b17d3f5a2c4086d1e3b5f7a9c0e2d4f6081a3b",
      workflow: workflow(
        job("test", BASELINE_STEPS.replace('"3.12"', '"3.12"\n          cache: pip\n          cache-dependency-path: requirements.txt')),
      ),
    },
    // Run #1 on each side is the cold runner; every warm baseline run is still slower than any candidate run.
    timings: {
      baseline_seconds: [97.0, 88.8, 89.4, 88.6, 88.4],
      candidate_seconds: [88.0, 48.0, 46.0, 58.0, 44.0],
    },
    caught: [true, true, true, true],
    verdict: {
      keep: true, speedup: 1.85, p_value: 0.004, reason: "1.85x faster, caught 4/4 bugs (SLA met)",
      median_seconds: 48.0, p95_seconds: 82.0, sla_met: true,
    },
  }),
});

/** Return the scenario for `id`, or throw for an unknown id. */
export function getScenario(id) {
  if (!DEMO_SCENARIO_IDS.includes(id)) throw new Error(`Unknown demo scenario: ${id}`);
  return DEMO_SCENARIOS[id];
}

function attemptFor(s, stageIndex) {
  const { hypothesis: h, candidate: c } = s;
  const v = stageIndex >= 4 ? s.verdict : null;
  const pick = (key) => (v === null ? null : v[key]);
  if (stageIndex < 1) {
    return {
      hypothesis_id: h.id, skill: h.skill, description: h.description, candidate_id: null, branch: null,
      base_sha: h.base_sha, head_sha: null, summary: null, changed_files: [], rated: false, keep: null,
      speedup: null, p_value: null, median_seconds: null, p95_seconds: null, sla_met: null, reason: null,
    };
  }
  return {
    hypothesis_id: h.id, skill: h.skill, description: h.description, candidate_id: c.id, branch: c.branch,
    base_sha: c.base_sha, head_sha: c.head_sha, summary: c.summary, changed_files: [...c.changed_files],
    rated: v !== null, keep: pick("keep"), speedup: pick("speedup"), p_value: pick("p_value"),
    median_seconds: pick("median_seconds"), p95_seconds: pick("p95_seconds"), sla_met: pick("sla_met"),
    reason: pick("reason"),
  };
}

/**
 * Pure: the `{ status, attempts, records }` a live Pit Wall would see once `stageIndex`
 * (0 = Analysis ... 4 = Rater) has finished. The cycle stays `active` until the Rater rules;
 * a kept verdict advances `base_sha` to the candidate head, like the server does.
 */
export function scenarioSnapshotAtStage(scenarioId, stageIndex) {
  const s = getScenario(scenarioId);
  if (!Number.isInteger(stageIndex) || stageIndex < 0 || stageIndex >= DEMO_STAGES.length) {
    throw new RangeError(`Demo stage index out of range: ${stageIndex}`);
  }
  const clone = (record) => structuredClone(record);
  const upTo = (index, record) => (stageIndex >= index ? [clone(record)] : []);
  const verdict = stageIndex >= 4 ? clone(s.verdict) : null;
  return {
    status: {
      repo: DEMO_REPO,
      active: verdict === null,
      base_sha: verdict?.keep ? s.candidate.head_sha : BASE_SHA,
      sla_met: verdict?.sla_met ?? false,
      latest_verdict: verdict,
    },
    attempts: [attemptFor(s, stageIndex)],
    records: {
      hypotheses: [clone(s.hypothesis)],
      candidates: upTo(1, s.candidate),
      replay_plans: upTo(2, s.replayPlan),
      evidence: upTo(3, s.evidence),
      verdicts: verdict === null ? [] : [verdict],
    },
  };
}
