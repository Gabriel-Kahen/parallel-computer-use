// Optional end-to-end smoke test. Run from the repository root with Node 22+
// and Chromium/Chrome. Set CHROME_BIN for a custom browser executable.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";

const chrome = process.env.CHROME_BIN || [
  "/opt/google/chrome/chrome", "/usr/bin/chromium", "/usr/bin/google-chrome", "/usr/bin/chromium-browser"
].find(existsSync);
assert(chrome && typeof WebSocket === "function", "Run this smoke test with Node 22+ and Chrome or Chromium.");
const output = mkdtempSync(join(tmpdir(), "lcu-ui-smoke-"));
const data = join(output, "state");
const profile = join(output, "chrome");
mkdirSync(data, { mode: 0o700 });
const service = spawn("python3", ["-m", "local_computer_use", "serve", "--demo", "--sessions", "3", "--max-workers", "6",
  "--allow-domain", "example.invalid", "--browser-profile", "smoke-profile", "--state-dir", data]);
const browser = spawn(chrome, ["--headless", "--disable-dev-shm-usage", "--disable-extensions", "--disable-background-networking",
  "--no-first-run", "--remote-debugging-port=0", "--user-data-dir=" + profile, "about:blank"]);

function startup(process, stream, pattern) {
  return new Promise((resolve, reject) => {
    let output = "";
    stream.on("data", chunk => {
      output += chunk;
      const match = output.match(pattern);
      if (match) resolve(match[1]);
    });
    process.once("error", reject);
    process.once("exit", code => reject(new Error("Smoke test process exited early (" + code + ").")));
  });
}

async function stop(process) {
  if (!process.pid || process.exitCode !== null || process.signalCode !== null) return;
  const exited = once(process, "exit");
  process.kill("SIGTERM");
  await Promise.race([exited, sleep(5000, undefined, { ref: false })]);
  if (process.exitCode === null && process.signalCode === null) process.kill("SIGKILL");
}

async function connect(address) {
  const socket = new WebSocket(address);
  const pending = new Map();
  const responses = [];
  const commands = [];
  const errors = [];
  let id = 0;
  socket.onmessage = event => {
    const { id, method, params, result, error } = JSON.parse(event.data);
    if (id) {
      const { resolve, reject } = pending.get(id);
      pending.delete(id);
      if (error) reject(new Error(error.message));
      else resolve(result);
    } else if (method === "Network.responseReceived") {
      responses.push(params.response);
    } else if (method === "Network.requestWillBeSent" && params.request.url.endsWith("/command")) {
      commands.push(JSON.parse(params.request.postData));
    } else if (method === "Runtime.exceptionThrown") {
      errors.push(params.exceptionDetails.exception?.description || params.exceptionDetails.text);
    } else if (method === "Log.entryAdded" && params.entry.source === "security") {
      errors.push(params.entry.text);
    }
  };
  await once(socket, "open");
  socket.onclose = () => {
    for (const { reject } of pending.values()) reject(new Error("Chrome DevTools connection closed."));
  };
  return {
    socket, responses, commands, errors,
    send(method, params = {}) {
      const sequence = ++id;
      return new Promise((resolve, reject) => {
        pending.set(sequence, { resolve, reject });
        socket.send(JSON.stringify({ id: sequence, method, params }));
      });
    }
  };
}

async function smoke() {
  const [url, devtools] = await Promise.race([
    Promise.all([
      startup(service, service.stdout, /Open the local app: (http:\/\/[^\s]+)/),
      startup(browser, browser.stderr, /DevTools listening on ws:\/\/([^/]+)/)
    ]),
    sleep(15000, undefined, { ref: false }).then(() => { throw new Error("Dashboard or Chrome did not start."); })
  ]);
  const targets = await (await fetch("http://" + devtools + "/json/list")).json();
  const cdp = await connect(targets.find(target => target.type === "page").webSocketDebuggerUrl);
  try {
    await Promise.all(["Runtime", "Log", "Page", "Network"].map(domain => cdp.send(domain + ".enable")));
    const viewport = (width, height) => cdp.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: false });
    await viewport(1365, 900);
    await cdp.send("Page.navigate", { url });
    await cdp.send("Page.bringToFront");
    const evaluate = async expression => {
      const reply = await cdp.send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
      if (reply.exceptionDetails) throw new Error(reply.exceptionDetails.exception?.description || reply.exceptionDetails.text);
      return reply.result.value;
    };
    async function waitFor(check, description, ms = 8000) {
      const deadline = performance.now() + ms;
      while (performance.now() < deadline) {
        if (await check()) return;
        await sleep(85);
      }
      throw new Error("Timed out waiting for " + description + ".");
    }
    const wait = (expression, ms) => waitFor(() => evaluate(expression), expression, ms);
    const response = (part, status) => cdp.responses.some(item => item.url.includes(part) && item.status === status);
    const capture = async name => {
      const { data } = await cdp.send("Page.captureScreenshot", { format: "png" });
      writeFileSync(join(output, name + ".png"), Buffer.from(data, "base64"));
    };

    await wait('document.querySelector("#connection")?.dataset.status === "online"');
    assert(await evaluate('!location.hash && !document.cookie && Boolean(sessionStorage.getItem("lcu_session"))'));
    assert(await evaluate('!$("grid-empty").hidden && $("share-results-wrap").hidden'));
    assert(await evaluate('$("session-template").value === "auto" && !$("start-url-field").hidden'));
    assert(await evaluate('!$("browser-profile-wrap").hidden && $("browser-profile").value === ""'));
    await capture("empty");
    await waitFor(() => response("/api/state", 304), "conditional state response");

    await evaluate([
      '$("toggle-options").click(); $("session-template").value = "browser";',
      '$("session-template").dispatchEvent(new Event("change"));'
    ].join(" "));
    assert(await evaluate('!$("start-url-field").hidden && $("start-url-help").textContent.includes("example.invalid")'));
    await evaluate([
      '$("session-template").value = "editor"; $("session-template").dispatchEvent(new Event("change"));',
      '$("assignments-section").open = true; $("worker-count").value = 4; $("worker-count").dispatchEvent(new Event("input"));',
      '$("assignments-list").querySelector("textarea").value = "Own demo task"; $("prompt").value = "Synthetic worker test";',
      '$("assignments-list").children[3].querySelector("[data-field=dependencies]").value = "1, 2";',
      '$("assignments-list").children[3].querySelector("[data-field=observe]").checked = true;',
      'const imported = new DataTransfer(); imported.items.add(new File(["Synthetic input"], "brief.txt"));',
      '$("input-files").files = imported.files; $("input-files").dispatchEvent(new Event("change"));',
      '$("layout-fixed").click(); $("grid-rows").value = 1; $("grid-rows").dispatchEvent(new Event("change"));',
      '$("run-form").requestSubmit();'
    ].join(" "));
    await wait('document.querySelectorAll(".worker-tile").length === 4');
    assert(await evaluate('document.querySelector(".tile-task").textContent === "Own demo task"'));
    assert(await evaluate('$("run-imports").textContent.includes("brief.txt") && $("browser-profile-wrap").hidden && !$("input-files").files.length'));
    assert(await evaluate('state.runs[0].jobs[3].depends_on.join(",") === state.runs[0].jobs.slice(0, 2).map(job => job.id).join(",")'));
    assert(await evaluate('state.runs[0].jobs[3].observe_only === true && state.runs[0].jobs[0].observe_only === false'));
    assert(await evaluate('$("grid-viewport").scrollHeight > $("grid-viewport").clientHeight && getComputedStyle($("workers-grid")).gridTemplateColumns.split(" ").length === 2'));
    await wait('Array.from(document.querySelectorAll(".tile-image")).filter(image => image.complete && image.naturalWidth).length >= 2');
    const worker = await evaluate('state.runs[0].jobs[0].id');
    assert.equal(readFileSync(join(data, "work", worker, "imports", "brief.txt"), "utf8"), "Synthetic input");
    await capture("live-grid");
    await evaluate('globalThis.smokeTile = document.querySelector(".worker-tile"); smokeTile.querySelector(".tile-open").click();');
    await wait('!$("viewer-takeover").disabled && $("viewer-image").naturalWidth > 0');
    await evaluate('$("viewer-takeover").click()');
    await wait('$("viewer-owner").textContent === "You" && document.activeElement.id === "viewer-desktop"');
    const points = await evaluate('(() => { const img = $("viewer-image"); const box = img.getBoundingClientRect(); return { center: { x: box.x + box.width / 2, y: box.y + box.height / 2 }, edge: { x: box.x + 2, y: box.y + 2 }, width: img.naturalWidth, height: img.naturalHeight }; })()');
    async function click(point) {
      await cdp.send("Input.dispatchMouseEvent", { type: "mousePressed", button: "left", clickCount: 1, ...point });
      await cdp.send("Input.dispatchMouseEvent", { type: "mouseReleased", button: "left", clickCount: 1, ...point });
    }
    await click(points.edge);
    await click(points.center);
    await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: "F3", code: "F3" });
    await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: "F3", code: "F3" });
    await wait('$("viewer-action").textContent === "Pressed a key."');
    const clicks = cdp.commands.flatMap(value => value.action?.type === "click" ? [value.action] : []);
    assert.equal(clicks.length, 1, "An input in the letterbox must be ignored");
    assert(Math.abs(clicks[0].x - points.width / 2) <= 1 && Math.abs(clicks[0].y - points.height / 2) <= 1);
    await capture("viewer");
    await cdp.send("Input.dispatchKeyEvent", { type: "rawKeyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
    await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
    await wait('$("viewer").open && document.activeElement.id === "viewer-privacy"');
    await evaluate('$("viewer-privacy").click()');
    await wait('$("viewer-privacy").getAttribute("aria-pressed") === "true" && control.privateFrame?.url && $("viewer-image").complete');
    assert(await evaluate('smokeTile.querySelector(".tile-image").hidden && $("viewer-takeover").disabled && !previews.has(viewerId)'));
    assert(await evaluate('fetch("/api/frame/" + viewerId, { headers: apiHeaders() }).then(response => response.status === 204 && response.headers.get("X-LCU-Private") === "1")'));
    await capture("private");
    await evaluate('$("viewer-close").click()');
    await wait('!control && smokeTile.querySelector(".tile-owner").textContent === "Input: None" && smokeTile.querySelector(".tile-pause").disabled');
    await evaluate('smokeTile.querySelector(".tile-open").click()');
    assert(await evaluate('$("viewer-image").hidden && $("viewer-privacy").hidden && $("viewer-empty").textContent.includes("Private view")'));
    await evaluate('$("viewer-takeover").click()');
    await wait('$("viewer-owner").textContent === "You" && control.privateFrame?.url && $("viewer-image").complete');
    await evaluate('$("viewer-privacy").click()');
    await wait('!control.privateFrame && !control.privacyToken && !$("viewer-takeover").disabled');
    await evaluate('$("viewer-takeover").click()');
    await wait('$("viewer-owner").textContent === "Agent"');
    await evaluate('$("viewer-message").click()');
    assert(await evaluate('document.activeElement.id === "follow-up" && $("recipient").value === smokeTile.dataset.job && smokeTile === document.querySelector(".worker-tile")'));
    await evaluate('$("follow-up").value = "Synthetic worker follow-up"; $("message-form").requestSubmit();');
    await wait('$("events").textContent.includes("Synthetic worker follow-up")');
    await evaluate('$("recipient").value = "coordinator"; $("follow-up").value = "Synthetic coordinator follow-up"; $("message-form").requestSubmit();');
    await wait('document.querySelector(".event-row[data-reply] strong")?.textContent === "Coordinator → You"');
    await evaluate('smokeTile.querySelector(".tile-open").click(); $("viewer-takeover").click()');
    await wait('$("viewer-owner").textContent === "You"');
    await evaluate('$("viewer-close").click()');
    await wait('smokeTile.querySelector(".tile-owner").textContent === "Input: None" && smokeTile.querySelector(".tile-pause").textContent === "Resume"');
    await evaluate('smokeTile.querySelector(".tile-pause").click()');
    await wait('smokeTile.querySelector(".tile-owner").textContent === "Input: Agent" || smokeTile.querySelector(".worker-status").dataset.status === "completed"');
    await evaluate('$("stop").click()');
    await wait('$("metric-active").textContent === "0" && $("metric-queued").textContent === "0"');
    await waitFor(() => response("/api/frame/", 304), "conditional frame response");
    await evaluate('$("layout-auto").click(); window.scrollTo(0, 0);');
    await viewport(390, 800);
    assert(await evaluate('document.documentElement.scrollWidth === document.documentElement.clientWidth'));
    await capture("mobile");
    await viewport(1365, 900);
    await evaluate('$("session-template").value = "auto"; $("session-template").dispatchEvent(new Event("change")); $("worker-count").value = 1; $("prompt").value = "Synthetic export exercise"; $("run-form").requestSubmit();');
    await wait('$("workspace-title").textContent === "Run 2"');
    await wait('$("metric-finished").textContent === "1"', 15000);
    await evaluate('$("results-tab").click();');
    await wait('document.querySelector(".artifact-links button")?.textContent === "notes.txt"');
    await cdp.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: output });
    await evaluate('document.querySelector(".artifact-links button").click();');
    await waitFor(() => existsSync(join(output, "notes.txt")), "downloaded export");
    assert(readFileSync(join(output, "notes.txt"), "utf8").includes("Synthetic export exercise"));
    await evaluate('$("worker-count").value = 2; $("browser-profile").value = "smoke-profile"; $("prompt").value = "Named profile exercise"; $("run-form").requestSubmit();');
    await wait('$("workspace-title").textContent === "Run 3"');
    await wait('document.querySelectorAll(".worker-tile .worker-status")[0]?.dataset.status === "running"', 12000);
    assert(await evaluate('state.runs[0].jobs.every(job => job.browser_profile === "smoke-profile" && job.template === "browser") && document.querySelectorAll(".worker-tile .worker-status")[1].dataset.status === "queued"'));
    await evaluate('$("stop").click()');
    await wait('$("metric-active").textContent === "0" && $("metric-queued").textContent === "0"');
    await evaluate('$("all-workers").click();');
    await cdp.send("Page.reload");
    await wait('document.querySelector("#connection")?.dataset.status === "online" && document.querySelectorAll(".worker-tile").length === 7');

    // Enable a cloud option in the browser fixture and intercept its launch locally.
    // The real demo backend handles the remainder of this smoke test.
    await evaluate([
      'state.providers = state.providers.map(item => item.id === "gemini" ? { ...item, configured: true } : item); render();',
      'const textJob = state.runs[0].jobs[0]; textJob.has_desktop = false; render();',
      'globalThis.smokeLaunches = []; const originalFetch = fetch;',
      'window.fetch = (path, options) => { if (path !== "/api/runs") return originalFetch(path, options);',
      'smokeLaunches.push(JSON.parse(options.body));',
      'return Promise.resolve(Response.json({ id: state.runs[0].id }, { status: 201 })); };',
      '$("provider").value = "gemini"; $("provider").dispatchEvent(new Event("change"));'
    ].join(" "));
    assert(await evaluate('!$("share-results-wrap").hidden && !$("share-results").checked'));
    assert(await evaluate('((tile) => tile.preview.hidden && tile.open.hidden && !tile.textOnly.hidden && !tile.pause.hidden)(tiles.get(textJob.id))'));
    await evaluate('$("cloud-consent").checked = true; $("prompt").value = "Cloud consent fixture"; $("run-form").requestSubmit();');
    await wait('smokeLaunches.length === 1 && !launching');
    assert(await evaluate('smokeLaunches[0].cloud_consent && !("share_results" in smokeLaunches[0])'));
    await evaluate('$("cloud-consent").checked = true; $("share-results").checked = true; $("prompt").value = "Cloud result sharing fixture"; $("run-form").requestSubmit();');
    await wait('smokeLaunches.length === 2 && !launching');
    assert(await evaluate('smokeLaunches[1].share_results === true && !$("share-results").checked && !$("cloud-consent").checked'));
    await evaluate([
      '$("provider").value = "local"; $("provider").dispatchEvent(new Event("change"));',
      '$("worker-count").value = 2; $("assignments-section").open = true; assignWorkers();',
      '$("assignments-list").querySelector("[data-field=observe]").checked = true;',
      '$("prompt").value = "Observe only fixture"; $("run-form").requestSubmit();'
    ].join(" "));
    await wait('smokeLaunches.length === 3 && !launching');
    assert(await evaluate('smokeLaunches[2].assignments[0].observe_only === true && !("observe_only" in smokeLaunches[2].assignments[1])'));
    assert.deepEqual(cdp.errors, []);
  } finally {
    cdp.socket.close();
  }
}

try {
  await smoke();
  console.log("Dashboard smoke passed. Screenshots and synthetic download in " + output);
} finally {
  await Promise.all([stop(service), stop(browser)]);
  rmSync(profile, { recursive: true, force: true });
  rmSync(data, { recursive: true, force: true });
}
