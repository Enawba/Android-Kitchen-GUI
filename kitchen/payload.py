"""A/B OTA payload.bin dumper (full OTAs). No protobuf dependency."""
from __future__ import annotations

import bz2
import hashlib
import lzma
import os
import struct
import zipfile
from dataclasses import dataclass, field

from .util import KitchenError, SubFile

PAYLOAD_MAGIC = b"CrAU"

OP_REPLACE, OP_REPLACE_BZ, OP_MOVE, OP_BSDIFF = 0, 1, 2, 3
OP_SOURCE_COPY, OP_SOURCE_BSDIFF, OP_ZERO, OP_DISCARD = 4, 5, 6, 7
OP_REPLACE_XZ, OP_PUFFDIFF, OP_BROTLI_BSDIFF, OP_ZUCCHINI = 8, 9, 10, 11
OP_LZ4DIFF_BSDIFF, OP_LZ4DIFF_PUFFDIFF, OP_ZSTD = 12, 13, 14
DELTA_OPS = {OP_MOVE, OP_BSDIFF, OP_SOURCE_COPY, OP_SOURCE_BSDIFF, OP_PUFFDIFF,
             OP_BROTLI_BSDIFF, OP_ZUCCHINI, OP_LZ4DIFF_BSDIFF, OP_LZ4DIFF_PUFFDIFF}


# ---------------------------------------------------------------- tiny protobuf reader
def _varint(b, i):
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        if c < 0x80:
            return r, i
        s += 7


def _fields(b):
    """Yield (field_no, wire_type, value) for a protobuf message."""
    i, n = 0, len(b)
    while i < n:
        key, i = _varint(b, i)
        fno, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 1:
            v = struct.unpack_from("<Q", b, i)[0]
            i += 8
        elif wt == 2:
            ln, i = _varint(b, i)
            v = b[i:i + ln]
            i += ln
        elif wt == 5:
            v = struct.unpack_from("<I", b, i)[0]
            i += 4
        else:
            raise KitchenError(f"Unsupported protobuf wire type {wt}")
        yield fno, wt, v


@dataclass
class Extent:
    start_block: int = 0
    num_blocks: int = 0


@dataclass
class Operation:
    type: int = 0
    data_offset: int = 0
    data_length: int = 0
    dst_extents: list = field(default_factory=list)
    data_sha256: bytes = b""


@dataclass
class PartitionUpdate:
    name: str = ""
    size: int = 0
    operations: list = field(default_factory=list)


def _extent(b):
    e = Extent()
    for f, _, v in _fields(b):
        if f == 1:
            e.start_block = v
        elif f == 2:
            e.num_blocks = v
    return e


def _operation(b):
    op = Operation()
    for f, _, v in _fields(b):
        if f == 1:
            op.type = v
        elif f == 2:
            op.data_offset = v
        elif f == 3:
            op.data_length = v
        elif f == 6:
            op.dst_extents.append(_extent(v))
        elif f == 8:
            op.data_sha256 = bytes(v)
    return op


def _partition(b):
    p = PartitionUpdate()
    for f, _, v in _fields(b):
        if f == 1:
            p.name = bytes(v).decode()
        elif f == 7:  # new_partition_info
            for f2, _, v2 in _fields(v):
                if f2 == 1:
                    p.size = v2
        elif f == 8:
            p.operations.append(_operation(v))
    return p


class Payload:
    """Opened payload.bin (plain file or stored entry inside an OTA zip)."""

    def __init__(self, path: str, member: str | None = None):
        self.path = path
        self._fh = open(path, "rb")
        if member:
            self.f = zip_member_file(self._fh, path, member)
        else:
            self._fh.seek(0, 2)
            self.f = SubFile(self._fh, 0, self._fh.tell())
        hdr = self.f.pread(0, 24)
        if hdr[:4] != PAYLOAD_MAGIC:
            raise KitchenError("Not a payload.bin (CrAU magic missing)")
        version, manifest_size = struct.unpack(">QQ", hdr[4:20])
        if version == 1:
            sig_size, hsize = 0, 20
        else:
            sig_size = struct.unpack(">I", hdr[20:24])[0]
            hsize = 24
        manifest = self.f.pread(hsize, manifest_size)
        self.data_offset = hsize + manifest_size + sig_size
        self.block_size = 4096
        self.partitions: list[PartitionUpdate] = []
        self.dynamic_partitions: set[str] = set()   # partitions that live inside super
        for fno, _, v in _fields(manifest):
            if fno == 3:
                self.block_size = v
            elif fno == 13:
                self.partitions.append(_partition(v))
            elif fno == 15:  # DynamicPartitionMetadata
                for f2, _, grp in _fields(v):
                    if f2 == 1:  # DynamicPartitionGroup
                        for f3, _, val in _fields(grp):
                            if f3 == 3:  # partition_names
                                self.dynamic_partitions.add(bytes(val).decode())

    def close(self):
        self._fh.close()

    def is_incremental(self, part: PartitionUpdate) -> bool:
        return any(op.type in DELTA_OPS for op in part.operations)

    def extract(self, part: PartitionUpdate, out_path: str, job=None, verify=False):
        if self.is_incremental(part):
            raise KitchenError(f"{part.name}: incremental OTA payloads are not supported "
                               "(use a full OTA / full ROM)")
        bs = self.block_size
        total = len(part.operations) or 1
        with open(out_path, "wb") as out:
            out.truncate(part.size)
            for idx, op in enumerate(part.operations):
                if job:
                    job.check()
                    job.progress(idx, total, f"payload -> {part.name}")
                if op.type in (OP_ZERO, OP_DISCARD):
                    continue  # file is pre-zeroed by truncate
                raw = self.f.pread(self.data_offset + op.data_offset, op.data_length)
                if verify and op.data_sha256 and hashlib.sha256(raw).digest() != op.data_sha256:
                    raise KitchenError(f"{part.name}: checksum mismatch in operation {idx}")
                if op.type == OP_REPLACE:
                    data = raw
                elif op.type == OP_REPLACE_BZ:
                    data = bz2.decompress(raw)
                elif op.type == OP_REPLACE_XZ:
                    data = lzma.decompress(raw)
                elif op.type == OP_ZSTD:
                    import zstandard
                    data = zstandard.ZstdDecompressor().decompressobj().decompress(raw)
                else:
                    raise KitchenError(f"{part.name}: unsupported operation type {op.type}")
                pos = 0
                for e in op.dst_extents:
                    n = e.num_blocks * bs
                    out.seek(e.start_block * bs)
                    out.write(data[pos:pos + n])
                    pos += n
        if job:
            job.progress(total, total, f"payload -> {part.name}")


def zip_member_file(fh, path, member) -> SubFile:
    """Return a random-access view of a STORED zip member (no extraction needed)."""
    with zipfile.ZipFile(path) as z:
        info = z.getinfo(member)
    if info.compress_type != zipfile.ZIP_STORED:
        raise KitchenError(f"{member} is compressed inside the zip")
    fh.seek(info.header_offset)
    lh = fh.read(30)
    if lh[:4] != b"PK\x03\x04":
        raise KitchenError("Bad zip local header")
    name_len, extra_len = struct.unpack("<HH", lh[26:30])
    return SubFile(fh, info.header_offset + 30 + name_len + extra_len, info.file_size)


def is_payload(head: bytes) -> bool:
    return head[:4] == PAYLOAD_MAGIC
