// Pit Wall shell: Header Bar, Demo Scenario Switcher and boot. Plain ES module, no build step.
// Shared state lives in store.js; live polling and POST /triggers/cycle land in #53.

import { DEMO_SCENARIO_IDS, DEMO_SCENARIOS, DEMO_STAGE_LABELS } from "./demo_scenarios.js";
import { createSectorPipeline } from "./pipeline.js";
import { createPitWallStore, resolveInitialBoot } from "./store.js";

export const SLA_MEDIAN_TARGET_SECONDS = 60;
export const SLA_P95_TARGET_SECONDS = 120;
export const MODES = Object.freeze(["live", "demo"]);

const REPO_PATTERN = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const EMPTY_SHA = "-------";
const EMPTY_SECONDS = "--";

/** Return true when `value` is an `owner/name` repository slug. */
export function isRepoSlug(value) {
  return typeof value === "string" && REPO_PATTERN.test(value.trim());
}

/** Shorten a commit SHA for the chip; missing SHAs render as a placeholder. */
export function shortSha(sha) {
  return typeof sha === "string" && sha.length > 0 ? sha.slice(0, 7) : EMPTY_SHA;
}

/** Format seconds for a telemetry readout with one decimal place. */
export function formatSeconds(seconds) {
  return typeof seconds === "number" && Number.isFinite(seconds) ? seconds.toFixed(1) : EMPTY_SECONDS;
}

/** Derive Header Bar view state from a `/api/repos/{owner}/{name}/status` payload. */
export function headerViewFromStatus(status) {
  const verdict = status?.latest_verdict ?? null;
  return {
    repo: typeof status?.repo === "string" ? status.repo : "",
    sha: shortSha(status?.base_sha),
    median: formatSeconds(verdict?.median_seconds),
    p95: formatSeconds(verdict?.p95_seconds),
    slaMet: status?.sla_met === true,
    active: status?.active === true,
  };
}

/** Bind the Header Bar to `root` and return the controls downstream slices drive. */
export function createHeaderBar(root) {
  const el = (id) => {
    const node = root.getElementById(id);
    if (node === null) throw new Error(`Pit Wall header element #${id} is missing`);
    return node;
  };
  const repoInput = el("repo-input");
  const repoOptions = el("repo-options");
  const shaValue = el("base-sha-value");
  const median = el("sla-median");
  const p95 = el("sla-p95");
  const badge = el("sla-badge");
  const modeToggle = el("mode-toggle");
  const runButton = el("run-pit-stop");
  const modeButtons = [...modeToggle.querySelectorAll("[data-mode]")];

  const header = {
    repoInput,
    runButton,
    mode: "demo",
    setRepo(repo) {
      repoInput.value = repo;
    },
    setRepoOptions(repos) {
      repoOptions.replaceChildren(
        ...repos.filter(isRepoSlug).map((repo) => Object.assign(root.createElement("option"), { value: repo })),
      );
    },
    setBaseSha(sha) {
      shaValue.textContent = shortSha(sha);
      shaValue.title = typeof sha === "string" ? sha : "";
    },
    setSla({ median: medianText, p95: p95Text, slaMet }) {
      median.textContent = medianText;
      p95.textContent = p95Text;
      badge.hidden = !slaMet;
    },
    setMode(mode) {
      if (!MODES.includes(mode)) throw new Error(`Unknown Pit Wall mode: ${mode}`);
      header.mode = mode;
      for (const button of modeButtons) {
        button.setAttribute("aria-pressed", String(button.dataset.mode === mode));
      }
      modeToggle.dispatchEvent(new CustomEvent("pitwall:mode", { detail: { mode }, bubbles: true }));
    },
    setRunning(running) {
      runButton.disabled = running;
      runButton.setAttribute("aria-busy", String(running));
      runButton.textContent = running ? "Pit Stop Running" : "Run Pit Stop";
    },
    applyStatus(status) {
      const view = headerViewFromStatus(status);
      if (view.repo) header.setRepo(view.repo);
      header.setBaseSha(status?.base_sha);
      header.setSla(view);
      header.setRunning(view.active);
    },
  };

  for (const button of modeButtons) {
    button.addEventListener("click", () => header.setMode(button.dataset.mode));
  }
  return header;
}

/** Render the Demo Scenario Switcher bar into `slot` and keep it in sync with `store`. */
export function createDemoSwitcher(root, slot, store) {
  const make = (tag, className, text = "") => Object.assign(root.createElement(tag), { className, textContent: text });
  const button = (className, text, onClick) => {
    const node = make("button", className, text);
    node.type = "button";
    node.addEventListener("click", onClick);
    return node;
  };
  const scenarioButtons = DEMO_SCENARIO_IDS.map((id) => {
    const node = button("demo-switcher__scenario", DEMO_SCENARIOS[id].title, () => store.selectScenario(id));
    node.dataset.scenario = id;
    return node;
  });
  const stageItems = DEMO_STAGE_LABELS.map((label) => make("li", "demo-switcher__stage", label));
  const playButton = button("demo-switcher__control", "Play", () => {
    if (store.getState().isPlaying) store.pause();
    else store.play();
  });
  const stepButton = button("demo-switcher__control", "Step", () => store.step());
  const resetButton = button("demo-switcher__control", "Reset", () => store.reset());

  const scenarios = make("div", "demo-switcher__scenarios");
  scenarios.setAttribute("role", "group");
  scenarios.setAttribute("aria-label", "Demo scenario");
  scenarios.append(...scenarioButtons);
  const stages = make("ol", "demo-switcher__stages");
  stages.setAttribute("aria-label", "Pit stop stage");
  stages.append(...stageItems);
  const controls = make("div", "demo-switcher__controls");
  controls.append(playButton, stepButton, resetButton);
  slot.replaceChildren(make("span", "demo-switcher__label", "Demo"), scenarios, stages, controls);

  const render = (state) => {
    slot.hidden = state.mode !== "demo";
    for (const node of scenarioButtons) {
      node.setAttribute("aria-pressed", String(node.dataset.scenario === state.scenarioId));
    }
    stageItems.forEach((node, index) => {
      const step = index < state.stageIndex ? "done" : index === state.stageIndex ? "current" : "pending";
      node.dataset.state = step;
      node.setAttribute("aria-current", step === "current" ? "step" : "false");
    });
    playButton.textContent = state.isPlaying ? "Pause" : "Play";
    playButton.setAttribute("aria-pressed", String(state.isPlaying));
    const atEnd = state.stageIndex === stageItems.length - 1;
    stepButton.disabled = atEnd;
    resetButton.disabled = state.stageIndex === 0 && !state.isPlaying;
  };
  render(store.getState());
  store.subscribe(render);
  return { slot, scenarioButtons, stageItems, playButton, stepButton, resetButton };
}

/** Wire the Header Bar to `store`; header mode clicks update the store without echoing events back. */
export function bindHeaderToStore(header, modeToggle, store) {
  let prevMode = null;
  let prevRepo = store.getState().repo;
  let liveRepo = prevRepo;
  const render = (state) => {
    if (prevMode === "live") liveRepo = header.repoInput.value;
    if (prevRepo !== state.repo) liveRepo = state.repo;
    if (header.mode !== state.mode) header.setMode(state.mode);
    if (state.mode === "demo") {
      header.applyStatus(state.status);
      header.setRunning(state.isPlaying);
    } else {
      header.applyStatus(state.status ? { ...state.status, repo: "" } : null);
      if (header.repoInput.value !== liveRepo) header.setRepo(liveRepo);
    }
    prevMode = state.mode;
    prevRepo = state.repo;
  };
  modeToggle.addEventListener("pitwall:mode", (event) => {
    if (store.getState().mode !== event.detail.mode) store.setMode(event.detail.mode);
  });
  header.runButton.addEventListener("click", () => store.play()); // play() is a no-op in Live Mode
  render(store.getState());
  store.subscribe(render);
}

/** Boot the Pit Wall against `root`, choosing Demo or Live Mode from the URL query string. */
export function bootPitWall(root, search) {
  const header = createHeaderBar(root);
  const store = createPitWallStore(resolveInitialBoot(search));
  bindHeaderToStore(header, root.getElementById("mode-toggle"), store);
  const switcher = createDemoSwitcher(root, root.getElementById("demo-switcher"), store);
  const pipelineSlot = root.getElementById("sector-pipeline");
  const pipeline = pipelineSlot ? createSectorPipeline(root, pipelineSlot, store) : null;
  return { header, store, switcher, pipeline };
}

if (typeof document !== "undefined") {
  const init = () => {
    window.pitWall = bootPitWall(document, globalThis.location?.search ?? "");
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
}
