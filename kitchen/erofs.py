"""Pure-Python read-only EROFS reader.

Supports plain, inline (tail-packed), chunk-based and compressed inodes
(legacy & compact indexes, big pclusters, ztailpacking, fragments, interlaced
plain pclusters) with LZ4, LZMA (MicroLZMA), DEFLATE and Zstandard.
"""
from __future__ import annotations

import lzma
import mmap
import stat
import struct
import zlib

from .fsbase import Entry, FsReader
from .util import KitchenError

EROFS_MAGIC = 0xE0F5E1E2
SB_OFFSET = 1024

FEATURE_INCOMPAT_ZERO_PADDING = 0x1

LAYOUT_FLAT_PLAIN, LAYOUT_COMPRESSED_FULL, LAYOUT_FLAT_INLINE = 0, 1, 2
LAYOUT_COMPRESSED_COMPACT, LAYOUT_CHUNK_BASED = 3, 4

ADV_COMPACTED_2B = 0x0001
ADV_BIG_PCLUSTER_1 = 0x0002
ADV_BIG_PCLUSTER_2 = 0x0004
ADV_INLINE_PCLUSTER = 0x0008
ADV_INTERLACED_PCLUSTER = 0x0010
ADV_FRAGMENT_PCLUSTER = 0x0020

T_PLAIN, T_HEAD1, T_NONHEAD, T_HEAD2 = 0, 1, 2, 3
LI_D0_CBLKCNT = 1 << 11

ALG_LZ4, ALG_LZMA, ALG_DEFLATE, ALG_ZSTD = 0, 1, 2, 3

CHUNK_FORMAT_INDEXES = 0x20
NULL_ADDR = 0xFFFFFFFF

XATTR_PREFIX = {1: "user.", 2: "system.posix_acl_access", 3: "system.posix_acl_default",
                4: "trusted.", 5: "lustre.", 6: "security."}


def _align(x, a):
    return (x + a - 1) & ~(a - 1)


class _Inode:
    __slots__ = ("nid", "iloc", "layout", "isize", "xsize", "mode", "size", "iu",
                 "uid", "gid", "_zmap")


class _LRec:
    """Decoded logical-cluster index record."""
    __slots__ = ("type", "clusterofs", "pblk", "delta0", "cblks", "nextpackoff")

    def __init__(self):
        self.type = 0
        self.clusterofs = 0
        self.pblk = 0
        self.delta0 = 0
        self.cblks = 0
        self.nextpackoff = 0


class ErofsReader(FsReader):
    fs_type = "erofs"

    def __init__(self, path: str):
        self._f = open(path, "rb")
        self.mm = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
        sb = self.mm[SB_OFFSET:SB_OFFSET + 128]
        if struct.unpack_from("<I", sb, 0)[0] != EROFS_MAGIC:
            raise KitchenError("Not an EROFS image")
        self.blkbits = sb[12]
        self.bs = 1 << self.blkbits
        self.root_nid = struct.unpack_from("<H", sb, 14)[0]
        self.meta_blkaddr, self.xattr_blkaddr = struct.unpack_from("<II", sb, 40)
        self.volume_name = sb[64:80].split(b"\0")[0].decode("utf-8", "replace")
        self.feature_incompat = struct.unpack_from("<I", sb, 80)[0]
        self.packed_nid = struct.unpack_from("<Q", sb, 96)[0]
        self.zero_padding = bool(self.feature_incompat & FEATURE_INCOMPAT_ZERO_PADDING)
        self._packed_data = None
        self._zstd = None

    def close(self):
        self.mm.close()
        self._f.close()

    # ------------------------------------------------------------------ inodes
    def inode(self, nid: int) -> _Inode:
        off = self.meta_blkaddr * self.bs + nid * 32
        fmt = struct.unpack_from("<H", self.mm, off)[0]
        i = _Inode()
        i.nid, i.iloc = nid, off
        i.layout = (fmt >> 1) & 7
        i._zmap = None
        if fmt & 1:  # extended 64-byte inode
            (_f, icount, mode, _r, size, iu, _ino, uid, gid) = struct.unpack_from(
                "<HHHHQIIII", self.mm, off)
            i.isize = 64
        else:
            (_f, icount, mode, _nl, size, _r, iu, _ino, uid, gid) = struct.unpack_from(
                "<HHHHIIIIHH", self.mm, off)
            i.isize = 32
        i.xsize = 0 if icount == 0 else 12 + (icount - 1) * 4
        i.mode, i.size, i.iu, i.uid, i.gid = mode, size, iu, uid, gid
        return i

    # ------------------------------------------------------------------ xattrs
    def _xattr_entry(self, o: int, out: dict) -> int:
        name_len, idx, vsize = struct.unpack_from("<BBH", self.mm, o)
        name = self.mm[o + 4:o + 4 + name_len].decode("utf-8", "replace")
        value = self.mm[o + 4 + name_len:o + 4 + name_len + vsize]
        if not idx & 0x80:  # long (custom) prefixes are not needed for Android metadata
            out[XATTR_PREFIX.get(idx, f"unknown{idx}.") + name] = bytes(value)
        return _align(4 + name_len + vsize, 4)

    def _xattrs(self, ino: _Inode) -> dict:
        out = {}
        if not ino.xsize:
            return out
        base = ino.iloc + ino.isize
        shared_count = self.mm[base + 4]
        for k in range(shared_count):
            xid = struct.unpack_from("<I", self.mm, base + 12 + 4 * k)[0]
            self._xattr_entry(self.xattr_blkaddr * self.bs + xid * 4, out)
        o = base + 12 + 4 * shared_count
        end = base + ino.xsize
        while o + 4 <= end:
            o += self._xattr_entry(o, out)
        return out

    def xattrs(self, entry: Entry) -> dict:
        return self._xattrs(self.inode(entry.ino))

    # ------------------------------------------------------------------ data
    def iter_data(self, entry: Entry):
        yield from self._iter_inode(self.inode(entry.ino))

    def _iter_inode(self, ino: _Inode):
        size, bs, lay = ino.size, self.bs, ino.layout
        if size == 0:
            return
        if lay == LAYOUT_FLAT_PLAIN:
            off = ino.iu * bs
            yield from self._slices(off, size)
        elif lay == LAYOUT_FLAT_INLINE:
            nblocks = (size + bs - 1) // bs
            head = (nblocks - 1) * bs
            if head:
                yield from self._slices(ino.iu * bs, head)
            tail_off = ino.iloc + ino.isize + ino.xsize
            yield self.mm[tail_off:tail_off + size - head]
        elif lay == LAYOUT_CHUNK_BASED:
            yield from self._iter_chunked(ino)
        elif lay in (LAYOUT_COMPRESSED_FULL, LAYOUT_COMPRESSED_COMPACT):
            yield from self._iter_compressed(ino)
        else:
            raise KitchenError(f"Unsupported EROFS data layout {lay}")

    def _slices(self, off, n, step=8 << 20):
        for c in range(0, n, step):
            yield self.mm[off + c:off + min(n, c + step)]

    def _iter_chunked(self, ino: _Inode):
        fmt = ino.iu & 0xFFFF
        chunksize = self.bs << (fmt & 0x1F)
        nchunks = (ino.size + chunksize - 1) // chunksize
        base = ino.iloc + ino.isize + ino.xsize
        left = ino.size
        for k in range(nchunks):
            if fmt & CHUNK_FORMAT_INDEXES:
                o = _align(base, 8) + k * 8
                _adv, dev, blk = struct.unpack_from("<HHI", self.mm, o)
            else:
                o = _align(base, 4) + k * 4
                blk = struct.unpack_from("<I", self.mm, o)[0]
            n = min(chunksize, left)
            if blk == NULL_ADDR:
                yield bytes(n)
            else:
                yield from self._slices(blk * self.bs, n)
            left -= n

    # ------------------------------------------------------------------ compression
    def _zmap(self, ino: _Inode):
        if ino._zmap:
            return ino._zmap
        hpos = _align(ino.iloc + ino.isize + ino.xsize, 8)
        h = self.mm[hpos:hpos + 8]
        frag_or_idata, = struct.unpack_from("<I", h, 0)
        idata_size, = struct.unpack_from("<H", h, 2)
        advise, = struct.unpack_from("<H", h, 4)
        algtype, clusterbits = h[6], h[7]
        z = {"hpos": hpos}
        if clusterbits & 0x80:
            # whole file lives in the packed inode
            z["whole_fragment"] = int.from_bytes(h, "little") & ~(1 << 63)
            ino._zmap = z
            return z
        z["advise"] = advise
        z["alg"] = (algtype & 0xF, algtype >> 4)
        z["lbits"] = self.blkbits + (clusterbits & 7)
        z["idata_size"] = idata_size
        z["fragoff"] = frag_or_idata
        ino._zmap = z
        return z

    def _load_legacy(self, ino, z, lcn) -> _LRec:
        pos = hpos_full = _align(ino.iloc + ino.isize + ino.xsize, 8) + 16 + lcn * 8
        del hpos_full
        adv, cofs, u = struct.unpack_from("<HHI", self.mm, pos)
        r = _LRec()
        r.nextpackoff = pos + 8
        r.type = adv & 3
        if r.type == T_NONHEAD:
            r.clusterofs = 1 << z["lbits"]
            d0 = u & 0xFFFF
            if d0 & LI_D0_CBLKCNT:
                r.cblks = d0 & ~LI_D0_CBLKCNT
                d0 = 1
            r.delta0 = d0
        else:
            r.clusterofs = cofs
            r.pblk = u
        return r

    def _load_compact(self, ino, z, lcn, totalidx) -> _LRec:
        lbits = z["lbits"]
        ebase = _align(ino.iloc + ino.isize + ino.xsize, 8) + 8
        initial = (32 - ebase % 32) // 4
        if initial == 8:
            initial = 0
        if (z["advise"] & ADV_COMPACTED_2B) and initial < totalidx:
            c2b = ((totalidx - initial) // 16) * 16
        else:
            c2b = 0
        pos = ebase
        if lcn < initial:
            shift = 2
        else:
            pos += initial * 4
            lcn -= initial
            if lcn < c2b:
                shift = 1
            else:
                pos += c2b * 2
                lcn -= c2b
                shift = 2
        pos += lcn << shift

        if shift == 2 and lbits <= 14:
            vcnt = 2
        elif shift == 1 and lbits <= 12:
            vcnt = 16
        else:
            raise KitchenError("Unsupported EROFS compact index configuration")
        packsize = vcnt << shift
        base = pos - (pos % packsize)
        pack = self.mm[base:base + packsize] + b"\0\0\0\0"
        big = bool(z["advise"] & ADV_BIG_PCLUSTER_1)
        lobits = max(lbits, 12)
        encodebits = ((packsize - 4) * 8) // vcnt
        mask = (1 << lobits) - 1

        def dec(k):
            bit = encodebits * k
            v = int.from_bytes(pack[bit >> 3:(bit >> 3) + 4], "little") >> (bit & 7)
            return v & mask, (v >> lobits) & 3

        r = _LRec()
        r.nextpackoff = base + packsize
        i = (pos - base) >> shift
        lo, typ = dec(i)
        r.type = typ
        if typ == T_NONHEAD:
            r.clusterofs = 1 << lbits
            if lo & LI_D0_CBLKCNT:
                r.cblks = lo & ~LI_D0_CBLKCNT
                r.delta0 = 1
                return r
            if i + 1 != vcnt:
                r.delta0 = lo
                return r
            lo, t2 = dec(i - 1)
            if t2 != T_NONHEAD:
                lo = 0
            elif lo & LI_D0_CBLKCNT:
                lo = 1
            r.delta0 = lo + 1
            return r
        r.clusterofs = lo
        if not big:
            nblk = 1
            while i > 0:
                i -= 1
                lo, t = dec(i)
                if t == T_NONHEAD:
                    i -= lo
                if i >= 0:
                    nblk += 1
        else:
            nblk = 0
            while i > 0:
                i -= 1
                lo, t = dec(i)
                if t == T_NONHEAD:
                    if lo & LI_D0_CBLKCNT:
                        i -= 1
                        nblk += lo & ~LI_D0_CBLKCNT
                        continue
                    if lo <= 1:
                        raise KitchenError("Corrupt EROFS compact index (big pcluster)")
                    i -= lo - 2
                    continue
                nblk += 1
        r.pblk = struct.unpack_from("<I", pack, packsize - 4)[0] + nblk
        return r

    def _packed(self) -> bytes:
        if self._packed_data is None:
            pino = self.inode(self.packed_nid)
            self._packed_data = b"".join(bytes(c) for c in self._iter_inode(pino))
        return self._packed_data

    def _iter_compressed(self, ino: _Inode):
        z = self._zmap(ino)
        size = ino.size
        if "whole_fragment" in z:
            off = z["whole_fragment"]
            yield self._packed()[off:off + size]
            return
        lbits = z["lbits"]
        ls = 1 << lbits
        totalidx = (size + ls - 1) >> lbits
        compact = ino.layout == LAYOUT_COMPRESSED_COMPACT
        advise = z["advise"]

        def load(lcn):
            return (self._load_compact(ino, z, lcn, totalidx) if compact
                    else self._load_legacy(ino, z, lcn))

        recs = [load(lcn) for lcn in range(totalidx)]
        # Heads starting at/after EOF are zero-length markers (mkfs emits one when the
        # tail is packed/fragmented); they must not be treated as the tail extent.
        heads = [(lcn, r) for lcn, r in enumerate(recs)
                 if r.type != T_NONHEAD and (lcn << lbits) + r.clusterofs < size]
        if not heads:
            raise KitchenError("Corrupt EROFS compressed inode (no head lcluster)")

        tail_inline = bool(advise & ADV_INLINE_PCLUSTER)
        tail_fragment = bool(advise & ADV_FRAGMENT_PCLUSTER)
        idataoff = recs[-1].nextpackoff
        fragoff = z["fragoff"]
        if tail_fragment and not compact:
            fragoff |= heads[-1][1].pblk << 32

        bs = self.bs
        for k, (lcn, r) in enumerate(heads):
            la = (lcn << lbits) + r.clusterofs
            if k + 1 < len(heads):
                nl, nr = heads[k + 1]
                end = (nl << lbits) + nr.clusterofs
            else:
                end = size
            end = min(end, size)
            llen = end - la
            if llen <= 0:
                continue
            is_tail = k == len(heads) - 1
            if is_tail and tail_fragment:
                yield self._packed()[fragoff:fragoff + llen]
                continue
            if is_tail and tail_inline:
                pa, plen = idataoff, z["idata_size"]
            else:
                # compressed length (in blocks) of this pcluster
                bigpcl1 = advise & ADV_BIG_PCLUSTER_1
                bigpcl2 = advise & ADV_BIG_PCLUSTER_2
                nxt = lcn + 1
                if ((r.type == T_HEAD1 and not bigpcl1) or
                        (r.type in (T_PLAIN, T_HEAD2) and not bigpcl2) or
                        (nxt << lbits) >= size):
                    cblks = 1
                else:
                    nr2 = recs[nxt]
                    if nr2.type != T_NONHEAD:
                        cblks = 1 << (lbits - self.blkbits)
                    elif nr2.delta0 == 1 and nr2.cblks:
                        cblks = nr2.cblks
                    else:
                        raise KitchenError("Corrupt EROFS big pcluster")
                pa, plen = r.pblk * bs, cblks * bs
            raw = self.mm[pa:pa + plen]
            if r.type == T_PLAIN:
                if advise & ADV_INTERLACED_PCLUSTER:
                    sh = la % bs
                    raw = raw[sh:] + raw[:sh]
                out = raw[:llen]
            else:
                alg = z["alg"][0] if r.type == T_HEAD1 else z["alg"][1]
                out = self._decompress(alg, raw, llen)
            if len(out) < llen:
                raise KitchenError(f"EROFS decompression produced {len(out)} of {llen} bytes")
            yield out[:llen]

    def _decompress(self, alg: int, raw: bytes, llen: int) -> bytes:
        if self.zero_padding or alg != ALG_LZ4:
            n = len(raw)
            raw = raw.lstrip(b"\0") if alg != ALG_LZMA else raw
            if not raw and n:
                raise KitchenError("Empty EROFS pcluster")
        if alg == ALG_LZ4:
            import lz4.block
            try:
                return lz4.block.decompress(raw, uncompressed_size=llen)
            except Exception:
                return lz4.block.decompress(raw, uncompressed_size=max(llen, len(raw) * 255))
        if alg == ALG_LZMA:
            raw = raw.lstrip(b"\0")
            props = (~raw[0]) & 0xFF
            lc = props % 9
            rest = props // 9
            lp, pb = rest % 5, rest // 5
            d = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[{
                "id": lzma.FILTER_LZMA1, "lc": lc, "lp": lp, "pb": pb, "dict_size": 1 << 24}])
            return d.decompress(b"\0" + raw[1:], max_length=llen)
        if alg == ALG_DEFLATE:
            d = zlib.decompressobj(-15)
            return d.decompress(raw, llen)
        if alg == ALG_ZSTD:
            import zstandard
            if self._zstd is None:
                self._zstd = zstandard.ZstdDecompressor()
            return self._zstd.decompressobj().decompress(raw)[:llen]
        raise KitchenError(f"Unsupported EROFS compression algorithm {alg}")

    # ------------------------------------------------------------------ directories
    def _dir_entries(self, ino: _Inode):
        data = b"".join(bytes(c) for c in self._iter_inode(ino))
        bs = self.bs
        for bo in range(0, len(data), bs):
            blk = data[bo:bo + bs]
            if len(blk) < 12:
                break
            first_nameoff = struct.unpack_from("<H", blk, 8)[0]
            count = first_nameoff // 12
            for k in range(count):
                nid, nameoff, _ft = struct.unpack_from("<QHB", blk, k * 12)
                if k + 1 < count:
                    nend = struct.unpack_from("<H", blk, (k + 1) * 12 + 8)[0]
                    name = blk[nameoff:nend]
                else:
                    name = blk[nameoff:].split(b"\0", 1)[0]
                name = name.decode("utf-8", "surrogateescape")
                if name not in (".", ".."):
                    yield name, nid

    def walk(self):
        root = self.inode(self.root_nid)
        yield Entry("", root.nid, root.mode, root.uid, root.gid, root.size)
        stack = [("", root)]
        seen = {root.nid}
        while stack:
            path, dino = stack.pop()
            subdirs = []
            for name, nid in sorted(self._dir_entries(dino)):
                ci = self.inode(nid)
                if stat.S_ISDIR(ci.mode):
                    if nid in seen:
                        continue
                    seen.add(nid)
                p = f"{path}/{name}" if path else name
                yield Entry(p, nid, ci.mode, ci.uid, ci.gid, ci.size)
                if stat.S_ISDIR(ci.mode):
                    subdirs.append((p, ci))
            stack.extend(reversed(subdirs))
