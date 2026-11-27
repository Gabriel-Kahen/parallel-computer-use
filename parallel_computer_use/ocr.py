"""Read visible text from a private worker PNG with optional local Tesseract."""

import os
import selectors
import shutil
import struct
import subprocess
import time
from threading import Event


_PATH = "/usr/bin:/bin"
_PNG_IHDR = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
_MAX_PNG = 8 * 1024 * 1024
_MAX_TSV = 256 * 1024
_MAX_TEXT = 4000
_TIMEOUT = 3.0
_POLL = 0.05


def _read_tsv(executable, image, cancel):
    deadline = time.monotonic() + _TIMEOUT
    command = [executable, "stdin", "stdout", "-l", "eng", "--psm", "11", "tsv"]
    environment = {"PATH": _PATH, "LANG": "C.UTF-8", "OMP_THREAD_LIMIT": "1"}
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, env=environment) as process:
        try:
            pending = memoryview(image)
            output = bytearray()
            with selectors.DefaultSelector() as selector:
                os.set_blocking(process.stdin.fileno(), False)
                os.set_blocking(process.stdout.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE)
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or (cancel is not None and cancel.is_set()):
                        return None
                    for key, event in selector.select(min(_POLL, remaining)):
                        if event == selectors.EVENT_WRITE:
                            pending = pending[os.write(key.fd, pending[:65536]):]
                            if not pending:
                                selector.unregister(key.fileobj)
                                key.fileobj.close()
                        else:
                            chunk = os.read(key.fd, min(65536, _MAX_TSV + 1 - len(output)))
                            if not chunk:
                                selector.unregister(key.fileobj)
                            output.extend(chunk)
                            if len(output) > _MAX_TSV:
                                return None
            while process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or (cancel is not None and cancel.is_set()):
                    return None
                time.sleep(min(0.005, remaining))
            if process.returncode == 0 and (cancel is None or not cancel.is_set()):
                return output
        finally:
            if process.poll() is None:
                process.kill()
    return None


def _visible_text(tsv):
    lines = []
    line_key = None
    for row in tsv.decode("utf-8", errors="replace").split("\n"):
        fields = row.split("\t", 11)
        if len(fields) != 12 or fields[0] != "5":
            continue
        word = "".join(char for char in fields[11] if char.isprintable()).strip()
        if not word:
            continue
        key = fields[1:5]
        if key != line_key:
            lines.append([])
            line_key = key
        lines[-1].append(word)
    result = "\n".join(" ".join(words) for words in lines)
    if len(result) > _MAX_TEXT:
        result = result[:_MAX_TEXT].rpartition("\n")[0]
    return result or None


class OcrObserver:
    """Recognize English screen text without passing a frame to a model."""

    def __init__(self):
        self._executable = shutil.which("tesseract", path=_PATH)

    @property
    def available(self) -> bool:
        """Return whether the optional system executable was found."""
        return self._executable is not None

    def observe(self, image: bytes, cancel: Event | None = None) -> str | None:
        """Return bounded OCR text or None when no usable text is available."""
        if not self.available or (cancel is not None and cancel.is_set()):
            return None
        if not isinstance(image, bytes) or not 33 <= len(image) <= _MAX_PNG or not image.startswith(_PNG_IHDR):
            return None
        width, height = struct.unpack_from(">II", image, 16)
        if not 1 <= width <= 1920 or not 1 <= height <= 1080:
            return None
        try:
            tsv = _read_tsv(self._executable, image, cancel)
        except OSError:
            return None
        return _visible_text(tsv) if tsv else None
