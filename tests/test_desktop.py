"""Opt-in native display, sandbox, and broker shutdown checks."""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from local_computer_use.desktop import (
    DesktopError, DesktopSession, _auth_record, _environment, _X11, validate_native_argv,
)
from local_computer_use._profiles import BrowserProfile, ProfileError


class NativeTemplatePolicyTest(unittest.TestCase):
    def test_arguments_require_a_system_executable_and_native_sandbox(self):
        if Path("/usr/bin/true").is_file():
            self.assertEqual(validate_native_argv(["/usr/bin/true", "--version"]),
                             ("/usr/bin/true", "--version"))
        for argv in ("usr/bin/true", ("true",), ("/tmp/true",), ("/usr/bin/../bin/true",),
                     ("/usr/bin/true", "bad\x00argument")):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                validate_native_argv(argv)
        with self.assertRaises(ValueError):
            DesktopSession("native", mode="host-private", native_argv=("/usr/bin/true",))
        with self.assertRaises(ValueError):
            DesktopSession("editor", native_argv=("/usr/bin/true",))


class BrowserProfilePolicyTest(unittest.TestCase):
    def test_profile_requires_exclusive_app_owned_data_and_a_consistent_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "named"
            profile = BrowserProfile(root)
            try:
                profile.set_engine("chromium")
                with self.assertRaisesRegex(ProfileError, "already in use"):
                    BrowserProfile(root)
            finally:
                profile.close()
            self.assertTrue(profile.data.is_dir())
            profile = BrowserProfile(root)
            try:
                with self.assertRaisesRegex(ProfileError, "different browser engine"):
                    profile.set_engine("firefox")
            finally:
                profile.close()
            host_profile = Path(directory) / "existing-browser-config"
            host_profile.mkdir(mode=0o700)
            (host_profile / "Default").mkdir(mode=0o700)
            with self.assertRaisesRegex(ProfileError, "unrelated files"):
                BrowserProfile(host_profile)


@unittest.skipUnless(os.environ.get("LCU_TEST_DESKTOP") == "1", "opt in with LCU_TEST_DESKTOP=1")
class DesktopBoundaryTest(unittest.TestCase):
    def wait_until(self, condition, timeout=5):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if condition():
                return
            time.sleep(0.05)
        self.fail("Private desktop did not reach the expected state")

    def test_parallel_credentials_pointer_and_unicode(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(DesktopSession, width=640, height=480) for _ in range(4)]
            sessions = []
            try:
                for future in futures:
                    sessions.append(future.result())
                left, right, *_ = sessions
                self.assertEqual(len({session.display for session in sessions}), 4)
                original = right.observation()
                left.apply({"type": "move", "x": 64, "y": 83})
                self.assertEqual((left.observation()["x"], left.observation()["y"]), (64, 83))
                self.assertEqual(right.observation(), original)
                with tempfile.NamedTemporaryFile() as wrong_authority:
                    wrong_authority.write(_auth_record(os.urandom(16), left.display[1:].encode()))
                    wrong_authority.flush()
                    for authority in (wrong_authority.name, str(right.authority), "/dev/null"):
                        bad_env = {**left._env, "XAUTHORITY": authority, "DISPLAY": right.display}
                        with self.subTest(authority=authority):
                            result = subprocess.run(
                                [sys.executable, "-I", str(_X11), "observe", "unix/" + left.display],
                                env=bad_env, capture_output=True, timeout=3,
                            )
                            self.assertNotEqual(result.returncode, 0)
                left.apply({"type": "type", "text": "Café 🍎"})
                left.apply({"type": "key", "key": "Ctrl+O"})
                left.apply({"type": "key", "key": "Enter"})
                self.wait_until(lambda: (left.workspace / "notes.txt").stat().st_size > 0)
                self.assertEqual((left.workspace / "notes.txt").read_text(), "Café 🍎\n")
                self.assertEqual((right.workspace / "notes.txt").read_bytes(), b"")
                self.assertEqual(right.observation()["clipboard_owner"], 0)
            finally:
                for future in futures:
                    try:
                        session = future.result()
                    except Exception:
                        continue
                    session.close()

    def test_guest_cannot_reach_host_files_display_or_loopback(self):
        with (DesktopSession(width=640, height=480) as guest,
              DesktopSession(width=640, height=480) as neighbor,
              socket.socket() as host_service, tempfile.NamedTemporaryFile() as host_file):
            host_service.bind(("127.0.0.1", 0))
            host_service.listen()
            guest.open_artifact("allowed.txt", b"fixture")
            with self.assertRaises(ValueError):
                guest.open_artifact("../escaped.txt", b"fixture")
            env = _environment(Path("/home/worker"), Path("/work"))
            env.update(DISPLAY=guest.display, XAUTHORITY="/run/lcu.auth")
            source = """
import json, os, pathlib, socket, sys
port, host_file = sys.argv[1:]
try:
    socket.create_connection(("127.0.0.1", int(port)), timeout=.5).close()
    host_service = True
except OSError:
    host_service = False
print(json.dumps({
    "host_service": host_service,
    "host_file": pathlib.Path(host_file).exists(),
    "x11": sorted(os.listdir("/tmp/.X11-unix")),
    "home": os.environ["HOME"],
    "bus": os.environ["DBUS_SESSION_BUS_ADDRESS"],
    "wayland": "WAYLAND_DISPLAY" in os.environ,
    "allowed": pathlib.Path("/work/allowed.txt").read_text(),
    "network": os.readlink("/proc/self/ns/net"),
    "pid": os.readlink("/proc/self/ns/pid"),
}))
"""
            result = subprocess.run(guest._sandbox(None) + [
                "/usr/bin/dbus-run-session", "--config-file=/home/worker/dbus.conf", "--",
                "/usr/bin/python3", "-I", "-c", source, str(host_service.getsockname()[1]),
                host_file.name,
            ], env=env, capture_output=True, check=True, timeout=5)
            state = json.loads(result.stdout)
            self.assertFalse(state["host_service"])
            self.assertFalse(state["host_file"])
            self.assertFalse(state["wayland"])
            self.assertEqual(state["home"], "/home/worker")
            self.assertTrue(state["bus"].startswith("unix:"))
            self.assertNotEqual(state["bus"], os.environ.get("DBUS_SESSION_BUS_ADDRESS"))
            self.assertEqual(state["x11"], ["X" + guest.display[1:]])
            self.assertNotIn("X" + neighbor.display[1:], state["x11"])
            self.assertEqual(state["allowed"], "fixture")
            self.assertNotEqual(state["network"], os.readlink("/proc/self/ns/net"))
            self.assertNotEqual(state["pid"], os.readlink("/proc/self/ns/pid"))

    def test_staged_import_is_read_only_and_a_symlink_mount_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            imports = workspace / "imports"
            imports.mkdir(mode=0o700)
            copied = imports / "source.txt"
            copied.write_text("staged fixture")
            with DesktopSession(workspace=workspace, width=640, height=480) as guest:
                source = """
from pathlib import Path
staged = Path("/work/imports/source.txt")
assert staged.read_text() == "staged fixture"
try:
    staged.write_text("modified")
except OSError:
    pass
else:
    raise AssertionError("import was writable")
Path("/work/output.txt").write_text("workspace is still writable")
"""
                subprocess.run(guest._sandbox(None) + ["/usr/bin/python3", "-I", "-c", source],
                               env=guest._env, capture_output=True, check=True, timeout=5)
                self.assertEqual(copied.read_text(), "staged fixture")
                self.assertTrue((workspace / "output.txt").is_file())
            imports.rename(workspace / "not-imports")
            imports.symlink_to(workspace / "not-imports", target_is_directory=True)
            with self.assertRaisesRegex(DesktopError, "imports must be a user-owned directory"):
                DesktopSession(workspace=workspace, width=640, height=480)

    @unittest.skipUnless(Path("/usr/bin/gnome-calculator").is_file() and shutil.which("xclip"),
                         "GNOME Calculator and xclip are needed for the native template fixture")
    def test_operator_native_program_renders_and_calculates_in_private_session(self):
        with DesktopSession("native", native_argv=("/usr/bin/gnome-calculator",),
                            width=640, height=480) as guest:
            initial = guest.png()
            guest.apply({"type": "type", "text": "6*7"})
            self.wait_until(lambda: guest.png() != initial)
            expression = guest.png()
            guest.apply({"type": "key", "key": "Enter"})
            self.wait_until(lambda: guest.png() != expression)
            guest.apply({"type": "key", "key": "Ctrl+A"})
            guest.apply({"type": "key", "key": "Ctrl+C"})
            self.wait_until(lambda: guest.observation()["clipboard_owner"] != 0)
            result = subprocess.run(["/usr/bin/xclip", "-selection", "clipboard", "-o"],
                                    env=guest._env, capture_output=True, check=True, timeout=2)
            self.assertEqual(result.stdout, b"42")
            self.assertTrue(guest.png().startswith(b"\x89PNG"))

    def test_sigkill_broker_reaps_display_and_private_runtime(self):
        broker = subprocess.Popen(
            [sys.executable, "-c", """
import json
import time
from local_computer_use.desktop import DesktopSession
desktop = DesktopSession(width=640, height=480)
print(json.dumps({"display": desktop.display, "authority": str(desktop.authority),
                  "guards": [desktop._display_process[0].pid, desktop._app_process[0].pid,
                             desktop._x11[0].pid]}), flush=True)
time.sleep(30)
"""], stdout=subprocess.PIPE, text=True,
        )
        try:
            state = json.loads(broker.stdout.readline())
            path = Path("/tmp/.X11-unix") / ("X" + state["display"][1:])
            scratch = Path(state["authority"]).parent
            self.assertTrue(path.is_socket())
            self.assertTrue(scratch.exists())
            broker.kill()
            broker.wait(timeout=3)
            self.wait_until(lambda: not path.exists() and not scratch.exists())
            def guard_stopped(pid):
                try:
                    return (Path("/proc") / str(pid) / "stat").read_text().split(") ", 1)[1][0] == "Z"
                except FileNotFoundError:
                    return True
            self.wait_until(lambda: all(guard_stopped(pid) for pid in state["guards"]))
        finally:
            broker.kill()
            broker.wait(timeout=3)
            broker.stdout.close()
