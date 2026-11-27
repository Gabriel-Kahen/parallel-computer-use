"""Exclusive custody of browser data created by Local Computer Use."""

import fcntl
import os
import stat
from pathlib import Path


_LOCK = ".lcu-profile"
_MARKER = b"local-computer-use browser profile v1\n"


class ProfileError(RuntimeError):
    pass


def _private_directory(fd):
    info = os.fstat(fd)
    return info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700


class BrowserProfile:
    def __init__(self, directory):
        directory = Path(directory).absolute()
        self.data = directory / "data"
        self.lock = None
        self.engine = None
        try:
            directory.mkdir(mode=0o700, exist_ok=True)
            root = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as error:
            raise ProfileError("Could not open a dedicated browser profile directory") from error
        try:
            if not _private_directory(root):
                raise ProfileError("Browser profile directory must be user-owned with mode 0700")
            if set(os.listdir(root)) - {_LOCK, "data"}:
                raise ProfileError("Browser profile directory contains unrelated files")
            self.lock = os.open(
                _LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=root,
            )
            info = os.fstat(self.lock)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                    info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
                raise ProfileError("Browser profile lock must be a private regular file")
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ProfileError("Browser profile is already in use") from error
            marker = os.pread(self.lock, 128, 0)
            if not marker and "data" not in os.listdir(root):
                os.pwrite(self.lock, _MARKER, 0)
                os.fsync(self.lock)
            elif marker not in {_MARKER, _MARKER + b"chromium\n", _MARKER + b"firefox\n"}:
                raise ProfileError("Browser profile is missing the Local Computer Use marker")
            else:
                self.engine = marker[len(_MARKER):].strip().decode("ascii") or None
            try:
                os.mkdir("data", mode=0o700, dir_fd=root)
            except FileExistsError:
                pass
            data = os.open("data", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            try:
                if not _private_directory(data):
                    raise ProfileError("Browser data directory must be user-owned with mode 0700")
            finally:
                os.close(data)
        except OSError as error:
            self.close()
            raise ProfileError("Could not open browser profile data") from error
        except BaseException:
            self.close()
            raise
        finally:
            os.close(root)

    def set_engine(self, engine):
        expected = f"{engine}\n".encode("ascii")
        current = os.pread(self.lock, 128, 0)
        if current == _MARKER:
            os.pwrite(self.lock, expected, len(_MARKER))
            os.fsync(self.lock)
        elif current != _MARKER + expected:
            raise ProfileError("Browser profile belongs to a different browser engine")
        self.engine = engine

    def close(self):
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None
