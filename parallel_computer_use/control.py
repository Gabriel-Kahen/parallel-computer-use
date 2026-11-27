"""A private Unix socket for local launcher and CLI operations."""

import json
import os
import socket
import socketserver
import stat
import threading


class ControlSocket(socketserver.UnixStreamServer):
    def __init__(self, path, server):
        self.path = path
        self.server = server
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(mode):
                raise ValueError("Refusing to replace a non-socket control path.")
            path.unlink()
        super().__init__(str(path), ControlHandler)
        path.chmod(0o600)
        self.thread = threading.Thread(target=self.serve_forever, args=(0.15,), daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=2)
        self.path.unlink(missing_ok=True)


class ControlHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(0.8)
        try:
            command = self.rfile.readline(48).strip()
            app = self.server.server.app
            if command == b"open":
                reply = {"url": self.server.server.new_launch_url()}
            elif command == b"status":
                reply = {"running": True, **app.state()[1]["metrics"]}
            elif command == b"stop-all":
                app.stop_all()
                reply = {"ok": True}
            else:
                reply = {"error": "Command is unavailable."}
            self.wfile.write(json.dumps(reply).encode() + b"\n")
        except (OSError, TimeoutError):
            return


def send(path, command):
    metadata = path.lstat()
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise ValueError("The app control socket must be private and owned by this user.")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(3.0)
        connection.connect(str(path))
        connection.sendall(command.encode("ascii") + b"\n")
        with connection.makefile("rb") as reader:
            response = json.loads(reader.readline(8192))
    if "error" in response:
        raise ValueError(response["error"])
    return response
