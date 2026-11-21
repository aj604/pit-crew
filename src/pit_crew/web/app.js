// Pit Wall shell: Header Bar state and helpers. Plain ES module, no build step.
// Demo scenarios/playback land in #48; live polling and POST /triggers/cycle land in #53.

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

if (typeof document !== "undefined") {
  const init = () => {
    window.pitWall = { header: createHeaderBar(document) };
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
}
