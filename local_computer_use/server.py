"""Loopback-only application transport."""

import ipaddress
import json
import logging
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .orchestrator import RequestError

STATIC = Path(__file__).with_name("static")
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
IDENTIFIER = r"([0-9a-f]{32})"
FRAME = re.compile(rf"/api/frame/{IDENTIFIER}")
JOB_COMMAND = re.compile(rf"/api/jobs/{IDENTIFIER}/command")
RUN_COMMAND = re.compile(rf"/api/runs/{IDENTIFIER}/(message|cancel)")
ARTIFACT = re.compile(rf"/api/artifacts/{IDENTIFIER}/([A-Za-z0-9][A-Za-z0-9_.-]{{0,79}})")
MAX_BODY = 512 * 1024
AUTH_TTL = 300
SESSION_TTL = 7 * 24 * 3600
LOG = logging.getLogger(__name__)


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 24

    def __init__(self, orchestrator, port):
        self.app = orchestrator
        self.launch_key = secrets.token_urlsafe(32)
        self.launch_expiry = time.monotonic() + AUTH_TTL
        self.browser_key = secrets.token_urlsafe(32)
        self.browser_expiry = 0.0
        self.auth_lock = threading.Lock()
        self.requests = threading.BoundedSemaphore(24)
        super().__init__(("127.0.0.1", port), Handler)
        self.host_header = f"127.0.0.1:{self.server_port}"
        self.origin = f"http://{self.host_header}"

    def process_request(self, request, client_address):
        if not self.requests.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.requests.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.requests.release()

    def new_launch_url(self):
        with self.auth_lock:
            self.launch_key = secrets.token_urlsafe(32)
            self.launch_expiry = time.monotonic() + AUTH_TTL
            return f"{self.origin}/#{self.launch_key}"

    def redeem(self, candidate):
        with self.auth_lock:
            if (not isinstance(candidate, str) or not candidate.isascii()
                    or len(candidate) > 80 or time.monotonic() >= self.launch_expiry
                    or not secrets.compare_digest(candidate, self.launch_key)):
                raise RequestError("The launch link expired. Open the app again from its launcher.", 401)
            self.launch_key = ""
            self.launch_expiry = 0.0
            self.browser_key = secrets.token_urlsafe(32)
            self.browser_expiry = time.monotonic() + SESSION_TTL
            return self.browser_key

    def authenticated(self, key):
        with self.auth_lock:
            return (key.isascii() and time.monotonic() < self.browser_expiry
                    and secrets.compare_digest(key, self.browser_key))


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalComputerUse"
    sys_version = ""

    def setup(self):
        self.request.settimeout(4.0)
        super().setup()

    def log_message(self, *_args):
        # URLs, launch credentials, prompts, and screenshots stay out of access logs.
        return

    def _bytes(self, status, body=b"", content_type="application/json; charset=utf-8", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; "
                         "base-uri 'none'; form-action 'self'")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, status, value, headers=None):
        self._bytes(status, json.dumps(value, separators=(",", ":")).encode(), headers=headers)

    def _error(self, error):
        self._json(error.status, {"error": str(error)})

    def _host(self):
        if (self.headers.get_all("Host", []) != [self.server.host_header]
                or not ipaddress.ip_address(self.client_address[0]).is_loopback):
            raise RequestError("Open the app through its loopback address.", 403)
        target = urlsplit(self.path)
        if target.netloc or target.scheme or not target.path.startswith("/"):
            raise RequestError("Invalid request target.")
        return target.path

    def _authorized(self):
        if (self.headers.get("Origin") not in (None, self.server.origin)
                or self.headers.get("Sec-Fetch-Site") not in (None, "same-origin")):
            raise RequestError("This request must come from the local app.", 403)
        headers = self.headers.get_all("Authorization", [])
        key = re.fullmatch(r"Bearer ([A-Za-z0-9_-]{43})", headers[0]) if len(headers) == 1 else None
        if not key or not self.server.authenticated(key[1]):
            raise RequestError("Open the app with its launch link to sign in locally.", 401)

    def _body(self):
        if self.headers.get_all("Origin", []) != [self.server.origin]:
            raise RequestError("This action requires the app's local origin.", 403)
        if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin"):
            raise RequestError("This action must come from the local app.", 403)
        if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
            raise RequestError("Send a JSON request body.", 415)
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,8}", lengths[0]):
            raise RequestError("Invalid request length.")
        length = int(lengths[0])
        if length > MAX_BODY:
            raise RequestError("Request exceeds the size limit.", 413)
        try:
            data = json.loads(self.rfile.read(length))
        except (ValueError, RecursionError, UnicodeError) as error:
            raise RequestError("Invalid JSON request.") from error
        if not isinstance(data, dict):
            raise RequestError("The request must be a JSON object.")
        return data

    def do_GET(self):
        self._handle(self._get)

    def do_POST(self):
        self._handle(self._post)

    def _handle(self, dispatch):
        try:
            dispatch()
        except RequestError as error:
            self._error(error)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except Exception:
            LOG.exception("Local request failed.")
            self._json(500, {"error": "The local service could not complete the request."})

    def _get(self):
        path = self._host()
        if path in ASSETS:
            file, content_type = ASSETS[path]
            self._bytes(200, (STATIC / file).read_bytes(), content_type)
            return
        self._authorized()
        if path == "/api/state":
            incoming = self.headers.get("If-None-Match", "")
            matched = re.fullmatch(r'"state-([0-9]{1,16})"', incoming)
            revision, data = self.server.app.state(int(matched[1]) if matched else None)
            headers = {"ETag": f'"state-{revision}"'}
            if data is None:
                self._bytes(304, headers=headers)
            else:
                self._json(200, data, headers=headers)
            return
        if frame := FRAME.fullmatch(path):
            job_id = frame[1]
            payload, sequence, private = self.server.app.frame(job_id, self.headers.get("X-LCU-Privacy"))
            flags = {"X-LCU-Private": "1"} if private else {}
            if payload is None:
                self._bytes(204, headers=flags)
                return
            tag = f'"{job_id}-{sequence}"'
            headers = {**flags, "ETag": tag, "X-Frame-Sequence": str(sequence)}
            if self.headers.get("If-None-Match") == tag:
                self._bytes(304, headers=headers)
            else:
                self._bytes(200, payload, "image/png", headers)
            return
        if artifact := ARTIFACT.fullmatch(path):
            data = self.server.app.artifact(artifact[1], artifact[2])
            self._bytes(200, data, "application/octet-stream",
                        {"Content-Disposition": f'attachment; filename="{artifact[2]}"'})
            return
        raise RequestError("Page was not found.", 404)

    def _post(self):
        path = self._host()
        if path == "/api/auth":
            key = self.server.redeem(self._body().get("token"))
            self._json(200, {"authenticated": True, "token": key})
            return
        self._authorized()
        body = self._body()
        app = self.server.app
        if path == "/api/runs":
            self._json(201, {"id": app.start_run(body)})
        elif command := JOB_COMMAND.fullmatch(path):
            self._json(200, app.command(command[1], body))
        elif command := RUN_COMMAND.fullmatch(path):
            if command[2] == "message":
                message_id = app.send(command[1], body.get("target"), body.get("text"))
                self._json(201, {"id": message_id})
            else:
                app.cancel_run(command[1])
                self._json(200, {"ok": True})
        elif path == "/api/stop":
            app.stop_all()
            self._json(200, {"ok": True})
        else:
            raise RequestError("Page was not found.", 404)
