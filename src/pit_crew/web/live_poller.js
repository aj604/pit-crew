// Live Mode API polling, cycle trigger & fetch resilience (#53).
// Plain ES module, no build step; colours come only from /brand/tokens.css (--pc-*).

export const LIVE_POLL_ACTIVE_MS = 3000;
export const LIVE_POLL_IDLE_MS = 15000;
export const API_TOKEN_STORAGE_KEY = "pit_crew_api_token";

const REPO_PATTERN = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const RECORD_KEYS = Object.freeze(["hypotheses", "candidates", "replay_plans", "evidence", "verdicts"]);

const isRepoSlug = (v) => typeof v === "string" && REPO_PATTERN.test(v.trim());
const isPlainObject = (v) => v !== null && typeof v === "object" && !Array.isArray(v);

/** Resolve an optional Bearer token from `?token=` (persisting to `sessionStorage`) or `sessionStorage`. */
export function resolveApiToken(options = {}, root = null) {
  const rawSearch = typeof options.search === "string"
    ? options.search
    : (root?.defaultView?.location?.search ?? root?.location?.search ?? globalThis.location?.search ?? "");
  const storage = options.storage !== undefined
    ? options.storage
    : (root?.defaultView?.sessionStorage ?? globalThis.sessionStorage ?? null);
  const params = new URLSearchParams(typeof rawSearch === "string" ? rawSearch : "");
  const fromUrl = (params.get("token") ?? "").trim();
  if (fromUrl) {
    try { storage?.setItem?.(API_TOKEN_STORAGE_KEY, fromUrl); } catch { /* ignore restricted storage */ }
    return fromUrl;
  }
  try {
    const stored = storage?.getItem?.(API_TOKEN_STORAGE_KEY);
    return typeof stored === "string" ? stored.trim() : "";
  } catch {
    return "";
  }
}

function withBearerHeader(baseHeaders, token) {
  return token ? { ...baseHeaders, Authorization: `Bearer ${token}` } : baseHeaders;
}

/** Build URL-encoded Spec 1B API paths for an `owner/name` slug, or `null` if invalid. */
export function repoApiPaths(repo) {
  if (!isRepoSlug(repo)) return null;
  const trimmed = repo.trim();
  const slash = trimmed.indexOf("/");
  const base = `/api/repos/${encodeURIComponent(trimmed.slice(0, slash))}/${encodeURIComponent(trimmed.slice(slash + 1))}`;
  return { repo: trimmed, status: `${base}/status`, attempts: `${base}/attempts`, records: `${base}/records` };
}

/** Return the adaptive poll interval in ms (`3s` while active, `15s` when idle). */
export function pollIntervalForStatus(status, forceActive = false) {
  return forceActive || status?.active === true ? LIVE_POLL_ACTIVE_MS : LIVE_POLL_IDLE_MS;
}

/** Extract a human-readable detail message from a JSON error body when present. */
export function extractErrorDetail(body) {
  return isPlainObject(body) && typeof body.detail === "string" ? body.detail.trim() : "";
}

/** Validate that a `/records` payload matches the pinned Spec 1B structure. */
export function isValidRecordsPayload(records) {
  return isPlainObject(records) && RECORD_KEYS.every((key) => Array.isArray(records[key]));
}

async function readJsonSafe(response) {
  try { return await response.json(); } catch { return null; }
}

async function fetchJsonEndpoint(fetchFn, url, label, signal, token = "") {
  const headers = withBearerHeader({ Accept: "application/json" }, token);
  const response = await fetchFn(url, { method: "GET", headers, ...(signal ? { signal } : {}) });
  if (!response?.ok) {
    const detail = extractErrorDetail(await readJsonSafe(response));
    const code = typeof response?.status === "number" ? response.status : "error";
    throw new Error(detail ? `Live ${label} failed (HTTP ${code}): ${detail}` : `Live ${label} failed (HTTP ${code})`);
  }
  try { return await response.json(); } catch { throw new Error(`Invalid JSON in ${label} response`); }
}

function defaultFetch() {
  if (typeof globalThis.fetch !== "function") return null;
  const origin = globalThis.location?.origin;
  return typeof origin === "string" && origin.length > 0 && origin !== "null" ? globalThis.fetch.bind(globalThis) : null;
}

function mountStatusPill(root) {
  if (typeof root?.createElement !== "function") return { pill: null, textNode: null };
  const pill = Object.assign(root.createElement("div"), { id: "live-status-pill", className: "live-status-pill", hidden: true });
  pill.setAttribute("role", "status");
  pill.setAttribute("aria-live", "polite");
  const dot = Object.assign(root.createElement("span"), { className: "live-status-pill__dot" });
  dot.setAttribute("aria-hidden", "true");
  const textNode = Object.assign(root.createElement("span"), { className: "live-status-pill__text" });
  pill.append(dot, textNode);
  const mount = root.getElementById?.("header-bar") ?? root.getElementById?.("pit-wall") ?? null;
  if (mount && typeof mount.append === "function") mount.append(pill);
  return { pill, textNode };
}

/** Wire Live Mode polling, adaptive cadence, visibility pause/resume, error pill, and cycle trigger. */
export function createLivePoller(root, header, store, options = {}) {
  const fetchFn = options.fetch !== undefined ? options.fetch : defaultFetch();
  const timer = options.timer ?? globalThis;
  const doc = options.doc ?? root;
  const { pill, textNode } = mountStatusPill(root);
  let cachedApiToken = resolveApiToken(options, root);
  const currentApiToken = () => {
    const resolved = resolveApiToken(options, root);
    if (resolved) cachedApiToken = resolved;
    return cachedApiToken;
  };

  let destroyed = false, contextGen = 0, pollGen = 0, pollTimer = null, lastPollDelayMs = null;
  let activeController = null, triggerInFlight = false, assumedActive = false;
  let prevMode = store.getState().mode, prevRepo = store.getState().repo;

  const isHidden = () => doc?.hidden === true;
  const canPoll = () => {
    const s = store.getState();
    return !destroyed && typeof fetchFn === "function" && s.mode === "live" && isRepoSlug(s.repo) && !isHidden();
  };
  const clearPollTimer = () => { if (pollTimer !== null) { timer.clearTimeout(pollTimer); pollTimer = null; } };
  const abortInFlight = () => { activeController?.abort(); activeController = null; };
  const syncPill = (s) => {
    if (!pill || !textNode) return;
    const msg = s.mode === "live" && typeof s.fetchError === "string" ? s.fetchError : "";
    textNode.textContent = msg;
    pill.hidden = msg.length === 0;
  };
  const syncRunButton = (s) => {
    if (s.mode === "live") header.setRunning(triggerInFlight || assumedActive || s.status?.active === true);
  };
  const scheduleNextPoll = () => {
    clearPollTimer();
    if (!canPoll()) { lastPollDelayMs = null; return; }
    lastPollDelayMs = pollIntervalForStatus(store.getState().status, assumedActive);
    pollTimer = timer.setTimeout(() => { pollTimer = null; void refresh(); }, lastPollDelayMs);
  };
  const resetCycle = () => {
    contextGen += 1; pollGen += 1; clearPollTimer(); abortInFlight(); triggerInFlight = false; assumedActive = false;
  };

  const refresh = async ({ preserveNotice = null } = {}) => {
    clearPollTimer();
    if (!canPoll()) return false;
    const paths = repoApiPaths(store.getState().repo);
    if (!paths) return false;
    abortInFlight();
    const ctx = contextGen, pollToken = ++pollGen;
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    activeController = controller;
    const token = currentApiToken();
    try {
      const [status, attempts, records] = await Promise.all([
        fetchJsonEndpoint(fetchFn, paths.status, "status", controller?.signal, token),
        fetchJsonEndpoint(fetchFn, paths.attempts, "attempts", controller?.signal, token),
        fetchJsonEndpoint(fetchFn, paths.records, "records", controller?.signal, token),
      ]);
      const cur = store.getState();
      if (destroyed || ctx !== contextGen || pollToken !== pollGen || cur.mode !== "live" || cur.repo.trim() !== paths.repo) return false;
      if (!isPlainObject(status)) throw new Error("Invalid status response payload");
      if (!Array.isArray(attempts)) throw new Error("Invalid attempts response payload");
      if (!isValidRecordsPayload(records)) throw new Error("Invalid records response payload");
      if (activeController === controller) activeController = null;
      assumedActive = false;
      store.setLiveData({ status, attempts, records });
      if (preserveNotice && status.active === true) store.setFetchError(preserveNotice);
      syncRunButton(store.getState());
      scheduleNextPoll();
      return true;
    } catch (error) {
      if (activeController === controller) activeController = null;
      if (destroyed || ctx !== contextGen || pollToken !== pollGen || store.getState().mode !== "live") return false;
      store.setFetchError(error instanceof Error && error.message ? error.message : "Live telemetry fetch failed");
      syncRunButton(store.getState());
      scheduleNextPoll();
      return false;
    }
  };

  const resolveActiveSlug = () => {
    const s = store.getState();
    if (isRepoSlug(s.repo)) return s.repo.trim();
    const input = typeof header.repoInput?.value === "string" ? header.repoInput.value.trim() : "";
    if (!isRepoSlug(input)) return "";
    store.setRepo(input);
    return input;
  };

  const isStaleTrigger = (ctx) => destroyed || ctx !== contextGen || store.getState().mode !== "live";

  const triggerCycle = async () => {
    const s = store.getState();
    if (destroyed || s.mode !== "live" || typeof fetchFn !== "function" || triggerInFlight || s.status?.active === true) return false;
    const repo = resolveActiveSlug();
    if (!repo) { store.setFetchError("Enter a valid repository (owner/name) before running a Pit Stop."); return false; }
    const ctx = contextGen;
    triggerInFlight = true;
    syncRunButton(store.getState());
    try {
      const response = await fetchFn("/triggers/cycle", {
        method: "POST",
        headers: withBearerHeader({ "Content-Type": "application/json", Accept: "application/json" }, currentApiToken()),
        body: JSON.stringify({ repo, until_sla: true }),
      });
      if (isStaleTrigger(ctx)) return false;
      const detail = extractErrorDetail(await readJsonSafe(response));
      if (isStaleTrigger(ctx)) return false;
      triggerInFlight = false;
      if (response?.status === 202 || response?.ok) {
        assumedActive = true;
        if (store.getState().fetchError) store.setFetchError(null);
        syncRunButton(store.getState());
        await refresh();
        return true;
      }
      if (response?.status === 409) {
        const notice = detail || `Cycle already active for ${repo}`;
        assumedActive = true;
        store.setFetchError(notice);
        syncRunButton(store.getState());
        await refresh({ preserveNotice: notice });
        return false;
      }
      const code = typeof response?.status === "number" ? response.status : "error";
      store.setFetchError(detail || (code === 503 ? "Dispatcher is not configured" : `Failed to trigger Pit Stop (HTTP ${code})`));
      syncRunButton(store.getState());
      return false;
    } catch (error) {
      if (isStaleTrigger(ctx)) return false;
      triggerInFlight = false;
      store.setFetchError(`Failed to trigger Pit Stop: ${error instanceof Error && error.message ? error.message : "network error"}`);
      syncRunButton(store.getState());
      return false;
    }
  };

  const commitRepoInput = (refreshIfUnchanged) => {
    const raw = typeof header.repoInput?.value === "string" ? header.repoInput.value.trim() : "";
    const s = store.getState();
    if (raw === "") { if (s.repo !== "") store.setRepo(""); }
    else if (isRepoSlug(raw)) { if (s.repo !== raw) store.setRepo(raw); else if (refreshIfUnchanged && s.mode === "live") void refresh(); }
    else if (s.mode === "live") store.setFetchError("Invalid repository slug. Use owner/name.");
  };
  const onRepoChange = () => commitRepoInput(false);
  const onRepoKeydown = (e) => { if (e?.key === "Enter") { e.preventDefault?.(); commitRepoInput(true); } };
  const onRunClick = () => { if (store.getState().mode === "live") void triggerCycle(); };
  const onVisibilityChange = () => { if (!destroyed) { if (isHidden()) clearPollTimer(); else if (canPoll()) void refresh(); } };

  const onStoreChange = (s) => {
    if (destroyed) return;
    const modeChanged = s.mode !== prevMode, repoChanged = s.repo !== prevRepo;
    prevMode = s.mode; prevRepo = s.repo;
    if (modeChanged && s.mode === "live" && !isRepoSlug(s.repo)) {
      const candidate = typeof header.repoInput?.value === "string" ? header.repoInput.value.trim() : "";
      if (isRepoSlug(candidate)) { store.setRepo(candidate); return; }
    }
    syncPill(s);
    if (s.mode !== "live") { if (modeChanged) { resetCycle(); lastPollDelayMs = null; } return; }
    if (modeChanged || repoChanged) resetCycle();
    syncRunButton(s);
    if (modeChanged || repoChanged) {
      if (isRepoSlug(s.repo) && !isHidden() && typeof fetchFn === "function") void refresh();
      else lastPollDelayMs = null;
    }
  };

  header.repoInput?.addEventListener?.("change", onRepoChange);
  header.repoInput?.addEventListener?.("keydown", onRepoKeydown);
  header.runButton?.addEventListener?.("click", onRunClick);
  doc?.addEventListener?.("visibilitychange", onVisibilityChange);
  const unsubscribe = store.subscribe(onStoreChange);

  syncPill(store.getState());
  syncRunButton(store.getState());
  if (canPoll()) void refresh();

  const destroy = () => {
    if (destroyed) return;
    destroyed = true; resetCycle(); lastPollDelayMs = null; unsubscribe();
    header.repoInput?.removeEventListener?.("change", onRepoChange);
    header.repoInput?.removeEventListener?.("keydown", onRepoKeydown);
    header.runButton?.removeEventListener?.("click", onRunClick);
    doc?.removeEventListener?.("visibilitychange", onVisibilityChange);
    pill?.remove?.();
  };

  return {
    statusPill: pill, refresh, triggerCycle, destroy,
    getState: () => ({ destroyed, polling: pollTimer !== null, pollDelayMs: lastPollDelayMs, triggerInFlight, assumedActive }),
  };
}
