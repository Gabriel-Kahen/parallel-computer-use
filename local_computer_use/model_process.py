"""Supervise an optional dedicated llama-server for this app."""

import http.client
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4


class ManagedModel:
    def __init__(self, executable, model_file, projector, *, slots, context_per_slot, gpu_layers, threads,
                 gpu_device=None, image_min_tokens=None):
        self.executable = shutil.which(executable)
        if not self.executable:
            raise ValueError("Install llama-server or specify its path with --llama-server.")
        self.model_file = self._weight_path(model_file)
        self.projector = self._weight_path(projector)
        self.gpu_layers = gpu_layers
        self.gpu_device = gpu_device
        if gpu_layers and gpu_device is None and os.environ.get("CUDA_VISIBLE_DEVICES") in ("", "-1"):
            raise ValueError("CUDA is hidden in this environment. Use --gpu-device for CUDA or --gpu-layers 0 for CPU.")
        self.alias = f"lcu-{uuid4().hex[:12]}"
        self.api_key = secrets.token_urlsafe(32)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.port = reservation.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}/v1"
        self.options = ["--parallel", str(slots), "--ctx-size", str(slots * context_per_slot),
                        "--n-gpu-layers", str(gpu_layers), "--threads", str(threads),
                        "--no-webui", "--sleep-idle-seconds", "120"]
        if gpu_layers == 0:
            self.options.append("--no-mmproj-offload")
        if image_min_tokens:
            self.options += ["--image-min-tokens", str(image_min_tokens)]
        self.process = self.lifeline = self.log = self.key_file = None

    @staticmethod
    def _weight_path(path):
        if path is None:
            raise ValueError("A managed vision model requires its matching --mmproj file.")
        source = Path(path).expanduser().resolve()
        if not source.is_file() or source.stat().st_size < 16:
            raise ValueError(f"Model artifact is unavailable: {source.name}")
        with source.open("rb") as stream:
            if stream.read(4) != b"GGUF":
                raise ValueError(f"Expected a GGUF file: {source.name}")
        return source

    def _get(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=0.4)
        try:
            connection.request("GET", path, headers={"Authorization": f"Bearer {self.api_key}"})
            with connection.getresponse() as response:
                return json.loads(response.read(128 * 1024)) if response.status == 200 else None
        except (OSError, ValueError, http.client.HTTPException):
            return None
        finally:
            connection.close()

    def __enter__(self):
        self.log = tempfile.TemporaryFile()
        self.key_file = tempfile.TemporaryFile()
        self.key_file.write((self.api_key + "\n").encode("ascii"))
        self.key_file.seek(0)
        key_fd = self.key_file.fileno()
        lock_reader, self.lifeline = os.pipe()
        wrapper = Path(__file__).with_name("_spawn.py")
        arguments = [sys.executable, "-I", str(wrapper), "guard", str(lock_reader), str(key_fd), "-", "-",
                     self.executable, "--model", str(self.model_file), "--mmproj", str(self.projector),
                     "--alias", self.alias, "--host", "127.0.0.1", "--port", str(self.port),
                     "--api-key-file", f"/proc/self/fd/{key_fd}", *self.options]
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("LCU_", "LLAMA_ARG_")) and key not in {
                           "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
                           "ANTHROPIC_API_KEY", "HF_TOKEN",
                       }}
        if self.gpu_device is not None:
            environment["CUDA_VISIBLE_DEVICES"] = str(self.gpu_device)
        try:
            try:
                self.process = subprocess.Popen(arguments, pass_fds=(lock_reader, key_fd), start_new_session=True,
                                                stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log,
                                                env=environment)
            finally:
                os.close(lock_reader)
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline and self.process.poll() is None:
                health = self._get("/health")
                if health and health.get("status") == "ok":
                    visible = self._get("/v1/models") or {}
                    if any(model.get("id") == self.alias for model in visible.get("data", [])):
                        if self.gpu_device is not None and self.gpu_layers and "failed to initialize CUDA" in self._log_tail(32000):
                            raise ValueError("Managed llama-server could not initialize the selected CUDA device.")
                        return self
                time.sleep(0.2)
            raise ValueError(f"Managed llama-server did not become ready.\n{self._log_tail(1800)}")
        except BaseException:
            self.__exit__()
            raise

    def _log_tail(self, size):
        fd = self.log.fileno()
        offset = max(0, os.fstat(fd).st_size - size)
        return os.pread(fd, size, offset).decode("utf-8", errors="replace").strip()

    def __exit__(self, *_args):
        if self.lifeline is not None:
            os.close(self.lifeline)
            self.lifeline = None
        if self.process is not None:
            try:
                self.process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait(timeout=2)
        if self.log is not None:
            self.log.close()
            self.log = None
        if self.key_file is not None:
            self.key_file.close()
            self.key_file = None
