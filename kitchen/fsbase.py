"""Common interface for the read-only filesystem readers (ext4, EROFS)."""
from __future__ import annotations

import stat
from dataclasses import dataclass

XATTR_SELINUX = "security.selinux"
XATTR_CAPS = "security.capability"


@dataclass
class Entry:
    path: str           # relative path inside the image, '/' separated ('' = root)
    ino: int            # inode number / nid
    mode: int
    uid: int
    gid: int
    size: int

    @property
    def is_dir(self):
        return stat.S_ISDIR(self.mode)

    @property
    def is_file(self):
        return stat.S_ISREG(self.mode)

    @property
    def is_link(self):
        return stat.S_ISLNK(self.mode)


class FsReader:
    fs_type = "?"

    def walk(self):
        """Yield Entry objects, parents before children (root first)."""
        raise NotImplementedError

    def iter_data(self, entry: Entry):
        """Yield the file contents as a sequence of bytes-like chunks."""
        raise NotImplementedError

    def xattrs(self, entry: Entry) -> dict:
        return {}

    def read_bytes(self, entry: Entry) -> bytes:
        return b"".join(bytes(c) for c in self.iter_data(entry))

    def readlink(self, entry: Entry) -> str:
        return self.read_bytes(entry).decode("utf-8", "replace")

    def write_file(self, entry: Entry, out):
        for chunk in self.iter_data(entry):
            out.write(chunk)

    def close(self):
        pass


def capabilities_from_xattr(value: bytes) -> int:
    """Decode a vfs_cap_data xattr into the 64-bit permitted capability mask."""
    if not value or len(value) < 12:
        return 0
    import struct
    magic = struct.unpack_from("<I", value)[0]
    lo = struct.unpack_from("<I", value, 4)[0]
    hi = 0
    if (magic & 0xFF000000) in (0x02000000, 0x03000000) and len(value) >= 16:
        hi = struct.unpack_from("<I", value, 12)[0]
    return lo | (hi << 32)


def open_fs(path: str) -> FsReader:
    """Detect the filesystem type of a raw image and open a reader for it."""
    from .detect import sniff_fs
    kind = sniff_fs(path)
    if kind == "ext4":
        from .ext4 import Ext4Reader
        return Ext4Reader(path)
    if kind == "erofs":
        from .erofs import ErofsReader
        return ErofsReader(path)
    from .util import KitchenError
    raise KitchenError(f"Unsupported filesystem ({kind or 'unknown'})")
