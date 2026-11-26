"""Dynamic partition (super.img / LP metadata) parsing and extraction."""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field

from .util import CHUNK, KitchenError

LP_RESERVED = 4096
LP_GEOMETRY_SIZE = 4096
LP_GEOMETRY_MAGIC = 0x616C4467
LP_HEADER_MAGIC = 0x414C5030
LP_SECTOR = 512
LP_TARGET_LINEAR, LP_TARGET_ZERO = 0, 1


@dataclass
class LpExtent:
    num_sectors: int
    target_type: int
    target_data: int  # physical sector
    target_source: int


@dataclass
class LpPartition:
    name: str
    attributes: int
    group: str
    extents: list = field(default_factory=list)
    out_name: str = ""

    @property
    def size(self) -> int:
        return sum(e.num_sectors for e in self.extents) * LP_SECTOR


@dataclass
class LpMetadata:
    metadata_max_size: int
    slot_count: int
    block_size: int
    partitions: list
    block_devices: list


def is_super(head: bytes) -> bool:
    return (len(head) >= LP_RESERVED + 4 and
            struct.unpack_from("<I", head, LP_RESERVED)[0] == LP_GEOMETRY_MAGIC)


def metadata_bytes_needed(head: bytes) -> int:
    """How many bytes from the start of super are needed to parse slot 0 metadata."""
    _magic, _ss = struct.unpack_from("<II", head, LP_RESERVED)
    max_size, = struct.unpack_from("<I", head, LP_RESERVED + 40)
    return LP_RESERVED + 2 * LP_GEOMETRY_SIZE + max_size


def _cstr(b: bytes) -> str:
    return b.split(b"\0", 1)[0].decode("utf-8", "replace")


def parse_metadata(data: bytes) -> LpMetadata:
    g = LP_RESERVED
    magic, struct_size = struct.unpack_from("<II", data, g)
    if magic != LP_GEOMETRY_MAGIC:
        raise KitchenError("Not a super image (LP geometry magic missing)")
    max_size, slot_count, logical_block_size = struct.unpack_from("<III", data, g + 40)

    h = LP_RESERVED + 2 * LP_GEOMETRY_SIZE
    (hmagic, major, minor, header_size) = struct.unpack_from("<IHHI", data, h)
    if hmagic != LP_HEADER_MAGIC:
        raise KitchenError("LP metadata header magic missing")
    tables = h + header_size

    def desc(off):
        return struct.unpack_from("<III", data, h + off)

    p_off, p_num, p_sz = desc(80)
    e_off, e_num, e_sz = desc(92)
    g_off, g_num, g_sz = desc(104)
    b_off, b_num, b_sz = desc(116)

    groups = []
    for i in range(g_num):
        o = tables + g_off + i * g_sz
        groups.append(_cstr(data[o:o + 36]))

    extents = []
    for i in range(e_num):
        o = tables + e_off + i * e_sz
        ns, tt, td, ts = struct.unpack_from("<QIQI", data, o)
        extents.append(LpExtent(ns, tt, td, ts))

    devices = []
    for i in range(b_num):
        o = tables + b_off + i * b_sz
        first_sector, _align, _align_off, size = struct.unpack_from("<QIIQ", data, o)
        devices.append((_cstr(data[o + 24:o + 60]), first_sector, size))

    parts = []
    for i in range(p_num):
        o = tables + p_off + i * p_sz
        name = _cstr(data[o:o + 36])
        attrs, first_ext, num_ext, grp = struct.unpack_from("<IIII", data, o + 36)
        p = LpPartition(name, attrs, groups[grp] if grp < len(groups) else "",
                        extents[first_ext:first_ext + num_ext])
        parts.append(p)

    # Friendly output names: drop the _a/_b slot suffix when only one slot has data.
    names_with_data = {p.name for p in parts if p.extents}
    for p in parts:
        n = p.name
        if n.endswith(("_a", "_b")):
            other = n[:-1] + ("b" if n.endswith("_a") else "a")
            if other not in names_with_data:
                n = n[:-2]
        p.out_name = n
    return LpMetadata(max_size, slot_count, logical_block_size, parts, devices)


def _segments(meta: LpMetadata, names, outdir):
    """Build (phys_offset, length, partition, out_offset) list for selected partitions."""
    segs = []
    selected = [p for p in meta.partitions if p.extents and (names is None or p.out_name in names
                                                             or p.name in names)]
    for p in selected:
        off = 0
        for e in p.extents:
            ln = e.num_sectors * LP_SECTOR
            if e.target_type == LP_TARGET_LINEAR:
                if e.target_source != 0:
                    raise KitchenError(f"{p.name}: extents on secondary block device are not supported")
                segs.append((e.target_data * LP_SECTOR, ln, p, off))
            off += ln
    segs.sort(key=lambda s: s[0])
    return selected, segs


def extract_seekable(f, meta: LpMetadata, names, outdir, job=None):
    """Extract logical partitions from a raw (non-sparse) super image file object."""
    selected, segs = _segments(meta, names, outdir)
    total = sum(s[1] for s in segs) or 1
    done = 0
    outs = {}
    try:
        for p in selected:
            path = os.path.join(outdir, p.out_name + ".img")
            fh = open(path, "wb")
            fh.truncate(p.size)
            outs[p.name] = (fh, path)
        for phys, ln, p, oo in segs:
            fh = outs[p.name][0]
            f.seek(phys)
            fh.seek(oo)
            left = ln
            while left:
                chunk = f.read(min(CHUNK, left))
                if not chunk:
                    raise KitchenError("super image is truncated")
                fh.write(chunk)
                left -= len(chunk)
                done += len(chunk)
                if job:
                    job.check()
                    job.progress(done, total, f"super -> {p.out_name}")
    finally:
        for fh, _ in outs.values():
            fh.close()
    return [(p.out_name, outs[p.name][1]) for p in selected]


class RawStream:
    """Adapter giving a plain stream the read/skip/chunk_kind API of SparseReader."""

    def __init__(self, f):
        self.f, self.pos = f, 0

    def chunk_kind(self):
        return "raw"

    def read(self, n):
        data = self.f.read(n)
        self.pos += len(data)
        return data

    def skip(self, n):
        from .util import skip_stream
        skip_stream(self.f, n)
        self.pos += n


def extract_stream(reader, head: bytes, meta: LpMetadata, names, outdir, job=None):
    """Single-pass extraction from a sequential stream (e.g. lz4 -> sparse -> raw).

    `head` holds the bytes already consumed from the start of the raw stream.
    """
    selected, segs = _segments(meta, names, outdir)
    total = sum(s[1] for s in segs) or 1
    done = 0
    outs = {}
    pos = len(head)
    try:
        for p in selected:
            path = os.path.join(outdir, p.out_name + ".img")
            fh = open(path, "wb")
            fh.truncate(p.size)
            outs[p.name] = (fh, path)
        for phys, ln, p, oo in segs:
            fh = outs[p.name][0]
            if phys < len(head):
                # part of this extent was already consumed while reading metadata
                take = min(ln, len(head) - phys)
                fh.seek(oo)
                fh.write(head[phys:phys + take])
                phys, ln, oo = phys + take, ln - take, oo + take
                if ln == 0:
                    continue
            if phys < pos:
                raise KitchenError("Overlapping super extents are not supported in stream mode")
            if phys > pos:
                reader.skip(phys - pos)
                pos = phys
            left = ln
            while left:
                kind = reader.chunk_kind()
                if kind is None:
                    raise KitchenError("super image stream ended early")
                if kind == "hole":
                    n = min(left, reader.cur_left)
                    reader.skip(n)
                else:
                    n = min(left, CHUNK)
                    data = reader.read(n)
                    if not data:
                        raise KitchenError("super image stream ended early")
                    n = len(data)
                    fh.seek(oo + (ln - left))
                    fh.write(data)
                left -= n
                pos += n
                done += n
                if job:
                    job.check()
                    job.progress(done, total, f"super -> {p.out_name}")
    finally:
        for fh, _ in outs.values():
            fh.close()
    return [(p.out_name, outs[p.name][1]) for p in selected]
