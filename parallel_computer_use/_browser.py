"""Guest-only semantic driver for the focused Chromium page."""

import fcntl
import json
import os
import runpy
import select
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

# Runs in an isolated world. Only the current view's visible controls receive refs.
_OBSERVE = r"""((hosts) => {
  const clean = value => value.replace(/\s+/g, ' ').trim().slice(0, 160);
  const hidden = '[aria-hidden="true"], [inert], [hidden]';
  const visibility = {opacityProperty:true, visibilityProperty:true, contentVisibilityAuto:true};
  const position = (element, fragments = false) => {
    if (!element.isConnected || element.closest(hidden) || !element.checkVisibility(visibility)) return null;
    const rects = fragments ? element.getClientRects() : [element.getBoundingClientRect()];
    for (let index = 0; index < Math.min(rects.length, 8); index++) {
      const rect = rects[index];
      const left = Math.max(0, rect.left), right = Math.min(innerWidth, rect.right);
      const top = Math.max(0, rect.top), bottom = Math.min(innerHeight, rect.bottom);
      if (right - left < 1 || bottom - top < 1) continue;
      const x = (left + right) / 2, y = (top + bottom) / 2;
      if (element.contains(document.elementFromPoint(x, y))) return [x, y];
    }
    return null;
  };
  const describe = element => {
    const label = clean(element.getAttribute('aria-label') || element.labels?.[0]?.innerText ||
      element.getAttribute('title') || (element instanceof HTMLInputElement ? element.placeholder : element.innerText) || '');
    const section = element.closest('section,fieldset,article')?.querySelector('h1,h2,h3,legend');
    return [element.localName, label, clean(section?.innerText || ''), element.getAttribute('href'), element.type || ''];
  };
  const destination = element => {
    if (!element.matches('a[href]:not([download],[ping],[aria-disabled="true"])') ||
        (element.hasAttribute('role') && element.getAttribute('role') !== 'link')) return null;
    let url;
    try { url = new URL(element.href, document.baseURI); }
    catch { return null; }
    if (url.protocol !== 'https:' || url.username || url.password || url.port ||
        !hosts.includes(url.hostname.replace(/\.$/, '')) || url.href.length > 2048) return null;
    return url;
  };
  const live = hosts.length !== 0;
  const targets = new Map(), lines = [];
  let cropCanvas = null;
  globalThis.pcuCanvasAt = points => !!cropCanvas && cropCanvas.isConnected && document.hasFocus() &&
    document.visibilityState === 'visible' && points.every(([x, y]) => cropCanvas.contains(document.elementFromPoint(x, y)));
  globalThis.pcuResolve = ref => {
    const item = targets.get(ref) || (live && [...targets.values()].find(link => link.publicURL === ref));
    if (!document.hasFocus() || document.visibilityState !== 'visible' || !item) return null;
    if (live) {
      if (destination(item.element)?.href !== item.signature || item.label() !== item.text) return null;
    } else if (item.element.matches(':disabled,[aria-disabled="true"]') ||
               JSON.stringify(describe(item.element)) !== item.signature) return null;
    return position(item.element, live);
  };
  if (!document.hasFocus() || document.visibilityState !== 'visible' || !document.body) return null;
  let visual = false, status = '';
  if (live) {
    // Visit the main content before the surrounding navigation. Read text nodes
    // through their painted ranges so hidden descendants and field values stay out.
    const main = document.querySelector('main,[role="main"]');
    const skip = `script,style,noscript,template,iframe,object,embed,input,textarea,select,
      [contenteditable]:not([contenteditable="false"]),${hidden}`;
    const group = 'a[href],button,h1,h2,h3,h4,h5,h6,p,li,label,dt,dd,th,td,output,[role="status"]';
    const range = document.createRange();
    const start = performance.now();
    let textLength = 0;
    const visibleText = node => {
      const element = node.parentElement;
      if (!element || element.closest(hidden) || !element.checkVisibility(visibility)) return '';
      if (/^rgba\(.*, 0\)$|\/ 0\)$/.test(getComputedStyle(element).webkitTextFillColor)) return '';
      const end = Math.min(node.length, 192);
      range.setStart(node, 0);
      range.setEnd(node, end);
      const rects = range.getClientRects();
      if (!rects.length || rects.length > 8) return '';
      let painted = false;
      for (const rect of rects) {
        if (rect.width === 0) continue;
        if (rect.height < 2 || rect.left < 0 || rect.right > innerWidth || rect.top < 0 || rect.bottom > innerHeight ||
            ![0.25, 0.5, 0.75].every(fraction => element.contains(document.elementFromPoint(
              rect.left + rect.width * fraction, rect.top + rect.height / 2)))) return '';
        painted = true;
      }
      return painted ? node.data.slice(0, end) : '';
    };
    const read = (root, omit, limit) => {
      const rows = new Map(), stack = [root];
      for (let visited = 0; stack.length && visited < limit && performance.now() - start < 130; visited++) {
        const node = stack.pop();
        if (node !== root && node.nextSibling) stack.push(node.nextSibling);
        if (node.nodeType === Node.TEXT_NODE) {
          const parent = node.parentElement;
          const link = parent.closest('a[href]');
          const element = link || parent.closest(group) || parent;
          const row = rows.get(element);
          if (row?.length >= 18 || (!row && (rows.size >= 30 || !node.data.slice(0, 192).trim()))) continue;
          const text = visibleText(node);
          if (text) {
            if (row) row.push([node, text]);
            else rows.set(element, [[node, text]]);
          }
        } else if (node.nodeType === Node.ELEMENT_NODE && node !== omit && !node.matches(skip)) {
          if (!visual && node.matches('canvas,svg,img,video') && position(node)) {
            const rect = node.getBoundingClientRect();
            const width = Math.min(rect.right, innerWidth) - Math.max(rect.left, 0);
            const height = Math.min(rect.bottom, innerHeight) - Math.max(rect.top, 0);
            visual = node.matches('canvas,svg') ? width >= 96 && height >= 96 :
              width * height >= innerWidth * innerHeight / 4;
          }
          if (node.firstChild) stack.push(node.firstChild);
        }
      }
      return rows;
    };
    const append = (rows, maximum) => {
      for (const [element, parts] of rows) {
        if (lines.length >= maximum) break;
        const separator = element.matches('a,h1,h2,h3,h4,h5,h6') ? '' : ' ';
        const label = clean(parts.map(([, text]) => text).join(separator));
        if (!label) continue;
        let line = `${element.localName} ${label}`;
        if (!status && element.matches('output,[role="status"]')) status = label;
        const url = destination(element);
        if (url) {
          const path = url.origin + url.pathname;
          if (path.length <= 180) line += ` (${path})`;
        }
        // Reserve room for the metadata and JSON escaping in the host response.
        if (textLength + line.length + 8 >= 4700) break;
        if (url && position(element, true)) {
          const ref = `e${targets.size + 1}`;
          // Keep node identities and visible labels for a fresh click-time check.
          const nodes = parts.map(([node]) => node);
          const publicURL = url.href.length <= 180 && !url.href.includes('?') && !url.href.includes('#') ? url.href : null;
          targets.set(ref, {element, signature:url.href, publicURL, text:label,
            label:() => clean(nodes.map(node => element.contains(node) ? visibleText(node) : '').join(separator))});
          line += ` [${ref}]`;
        }
        textLength += line.length + 1;
        lines.push(line);
      }
    };
    if (main) append(read(main, null, 2100), 26);
    append(read(document.body, main, main ? 900 : 2600), 30);
  } else {
    const cursor = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
    for (let index = 0, element; index < 800 && lines.length < 30 && (element = cursor.nextNode()); index++) {
      if (!element.matches('h1,h2,h3,li,p,output,button,a[href],input,textarea,select,canvas,svg,[role="status"]')) continue;
      if (!position(element)) continue;
      if (element.matches('canvas,svg')) {
        const rect = element.getBoundingClientRect();
        if (rect.width >= 96 && rect.height >= 96) visual = true;
        continue;
      }
      if (element.matches('input[type=password],input[type=file],input[type=hidden]')) continue;
      const description = describe(element), [tag, label, section] = description;
      const control = element.matches('button,a[href],input,textarea,select');
      if (!label && !control) continue;
      if (!status && element.matches('output,[role="status"]')) status = label;
      let line = `${tag} ${label}`;
      if (control) {
        if (section && section !== label) line += ` (${section})`;
        if (document.activeElement === element) line += ' [focused]';
        if (element.matches(':disabled,[aria-disabled="true"]')) line += ' [disabled]';
        else {
          const ref = `e${targets.size + 1}`;
          targets.set(ref, {element, signature:JSON.stringify(description)});
          line += ` [${ref}]`;
        }
        if (element.matches('input:not([type=checkbox],[type=radio]),textarea') && element.value)
          line += ` value: ${clean(element.value)}`;
      }
      lines.push(line);
    }
  }
  const state = {text:lines.join('\n'), count:targets.size, visual, status};
  if (live) state.ready = document.readyState;
  if (!visual || devicePixelRatio !== 1 || visualViewport?.scale !== 1 ||
      visualViewport.offsetLeft !== 0 || visualViewport.offsetTop !== 0 ||
      outerWidth !== innerWidth || outerHeight !== innerHeight) return state;
  const canvases = document.getElementsByTagName('canvas');
  if (canvases.length > 16) return state;
  let canvasRect = null;
  for (const canvas of canvases) {
    if (!position(canvas)) continue;
    const rect = canvas.getBoundingClientRect();
    const left = Math.max(0, Math.ceil(rect.left)), top = Math.max(0, Math.ceil(rect.top));
    const right = Math.min(innerWidth, Math.floor(rect.right)), bottom = Math.min(innerHeight, Math.floor(rect.bottom));
    if (right - left < 96 || bottom - top < 96) continue;
    if (canvasRect) return state;
    // Reject a transformed or occluded corner where the rectangle would include other content.
    if (![[left, top], [right - 1, top], [left, bottom - 1], [right - 1, bottom - 1]]
        .every(([x, y]) => canvas.contains(document.elementFromPoint(x + 0.5, y + 0.5)))) return state;
    canvasRect = [left, top, right - left, bottom - top];
    cropCanvas = canvas;
  }
  if (canvasRect) {
    state.canvas_rect = canvasRect;
    state.viewport = [screenX, screenY, innerWidth, innerHeight, devicePixelRatio];
  }
  return state;
})"""


class Chrome:
    def __init__(self, command, url, allowed_domains):
        self.url = url
        if not isinstance(allowed_domains, list):
            raise ValueError("Expected a browser domain list")
        if allowed_domains:
            network = runpy.run_path(str(Path(__file__).with_name("_network.py")))
            self.allowed_domains = sorted(network["normalize_domains"](allowed_domains))
            self.validate_url = network["validate_network_target"]
            self.validate_url(self.url, self.allowed_domains)
        else:
            self.allowed_domains = []
            if not self.url.startswith("file:///"):
                raise ValueError("The offline browser requires a local catalog")
        self.observer = f"{_OBSERVE}({json.dumps(self.allowed_domains)})"
        self.request = 0
        self.pending = bytearray()
        self.responses = {}
        self.pipe_intact = True
        self.session = self.document = self.world = None
        self.targets = {}
        self.revision = 0
        self.document_revision = 0
        self.deadline = 0.0
        incoming, self.input = os.pipe()
        self.output, outgoing = os.pipe()
        # Chromium needs numbered pipes. Relocate the originals before dup2.
        read_fd = fcntl.fcntl(incoming, fcntl.F_DUPFD_CLOEXEC, 10)
        write_fd = fcntl.fcntl(outgoing, fcntl.F_DUPFD_CLOEXEC, 10)
        os.close(incoming)
        os.close(outgoing)
        os.set_blocking(self.input, False)
        try:
            self.process = subprocess.Popen(
                ["/bin/bash", "-c", 'exec 3<&"$1" 4>&"$2"; shift 2; exec "$@"',
                 "pcu-chrome", str(read_fd), str(write_fd), *command, "--remote-debugging-pipe"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                pass_fds=(read_fd, write_fd),
            )
        except BaseException:
            os.close(self.input)
            os.close(self.output)
            raise
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def packet(self, method, browser, params):
        self.request += 1
        request = {"id": self.request, "method": method, "params": params}
        if not browser:
            request["sessionId"] = self.session
        return json.dumps(request, separators=(",", ":")).encode() + b"\0"

    def receive(self, request):
        if request in self.responses:
            return self.result(self.responses.pop(request))
        deadline = min(self.deadline, time.monotonic() + 0.65)
        while time.monotonic() < deadline:
            if (end := self.pending.find(0)) >= 0:
                response = json.loads(self.pending[:end])
                del self.pending[:end + 1]
                response_id = response.get("id")
                if response_id == request:
                    return self.result(response)
                if response_id == request + 1 and response_id <= self.request:
                    self.responses[response_id] = response
                continue
            if not select.select([self.output], [], [], max(0, deadline - time.monotonic()))[0]:
                break
            block = os.read(self.output, 65536)
            if not block or len(self.pending) + len(block) > 96 * 1024:
                raise RuntimeError("Invalid Chromium observation response")
            self.pending.extend(block)
        raise TimeoutError("Chromium observation timed out")

    @staticmethod
    def result(response):
        if "error" in response:
            raise RuntimeError("Chromium could not resolve the current page")
        return response["result"]

    def send(self, method, *, browser=False, **params):
        if not self.pipe_intact:
            raise OSError("Chromium control pipe is unavailable")
        self.responses.clear()
        data = self.packet(method, browser, params)
        pending = memoryview(data)
        while pending:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [self.input], [], remaining)[1]:
                self.pipe_intact = len(pending) == len(data)
                raise TimeoutError("Chromium observation timed out")
            try:
                pending = pending[os.write(self.input, pending):]
            except BlockingIOError:
                continue
        return self.receive(self.request)

    def eligible(self, url):
        if not self.allowed_domains:
            return url == self.url
        try:
            self.validate_url(url, self.allowed_domains)
        except ValueError:
            return False
        return True

    def context(self):
        pages = [page for page in self.send("Target.getTargets", browser=True)["targetInfos"]
                 if page["type"] == "page" and not page.get("subtype")]
        selected = None
        if len(pages) <= (6 if self.allowed_domains else 1):
            open_targets = {page["targetId"] for page in pages}
            self.targets = {target: state for target, state in self.targets.items() if target in open_targets}
            for page in pages:
                if not self.eligible(page["url"]):
                    continue
                target = page["targetId"]
                self.session, previous, self.world = self.targets.get(target, (None, None, None))
                if self.session is None:
                    self.session = self.send("Target.attachToTarget", browser=True, targetId=target,
                                             flatten=True)["sessionId"]
                    self.targets[target] = self.session, None, None
                frame = self.send("Page.getFrameTree")["frameTree"]["frame"]
                url = frame["url"] + frame.get("urlFragment", "")
                if frame.get("unreachableUrl") or url != page["url"] or not self.eligible(url):
                    continue
                document = target, frame["id"], frame["loaderId"], url
                if previous is None or previous[:3] != document[:3]:
                    self.world = self.send("Page.createIsolatedWorld", frameId=frame["id"],
                                           worldName="pcu-observer")["executionContextId"]
                self.targets[target] = self.session, document, self.world
                if len(pages) > 1 and self.evaluate("document.hasFocus() && document.visibilityState === 'visible'",
                                                    url) is not True:
                    continue
                if selected is not None:
                    selected = None
                    break
                selected = self.session, document, self.world
        if selected is None:
            self.session = self.document = self.world = None
            return None
        if self.document != selected[1]:
            self.revision += 1
            self.document_revision += 1
        self.session, self.document, self.world = selected
        return self.document

    def evaluate(self, expression, url, *, await_promise=False):
        expression = f"location.href === {json.dumps(url)} && ({expression})"
        result = self.send("Runtime.evaluate", expression=expression, contextId=self.world, returnByValue=True,
                           awaitPromise=await_promise)
        if "exceptionDetails" in result:
            return None
        return result["result"].get("value")

    def observe(self):
        self.revision += 1
        if not (document := self.context()):
            return None
        state = self.evaluate(self.observer, document[-1])
        if not state or not (state["text"] or state["visual"]):
            return None
        if self.allowed_domains:
            state["page"] = urlsplit(document[-1])._replace(query="", fragment="").geturl()
            state["document"] = self.document_revision
        return dict(state, revision=self.revision)

    def paint(self):
        if not (document := self.context()):
            return False
        return self.evaluate("new Promise(resolve => requestAnimationFrame(() => "
                             "requestAnimationFrame(() => resolve(document.hasFocus()))))",
                             document[-1], await_promise=True) is True

    def click(self, revision, target):
        if type(revision) is not int or not isinstance(target, str) or len(target) > (180 if self.allowed_domains else 8):
            return False
        document = self.document
        if not document or self.context() != document or revision != self.revision:
            return False
        point = self.evaluate(f"pcuResolve({json.dumps(target)})", document[-1])
        self.revision += 1
        if not point or self.deadline - time.monotonic() < 0.065:
            return False
        # Queue the full click together so a stalled acknowledgement never leaves a held button.
        data = b"".join(self.packet("Input.dispatchMouseEvent", False, {"type": event, "x": point[0], "y": point[1],
                                                                  "button": "left", "clickCount": 1})
                        for event in ("mousePressed", "mouseReleased"))
        if os.write(self.input, data) != len(data):
            raise OSError("Chromium control pipe ended early")
        for request in (self.request - 1, self.request):
            self.receive(request)
        return True

    def canvas_at(self, revision, points):
        if (type(revision) is not int or not isinstance(points, list) or not 1 <= len(points) <= 2
                or any(not isinstance(point, list) or len(point) != 2
                       or any(type(value) is not int for value in point) for point in points)):
            return False
        document = self.document
        return bool(document and self.context() == document and revision == self.revision
                    and self.evaluate(f"pcuCanvasAt({json.dumps(points)})", document[-1]) is True)

    def close(self):
        os.close(self.input)
        os.close(self.output)
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


def main():
    url, allowed_domains, *command = sys.argv[1:]
    driver = Chrome(command, url, json.loads(allowed_domains))
    source = sys.stdin.fileno()
    pending = bytearray()
    try:
        while driver.process.poll() is None:
            end = pending.find(b"\n")
            if end < 0:
                if select.select([source], [], [], 0.25)[0]:
                    block = os.read(source, 4096)
                    if not block or len(pending) + len(block) > 4096:
                        break
                    pending.extend(block)
                continue
            raw = pending[:end]
            del pending[:end + 1]
            request_id = None
            try:
                request_id, operation, deadline, *arguments = json.loads(raw)
                driver.deadline = min(deadline, time.monotonic() + 0.73)
                if operation == "observe" and not arguments:
                    result = driver.observe()
                elif operation == "paint" and not arguments:
                    result = driver.paint()
                elif operation == "click" and len(arguments) == 2:
                    result = driver.click(*arguments)
                elif operation == "canvas-at" and len(arguments) == 2:
                    result = driver.canvas_at(*arguments)
                else:
                    result = None
            except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                result = None
            sys.stdout.write(json.dumps([request_id, result], separators=(",", ":")) + "\n")
            sys.stdout.flush()
    finally:
        driver.close()


if __name__ == "__main__":
    main()
