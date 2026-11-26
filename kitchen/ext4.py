"""Pure-Python read-only ext2/3/4 reader (extents, indirect blocks, inline data, xattrs)."""
from __future__ import annotations

import mmap
import stat
import struct

from .fsbase import Entry, FsReader
from .util import KitchenError

EXT4_MAGIC = 0xEF53
INCOMPAT_64BIT = 0x80
INCOMPAT_META_BG = 0x10
INCOMPAT_FILETYPE = 0x2
EXTENTS_FL = 0x80000
INLINE_DATA_FL = 0x10000000
XATTR_MAGIC = 0xEA020000
EXT_MAGIC = 0xF30A

XATTR_PREFIX = {1: "user.", 2: "system.posix_acl_access", 3: "system.posix_acl_default",
                4: "trusted.", 6: "security.", 7: "system.", 8: "system.richacl"}

ZERO_CHUNK = bytes(1 << 20)


class _Inode:
    __slots__ = ("ino", "raw", "mode", "uid", "gid", "size", "flags", "block", "file_acl")


class Ext4Reader(FsReader):
    fs_type = "ext4"

    def __init__(self, path: str):
        self._f = open(path, "rb")
        try:
            self.mm = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
        except ValueError:
            raise KitchenError("Image is empty")
        sb = self.mm[1024:2048]
        if struct.unpack_from("<H", sb, 56)[0] != EXT4_MAGIC:
            raise KitchenError("Not an ext4 image")
        self.inodes_count = struct.unpack_from("<I", sb, 0)[0]
        first_data_block = struct.unpack_from("<I", sb, 20)[0]
        self.bs = 1024 << struct.unpack_from("<I", sb, 24)[0]
        self.inodes_per_group = struct.unpack_from("<I", sb, 40)[0]
        rev = struct.unpack_from("<I", sb, 76)[0]
        self.inode_size = struct.unpack_from("<H", sb, 88)[0] if rev >= 1 else 128
        self.incompat = struct.unpack_from("<I", sb, 96)[0]
        self.desc_size = 32
        if self.incompat & INCOMPAT_64BIT:
            self.desc_size = struct.unpack_from("<H", sb, 254)[0] or 64
        if self.incompat & INCOMPAT_META_BG:
            raise KitchenError("ext4 meta_bg layout is not supported")
        self.volume_name = sb[120:136].split(b"\0")[0].decode("utf-8", "replace")
        self.last_mounted = sb[136:200].split(b"\0")[0].decode("utf-8", "replace")
        ngroups = (self.inodes_count + self.inodes_per_group - 1) // self.inodes_per_group
        gdt = (first_data_block + 1) * self.bs
        self.inode_tables = []
        for g in range(ngroups):
            o = gdt + g * self.desc_size
            lo = struct.unpack_from("<I", self.mm, o + 8)[0]
            hi = struct.unpack_from("<I", self.mm, o + 40)[0] if self.desc_size >= 64 else 0
            self.inode_tables.append(lo | (hi << 32))

    def close(self):
        self.mm.close()
        self._f.close()

    # ------------------------------------------------------------------ inodes
    def inode(self, ino: int) -> _Inode:
        g, idx = divmod(ino - 1, self.inodes_per_group)
        off = self.inode_tables[g] * self.bs + idx * self.inode_size
        raw = self.mm[off:off + self.inode_size]
        i = _Inode()
        i.ino = ino
        i.raw = raw
        mode, uid_lo, size_lo = struct.unpack_from("<HHI", raw, 0)
        gid_lo = struct.unpack_from("<H", raw, 24)[0]
        i.flags = struct.unpack_from("<I", raw, 32)[0]
        i.block = raw[40:100]
        acl_lo = struct.unpack_from("<I", raw, 104)[0]
        size_hi = struct.unpack_from("<I", raw, 108)[0]
        acl_hi, uid_hi, gid_hi = struct.unpack_from("<HHH", raw, 118)
        i.mode = mode
        i.uid = uid_lo | (uid_hi << 16)
        i.gid = gid_lo | (gid_hi << 16)
        i.size = size_lo | (size_hi << 32)
        i.file_acl = acl_lo | (acl_hi << 32)
        return i

    # ------------------------------------------------------------------ block mapping
    def _extent_runs(self, node: bytes, runs: list):
        magic, entries, _max, depth = struct.unpack_from("<HHHH", node, 0)
        if magic != EXT_MAGIC:
            raise KitchenError("Corrupt extent header")
        for k in range(entries):
            o = 12 + k * 12
            if depth == 0:
                lblk, ln, hi, lo = struct.unpack_from("<IHHI", node, o)
                uninit = ln > 32768
                if uninit:
                    ln -= 32768
                runs.append((lblk, (hi << 32) | lo, ln, uninit))
            else:
                _lblk, lo, hi = struct.unpack_from("<IIH", node, o)
                child = ((hi << 32) | lo) * self.bs
                self._extent_runs(self.mm[child:child + self.bs], runs)

    def _indirect_runs(self, blocks: bytes, runs: list):
        bs = self.bs
        per = bs // 4
        ptrs = struct.unpack("<15I", blocks)
        lblk = 0

        def add(lb, pb):
            if pb:
                if runs and not runs[-1][3] and runs[-1][0] + runs[-1][2] == lb \
                        and runs[-1][1] + runs[-1][2] == pb:
                    s = runs[-1]
                    runs[-1] = (s[0], s[1], s[2] + 1, False)
                else:
                    runs.append((lb, pb, 1, False))

        for k in range(12):
            add(k, ptrs[k])
        lblk = 12

        def walk(pb, level, lb):
            if pb == 0:
                return lb + per ** level
            tbl = struct.unpack_from(f"<{per}I", self.mm, pb * bs)
            for p in tbl:
                if level == 1:
                    add(lb, p)
                    lb += 1
                else:
                    lb = walk(p, level - 1, lb)
            return lb

        lblk = walk(ptrs[12], 1, lblk)
        lblk = walk(ptrs[13], 2, lblk)
        walk(ptrs[14], 3, lblk)

    def _runs(self, ino: _Inode):
        runs = []
        if ino.flags & EXTENTS_FL:
            self._extent_runs(ino.block, runs)
        else:
            self._indirect_runs(ino.block, runs)
        runs.sort(key=lambda r: r[0])
        return runs

    # ------------------------------------------------------------------ data
    def _iter_inode_data(self, ino: _Inode):
        size = ino.size
        if ino.flags & INLINE_DATA_FL:
            data = bytes(ino.block) + self._xattrs(ino).get("system.data", b"")
            yield data[:size]
            return
        if stat.S_ISLNK(ino.mode) and size < 60 and self._is_fast_symlink(ino):
            yield bytes(ino.block[:size])
            return
        bs = self.bs
        pos = 0
        for lblk, pblk, ln, uninit in self._runs(ino):
            start = lblk * bs
            if start >= size:
                break
            if start > pos:
                yield from _zeros(start - pos)
                pos = start
            n = min(ln * bs, size - start)
            if uninit:
                yield from _zeros(n)
            else:
                off = pblk * bs
                step = 8 << 20
                for c in range(0, n, step):
                    yield self.mm[off + c:off + min(n, c + step)]
            pos = start + n
        if pos < size:
            yield from _zeros(size - pos)

    def _is_fast_symlink(self, ino: _Inode) -> bool:
        # Fast symlinks store the target in i_block and own no data blocks.
        blocks_lo = struct.unpack_from("<I", ino.raw, 28)[0]
        ea_blocks = (self.bs // 512) if ino.file_acl else 0
        return blocks_lo - ea_blocks == 0

    def iter_data(self, entry: Entry):
        yield from self._iter_inode_data(self.inode(entry.ino))

    # ------------------------------------------------------------------ xattrs
    def _parse_xattr_entries(self, buf: bytes, start: int, value_base: int, out: dict):
        o = start
        while o + 16 <= len(buf):
            if struct.unpack_from("<I", buf, o)[0] == 0:
                break
            name_len, idx, voff, vinum, vsize = struct.unpack_from("<BBHII", buf, o)
            name = buf[o + 16:o + 16 + name_len].decode("utf-8", "replace")
            if vinum == 0:
                v = buf[value_base + voff:value_base + voff + vsize]
                out[XATTR_PREFIX.get(idx, f"unknown{idx}.") + name] = bytes(v)
            o += (16 + name_len + 3) & ~3
        return out

    def _xattrs(self, ino: _Inode) -> dict:
        out = {}
        raw = ino.raw
        if self.inode_size > 128:
            extra = struct.unpack_from("<H", raw, 128)[0]
            st = 128 + extra
            if st + 4 <= len(raw) and struct.unpack_from("<I", raw, st)[0] == XATTR_MAGIC:
                self._parse_xattr_entries(raw, st + 4, st + 4, out)
        if ino.file_acl:
            blk = self.mm[ino.file_acl * self.bs:(ino.file_acl + 1) * self.bs]
            if struct.unpack_from("<I", blk, 0)[0] == XATTR_MAGIC:
                self._parse_xattr_entries(blk, 32, 0, out)
        return out

    def xattrs(self, entry: Entry) -> dict:
        return self._xattrs(self.inode(entry.ino))

    # ------------------------------------------------------------------ directories
    def _dir_entries(self, ino: _Inode):
        if ino.flags & INLINE_DATA_FL:
            parts = [bytes(ino.block[4:]), self._xattrs(ino).get("system.data", b"")]
        else:
            parts = [b"".join(bytes(c) for c in self._iter_inode_data(ino))]
        has_ft = bool(self.incompat & INCOMPAT_FILETYPE)
        for data in parts:
            o, n = 0, len(data)
            while o + 8 <= n:
                child, rec_len, name_len, _ft = struct.unpack_from("<IHBB", data, o)
                if rec_len < 8:
                    break
                if not has_ft:
                    name_len = struct.unpack_from("<H", data, o + 6)[0] & 0xFF
                if child and name_len:
                    name = data[o + 8:o + 8 + name_len].decode("utf-8", "surrogateescape")
                    if name not in (".", ".."):
                        yield name, child
                o += rec_len

    def walk(self):
        root = self.inode(2)
        yield Entry("", 2, root.mode, root.uid, root.gid, root.size)
        stack = [("", root)]
        seen = {2}
        while stack:
            path, dino = stack.pop()
            children = sorted(self._dir_entries(dino))
            subdirs = []
            for name, cino in children:
                if cino in seen and cino != 0:
                    ci = self.inode(cino)
                    if stat.S_ISDIR(ci.mode):
                        continue
                ci = self.inode(cino)
                p = f"{path}/{name}" if path else name
                yield Entry(p, cino, ci.mode, ci.uid, ci.gid, ci.size)
                if stat.S_ISDIR(ci.mode):
                    seen.add(cino)
                    subdirs.append((p, ci))
            stack.extend(reversed(subdirs))


def _zeros(n):
    while n > 0:
        k = min(n, len(ZERO_CHUNK))
        yield ZERO_CHUNK[:k]
        n -= k
