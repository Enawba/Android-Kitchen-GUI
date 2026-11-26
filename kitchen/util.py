"""Shared helpers: cancellation, progress reporting, stream wrappers."""
from __future__ import annotations

import os
import threading

CHUNK = 4 * 1024 * 1024


class Cancelled(Exception):
    """Raised when the user cancels a running job."""


class KitchenError(Exception):
    """A user-facing error (bad / unsupported input)."""


class Job:
    """Carries logging, progress and cancellation between worker code and the GUI.

    The GUI subclasses / replaces the callbacks; the CLI uses the defaults (print).
    """

    def __init__(self, log=None, progress=None):
        self._log = log or (lambda msg, level="info": print(msg))
        self._progress = progress or (lambda cur, total, text="": None)
        self._cancel = threading.Event()

    def log(self, msg: str, level: str = "info"):
        self._log(msg, level)

    def warn(self, msg: str):
        self._log(msg, "warn")

    def error(self, msg: str):
        self._log(msg, "error")

    def progress(self, cur: int, total: int, text: str = ""):
        self._progress(cur, total, text)

    def cancel(self):
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check(self):
        if self._cancel.is_set():
            raise Cancelled()


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def read_exact(f, n: int) -> bytes:
    """Read exactly n bytes from a (possibly non-seekable) stream."""
    buf = bytearray()
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def skip_stream(f, n: int):
    """Skip n bytes forward on a stream, seeking if possible."""
    try:
        if f.seekable():
            f.seek(n, os.SEEK_CUR)
            return
    except (AttributeError, OSError):
        pass
    while n > 0:
        chunk = f.read(min(n, CHUNK))
        if not chunk:
            raise KitchenError("Unexpected end of stream while skipping")
        n -= len(chunk)


def copy_stream(src, dst, length: int | None, job: Job | None = None,
                total: int | None = None, done: int = 0, label: str = ""):
    """Copy `length` bytes (or until EOF if None) from src to dst."""
    copied = 0
    while length is None or copied < length:
        want = CHUNK if length is None else min(CHUNK, length - copied)
        chunk = src.read(want)
        if not chunk:
            if length is not None:
                raise KitchenError("Unexpected end of stream")
            break
        dst.write(chunk)
        copied += len(chunk)
        if job:
            job.check()
            if total:
                job.progress(done + copied, total, label)
    return copied


def safe_name(name: str) -> str:
    """Make a filesystem name safe for Windows (keeps it readable)."""
    bad = '<>:"\\|?*'
    out = "".join("_" if (c in bad or ord(c) < 32) else c for c in name)
    out = out.rstrip(" .")
    if out.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                                     *(f"LPT{i}" for i in range(1, 10))}:
        out = "_" + out
    return out or "_"


def long_path(p: str) -> str:
    """Return a path usable beyond MAX_PATH on Windows."""
    if os.name != "nt":
        return p
    p = os.path.abspath(p)
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


class SubFile:
    """Read-only random-access window [offset, offset+size) over a seekable file."""

    def __init__(self, f, offset: int, size: int):
        self.f, self.offset, self.size, self.pos = f, offset, size, 0

    def seekable(self):
        return True

    def readable(self):
        return True

    def seek(self, pos, whence=os.SEEK_SET):
        if whence == os.SEEK_SET:
            self.pos = pos
        elif whence == os.SEEK_CUR:
            self.pos += pos
        else:
            self.pos = self.size + pos
        return self.pos

    def tell(self):
        return self.pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = max(0, min(n, self.size - self.pos))
        if n == 0:
            return b""
        self.f.seek(self.offset + self.pos)
        data = self.f.read(n)
        self.pos += len(data)
        return data

    def pread(self, off, n):
        self.f.seek(self.offset + off)
        return self.f.read(n)
