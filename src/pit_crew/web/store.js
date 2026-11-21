// Shared Pit Wall client state. Panels (#49 Sector Pipeline, #50 Telemetry, #51 Lap History) subscribe
// here; Demo Mode fills it from demo_scenarios.js and Live Mode (#53) pushes API payloads via setLiveData.

import { DEMO_STAGES, DEMO_SCENARIO_IDS, scenarioSnapshotAtStage } from "./demo_scenarios.js";

export const DEFAULT_SCENARIO_ID = "proven_keep";
export const PLAY_INTERVAL_MS = 1200;
const LAST_STAGE = DEMO_STAGES.length - 1;
const REPO_PATTERN = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const emptyRecords = () => ({ hypotheses: [], candidates: [], replay_plans: [], evidence: [], verdicts: [] });

/**
 * Pure: pick the boot state from a URL query string. `?mode=demo` always wins; a valid
 * `?repo=owner/name` boots Live Mode; anything else lands on Demo Mode with `proven_keep`.
 */
export function resolveInitialBoot(search) {
  const params = new URLSearchParams(typeof search === "string" ? search : "");
  const repoParam = (params.get("repo") ?? "").trim();
  const repo = REPO_PATTERN.test(repoParam) ? repoParam : "";
  const mode = params.get("mode") !== "demo" && repo ? "live" : "demo";
  return { mode, repo, scenarioId: DEFAULT_SCENARIO_ID };
}

/** Create the store. Demo scenarios load at the Rater stage so `/` never shows an empty Pit Wall. */
export function createPitWallStore(initialOptions = {}) {
  const {
    mode = "demo",
    repo = "",
    scenarioId = DEFAULT_SCENARIO_ID,
    stageIndex = LAST_STAGE,
    intervalMs = PLAY_INTERVAL_MS,
    timer = globalThis,
  } = initialOptions;
  const listeners = new Set();
  let live = { status: null, attempts: [], records: emptyRecords() };
  let ticker = null;
  let state = {
    mode, repo, scenarioId, stageIndex, isPlaying: false, selectedAttemptIndex: null, fetchError: null,
  };

  const derive = () => {
    const data = state.mode === "demo" ? scenarioSnapshotAtStage(state.scenarioId, state.stageIndex) : live;
    const selected = state.selectedAttemptIndex;
    const index = selected !== null && selected < data.attempts.length ? selected : data.attempts.length - 1;
    return {
      ...state,
      stage: DEMO_STAGES[state.stageIndex],
      ...data,
      selectedAttemptIndex: index >= 0 ? index : null,
      selectedAttempt: data.attempts[index] ?? null,
    };
  };
  let snapshot = derive();
  const update = (patch) => {
    state = { ...state, ...patch };
    snapshot = derive();
    for (const listener of [...listeners]) listener(snapshot);
  };
  const stopTicker = () => {
    if (ticker !== null) timer.clearInterval(ticker);
    ticker = null;
  };
  const inDemo = () => state.mode === "demo";

  const store = {
    getState: () => snapshot,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    setMode(nextMode) {
      if (nextMode !== "demo" && nextMode !== "live") throw new Error(`Unknown Pit Wall mode: ${nextMode}`);
      stopTicker();
      update({ mode: nextMode, isPlaying: false, selectedAttemptIndex: null });
    },
    setRepo(nextRepo) {
      update({ repo: nextRepo });
    },
    selectScenario(nextId) {
      if (!DEMO_SCENARIO_IDS.includes(nextId)) throw new Error(`Unknown demo scenario: ${nextId}`);
      stopTicker();
      update({ scenarioId: nextId, stageIndex: LAST_STAGE, isPlaying: false, selectedAttemptIndex: null });
    },
    setStage(index) {
      if (!Number.isInteger(index) || index < 0 || index > LAST_STAGE) {
        throw new RangeError(`Demo stage index out of range: ${index}`);
      }
      update({ stageIndex: index });
    },
    step() {
      if (!inDemo()) return;
      const next = Math.min(state.stageIndex + 1, LAST_STAGE);
      if (next === LAST_STAGE) stopTicker();
      update({ stageIndex: next, isPlaying: ticker !== null });
    },
    reset() {
      if (!inDemo()) return;
      stopTicker();
      update({ stageIndex: 0, isPlaying: false });
    },
    play() {
      if (!inDemo() || ticker !== null) return;
      ticker = timer.setInterval(() => store.step(), intervalMs);
      update({ stageIndex: state.stageIndex === LAST_STAGE ? 0 : state.stageIndex, isPlaying: true });
    },
    pause() {
      stopTicker();
      update({ isPlaying: false });
    },
    selectAttempt(index) {
      const count = snapshot.attempts.length;
      if (!Number.isInteger(index) || index < 0 || index >= count) {
        throw new RangeError(`Attempt index out of range: ${index}`);
      }
      update({ selectedAttemptIndex: index });
    },
    setLiveData({ status = null, attempts = [], records = emptyRecords() } = {}) {
      live = { status, attempts, records };
      update({ fetchError: null });
    },
    setFetchError(message) {
      update({ fetchError: message ?? null });
    },
  };
  return store;
}
