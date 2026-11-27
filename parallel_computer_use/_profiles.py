"""Exclusive custody of browser data created by Parallel Computer Use."""

import fcntl
import os
import stat
from pathlib import Path


_LOCK = ".pcu-profile"
_MARKER = b"parallel-computer-use browser profile v1\n"
_PREVIOUS_LOCK = ".lcu-profile"
_PREVIOUS_MARKER = b"local-computer-use browser profile v1\n"


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
            contents = set(os.listdir(root))
            lock_name = _PREVIOUS_LOCK if _PREVIOUS_LOCK in contents else _LOCK
            self._marker = _PREVIOUS_MARKER if lock_name == _PREVIOUS_LOCK else _MARKER
            if contents - {lock_name, "data"}:
                raise ProfileError("Browser profile directory contains unrelated files")
            self.lock = os.open(
                lock_name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=root,
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
                os.pwrite(self.lock, self._marker, 0)
                os.fsync(self.lock)
            elif marker not in {self._marker, self._marker + b"chromium\n", self._marker + b"firefox\n"}:
                raise ProfileError("Browser profile is missing the Parallel Computer Use marker")
            else:
                self.engine = marker[len(self._marker):].strip().decode("ascii") or None
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
        if current == self._marker:
            os.pwrite(self.lock, expected, len(self._marker))
            os.fsync(self.lock)
        elif current != self._marker + expected:
            raise ProfileError("Browser profile belongs to a different browser engine")
        self.engine = engine

    def close(self):
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None
