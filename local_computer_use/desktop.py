"""Authenticated offscreen X11 sessions and a rootless offline application sandbox."""

import json
import os
import re
import secrets
import select
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from ._network import (
    GUEST_PROXY_PORT, GUEST_PROXY_SOCKET, NetworkProxy, normalize_domains, validate_network_target,
)
from ._profiles import BrowserProfile, ProfileError


TEMPLATES = ("editor", "browser", "native")
MODES = ("sandbox", "host-private")
_PATH = "/usr/bin:/bin"
_SPAWN = Path(__file__).with_name("_spawn.py").resolve()
_X11 = Path(__file__).with_name("_x11.py").resolve()
_NETWORK = Path(__file__).with_name("_network.py").resolve()
_CHROME = Path("/opt/google/chrome/chrome")
_EDITOR_FOREGROUND = "#171717"
_I3_CONFIG = """font pango:monospace 9
focus_follows_mouse no
default_border pixel 2
workspace_layout default
bindsym Mod1+F4 kill
"""
_DBUS_CONFIG = """<busconfig>
  <type>session</type>
  <listen>unix:tmpdir=/tmp</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow own="*"/>
    <allow send_destination="*"/>
    <allow receive_sender="*"/>
    <allow eavesdrop="true"/>
  </policy>
</busconfig>
"""
_CATALOG = """<!doctype html>
<html lang="en"><meta charset="utf-8"><title>Opening catalog</title>
<style>
body { font: 20px system-ui; max-width: 760px; margin: 3rem auto; padding: 0 1rem; color: #18212e }
input { font: inherit; width: 100%; padding: .5rem; box-sizing: border-box }
li { padding: .5rem }
</style>
<h1>Local catalog</h1>
<label for="search">Search inventory</label>
<input id="search" type="search" autofocus>
<ul><li>Notebook · Elm · $8</li><li>Desk lamp · Iris · $24</li>
<li>Travel mug · Cedar · $16</li><li>Notebook · Cedar · $11</li></ul>
<script>
const search = document.getElementById("search");
requestAnimationFrame(() => requestAnimationFrame(() => {
  document.title = "Local catalog";
}));
search.addEventListener("input", () => {
  for (const item of document.querySelectorAll("li")) {
    item.hidden = !item.textContent.toLowerCase().includes(search.value.toLowerCase());
  }
});
</script></html>
"""


class DesktopError(RuntimeError):
    pass


def _binary(*names):
    for name in names:
        path = shutil.which(name, path=_PATH)
        if path:
            return path
    raise DesktopError(f"Install a system executable for {', '.join(names)}")


def validate_native_argv(argv):
    """Validate a trusted startup command for a sandboxed native app."""
    if (not isinstance(argv, (tuple, list)) or not 1 <= len(argv) <= 32 or
            any(not isinstance(arg, str) or not 1 <= len(arg) <= 2048 or
                not arg.isprintable() for arg in argv)):
        raise ValueError("Native app startup requires a fixed list of printable arguments")
    executable = Path(argv[0])
    if (executable.parent != Path("/usr/bin") or not executable.is_file() or
            not os.access(executable, os.X_OK) or
            not executable.resolve().is_relative_to("/usr")):
        raise ValueError("Native app startup requires an executable from /usr/bin")
    return tuple(argv)


def _environment(home, workspace):
    return {
        "PATH": _PATH,
        "HOME": str(home),
        "USER": "worker",
        "LOGNAME": "worker",
        "LANG": "C.UTF-8",
        "TERM": "xterm-256color",
        "XDG_RUNTIME_DIR": str(home / "runtime"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "XDG_DOWNLOAD_DIR": str(workspace),
        "TMPDIR": str(home / "tmp"),
        "XDG_SESSION_TYPE": "x11",
        "GDK_BACKEND": "x11",
        "GSK_RENDERER": "cairo",
        "QT_QPA_PLATFORM": "xcb",
        "MOZ_ENABLE_WAYLAND": "0",
        "LIBGL_ALWAYS_SOFTWARE": "1",
        # Disallow accidentally starting another real user bus or reaching the host bus.
        "DBUS_SYSTEM_BUS_ADDRESS": "unix:path=/run/lcu-no-system-bus",
    }


def _auth_record(cookie, display_number=b""):
    fields = (b"", display_number, b"MIT-MAGIC-COOKIE-1", cookie)
    return struct.pack(">H", 0xFFFF) + b"".join(
        struct.pack(">H", len(field)) + field for field in fields
    )


class DesktopSession:
    """Own an Xvfb display and a trusted, fixed application template.

    An explicit workspace must be a dedicated user-owned mode-0700 directory.
    It is mounted at /work in sandbox mode and survives close(). With no
    explicit workspace, the session owns a disposable workspace.

    The browser starts offline unless an allowlisted HTTPS start URL is supplied.
    Use browser_profile with an application-owned directory to preserve its data.
    Native argv is trusted host configuration and applies only to the native
    template. The native template always uses the offline sandbox.
    """

    def __init__(self, template="editor", *, width=1024, height=768, mode="sandbox",
                 workspace=None, allowed_domains=(), start_url=None, native_argv=None,
                 browser_profile=None):
        if template not in TEMPLATES:
            raise ValueError("Unknown desktop template")
        if mode not in MODES:
            raise ValueError("Unknown desktop mode")
        if (type(width) is not int or type(height) is not int or
                not 320 <= width <= 1920 or not 240 <= height <= 1080):
            raise ValueError("Viewport must be between 320×240 and 1920×1080")
        if template == "native":
            if mode != "sandbox":
                raise ValueError("Operator-configured native apps require the offline sandbox")
            native_argv = validate_native_argv(native_argv)
        elif native_argv is not None:
            raise ValueError("Native startup arguments require the native template")
        if browser_profile is not None and (template != "browser" or mode != "sandbox"):
            raise ValueError("Persistent profiles require the sandboxed browser template")
        domains = normalize_domains(allowed_domains)
        if start_url is not None:
            if template != "browser" or mode != "sandbox":
                raise ValueError("HTTPS browsing requires the sandboxed browser template")
            validate_network_target(start_url, domains)
        self.template, self.mode = template, mode
        self.start_url = start_url
        self._native_argv = native_argv
        self.width, self.height = width, height
        self._lock = threading.RLock()
        self._closed = False
        self._display_process = self._app_process = self._clipboard = None
        self._x11 = None
        self._network_proxy = None
        self._browser_profile = None
        self._workspace_fd = None
        self._temporary = tempfile.TemporaryDirectory(prefix="lcu-", dir="/tmp")
        self._root = Path(self._temporary.name)
        self.display = ""
        self.workspace = Path(workspace).absolute() if workspace is not None else self._root / "work"
        try:
            if browser_profile is not None:
                try:
                    self._browser_profile = BrowserProfile(browser_profile)
                except ProfileError as error:
                    raise DesktopError(str(error)) from error
            if workspace is None:
                self.workspace.mkdir(mode=0o700)
            self._workspace_fd = os.open(
                self.workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            metadata = os.fstat(self._workspace_fd)
            if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
                raise ValueError("Desktop workspace must be user-owned with mode 0700")
            self._home = self._root / "home"
            self._home.mkdir(mode=0o700)
            for relative in ("runtime", "tmp", ".config", ".cache", ".local/share", ".local/state"):
                (self._home / relative).mkdir(mode=0o700, parents=True)
            (self._home / "i3.conf").write_text(_I3_CONFIG, encoding="utf-8")
            (self._home / "dbus.conf").write_text(_DBUS_CONFIG, encoding="utf-8")
            (self._home / "catalog.html").write_text(_CATALOG, encoding="utf-8")
            self._auth = self._root / "authority"
            cookie = secrets.token_bytes(16)
            self._env = _environment(self._home, self.workspace)
            self._env["XAUTHORITY"] = str(self._auth)
            self._start_display(cookie)
            if self.start_url is not None:
                self._network_proxy = NetworkProxy(self._root / "proxy.sock", domains)
            self._start_application()
        except BaseException:
            try:
                self.close()
            except DesktopError:
                pass
            raise

    @property
    def alive(self):
        return (not self._closed and self._display_process is not None and
                self._app_process is not None and self._display_process[0].poll() is None and
                self._app_process[0].poll() is None)

    @property
    def authority(self):
        return self._auth

    def _guard(self, command, environment, cwd, *, inherited=None, cleanup=None, custody=None):
        reader, writer = os.pipe()
        arguments = [sys.executable, "-I", str(_SPAWN), "guard", str(reader),
                     str(inherited) if inherited is not None else "-",
                     str(cleanup) if cleanup is not None else "-",
                     str(custody) if custody is not None else "-", *map(str, command)]
        try:
            process = subprocess.Popen(
                arguments, env=environment, cwd=cwd, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                pass_fds=tuple(fd for fd in (reader, inherited, custody) if fd is not None),
                start_new_session=True,
            )
        except BaseException:
            os.close(writer)
            raise
        finally:
            os.close(reader)
        return process, writer

    def _write_authority(self, record):
        temporary = self._auth.with_suffix(".new")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(record)
        temporary.replace(self._auth)

    def _start_display(self, cookie):
        xvfb = _binary("Xvfb")
        failure = None
        for _ in range(3):
            number = str(128 + secrets.randbelow(32640))
            self._write_authority(_auth_record(cookie, number.encode("ascii")))
            self.display = ":" + number
            self._env["DISPLAY"] = self.display
            reader, writer = os.pipe()
            try:
                try:
                    self._display_process = self._guard(
                        [xvfb, self.display, "-displayfd", str(writer), "-noreset",
                         "-nolisten", "tcp", "-nopn", "-auth", self._auth,
                         "-screen", "0", f"{self.width}x{self.height}x24"],
                        self._env, self._root, inherited=writer, cleanup=self._root,
                    )
                finally:
                    os.close(writer)
                if not select.select([reader], [], [], 4)[0]:
                    raise DesktopError("Private Xvfb startup timed out")
                if os.read(reader, 16).strip() != number.encode("ascii"):
                    raise DesktopError("The selected private display is unavailable")
                state = self._observe()
                if (state["width"], state["height"]) != (self.width, self.height):
                    raise DesktopError("Xvfb did not provide the configured viewport")
                os.write(self._display_process[1], b"1")
                return
            except (DesktopError, OSError) as error:
                failure = error
                self._stop_helper()
                self._stop(self._display_process)
                self._display_process = None
            finally:
                os.close(reader)
        raise DesktopError("Could not start an authenticated private display") from failure

    def _app_command(self, home, workspace):
        if self.template == "native":
            return self._native_argv, None
        if self.template == "editor":
            filename = "notes.txt"
            try:
                self.open_artifact(filename, b"")
            except FileExistsError:
                info = os.stat(filename, dir_fd=self._workspace_fd, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise DesktopError("Editor input must be a regular, unlinked workspace file")
            return [
                _binary("xterm"), "-class", "LCUEditor", "-title", "Session editor",
                "-fa", "Monospace", "-fs", "13", "-b", "2",
                "-bg", "#f8f8f8", "-fg", _EDITOR_FOREGROUND,
                "-xrm", "LCUEditor*selectToClipboard: true",
                "-xrm", "LCUEditor*VT100.translations: #override\nShift <Key>Insert: insert-selection(CLIPBOARD)",
                "-e", _binary("nano"), "-I", "-R", workspace / filename,
            ], None
        if self.mode != "sandbox":
            raise DesktopError("Browser template requires the sandbox")
        url = self.start_url or (home / "catalog.html").as_uri()
        engine = self._browser_profile.engine if self._browser_profile is not None else None
        firefox = shutil.which("firefox", path=_PATH) or shutil.which("firefox-esr", path=_PATH)
        if engine == "chromium" or (firefox is None and engine != "firefox"):
            chromium = (
                shutil.which("chromium", path=_PATH) or shutil.which("chromium-browser", path=_PATH)
            )
            if chromium is None and os.access(_CHROME, os.X_OK):
                chromium = str(_CHROME)
            if chromium is None:
                raise DesktopError("Install the required Firefox or Chromium browser")
            self._profile_engine("chromium")
            browser_files = _CHROME.parent if chromium == str(_CHROME) else None
            arguments = [
                chromium, f"--user-data-dir={home / 'browser'}", "--no-first-run",
                "--disable-background-networking", "--disable-sync", "--disable-gpu",
                "--ozone-platform=x11",
            ]
            if self._network_proxy is None:
                arguments += ["--app=" + url]
            else:
                arguments += [
                    f"--proxy-server=http://127.0.0.1:{GUEST_PROXY_PORT}",
                    "--proxy-bypass-list=<-loopback>", "--disable-http2", "--disable-quic",
                    "--disable-features=EncryptedClientHello", "--start-maximized", url,
                ]
            return arguments, browser_files
        if firefox is None:
            raise DesktopError("This browser profile requires Firefox")
        self._profile_engine("firefox")
        settings = {"network.proxy.type": 0}
        if self._network_proxy is not None:
            settings.update({
                "network.proxy.type": 1,
                "network.proxy.ssl": "127.0.0.1",
                "network.proxy.ssl_port": GUEST_PROXY_PORT,
                "network.proxy.no_proxies_on": "",
                "network.proxy.failover_direct": False,
                "network.proxy.allow_hijacking_localhost": True,
                "network.dns.disablePrefetch": True,
                "network.dns.echconfig.enabled": False,
                "security.tls.ech.grease_probability": 0,
                "network.http.http2.enabled": False,
                "network.http.http3.enable": False,
            })
        profile = self._browser_profile.data if self._browser_profile is not None else self._home / "browser"
        self._write_firefox_settings(profile, settings)
        return [
            firefox, "--no-remote", "--profile", home / "browser", url,
        ], None

    def _profile_engine(self, name):
        if self._browser_profile is not None:
            try:
                self._browser_profile.set_engine(name)
            except (ProfileError, OSError) as error:
                raise DesktopError("Could not use the persistent profile with this browser") from error

    @staticmethod
    def _write_firefox_settings(profile, settings):
        fd, staged = tempfile.mkstemp(prefix=".lcu-settings-", dir=profile)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as file:
                for key, value in settings.items():
                    file.write(f"user_pref({json.dumps(key)}, {json.dumps(value)});\n")
            os.replace(staged, profile / "user.js")
        finally:
            Path(staged).unlink(missing_ok=True)

    def _start_application(self):
        sandbox = self.mode == "sandbox"
        home = Path("/home/worker") if sandbox else self._home
        workspace = Path("/work") if sandbox else self.workspace
        if self.template == "browser":
            (self._home / "browser").mkdir(mode=0o700)
        app, browser_files = self._app_command(home, workspace)
        network_port = str(GUEST_PROXY_PORT) if self._network_proxy is not None else "-"
        config = home / ".config/user-dirs.dirs"
        (self._home / config.relative_to(home)).write_text(
            f'XDG_DOWNLOAD_DIR="{workspace}"\n', encoding="utf-8",
        )
        bus = [_binary("dbus-run-session"), f"--config-file={home / 'dbus.conf'}",
               "--", _binary("python3"), "-I"]
        wm = _binary("i3")
        environment = _environment(home, workspace)
        environment.update(DISPLAY=self.display, XAUTHORITY=str(self._auth))
        if sandbox:
            environment["XAUTHORITY"] = "/run/lcu.auth"
            command = self._sandbox(browser_files) + [
                *bus, "/opt/lcu/_spawn.py", "guest", wm, network_port, *app
            ]
        else:
            command = [*bus, _SPAWN, "guest", wm, network_port, *app]
        custody = self._browser_profile.lock if self._browser_profile is not None else None
        self._app_process = self._guard(command, environment, self._root if sandbox else workspace,
                                        custody=custody)
        deadline = time.monotonic() + (12 if self.template == "browser" else 5)
        while time.monotonic() < deadline and self.alive:
            observation = self._observe()
            if observation["active_window"] and observation["focus"] not in (0, 1, observation["root"]):
                if self.template == "editor":
                    if self._helper("editor-ready", int(_EDITOR_FOREGROUND[1:], 16)) == b"1":
                        return
                elif (self.template != "browser" or self.start_url is not None or
                      self._helper("catalog-ready") == b"1"):
                    return
            time.sleep(0.04)
        raise DesktopError(f"{self.template} did not become ready in {self.mode} mode")

    def _sandbox(self, browser_files):
        command = [
            _binary("bwrap"), "--unshare-user", "--unshare-pid", "--unshare-net",
            "--unshare-ipc", "--unshare-uts", "--cap-drop", "ALL", "--die-with-parent",
            "--new-session", "--hostname", "lcu", "--ro-bind", "/usr", "/usr",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/run",
            "--dir", "/tmp/.X11-unix", "--dir", "/etc", "--dir", "/home", "--dir", "/opt",
            "--ro-bind", _SPAWN, "/opt/lcu/_spawn.py",
            "--bind", self._home, "/home/worker", "--bind", self.workspace, "/work",
            "--ro-bind", self._auth, "/run/lcu.auth", "--chdir", "/work",
        ]
        if self.template != "browser":
            command += ["--disable-userns"]
        try:
            imports = os.stat("imports", dir_fd=self._workspace_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISDIR(imports.st_mode) or imports.st_uid != os.getuid():
                raise DesktopError("Workspace imports must be a user-owned directory")
            command += ["--ro-bind", self.workspace / "imports", "/work/imports"]
        socket = "/tmp/.X11-unix/X" + self.display[1:]
        command += ["--ro-bind", socket, socket]
        for name in ("/bin", "/sbin", "/lib", "/lib32", "/lib64"):
            if os.path.islink(name):
                command += ["--symlink", os.readlink(name), name]
            elif os.path.isdir(name):
                command += ["--ro-bind", name, name]
        for name in ("/etc/fonts", "/etc/ld.so.cache"):
            command += ["--ro-bind-try", name, name]
        for name, content in {
            "passwd": f"worker:x:{os.getuid()}:{os.getgid()}:Desktop worker:/home/worker:/bin/false\n",
            "group": f"worker:x:{os.getgid()}:\n",
            "machine-id": secrets.token_hex(16) + "\n",
        }.items():
            path = self._root / name
            if not path.exists():
                path.write_text(content, encoding="ascii")
            command += ["--ro-bind", path, "/etc/" + name]
        if browser_files is not None:
            command += ["--ro-bind", browser_files, browser_files]
        if self._browser_profile is not None:
            command += ["--bind", self._browser_profile.data, "/home/worker/browser"]
        if self._network_proxy is not None:
            policy = self._root / "chrome-policy.json"
            policy.write_text(json.dumps({
                "EncryptedClientHelloEnabled": False,
                "DnsOverHttpsMode": "off",
                "QuicAllowed": False,
            }), encoding="ascii")
            command += [
                "--ro-bind", _NETWORK, "/opt/lcu/_network.py",
                "--ro-bind", self._network_proxy.path, GUEST_PROXY_SOCKET,
                "--ro-bind", policy, "/etc/chromium/policies/managed/lcu.json",
                "--ro-bind", policy, "/etc/opt/chrome/policies/managed/lcu.json",
            ]
        return command

    def open_artifact(self, name, content):
        """Stage a new regular file in the workspace without following symlinks."""
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name)
                or not isinstance(content, bytes)):
            raise ValueError("Expected a safe single-component filename and byte content")
        with self._lock:
            if self._closed:
                raise DesktopError("Private desktop is closed")
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                         dir_fd=self._workspace_fd)
            with os.fdopen(fd, "wb") as file:
                file.write(content)

    def _helper(self, operation, action=None):
        if self._closed or self._display_process[0].poll() is not None:
            raise DesktopError("Private display is closed")
        if self._x11 is None:
            channel, child = socket.socketpair()
            command = [sys.executable, "-I", str(_X11), "serve", "unix/" + self.display]
            try:
                with child:
                    process = subprocess.Popen(
                        command, stdin=child, stdout=child, stderr=subprocess.DEVNULL,
                        env=self._env, cwd=self._root, start_new_session=True,
                    )
            except BaseException:
                channel.close()
                raise
            self._x11 = process, channel
        _, channel = self._x11
        deadline = time.monotonic() + 3
        try:
            channel.settimeout(3)
            channel.sendall(json.dumps([operation, action]).encode("ascii") + b"\n")
            status, size = struct.unpack(">BI", self._read_helper(channel, 5, deadline))
            if status not in (0, 1) or size > 8 * 1024 * 1024:
                raise OSError("Invalid private X11 helper response")
            data = self._read_helper(channel, size, deadline)
        except TimeoutError as error:
            self._stop_helper()
            raise DesktopError("Private display did not respond") from error
        except OSError as error:
            self._stop_helper()
            raise DesktopError("Private X11 connection closed") from error
        if status:
            raise DesktopError(data.decode("utf-8", "replace"))
        return data

    @staticmethod
    def _read_helper(channel, size, deadline):
        data = bytearray(size)
        pending = memoryview(data)
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            channel.settimeout(remaining)
            received = channel.recv_into(pending)
            if not received:
                raise OSError("Private X11 helper disconnected")
            pending = pending[received:]
        return bytes(data)

    def _stop_helper(self):
        if self._x11 is None:
            return
        process, channel = self._x11
        self._x11 = None
        channel.close()
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)

    def _observe(self):
        return json.loads(self._helper("observe"))

    def observation(self):
        with self._lock:
            return self._observe()

    def png(self):
        return self.png_region(0, 0, self.width, self.height)

    def png_region(self, x, y, width, height):
        """Capture a viewport region at native resolution, as an RGB PNG."""
        if (any(type(value) is not int for value in (x, y, width, height)) or
                not (0 <= x < x + width <= self.width and 0 <= y < y + height <= self.height)):
            raise ValueError("Capture region is outside the private viewport")
        with self._lock:
            image = self._helper("png", [x, y, width, height])
            self._check_png(image, width, height)
            return image

    def png_if_changed(self, previous_digest=None):
        """Capture the full viewport, omitting PNG bytes if the pixels match the digest."""
        if previous_digest is not None and (
                not isinstance(previous_digest, bytes) or len(previous_digest) != 32):
            raise ValueError("Expected a 32-byte capture digest")
        with self._lock:
            data = self._helper("png-changed", previous_digest.hex() if previous_digest else None)
            digest, image = data[:32], data[32:]
            if not image and digest == previous_digest:
                return None, digest
            self._check_png(image, self.width, self.height)
            return image, digest

    @staticmethod
    def _check_png(image, width, height):
        if len(image) < 24 or struct.unpack_from(">II", image, 16) != (width, height):
            raise DesktopError("Private display returned an unexpected capture size")

    def _point(self, action, x="x", y="y"):
        px, py = action.get(x), action.get(y)
        if (type(px) is not int or type(py) is not int or
                not (0 <= px < self.width and 0 <= py < self.height)):
            raise ValueError("Pointer coordinates are outside the private viewport")
        return {x: px, y: py}

    def _normalize(self, action):
        if not isinstance(action, dict) or not isinstance(action.get("type"), str):
            raise ValueError("Expected a typed desktop action")
        kind = action["type"]
        request = {"type": kind}
        if kind in {"move", "click", "drag", "scroll"}:
            request.update(self._point(action))
        if kind in {"click", "drag"}:
            button = action.get("button", "left")
            if isinstance(button, str):
                button = {"left": 1, "middle": 2, "right": 3}.get(button)
            if type(button) is not int or button not in (1, 2, 3):
                raise ValueError("Unsupported pointer button")
            request["button"] = button
        if kind == "drag":
            request.update(self._point(action, "to_x", "to_y"))
        if kind == "scroll":
            for axis in ("dy", "dx"):
                delta = action.get(axis, 0)
                if type(delta) is not int or not -24 <= delta <= 24:
                    raise ValueError("Scroll steps must be between -24 and 24")
                request[axis] = delta
        if kind == "key":
            key = action.get("key")
            if not isinstance(key, str) or not 1 <= len(key) <= 64:
                raise ValueError("Invalid key")
            keys = [key] if len(key) == 1 else key.split("+")
            if len(keys) > 5 or any(not re.fullmatch(r"[ -~]|[A-Za-z][A-Za-z0-9_]*", part)
                                    for part in keys):
                raise ValueError("Invalid key combination")
            modifiers = {"ctrl": "Ctrl", "control": "Control", "alt": "Alt",
                         "shift": "Shift", "super": "Super", "meta": "Meta"}
            request["key"] = [modifiers.get(part.lower(), part) for part in keys]
        if kind == "type":
            text = action.get("text")
            if (not isinstance(text, str) or not 1 <= len(text) <= 4096 or
                    not all(character.isprintable() or character in "\n\t" for character in text)):
                raise ValueError("Text must be printable and at most 4096 characters")
            request["text"] = text
        if kind not in {"move", "click", "drag", "scroll", "key", "type"}:
            raise ValueError("Unsupported desktop action")
        return request

    def apply(self, action):
        request = self._normalize(action)
        with self._lock:
            if not self.alive:
                raise DesktopError("Private desktop application is closed")
            if request["type"] == "type" and not request["text"].isascii():
                self._paste(request["text"])
            else:
                self._helper("apply", request)

    def _paste(self, text):
        self._stop(self._clipboard)
        self._clipboard = None
        previous_owner = self._observe()["clipboard_owner"]
        with tempfile.TemporaryFile(dir=self._root) as file:
            file.write(text.encode("utf-8"))
            file.seek(0)
            fd = file.fileno()
            self._clipboard = self._guard(
                [_binary("xclip"), "-selection", "clipboard", "-loops", "1", "-quiet",
                 "-i", f"/proc/self/fd/{fd}"],
                self._env, self._root, inherited=fd,
            )
        for _ in range(10):
            owner = self._observe()["clipboard_owner"]
            if owner and owner != previous_owner:
                key = ["Shift", "Insert"] if self.template == "editor" else ["Ctrl", "v"]
                self._helper("apply", {"type": "key", "key": key})
                # XTest dispatches the paste shortcut before the target requests its selection.
                try:
                    self._clipboard[0].wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
                return
            if self._clipboard[0].poll() is not None:
                break
            time.sleep(0.02)
        raise DesktopError("Could not set the private clipboard")

    def release_held(self):
        with self._lock:
            if self.alive:
                self._helper("apply", {"type": "release"})

    @staticmethod
    def _stop(guard):
        if guard is None:
            return
        process, writer = guard
        os.close(writer)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)

    def close(self):
        with self._lock:
            if self._closed:
                return
            if self._browser_profile is not None:
                self._finish_browser()
            self._closed = True
            failure = None
            try:
                try:
                    self._stop_helper()
                except (OSError, subprocess.TimeoutExpired) as error:
                    failure = error
                if self._network_proxy is not None:
                    try:
                        self._network_proxy.close()
                    except OSError as error:
                        failure = error
                for guard in (self._clipboard, self._app_process, self._display_process):
                    try:
                        self._stop(guard)
                    except (OSError, subprocess.TimeoutExpired) as error:
                        failure = error
            finally:
                if self._browser_profile is not None:
                    self._browser_profile.close()
                if self._workspace_fd is not None:
                    os.close(self._workspace_fd)
                self._temporary.cleanup()
            if failure is not None:
                raise DesktopError("One or more private desktop processes did not shut down") from failure

    def _finish_browser(self):
        deadline = time.monotonic() + 2
        closing = 0
        try:
            while self.alive and time.monotonic() < deadline:
                active = self._observe()["active_window"]
                if active and active != closing:
                    self._helper("apply", {"type": "key", "key": ["Alt", "F4"]})
                    closing = active
                time.sleep(0.04)
        except DesktopError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
