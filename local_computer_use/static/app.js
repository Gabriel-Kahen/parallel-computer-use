"use strict";

const $ = id => document.getElementById(id);
const terminal = new Set(["completed", "failed", "cancelled", "interrupted", "partial"]);
const controllable = new Set(["running", "waiting", "paused", "awaiting_input"]);
const browserTemplates = new Set(["browser", "auto"]);
const localDate = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const localTime = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const runButtons = new Map();
const tiles = new Map();
const pendingJobs = new Set();
let state = { runs: [], providers: [], templates: [], limits: {} };
let jobs = new Map();
let selectedRun = null;
let visibleJobs = [];
let online = false;
let stateTag = "";
let hydrated = false;
let launching = false;
let stopping = false;
let writing = 0;
let revision = 0;
let configuredOptions = "";
let historySignature = "";
let resultSignature = "";
let recipientSignature = "";
let runPollTimer;
let polling = false;
let pollAgain = false;
let detailTab = "activity";
let lastView = null;
let viewerId = null;
let viewerOpener = null;
let control = null;
let lastError = "";
let layout = loadLayout();

const initialCredential = location.hash.slice(1);
if (initialCredential) history.replaceState(null, "", location.pathname + location.search);
let browserToken = "";
try {
  browserToken = sessionStorage.getItem("lcu_session") || "";
} catch {
  // A launch credential still works when session storage is unavailable.
}

function setText(element, value) {
  if (element.textContent !== value) element.textContent = value;
}

function humanize(value) {
  const label = String(value || "").replaceAll("_", " ");
  return label.charAt(0).toUpperCase() + label.slice(1);
}

function formatWhen(formatter, value, fallback = "") {
  const date = new Date(value);
  return value && !Number.isNaN(date.getTime()) ? formatter.format(date) : fallback;
}

function runHref(runId) {
  return "/api/runs/" + encodeURIComponent(runId);
}

function jobHref(jobId) {
  return "/api/jobs/" + encodeURIComponent(jobId) + "/command";
}

function apiHeaders(headers = {}) {
  return browserToken ? { Authorization: "Bearer " + browserToken, ...headers } : headers;
}

async function post(path, data, options = {}) {
  const response = await fetch(path, {
    method: "POST",
    credentials: "omit",
    headers: path === "/api/auth" ? { "Content-Type": "application/json" } : apiHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(data),
    keepalive: options.keepalive ?? false,
    signal: options.keepalive ? undefined : AbortSignal.timeout(9000)
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(result.error || "Request failed (" + response.status + ")");
    error.status = response.status;
    throw error;
  }
  return result;
}

async function transact(path, data) {
  revision++;
  writing++;
  try {
    return await post(path, data);
  } finally {
    writing--;
    revision++;
    schedulePoll(0);
  }
}

function announce(message, error = false) {
  if (error && lastError === message && !$("feedback").hidden) return;
  lastError = error ? message : "";
  $("feedback").dataset.level = error ? "error" : "success";
  setText($("feedback-text"), message);
  $("feedback").hidden = false;
}

function viewerNotice(message) {
  setText($("viewer-feedback"), message);
  $("viewer-feedback").hidden = !message;
}

function connection(status, label) {
  $("connection").dataset.status = status;
  setText($("connection"), label);
}

function schedulePoll(delay) {
  clearTimeout(runPollTimer);
  if (polling) {
    pollAgain ||= delay === 0;
    return;
  }
  if (!document.hidden) runPollTimer = setTimeout(pollState, delay);
}

async function pollState() {
  polling = true;
  pollAgain = false;
  const startingRevision = revision;
  let authenticated = true;
  try {
    const wasOnline = online;
    const response = await fetch("/api/state", {
      cache: "no-store",
      credentials: "omit",
      headers: apiHeaders(stateTag ? { "If-None-Match": stateTag } : {}),
      signal: AbortSignal.timeout(5000)
    });
    if (response.status === 401 || response.status === 403) {
      authenticated = false;
      stateTag = "";
      throw new Error("Open the dashboard using the launch URL from the local service.");
    }
    if (response.status !== 304 && !response.ok) throw new Error("Couldn't reach the local service.");
    online = true;
    connection("online", "Connected");
    if (response.status === 304) {
      if (!wasOnline) render();
      scheduleFrames();
      return;
    }
    const next = await response.json();
    if (startingRevision === revision && writing === 0) {
      stateTag = response.headers.get("ETag") || "";
      next.runs.sort((a, b) => b.created_at.localeCompare(a.created_at));
      if (hydrated) announceChanges(next);
      hydrated = true;
      state = next;
      jobs = new Map(state.runs.flatMap(run => run.jobs.map(job => [job.id, job])));
      for (const [id] of previews) if (jobs.get(id)?.private_view) forgetPublicFrame(id);
      if (control) {
        const job = jobs.get(control.jobId);
        if (job?.owner !== "user" || job?.lease !== control.lease) {
          dropControl();
          viewerNotice("Manual control ended.");
        } else if (!job.private_view) clearPrivateFrame(control);
      }
      if (selectedRun && !state.runs.some(run => run.id === selectedRun)) selectedRun = null;
      if (viewerId && !jobs.has(viewerId)) closeViewer();
      render();
      scheduleFrames();
    }
  } catch (error) {
    online = false;
    connection("offline", authenticated ? "Disconnected" : "Launch link required");
    if (control) releaseControl();
    if (!authenticated) announce(error.message, true);
    render();
  } finally {
    polling = false;
    const active = state.runs.some(run => !terminal.has(run.status));
    schedulePoll(pollAgain ? 0 : !online || !active ? 4000 : control ? 1100 : 1450);
  }
}

function announceChanges(next) {
  const previous = new Map(state.runs.map(run => [run.id, run.status]));
  const finished = next.runs.find(run => previous.has(run.id) && !terminal.has(previous.get(run.id)) && terminal.has(run.status));
  const waiting = next.runs.flatMap(run => run.jobs).find(job => jobs.has(job.id) && jobs.get(job.id).status !== "awaiting_input" && job.status === "awaiting_input");
  if (waiting) announce(waiting.title + " needs attention.");
  else if (finished) {
    const notice = finished.status === "partial" ? "Run finished with mixed results." : "Run " + finished.status + ".";
    announce(notice, finished.status === "failed");
  }
}

async function connect() {
  if (initialCredential) {
    try {
      const auth = await post("/api/auth", { token: initialCredential });
      if (typeof auth.token !== "string" || !auth.token) throw new Error("The launch link returned no dashboard session.");
      browserToken = auth.token;
      try {
        sessionStorage.setItem("lcu_session", browserToken);
      } catch {
        // The dashboard can keep the token in memory until the page closes.
      }
    } catch (error) {
      announce(error.message, true);
    }
  }
  schedulePoll(0);
}

function loadLayout() {
  try {
    const saved = JSON.parse(localStorage.getItem("desktop-grid-layout"));
    if (saved) return { fixed: saved.fixed === true, rows: gridDimension(saved.rows), columns: gridDimension(saved.columns) };
  } catch {
    // Layout preferences are optional.
  }
  return { fixed: false, rows: 2, columns: 2 };
}

function gridDimension(value) {
  return Math.max(1, Math.min(12, Math.round(Number(value) || 2)));
}

function setLayout(changes = {}) {
  layout = { ...layout, ...changes };
  const viewport = $("grid-viewport");
  viewport.dataset.fixed = layout.fixed;
  viewport.style.setProperty("--grid-rows", layout.rows);
  viewport.style.setProperty("--grid-columns", layout.columns);
  $("grid-dimensions").hidden = !layout.fixed;
  $("grid-rows").value = layout.rows;
  $("grid-columns").value = layout.columns;
  $("layout-auto").setAttribute("aria-pressed", !layout.fixed);
  $("layout-fixed").setAttribute("aria-pressed", layout.fixed);
  try {
    localStorage.setItem("desktop-grid-layout", JSON.stringify(layout));
  } catch {
    // Keep the in-memory preferences if storage is unavailable.
  }
}

function setOptions(select, items, fallback) {
  const previous = select.value;
  const options = items.map(({ id, label, disabled }) => {
    const option = new Option(label, id);
    option.disabled = !!disabled;
    return option;
  });
  select.replaceChildren(...options);
  if (items.some(item => item.id === previous && !item.disabled)) select.value = previous;
  else if (fallback !== undefined && items.some(item => item.id === fallback)) select.value = fallback;
  else select.selectedIndex = options.findIndex(option => !option.disabled);
}

function providerItems() {
  return state.providers.map(provider => ({
    id: provider.id,
    label: [provider.label, provider.cloud ? "Cloud" : "", provider.configured ? "" : "Unavailable"].filter(Boolean).join(" · "),
    disabled: !provider.configured
  }));
}

function renderRunOptions() {
  const signature = JSON.stringify([state.providers, state.templates, state.limits, state.browser_profiles]);
  if (configuredOptions === signature) return;
  configuredOptions = signature;
  const local = state.providers.find(provider => provider.configured && !provider.cloud);
  setOptions($("provider"), providerItems(), local?.id);
  const templates = [{ id: "auto", label: "Automatic" }, ...state.templates.filter(template => template.id !== "auto")];
  setOptions($("session-template"), templates, "auto");
  setOptions($("browser-profile"), [
    { id: "", label: "Temporary profile" },
    ...(state.browser_profiles || []).map(profile => ({ id: profile, label: profile }))
  ]);
  $("provider").disabled = !state.providers.some(provider => provider.configured);
  $("session-template").disabled = state.templates.length === 0;
  const limit = state.limits.workers || 32;
  $("worker-count").max = limit;
  if (Number($("worker-count").value) > limit) $("worker-count").value = limit;
  setText($("limit-detail"), state.limits.sessions
    ? "Up to " + state.limits.sessions + " desktop sessions and " + state.limits.inference + " model requests at a time."
    : "");
  assignWorkers();
}

function optionSelect(label, name) {
  const wrap = document.createElement("label");
  const select = document.createElement("select");
  select.dataset.field = name;
  wrap.append(label, select);
  return wrap;
}

function assignWorkers(resetConsent = false) {
  if (!$("assignments-section").open) {
    showProvider(resetConsent);
    return;
  }
  const list = $("assignments-list");
  const count = Math.min(Number($("worker-count").max), Number($("worker-count").value));
  if (count < 1) {
    showProvider(resetConsent);
    return;
  }
  for (let i = 0; i < count; i++) {
    let item = list.children[i];
    if (!item) {
      item = document.createElement("div");
      item.className = "assignment";
      const promptLabel = document.createElement("label");
      const prompt = document.createElement("textarea");
      prompt.rows = 2;
      prompt.maxLength = 4800;
      prompt.placeholder = "Inherit run prompt";
      promptLabel.append("Worker " + (i + 1) + " task", prompt);
      const fields = document.createElement("div");
      fields.className = "option-fields";
      fields.append(optionSelect("Provider", "provider"), optionSelect("Template", "template"));
      const observeLabel = document.createElement("label");
      observeLabel.className = "observe-only";
      const observe = document.createElement("input");
      observe.type = "checkbox";
      observe.dataset.field = "observe";
      const observeText = document.createElement("span");
      const observeNote = document.createElement("span");
      observeNote.className = "field-note";
      observeNote.textContent = "The model may read the desktop. Desktop input is manual.";
      observeText.append("Observe only ", observeNote);
      observeLabel.append(observe, observeText);
      item.append(promptLabel, fields, observeLabel);
      if (i > 0) {
        const after = document.createElement("label");
        const dependency = document.createElement("input");
        dependency.type = "text";
        dependency.dataset.field = "dependencies";
        dependency.placeholder = "Earlier worker numbers, separated by commas";
        dependency.maxLength = 390;
        after.append("After workers (optional)", dependency);
        item.append(after);
      }
      list.append(item);
    }
    if (item.dataset.options !== configuredOptions) {
      setOptions(item.querySelector('[data-field="provider"]'), [{ id: "", label: "Inherit run provider" }, ...providerItems()]);
      setOptions(item.querySelector('[data-field="template"]'), [
        { id: "", label: "Inherit run template" }, ...state.templates.filter(template => template.id !== "auto")
      ]);
      item.dataset.options = configuredOptions;
    }
  }
  Array.from(list.children).forEach((item, index) => { item.hidden = index >= count; });
  showProvider(resetConsent);
}

function assignmentRows() {
  const count = Number($("worker-count").value);
  return Array.from($("assignments-list").children).slice(0, count).map((item, index) => ({
    title: "Worker " + (index + 1),
    prompt: item.querySelector("textarea").value.trim(),
    provider: item.querySelector('[data-field="provider"]').value,
    template: item.querySelector('[data-field="template"]').value,
    dependencies: item.querySelector('[data-field="dependencies"]')?.value.trim() || "",
    observeOnly: item.querySelector('[data-field="observe"]').checked
  }));
}

function showProvider(resetConsent = false) {
  const id = $("provider").value;
  const template = $("session-template").value;
  const count = Number($("worker-count").value);
  const provider = state.providers.find(option => option.id === id);
  const assignments = assignmentRows();
  const used = new Set(assignments.map(assignment => assignment.provider || id));
  if (!assignments.length || assignments.length < count) used.add(id);
  const usedProviders = state.providers.filter(option => used.has(option.id));
  const hasCloud = usedProviders.some(option => option.cloud);
  const label = usedProviders.length > 1 ? "Mixed providers"
    : usedProviders[0] ? usedProviders[0].label + (hasCloud ? " · Cloud" : "") : "Local";
  setText($("provider-caption"), label);
  $("provider-caption").title = usedProviders.map(option => option.label).join(", ");
  $("provider-caption").dataset.cloud = hasCloud;
  setText($("provider-detail"), provider?.cloud ? "The coordinator runs locally. Assigned workers use the selected cloud provider."
    : state.demo ? "Demo workers use a deterministic local fixture." : "The coordinator runs locally.");
  $("cloud-consent-wrap").hidden = !hasCloud;
  $("share-results-wrap").hidden = !hasCloud;
  if (!hasCloud || resetConsent) {
    $("cloud-consent").checked = false;
    $("share-results").checked = false;
  }
  const usesBrowser = assignments.some(assignment => browserTemplates.has(assignment.template || template))
    || (assignments.length < count && browserTemplates.has(template));
  const domains = state.network_domains || [];
  const supportsStartUrl = usesBrowser && domains.length > 0;
  $("start-url-field").hidden = !supportsStartUrl;
  $("start-url").disabled = !supportsStartUrl;
  setText($("start-url-help"), domains.length ? "Applies to browser workers on " + domains.join(", ") + "." : "");
  const supportsProfile = usesBrowser && !!state.browser_profiles?.length;
  $("browser-profile-wrap").hidden = !supportsProfile;
  $("browser-profile").disabled = !supportsProfile;
  setText($("instance-label"), state.demo ? "Demo mode" : "On this machine");
  $("launch").disabled = !online || launching || !provider?.configured || !state.templates.length;
}

function statusMark(node, value) {
  setText(node, humanize(value));
  node.dataset.status = value;
}

function runOrdinal(run) {
  return state.runs.length - state.runs.indexOf(run);
}

function renderSidebar() {
  const list = $("run-list");
  const current = new Set(state.runs.map(run => run.id));
  for (const [id, node] of runButtons) {
    if (!current.has(id)) {
      node.remove();
      runButtons.delete(id);
    }
  }
  setText($("side-count"), String(jobs.size));
  $("all-workers").setAttribute("aria-current", selectedRun ? "false" : "page");
  $("history-empty").hidden = state.runs.length > 0;
  state.runs.forEach((run, index) => {
    let item = runButtons.get(run.id);
    if (!item) {
      item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "run-button";
      const title = document.createElement("span");
      const status = document.createElement("span");
      status.className = "worker-status";
      button.append(title, status);
      button.addEventListener("click", () => selectRun(run.id));
      item.append(button);
      runButtons.set(run.id, item);
    }
    const button = item.firstElementChild;
    setText(button.firstElementChild, run.prompt);
    button.title = "Run " + runOrdinal(run) + " · " + formatWhen(localDate, run.created_at) + " · " + run.prompt;
    statusMark(button.lastElementChild, run.status);
    button.setAttribute("aria-current", selectedRun === run.id ? "page" : "false");
    if (list.children[index] !== item) list.insertBefore(item, list.children[index] || null);
  });
}

function render() {
  const current = state.runs.find(run => run.id === selectedRun);
  const all = state.runs.slice().reverse().flatMap(run => run.jobs);
  visibleJobs = current?.jobs || all;
  const active = visibleJobs.filter(job => !terminal.has(job.status) && job.status !== "queued").length;
  const queued = visibleJobs.filter(job => job.status === "queued").length;
  renderRunOptions();
  showProvider();
  renderSidebar();
  setText($("workspace-label"), current ? formatWhen(localDate, current.created_at, "Run") : "WORKSPACE");
  setText($("workspace-title"), current ? "Run " + runOrdinal(current) : "Worker overview");
  setText($("workspace-note"), current ? current.prompt : "Launch a run and monitor its desktop sessions here.");
  setText($("metric-active"), String(active));
  setText($("metric-queued"), String(queued));
  setText($("metric-finished"), String(visibleJobs.length - active - queued));
  if (state.metrics) {
    $("metric-active").parentElement.title = state.metrics.active_sessions + " open desktops · " + state.metrics.active_inferences + " active model requests across runs";
  }
  $("stop").disabled = !online || stopping || !state.runs.some(run => !terminal.has(run.status));
  setText($("grid-count"), String(visibleJobs.length));
  $("grid-empty").hidden = visibleJobs.length > 0;
  if (lastView !== selectedRun) {
    $("grid-viewport").scrollTo(0, 0);
    lastView = selectedRun;
  }
  renderTiles();
  renderDetail(current);
  renderViewer();
}

function selectRun(id, recipient = "coordinator") {
  if (selectedRun !== id) $("follow-up").value = "";
  selectedRun = id;
  render();
  if (Array.from($("recipient").options).some(option => option.value === recipient)) $("recipient").value = recipient;
}

function desktopOwner(job) {
  if (job.owner === "user") return control?.jobId === job.id ? "You" : "User";
  return job.owner === "agent" ? "Agent" : "None";
}

function displayProvider(job) {
  const provider = state.providers.find(option => option.id === job.provider);
  return provider ? provider.label + (provider.cloud ? " · Cloud" : "") : job.provider;
}

function pauseCommand(job) {
  if (job.owner === "user") return "pause";
  return ["paused", "awaiting_input"].includes(job.status) ? "resume" : "pause";
}

function tileFor(job) {
  const node = $("worker-tile").content.firstElementChild.cloneNode(true);
  const parts = {
    node,
    preview: node.querySelector(".tile-preview"),
    textOnly: node.querySelector(".tile-text-only"),
    image: node.querySelector(".tile-image"),
    placeholder: node.querySelector(".tile-placeholder"),
    age: node.querySelector(".tile-preview-age"),
    title: node.querySelector("h3"),
    status: node.querySelector(".worker-status"),
    task: node.querySelector(".tile-task"),
    provider: node.querySelector(".tile-provider"),
    owner: node.querySelector(".tile-owner"),
    action: node.querySelector(".tile-action"),
    result: node.querySelector(".tile-result"),
    open: node.querySelector(".tile-open"),
    pause: node.querySelector(".tile-pause"),
    cancel: node.querySelector(".tile-cancel")
  };
  node.dataset.job = job.id;
  parts.preview.addEventListener("click", event => openViewer(job.id, event.currentTarget));
  parts.open.addEventListener("click", event => openViewer(job.id, event.currentTarget));
  parts.pause.addEventListener("click", () => command(job.id, pauseCommand(jobs.get(job.id))));
  parts.cancel.addEventListener("click", () => command(job.id, "cancel"));
  tiles.set(job.id, parts);
  tileObserver.observe(node);
  return parts;
}

function renderTiles() {
  const grid = $("workers-grid");
  const shown = new Set(visibleJobs.map(job => job.id));
  for (const [id, tile] of tiles) {
    if (!shown.has(id)) {
      tileObserver.unobserve(tile.node);
      visiblePreviews.delete(id);
      tile.node.remove();
      tiles.delete(id);
    }
  }
  visibleJobs.forEach((job, index) => {
    const tile = tiles.get(job.id) || tileFor(job);
    const hasDesktop = job.has_desktop !== false;
    const run = state.runs.find(candidate => candidate.id === job.run_id);
    setText(tile.title, job.title);
    tile.title.title = job.title;
    statusMark(tile.status, job.status);
    setText(tile.task, job.prompt);
    tile.task.title = job.prompt;
    setText(tile.provider, (!selectedRun && run ? "Run " + runOrdinal(run) + " · " : "") + displayProvider(job));
    setText(tile.owner, "Input: " + desktopOwner(job));
    setText(tile.action, job.detail || "");
    tile.action.title = job.detail || "";
    tile.action.hidden = !job.detail;
    setText(tile.result, job.result || "");
    tile.result.title = job.result || "";
    tile.result.hidden = !job.result;
    tile.preview.setAttribute("aria-label", "Open viewer for " + job.title);
    tile.open.setAttribute("aria-label", "Open viewer for " + job.title);
    tile.preview.hidden = !hasDesktop;
    tile.textOnly.hidden = hasDesktop;
    tile.open.hidden = !hasDesktop;
    const pausing = pauseCommand(job) === "pause";
    setText(tile.pause, pausing ? "Pause" : "Resume");
    tile.pause.setAttribute("aria-label", (pausing ? "Pause " : "Resume ") + job.title);
    tile.cancel.setAttribute("aria-label", "Cancel " + job.title);
    const unavailable = !online || pendingJobs.has(job.id) || terminal.has(job.status) || job.status === "cancelling";
    tile.pause.disabled = unavailable || !controllable.has(job.status) || (job.owner === "user" && control?.jobId !== job.id)
      || (job.private_view && !pausing);
    tile.pause.title = job.private_view && !pausing ? "Turn off private view in the session viewer before resuming." : "";
    tile.cancel.disabled = unavailable;
    if (hasDesktop) updateTileFrame(job);
    if (grid.children[index] !== tile.node) grid.insertBefore(tile.node, grid.children[index] || null);
  });
}

function emptyDetail(container, message) {
  const paragraph = document.createElement("p");
  paragraph.className = "detail-empty";
  paragraph.textContent = message;
  container.replaceChildren(paragraph);
}

function participant(id) {
  if (id === "user") return "You";
  if (id === "all") return "All workers";
  return jobs.get(id)?.title || humanize(id);
}

function renderEvents(current) {
  const container = $("events");
  const events = current?.events || state.runs.flatMap(run => run.events);
  const messages = current?.messages || state.runs.flatMap(run => run.messages);
  const signature = JSON.stringify([current?.id, events, messages]);
  if (signature === historySignature) return;
  historySignature = signature;
  const entries = events.filter(event => event.kind !== "message").map(event => ({
    at: event.at,
    order: event.seq,
    title: jobs.get(event.job_id)?.title || "Coordinator",
    text: event.text
  }));
  for (const message of messages) {
    entries.push({
      at: message.at,
      order: message.id,
      title: participant(message.sender) + " → " + participant(message.target),
      reply: message.sender === "coordinator" && message.target === "user",
      text: message.text
    });
  }
  entries.sort((a, b) => Date.parse(b.at) - Date.parse(a.at) || b.order - a.order);
  if (!entries.length) {
    emptyDetail(container, "Run events and follow-ups will appear here.");
    return;
  }
  const rows = entries.slice(0, 150).map(entry => {
    const row = document.createElement("div");
    row.className = "event-row";
    if (entry.reply) row.dataset.reply = "true";
    const time = document.createElement("time");
    time.dateTime = entry.at;
    time.title = formatWhen(localDate, entry.at);
    time.textContent = formatWhen(localTime, entry.at, "—");
    const title = document.createElement("strong");
    title.textContent = entry.title;
    title.title = entry.title;
    const paragraph = document.createElement("p");
    paragraph.textContent = entry.text;
    row.append(time, title, paragraph);
    return row;
  });
  container.replaceChildren(...rows);
}

function renderResults(current) {
  const container = $("results");
  const items = visibleJobs.filter(job => job.result || job.artifacts?.length);
  const signature = JSON.stringify([current?.id, items.map(job => [job.id, job.title, job.result, job.artifacts])]);
  if (signature === resultSignature) return;
  resultSignature = signature;
  if (!items.length) {
    emptyDetail(container, "Completed worker results will appear here.");
    return;
  }
  const rows = items.map(job => {
    const row = document.createElement("article");
    row.className = "result-row";
    const title = document.createElement("h3");
    title.textContent = job.title;
    const result = document.createElement("p");
    result.textContent = job.result;
    row.append(title);
    if (job.result) row.append(result);
    if (job.artifacts?.length) {
      const links = document.createElement("ul");
      links.className = "artifact-links";
      for (const file of job.artifacts) {
        const item = document.createElement("li");
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = file;
        button.setAttribute("aria-label", "Download " + file);
        button.addEventListener("click", () => downloadArtifact(job.id, file, button));
        item.append(button);
        links.append(item);
      }
      row.append(links);
    }
    return row;
  });
  container.replaceChildren(...rows);
}

async function downloadArtifact(jobId, file, button) {
  button.disabled = true;
  try {
    const response = await fetch("/api/artifacts/" + encodeURIComponent(jobId) + "/" + encodeURIComponent(file), {
      credentials: "omit",
      headers: apiHeaders(),
      signal: AbortSignal.timeout(9000)
    });
    if (!response.ok) throw new Error("Couldn't download " + file + ".");
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = file;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  } catch (error) {
    announce(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function renderDetail(current) {
  const summary = current?.summary || "";
  setText($("run-summary"), summary);
  $("run-summary").hidden = !summary;
  const imports = current?.input_names || [];
  $("run-imports").hidden = imports.length === 0;
  setText($("run-imports"), imports.length ? "Imported files · " + imports.join(", ") : "");
  const active = current && !terminal.has(current.status);
  $("cancel-run").hidden = !active;
  $("cancel-run").disabled = !online || !active || current?.status === "cancelling";
  $("message-form").hidden = !active;
  $("send").disabled = !online || !active;
  if (active) {
    const targets = [{ id: "coordinator", label: "Coordinator" }, ...current.jobs
      .filter(job => !terminal.has(job.status) && job.status !== "cancelling")
      .map(job => ({ id: job.id, label: job.title }))];
    const signature = JSON.stringify(targets);
    if (signature !== recipientSignature) {
      setOptions($("recipient"), targets, "coordinator");
      recipientSignature = signature;
    }
  }
  if (detailTab === "activity") renderEvents(current);
  else renderResults(current);
}

function setDetailTab(tab, focus = false) {
  detailTab = tab;
  for (const value of ["activity", "results"]) {
    const enabled = tab === value;
    const button = $(value + "-tab");
    button.setAttribute("aria-selected", enabled);
    button.tabIndex = enabled ? 0 : -1;
    $(value + "-panel").hidden = !enabled;
    if (enabled && focus) button.focus();
  }
  renderDetail(state.runs.find(run => run.id === selectedRun));
}

const previews = new Map();
const visiblePreviews = new Set();
let frameRequests = 0;
let frameTickTimer;
const tileObserver = new IntersectionObserver(entries => {
  for (const entry of entries) {
    const id = entry.target.dataset.job;
    if (entry.isIntersecting) visiblePreviews.add(id);
    else visiblePreviews.delete(id);
  }
  scheduleFrames(0);
});

function scheduleFrames(delay = 220) {
  clearTimeout(frameTickTimer);
  if (!document.hidden && online && (viewerId || visiblePreviews.size)) {
    frameTickTimer = setTimeout(frameTick, delay);
  }
}

function frameTick() {
  const now = performance.now();
  if (!document.hidden && online) {
    if (viewerId) requestFrame(viewerId, now, 320, 4, true);
    for (const id of visiblePreviews) {
      if (frameRequests >= 3) break;
      if (id !== viewerId) requestFrame(id, now, viewerId ? 3000 : 1700, 3);
    }
  }
  if (viewerId) showViewerFrame();
  else for (const id of visiblePreviews) if (jobs.has(id)) updateTileFrame(jobs.get(id));
  scheduleFrames();
}

function pruneFrames() {
  const inactive = Array.from(previews).filter(([id, frame]) => id !== viewerId && !visiblePreviews.has(id) && !frame.pending);
  const expired = inactive.sort(([, a], [, b]) => b.touched - a.touched).slice(12);
  for (const [id] of expired) {
    forgetPublicFrame(id);
    const job = jobs.get(id);
    if (job) updateTileFrame(job);
  }
}

function forgetPublicFrame(id) {
  const frame = previews.get(id);
  previews.delete(id);
  if (frame?.url) URL.revokeObjectURL(frame.url);
}

function clearPrivateFrame(ticket) {
  const url = ticket.privateFrame?.url;
  if (url) {
    if ($("viewer-image").getAttribute("src") === url) setImage($("viewer-image"), "");
    URL.revokeObjectURL(url);
  }
  ticket.privateFrame = null;
  ticket.privacyToken = "";
}

function viewerFrame(job) {
  if (!job.private_view) return previews.get(job.id);
  if (control?.jobId === job.id) return control.privateFrame;
  return null;
}

async function requestFrame(id, now, interval, concurrency, forViewer = false) {
  const job = jobs.get(id);
  if (!job || job.has_desktop === false || job.status === "queued" || frameRequests >= concurrency) return;
  let ticket = null;
  if (job.private_view) {
    if (!forViewer || viewerId !== id || control?.jobId !== id || !control.privacyToken) return;
    ticket = control;
  }
  const privacyToken = ticket?.privacyToken;
  const originalStateTag = stateTag;
  let frame = ticket ? ticket.privateFrame : previews.get(id);
  if (!frame) {
    frame = { url: "", etag: "", checked: 0, next: 0, touched: now, pending: false, final: false };
    if (ticket) ticket.privateFrame = frame;
    else previews.set(id, frame);
  }
  if (frame.pending || now < frame.next || frame.final) return;
  frame.next = now + interval;
  frame.pending = true;
  frame.touched = now;
  frameRequests++;
  const headers = frame.etag ? { "If-None-Match": frame.etag } : {};
  if (privacyToken) headers["X-LCU-Privacy"] = privacyToken;
  const stillCurrent = () => ticket
    ? control === ticket && viewerId === id && ticket.privacyToken === privacyToken && ticket.privateFrame === frame
    : previews.get(id) === frame && !jobs.get(id)?.private_view;
  try {
    const response = await fetch("/api/frame/" + encodeURIComponent(id), {
      credentials: "omit",
      cache: "no-store",
      headers: apiHeaders(headers),
      signal: AbortSignal.timeout(3500)
    });
    if (!stillCurrent()) return;
    if (!ticket && response.headers.get("X-LCU-Private") === "1" && originalStateTag === stateTag) {
      const current = jobs.get(id);
      if (current) {
        current.private_view = true;
        forgetPublicFrame(id);
        render();
        schedulePoll(0);
      }
      return;
    }
    if (response.status === 304) {
      frame.checked = performance.now();
    } else if (response.ok && response.headers.get("content-type")?.startsWith("image/png")) {
      const blob = await response.blob();
      if (!stillCurrent()) return;
      const oldUrl = frame.url;
      frame.url = URL.createObjectURL(blob);
      frame.etag = response.headers.get("ETag") || "";
      frame.checked = performance.now();
      if (!ticket) updateTileFrame(jobs.get(id) || job);
      if (viewerId === id) showViewerFrame();
      if (oldUrl) URL.revokeObjectURL(oldUrl);
    } else if (terminal.has(job.status) && [204, 404, 410].includes(response.status)) {
      frame.final = true;
    }
    if (terminal.has(job.status) && frame.url) frame.final = true;
    if (!frame.url && !frame.final) frame.next = performance.now() + Math.max(interval, 700);
  } catch {
    frame.next = performance.now() + Math.max(interval, 1100);
  } finally {
    frame.pending = false;
    frameRequests--;
    pruneFrames();
  }
}

function frameLabel(job, frame) {
  if (job.status === "queued") return "Queued";
  if (frame?.url && terminal.has(job.status)) return "Last frame";
  if (frame?.url && performance.now() - frame.checked > 7000 && (!online || !viewerId || viewerId === job.id)) return "Stale preview";
  if (frame?.url && job.status === "paused") return "Paused";
  if (!frame?.url && terminal.has(job.status)) return "Desktop closed";
  if (!frame?.url && job.status === "starting") return "Starting desktop";
  if (!frame?.url) return "Waiting for preview";
  return "";
}

function setImage(image, url) {
  if (url && image.getAttribute("src") !== url) image.src = url;
  else if (!url && image.hasAttribute("src")) image.removeAttribute("src");
  image.hidden = !url;
}

function updateTileFrame(job) {
  const tile = tiles.get(job.id);
  if (!tile) return;
  const frame = job.private_view ? null : previews.get(job.id);
  const url = frame?.url || "";
  const label = job.private_view ? "Private view active" : frameLabel(job, frame);
  setImage(tile.image, url);
  tile.placeholder.hidden = !!url;
  tile.age.hidden = !url || !label;
  setText(url ? tile.age : tile.placeholder, label);
}

function showViewerFrame() {
  const job = jobs.get(viewerId);
  if (!job) return;
  const frame = viewerFrame(job);
  const url = frame?.url || "";
  const label = job.private_view && !url ? "Private view active" : frameLabel(job, frame);
  setImage($("viewer-image"), url);
  $("viewer-empty").hidden = !!url;
  setText($("viewer-empty"), label || "Waiting for desktop frame");
  const status = job.owner === "user" ? "Manual control" : humanize(job.status);
  const privacy = job.private_view ? " · Private view" : "";
  setText($("viewer-status"), status + privacy + (url && label && label !== "Paused" ? " · " + label : ""));
}

function renderViewer() {
  const job = jobs.get(viewerId);
  if (!job) return;
  const controlling = control?.jobId === job.id;
  const somebodyElse = job.owner === "user" && !controlling;
  const unavailable = !online || pendingJobs.has(job.id) || terminal.has(job.status) || job.status === "cancelling";
  setText($("viewer-title"), job.title);
  setText($("viewer-provider"), displayProvider(job));
  setText($("viewer-profile"), job.browser_profile || "");
  $("viewer-profile-wrap").hidden = !job.browser_profile;
  setText($("viewer-owner"), somebodyElse ? "Another viewer" : desktopOwner(job));
  setText($("viewer-action"), job.detail || "No actions yet");
  const usage = job.model_usage || {};
  $("viewer-inference-wrap").hidden = !usage.requests;
  if (usage.requests) {
    const calls = usage.requests + (usage.requests === 1 ? " call" : " calls");
    const tokens = [["input_tokens", "in"], ["output_tokens", "out"]]
      .filter(([field]) => Number.isFinite(usage[field]))
      .map(([field, label]) => usage[field].toLocaleString() + " " + label).join(" / ");
    const partial = (usage.token_reports || 0) < usage.requests ? " (partial)" : "";
    setText($("viewer-inference"), calls + (tokens ? " · " + tokens + partial : ""));
  }
  setText($("viewer-task"), job.prompt);
  setText($("viewer-takeover"), controlling ? "Return control" : "Take control");
  setText($("viewer-pause"), controlling ? "Pause worker" : pauseCommand(job) === "resume" ? "Resume" : "Pause");
  let help = controlling
    ? "Escape moves focus to viewer controls. Closing the viewer pauses the worker."
    : "Take control to send input to this desktop.";
  if (job.private_view) {
    if (controlling) help = "Hide sensitive material before turning private view off. Escape moves focus to viewer controls.";
    else help = somebodyElse ? "The active viewer controls this private desktop." : "Take control to view this private desktop.";
  }
  setText($("viewer-help"), help);
  $("viewer-desktop").tabIndex = controlling ? 0 : -1;
  $("viewer-desktop").dataset.capture = controlling;
  $("viewer-privacy").hidden = !controlling;
  $("viewer-privacy").disabled = unavailable;
  $("viewer-privacy").setAttribute("aria-pressed", !!job.private_view);
  $("viewer-takeover").disabled = unavailable || somebodyElse || !controllable.has(job.status) || !job.width || !job.height
    || (controlling && job.private_view);
  $("viewer-takeover").title = controlling && job.private_view ? "Turn off private view to return control." : "";
  $("viewer-pause").disabled = unavailable || somebodyElse || !controllable.has(job.status)
    || (job.private_view && !controlling && pauseCommand(job) === "resume");
  $("viewer-cancel").disabled = unavailable;
  const run = state.runs.find(run => run.id === job.run_id);
  $("viewer-message").disabled = unavailable || !run || terminal.has(run.status);
  showViewerFrame();
}

function openViewer(id, element) {
  const job = jobs.get(id);
  if (!job || job.has_desktop === false) return;
  viewerId = id;
  viewerOpener = element;
  viewerNotice("");
  renderViewer();
  $("viewer").showModal();
  $("viewer-close").focus();
  scheduleFrames(0);
}

function closeViewer() {
  if (control) releaseControl();
  viewerId = null;
  $("viewer").close();
  setImage($("viewer-image"), "");
  if (viewerOpener?.isConnected) viewerOpener.focus();
  viewerOpener = null;
  scheduleFrames(0);
}

function dropControl() {
  const previous = control;
  if (!previous) return null;
  control = null;
  clearInterval(previous.heartbeat);
  clearTimeout(previous.dispatch);
  clearPrivateFrame(previous);
  return previous;
}

function releaseControl() {
  const previous = dropControl();
  if (!previous) return;
  renderViewer();
  revision++;
  post(jobHref(previous.jobId), { command: "pause", lease: previous.lease }, { keepalive: true })
    .catch(() => { if (viewerId === previous.jobId) viewerNotice("The viewer disconnected. The control lease will expire."); })
    .finally(() => {
      revision++;
      schedulePoll(0);
    });
}

function startControl(jobId, lease, privacyToken) {
  if (!Number.isInteger(lease)) throw new Error("The service did not provide a control lease.");
  if (viewerId !== jobId || document.hidden || !document.hasFocus()) {
    post(jobHref(jobId), { command: "pause", lease }, { keepalive: true }).catch(() => {});
    return;
  }
  control = {
    jobId, lease, heartbeat: null, actionQueue: [], dispatch: null, pendingInput: false, pointer: null,
    privacyToken: privacyToken || "", privateFrame: null
  };
  const current = control;
  current.heartbeat = setInterval(async () => {
    if (control !== current) return;
    try {
      await post(jobHref(jobId), { command: "heartbeat", lease });
    } catch {
      if (control !== current) return;
      releaseControl();
      viewerNotice("The control lease ended. Take control to reconnect.");
    }
  }, 4000);
}

async function command(id, name) {
  const job = jobs.get(id);
  if (!job || pendingJobs.has(id)) return;
  const previous = control?.jobId === id ? control : null;
  const lease = previous?.lease ?? job.lease;
  const changingPrivacy = name === "privacy-on" || name === "privacy-off";
  const wasPrivate = job.private_view;
  if (changingPrivacy && !previous) return;
  pendingJobs.add(id);
  if (changingPrivacy) {
    clearTimeout(previous.dispatch);
    previous.dispatch = null;
    previous.actionQueue.length = 0;
    if (name === "privacy-on") {
      job.private_view = true;
      forgetPublicFrame(id);
    }
  }
  if (previous && ["return", "pause", "cancel"].includes(name)) dropControl();
  if (viewerId === id) viewerNotice("");
  render();
  try {
    const result = await transact(jobHref(id), { command: name, lease });
    if (result.status) Object.assign(job, { status: result.status, owner: result.owner, lease: result.lease });
    if (typeof result.private_view === "boolean") job.private_view = result.private_view;
    if (name === "takeover") startControl(id, result.lease, result.privacy_token);
    if (changingPrivacy && control && control === previous) {
      clearPrivateFrame(control);
      if (job.private_view) control.privacyToken = result.privacy_token || "";
    }
  } catch (error) {
    if (changingPrivacy) job.private_view = wasPrivate;
    if (previous && !control) {
      post(jobHref(id), { command: "pause", lease: previous.lease }, { keepalive: true }).catch(() => {});
    }
    if (viewerId === id) viewerNotice(error.message);
    else announce(error.message, true);
  } finally {
    pendingJobs.delete(id);
    render();
    scheduleFrames(0);
    if ((name === "takeover" || changingPrivacy) && control?.jobId === id) $("viewer-desktop").focus();
  }
}

function desktopPoint(event, clamp = false) {
  const image = $("viewer-image");
  const job = jobs.get(viewerId);
  if (image.hidden || !image.complete || !image.naturalWidth || !job?.width || !job?.height) return null;
  if (performance.now() - (viewerFrame(job)?.checked || 0) > 7000) return null;
  const bounds = image.getBoundingClientRect();
  const scale = Math.min(bounds.width / image.naturalWidth, bounds.height / image.naturalHeight);
  const width = image.naturalWidth * scale;
  const height = image.naturalHeight * scale;
  if (!width || !height) return null;
  const x = (event.clientX - bounds.left - (bounds.width - width) / 2) / width;
  const y = (event.clientY - bounds.top - (bounds.height - height) / 2) / height;
  if (!clamp && (x < 0 || x >= 1 || y < 0 || y >= 1)) return null;
  return {
    x: Math.min(job.width - 1, Math.max(0, Math.floor(job.width * x))),
    y: Math.min(job.height - 1, Math.max(0, Math.floor(job.height * y)))
  };
}

function queueInput(action) {
  const current = control;
  if (!current || pendingJobs.has(current.jobId) || document.hidden || !document.hasFocus()) return;
  const job = jobs.get(current.jobId);
  if (job?.private_view && (!current.privacyToken || !current.privateFrame?.url
      || performance.now() - current.privateFrame.checked > 7000)) return;
  const queue = current.actionQueue;
  const last = queue.at(-1);
  if (action.type === "move" && last?.type === "move") queue[queue.length - 1] = action;
  else if (action.type === "scroll" && last?.type === "scroll") {
    const dy = Math.max(-12, Math.min(12, last.dy + action.dy));
    queue[queue.length - 1] = { ...action, dy };
  } else {
    if (queue.length > 22) {
      if (action.type !== "move") viewerNotice("Input is catching up. Try again in a moment.");
      return;
    }
    queue.push(action);
  }
  if (!current.dispatch && !current.pendingInput) {
    current.dispatch = setTimeout(() => dispatchInput(current), action.type === "move" || action.type === "scroll" ? 80 : 0);
  }
}

async function dispatchInput(current) {
  current.dispatch = null;
  if (control !== current || pendingJobs.has(current.jobId) || document.hidden || !document.hasFocus() || !current.actionQueue.length) return;
  const action = current.actionQueue.shift();
  if (action.type === "scroll" && !action.dy) {
    current.dispatch = setTimeout(() => dispatchInput(current), 0);
    return;
  }
  current.pendingInput = true;
  try {
    await post(jobHref(current.jobId), { command: "input", lease: current.lease, action });
    if (control === current && action.type !== "move") {
      const job = jobs.get(current.jobId);
      const frame = job && viewerFrame(job);
      if (frame && !frame.pending) frame.next = 0;
      scheduleFrames(25);
    }
  } catch (error) {
    if (control === current) {
      if (error.status === 409 || !error.status) releaseControl();
      viewerNotice(error.message);
    }
  } finally {
    current.pendingInput = false;
    if (control === current && current.actionQueue.length) {
      current.dispatch = setTimeout(() => dispatchInput(current), 0);
    }
  }
}

const xKeys = {
  Enter: "Return", Backspace: "BackSpace", ArrowUp: "Up", ArrowDown: "Down",
  ArrowLeft: "Left", ArrowRight: "Right", PageUp: "Page_Up", PageDown: "Page_Down",
  CapsLock: "Caps_Lock", NumLock: "Num_Lock", ScrollLock: "Scroll_Lock",
  " ": "space", "+": "plus"
};
const specialKeys = /^(?:F[1-9]|F1[0-2]|Tab|Delete|Insert|Home|End)$/;
const desktop = $("viewer-desktop");

desktop.addEventListener("keydown", event => {
  if (!control || event.isComposing || event.key === "Escape") return;
  const { key, ctrlKey, altKey, shiftKey, metaKey } = event;
  if ((ctrlKey || metaKey) && !altKey && !shiftKey && key.toLowerCase() === "v") return;
  if (["Control", "Alt", "Shift", "Meta", "AltGraph"].includes(key)) return;
  const normalized = xKeys[key] || (specialKeys.test(key) || /^[ -~]$/.test(key) ? key : null);
  if (normalized) {
    event.preventDefault();
    const actionKey = /^[A-Z]$/.test(normalized) ? normalized.toLowerCase() : normalized;
    const chord = [
      ctrlKey && "ctrl", altKey && "alt", metaKey && "super",
      shiftKey && (!/^[!-~]$/.test(key) || /^[A-Za-z]$/.test(key)) && "shift",
      actionKey
    ].filter(Boolean).join("+");
    queueInput({ type: "key", key: chord });
  } else if (Array.from(key).length === 1 && !ctrlKey && !altKey && !metaKey) {
    event.preventDefault();
    queueInput({ type: "type", text: key });
  }
});

desktop.addEventListener("compositionend", event => {
  if (control && event.data) queueInput({ type: "type", text: Array.from(event.data).slice(0, 3600).join("") });
});

desktop.addEventListener("paste", event => {
  if (!control) return;
  event.preventDefault();
  const text = event.clipboardData?.getData("text/plain") || "";
  if (text) queueInput({ type: "type", text: Array.from(text).slice(0, 3600).join("") });
});

desktop.addEventListener("pointerdown", event => {
  if (!control || event.button > 2 || !event.isPrimary) return;
  const point = desktopPoint(event);
  if (!point) return;
  event.preventDefault();
  desktop.focus({ preventScroll: true });
  control.pointer = {
    id: event.pointerId, button: event.button + 1, point,
    clientX: event.clientX, clientY: event.clientY
  };
  desktop.setPointerCapture(event.pointerId);
});

desktop.addEventListener("pointerup", event => {
  const pointer = control?.pointer;
  if (pointer?.id !== event.pointerId) return;
  control.pointer = null;
  const dragged = Math.hypot(event.clientX - pointer.clientX, event.clientY - pointer.clientY) >= 4;
  const point = desktopPoint(event, dragged);
  if (!point) return;
  if (dragged && pointer.button === 1) {
    queueInput({ type: "drag", ...pointer.point, to_x: point.x, to_y: point.y });
  } else if (!dragged) queueInput({ type: "click", ...point, button: pointer.button });
});

desktop.addEventListener("lostpointercapture", () => { if (control) control.pointer = null; });
desktop.addEventListener("pointermove", event => {
  if (!control || control.pointer || event.pointerType !== "mouse" || document.activeElement !== desktop) return;
  const point = desktopPoint(event);
  if (point) queueInput({ type: "move", ...point });
});

desktop.addEventListener("contextmenu", event => { if (control) event.preventDefault(); });
desktop.addEventListener("wheel", event => {
  if (!control) return;
  const point = desktopPoint(event);
  if (!point || !event.deltaY) return;
  event.preventDefault();
  const steps = event.deltaMode ? Math.abs(event.deltaY) : Math.abs(event.deltaY) / 80;
  queueInput({ type: "scroll", ...point, dy: Math.sign(event.deltaY) * Math.min(12, Math.max(1, Math.round(steps))) });
}, { passive: false });

function showRunOptions(show) {
  $("run-options").hidden = !show;
  $("toggle-options").setAttribute("aria-expanded", show);
}

function inputFileError(files) {
  if (files.length > 4 || files.reduce((total, file) => total + file.size, 0) > 256 * 1024) {
    return "Choose up to four input files, with a combined size of 256 KiB or less.";
  }
  if (files.some(file => !/^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/.test(file.name))) {
    return "Input filenames must start with a letter or number and use up to 80 letters, numbers, dots, underscores, or hyphens.";
  }
  if (new Set(files.map(file => file.name.toLowerCase())).size !== files.length) {
    return "Choose files with distinct names.";
  }
  return "";
}

function updateInputFiles() {
  const files = Array.from($("input-files").files);
  const error = inputFileError(files);
  const summary = $("input-files-summary");
  summary.hidden = !files.length;
  setText(summary, error || files.map(file => file.name).join(", "));
  if (error) $("input-files").setAttribute("aria-invalid", "true");
  else $("input-files").removeAttribute("aria-invalid");
}

function encodeInputFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("loadend", () => {
      if (typeof reader.result !== "string") {
        reject(new Error("Couldn't read " + file.name + "."));
        return;
      }
      resolve({ name: file.name, data: reader.result.slice(reader.result.indexOf(",") + 1) });
    });
    reader.readAsDataURL(file);
  });
}

async function launchRun(event) {
  event.preventDefault();
  const prompt = $("prompt").value.trim();
  if (!prompt || launching || !online) return;
  const files = Array.from($("input-files").files);
  const filesError = inputFileError(files);
  if (filesError) {
    showRunOptions(true);
    $("input-files").focus();
    announce(filesError, true);
    return;
  }
  const cloud = !$("cloud-consent-wrap").hidden;
  if (cloud && !$("cloud-consent").checked) {
    showRunOptions(true);
    $("cloud-consent").focus();
    announce("Allow the selected cloud workers to send task data before launching.", true);
    return;
  }
  const request = {
    prompt,
    count: Number($("worker-count").value),
    provider: $("provider").value,
    template: $("session-template").value,
    cloud_consent: cloud
  };
  if (cloud && $("share-results").checked) request.share_results = true;
  if (!$("start-url").disabled && $("start-url").value.trim()) request.start_url = $("start-url").value.trim();
  if (!$("browser-profile").disabled && $("browser-profile").value) request.browser_profile = $("browser-profile").value;
  const assignments = assignmentRows();
  const dependencies = assignments.map((assignment, index) => {
    if (!assignment.dependencies) return [];
    const numbers = assignment.dependencies.split(",").map(part => part.trim());
    if (numbers.some(number => !/^[1-9]\d*$/.test(number) || Number(number) > index)) return null;
    return [...new Set(numbers.map(number => Number(number) - 1))];
  });
  const invalidWorker = dependencies.indexOf(null);
  if (invalidWorker !== -1) {
    showRunOptions(true);
    $("assignments-section").open = true;
    $("assignments-list").children[invalidWorker].querySelector('[data-field="dependencies"]').focus();
    announce("Use earlier worker numbers separated by commas.", true);
    return;
  }
  if (assignments.some(assignment => assignment.prompt || assignment.provider || assignment.template || assignment.dependencies || assignment.observeOnly)) {
    request.mode = "manual";
    request.assignments = Array.from({ length: request.count }, (_, index) => {
      const assignment = assignments[index];
      const worker = {
        title: assignment?.title || "Worker " + (index + 1),
        prompt: assignment?.prompt || prompt,
        provider: assignment?.provider || request.provider,
        template: assignment?.template || request.template,
        depends_on: dependencies[index] || []
      };
      if (assignment?.observeOnly) worker.observe_only = true;
      return worker;
    });
  }
  launching = true;
  showProvider();
  try {
    if (files.length) request.input_files = await Promise.all(files.map(encodeInputFile));
    const response = await transact("/api/runs", request);
    selectedRun = response.id;
    $("prompt").value = "";
    $("start-url").value = "";
    $("browser-profile").value = "";
    $("input-files").value = "";
    updateInputFiles();
    $("follow-up").value = "";
    $("assignments-list").replaceChildren();
    assignWorkers();
    showProvider(true);
    showRunOptions(false);
    setDetailTab("activity");
    announce("Run launched with " + request.count + (request.count === 1 ? " worker." : " workers."));
  } catch (error) {
    announce(error.message, true);
  } finally {
    launching = false;
    showProvider();
  }
}

async function sendMessage(event) {
  event.preventDefault();
  const text = $("follow-up").value.trim();
  const runId = selectedRun;
  const target = $("recipient").value;
  if (!text || !runId || !target || $("send").disabled) return;
  $("send").disabled = true;
  try {
    await transact(runHref(runId) + "/message", { target, text });
    if (selectedRun === runId && $("follow-up").value.trim() === text) $("follow-up").value = "";
    announce("Follow-up sent.");
  } catch (error) {
    announce(error.message, true);
  } finally {
    renderDetail(state.runs.find(run => run.id === selectedRun));
  }
}

$("run-form").addEventListener("submit", launchRun);
$("message-form").addEventListener("submit", sendMessage);
$("toggle-options").addEventListener("click", () => showRunOptions($("run-options").hidden));
$("assignments-section").addEventListener("toggle", () => assignWorkers());
$("worker-count").addEventListener("input", () => assignWorkers(true));
$("provider").addEventListener("change", () => showProvider(true));
$("session-template").addEventListener("change", () => showProvider());
$("start-url").addEventListener("invalid", () => showRunOptions(true));
$("input-files").addEventListener("change", updateInputFiles);
$("assignments-list").addEventListener("change", event => {
  if (event.target.dataset.field === "provider") showProvider(true);
  else if (event.target.dataset.field === "template") showProvider();
});
$("all-workers").addEventListener("click", () => selectRun(null));
$("new-run").addEventListener("click", () => {
  showProvider(true);
  $("prompt").focus();
});
$("dismiss-feedback").addEventListener("click", () => { $("feedback").hidden = true; });
$("layout-auto").addEventListener("click", () => setLayout({ fixed: false }));
$("layout-fixed").addEventListener("click", () => setLayout({ fixed: true }));
$("grid-rows").addEventListener("change", () => setLayout({ rows: gridDimension($("grid-rows").value) }));
$("grid-columns").addEventListener("change", () => setLayout({ columns: gridDimension($("grid-columns").value) }));
for (const tab of ["activity", "results"]) {
  const button = $(tab + "-tab");
  button.addEventListener("click", () => setDetailTab(tab));
  button.addEventListener("keydown", event => {
    if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      setDetailTab(event.key === "Home" ? "activity" : event.key === "End" ? "results" : tab === "activity" ? "results" : "activity", true);
    }
  });
}
$("viewer-close").addEventListener("click", closeViewer);
$("viewer").addEventListener("cancel", event => {
  event.preventDefault();
  if (control && document.activeElement === desktop) {
    ($("viewer-privacy").disabled ? $("viewer-close") : $("viewer-privacy")).focus();
    return;
  }
  closeViewer();
});
$("viewer-pause").addEventListener("click", () => command(viewerId, pauseCommand(jobs.get(viewerId))));
$("viewer-cancel").addEventListener("click", () => command(viewerId, "cancel"));
$("viewer-privacy").addEventListener("click", () => {
  const job = jobs.get(viewerId);
  if (job) command(job.id, job.private_view ? "privacy-off" : "privacy-on");
});
$("viewer-takeover").addEventListener("click", () => command(viewerId, control?.jobId === viewerId ? "return" : "takeover"));
$("viewer-message").addEventListener("click", () => {
  const job = jobs.get(viewerId);
  if (!job) return;
  closeViewer();
  selectRun(job.run_id, job.id);
  setDetailTab("activity");
  $("follow-up").focus();
});

$("cancel-run").addEventListener("click", async () => {
  if (!selectedRun || $("cancel-run").disabled) return;
  $("cancel-run").disabled = true;
  try {
    await transact(runHref(selectedRun) + "/cancel", {});
    announce("Run cancellation requested.");
  } catch (error) {
    $("cancel-run").disabled = false;
    announce(error.message, true);
  }
});

$("stop").addEventListener("click", async () => {
  if ($("stop").disabled) return;
  stopping = true;
  $("stop").disabled = true;
  if (control) dropControl();
  renderViewer();
  try {
    await transact("/api/stop", {});
    announce("Stopped dispatching workers. Closing active desktops.");
  } catch (error) {
    announce(error.message, true);
  } finally {
    stopping = false;
  }
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    releaseControl();
    clearTimeout(runPollTimer);
    clearTimeout(frameTickTimer);
  } else {
    schedulePoll(0);
    scheduleFrames(0);
  }
});
window.addEventListener("blur", releaseControl);
window.addEventListener("pagehide", releaseControl);
window.addEventListener("online", () => schedulePoll(0));

setLayout();
render();
connect();
