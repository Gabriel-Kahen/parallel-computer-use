"""Guest-only semantic driver for the offline Chromium experiment."""

import fcntl
import json
import os
import select
import subprocess
import sys
import time

# Runs in an isolated world. Only the current view's visible controls receive refs.
_OBSERVE = r"""(() => {
  const clean = value => value.replace(/\s+/g, ' ').trim().slice(0, 160);
  const position = element => {
    if (!element.isConnected || element.closest('[aria-hidden="true"], [inert]') ||
        !element.checkVisibility({opacityProperty:true, visibilityProperty:true, contentVisibilityAuto:true})) return null;
    const rect = element.getBoundingClientRect();
    const left = Math.max(0, rect.left), right = Math.min(innerWidth, rect.right);
    const top = Math.max(0, rect.top), bottom = Math.min(innerHeight, rect.bottom);
    if (right - left < 1 || bottom - top < 1) return null;
    const x = (left + right) / 2, y = (top + bottom) / 2;
    return element.contains(document.elementFromPoint(x, y)) ? [x, y] : null;
  };
  const describe = element => {
    const label = clean(element.getAttribute('aria-label') || element.labels?.[0]?.innerText ||
      element.getAttribute('title') || (element instanceof HTMLInputElement ? element.placeholder : element.innerText) || '');
    const section = element.closest('section,fieldset,article')?.querySelector('h1,h2,h3,legend');
    return [element.localName, label, clean(section?.innerText || ''), element.getAttribute('href'), element.type || ''];
  };
  const targets = new Map(), lines = [];
  let cropCanvas = null;
  globalThis.pcuCanvasAt = points => !!cropCanvas && cropCanvas.isConnected && document.hasFocus() &&
    document.visibilityState === 'visible' && points.every(([x, y]) => cropCanvas.contains(document.elementFromPoint(x, y)));
  globalThis.pcuResolve = ref => {
    const item = targets.get(ref);
    if (!document.hasFocus() || document.visibilityState !== 'visible' || !item ||
        item.element.matches(':disabled,[aria-disabled="true"]') ||
        JSON.stringify(describe(item.element)) !== item.signature) return null;
    return position(item.element);
  };
  if (!document.hasFocus() || document.visibilityState !== 'visible' || !document.body) return null;
  let visual = false, status = '';
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
  const state = {text:lines.join('\n'), count:targets.size, visual, status};
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
})()"""


class Chrome:
    def __init__(self, command, url):
        self.url = url
        self.request = 0
        self.pending = bytearray()
        self.responses = {}
        self.pipe_intact = True
        self.session = self.document = self.world = None
        self.revision = 0
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

    def context(self):
        pages = [page for page in self.send("Target.getTargets", browser=True)["targetInfos"] if page["type"] == "page"]
        if len(pages) != 1 or pages[0]["url"] != self.url:
            return None
        target = pages[0]["targetId"]
        if self.document is None or target != self.document[0]:
            self.session = self.send("Target.attachToTarget", browser=True, targetId=target, flatten=True)["sessionId"]
        frame = self.send("Page.getFrameTree")["frameTree"]["frame"]
        if frame["url"] != self.url:
            return None
        document = target, frame["id"], frame["loaderId"]
        if document != self.document:
            self.world = self.send("Page.createIsolatedWorld", frameId=frame["id"], worldName="pcu-observer")["executionContextId"]
            self.document = document
            self.revision += 1
        return document

    def evaluate(self, expression):
        result = self.send("Runtime.evaluate", expression=expression, contextId=self.world, returnByValue=True)
        if "exceptionDetails" in result:
            return None
        return result["result"].get("value")

    def observe(self):
        self.revision += 1
        if not self.context():
            return None
        state = self.evaluate(_OBSERVE)
        if not state or not (state["text"] or state["visual"]):
            return None
        return dict(state, revision=self.revision)

    def click(self, revision, target):
        if type(revision) is not int or not isinstance(target, str) or len(target) > 8:
            return False
        document = self.document
        if not document or self.context() != document or revision != self.revision:
            return False
        point = self.evaluate(f"pcuResolve({json.dumps(target)})")
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
                    and self.evaluate(f"pcuCanvasAt({json.dumps(points)})") is True)

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
    url, *command = sys.argv[1:]
    driver = Chrome(command, url)
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
