"""Keep a Linux process group tied to the desktop broker's lifeline pipe."""

import os
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path


def stop_group(signum, _frame):
    signal.signal(signum, signal.SIG_IGN)
    os.killpg(os.getpgrp(), signum)


def guarded(lifeline, inherited, cleanup, custody, command):
    signal.signal(signal.SIGTERM, stop_group)
    if custody != "-":
        os.set_inheritable(int(custody), False)
    child = subprocess.Popen(command, pass_fds=inherited)
    for fd in inherited:
        os.close(fd)
    broker_closed = armed_cleanup = False
    while child.poll() is None:
        if select.select([lifeline], [], [], 0.2)[0]:
            update = os.read(lifeline, 16)
            if not update:
                broker_closed = True
                break
            armed_cleanup = armed_cleanup or b"1" in update
    stop_group(signal.SIGTERM, None)
    try:
        return child.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    finally:
        if broker_closed and armed_cleanup and cleanup != "-":
            shutil.rmtree(cleanup, ignore_errors=True)
    os.killpg(os.getpgrp(), signal.SIGKILL)


def wait_for_manager(manager):
    runtime = Path(os.environ["XDG_RUNTIME_DIR"])
    deadline = time.monotonic() + 3
    while manager.poll() is None and time.monotonic() < deadline:
        for path in runtime.glob("i3/ipc-socket.*"):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(0.1)
                try:
                    client.connect(str(path))
                    client.sendall(b"i3-ipc" + struct.pack("=II", 0, 7))
                    if client.recv(14).startswith(b"i3-ipc"):
                        return
                except OSError:
                    pass
        time.sleep(0.025)
    raise RuntimeError("Private X11 window manager did not start")


def start_network_bridge(port):
    bridge = subprocess.Popen([sys.executable, "-I", "/opt/lcu/_network.py", "bridge"])
    deadline = time.monotonic() + 2
    while bridge.poll() is None and time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=0.1):
                return bridge
        except OSError:
            time.sleep(0.025)
    bridge.terminate()
    raise RuntimeError("Private HTTPS proxy did not start")


def main():
    operation, first, *command = sys.argv[1:]
    if operation == "guard":
        fd, cleanup, custody, *command = command
        return guarded(int(first), () if fd == "-" else (int(fd),), cleanup, custody, command)
    if operation == "guest":
        network_port, *command = command
        bridge = start_network_bridge(network_port) if network_port != "-" else None
        manager = subprocess.Popen([first, "-c", os.path.join(os.environ["HOME"], "i3.conf")])
        try:
            wait_for_manager(manager)
            return subprocess.call(command)
        finally:
            manager.terminate()
            if bridge is not None:
                bridge.terminate()
            manager.wait(timeout=2)
    raise ValueError("Unknown desktop launcher operation")


if __name__ == "__main__":
    sys.exit(main())
