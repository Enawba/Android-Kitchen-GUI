"""Android sparse image (simg) decoding - streaming, works on non-seekable input."""
from __future__ import annotations

import struct

from .util import CHUNK, KitchenError, read_exact, skip_stream

SPARSE_MAGIC = 0xED26FF3A
CHUNK_RAW, CHUNK_FILL, CHUNK_DONT_CARE, CHUNK_CRC32 = 0xCAC1, 0xCAC2, 0xCAC3, 0xCAC4


def is_sparse(head: bytes) -> bool:
    return len(head) >= 4 and struct.unpack_from("<I", head)[0] == SPARSE_MAGIC


class SparseReader:
    """Presents a sparse image stream as a sequential raw stream.

    Supports read(n) and skip(n); skip() is cheap over DONT_CARE/FILL chunks.
    """

    def __init__(self, f):
        self.f = f
        hdr = read_exact(f, 28)
        (magic, major, _minor, fhdr_sz, chdr_sz, blk_sz, total_blks, total_chunks,
         _csum) = struct.unpack("<IHHHHIIII", hdr)
        if magic != SPARSE_MAGIC or major != 1:
            raise KitchenError("Not a valid Android sparse image")
        if fhdr_sz > 28:
            skip_stream(f, fhdr_sz - 28)
        self.chdr_sz = chdr_sz
        self.blk_sz = blk_sz
        self.size = total_blks * blk_sz
        self.chunks_left = total_chunks
        self.pos = 0
        # current chunk state
        self.cur_type = None
        self.cur_left = 0
        self.fill = b""

    def _next_chunk(self) -> bool:
        while self.cur_left == 0:
            if self.chunks_left == 0:
                return False
            self.chunks_left -= 1
            hdr = read_exact(self.f, 12)
            if len(hdr) < 12:
                raise KitchenError("Truncated sparse image")
            ctype, _r, csz, tsz = struct.unpack("<HHII", hdr)
            if self.chdr_sz > 12:
                skip_stream(self.f, self.chdr_sz - 12)
            data_sz = tsz - self.chdr_sz
            if ctype == CHUNK_RAW:
                self.cur_type, self.cur_left = CHUNK_RAW, csz * self.blk_sz
                if data_sz != self.cur_left:
                    raise KitchenError("Corrupt sparse RAW chunk")
            elif ctype == CHUNK_FILL:
                self.fill = read_exact(self.f, 4)
                if data_sz > 4:
                    skip_stream(self.f, data_sz - 4)
                self.cur_type, self.cur_left = CHUNK_FILL, csz * self.blk_sz
            elif ctype == CHUNK_DONT_CARE:
                if data_sz:
                    skip_stream(self.f, data_sz)
                self.cur_type, self.cur_left = CHUNK_DONT_CARE, csz * self.blk_sz
            elif ctype == CHUNK_CRC32:
                skip_stream(self.f, data_sz)
            else:
                raise KitchenError(f"Unknown sparse chunk type 0x{ctype:04x}")
        return True

    def chunk_kind(self):
        """Kind of data at current position: 'raw', 'fill', 'hole' or None at EOF."""
        if not self._next_chunk():
            return None
        if self.cur_type == CHUNK_RAW:
            return "raw"
        if self.cur_type == CHUNK_FILL and self.fill != b"\0\0\0\0":
            return "fill"
        return "hole"

    def read(self, n: int) -> bytes:
        out = bytearray()
        while n > 0 and self._next_chunk():
            take = min(n, self.cur_left)
            if self.cur_type == CHUNK_RAW:
                data = read_exact(self.f, take)
                if len(data) != take:
                    raise KitchenError("Truncated sparse image")
                out += data
            elif self.cur_type == CHUNK_FILL:
                # fill pattern is aligned to 4 bytes from chunk start; blocks are 4-aligned
                phase = (self.pos % 4)
                pat = (self.fill * ((take + phase) // 4 + 2))[phase:phase + take]
                out += pat
            else:
                out += bytes(take)
            self.cur_left -= take
            self.pos += take
            n -= take
        return bytes(out)

    def skip(self, n: int):
        while n > 0 and self._next_chunk():
            take = min(n, self.cur_left)
            if self.cur_type == CHUNK_RAW:
                skip_stream(self.f, take)
            self.cur_left -= take
            self.pos += take
            n -= take

    def write_to(self, out, job=None, label="", progress_base=0, progress_total=None):
        """Write the whole remaining raw image into `out` (seekable file).

        Holes are skipped with seek (so several sparse chunks can be layered onto one
        output file, as needed for Motorola *_sparsechunk.N files).
        """
        total = progress_total or self.size
        while True:
            kind = self.chunk_kind()
            if kind is None:
                break
            if kind == "hole":
                n = self.cur_left
                self.skip(n)
                out.seek(self.pos)
                continue
            n = min(self.cur_left, CHUNK)
            out.seek(self.pos)
            out.write(self.read(n))
            if job:
                job.check()
                job.progress(progress_base + self.pos, total, label)
        return self.size


def sparse_to_raw(src_path: str, dst_path: str, job=None, label="", append=False):
    """Convert a sparse image file to raw. With append=True the destination is not
    truncated (used for sparse chunks that each cover part of one image)."""
    import os
    mode = "r+b" if append and os.path.exists(dst_path) else "wb"
    with open(src_path, "rb") as f, open(dst_path, mode) as out:
        r = SparseReader(f)
        r.write_to(out, job, label)
        out.seek(0, 2)
        if out.tell() < r.size:
            out.truncate(r.size)
        return r.size
