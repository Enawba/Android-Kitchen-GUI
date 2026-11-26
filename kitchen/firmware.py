"""Firmware scanning: find every partition image inside a stock firmware package.

Handles: folders, .zip (OTA / fastboot / factory / nested), .tar / .tar.md5 (Samsung),
.tgz / .tar.gz / .tar.xz (Xiaomi fastboot), payload.bin, *.new.dat[.br],
*.img / *.img.lz4 / sparse images / *_sparsechunk.N / super.img.
"""
from __future__ import annotations

import os
import re
import tarfile
import zipfile
from dataclasses import dataclass, field

from . import lpunpack
from .detect import LZ4_FRAME_MAGIC, classify_file_head, classify_raw
from .payload import Payload
from .sdat2img import BrotliStream, image_size, sdat2img
from .sparse import SPARSE_MAGIC, SparseReader
from .util import CHUNK, KitchenError, SubFile, copy_stream, human

# Partitions that usually carry apps / configuration and are selected by default.
INTERESTING = {
    "system", "system_ext", "product", "vendor", "odm", "oem", "cust", "preload", "prism",
    "optics", "mi_ext", "hw_product", "version", "preas", "preavs", "india", "carrier",
    "odm_ext", "vendor_ext", "product_h", "system_other", "reserve", "oplus_product",
}
FS_KINDS = {"ext4", "erofs"}

IMAGE_NAME_RE = re.compile(
    r"(\.img(\.ext4)?(\.lz4)?|\.ext4(\.lz4)?|\.lz4|_sparsechunk\.\d+|\.img\.\d+|^payload\.bin)$",
    re.I)
NESTED_RE = re.compile(r"\.(zip|tar|tar\.md5|tgz|tar\.gz|tar\.xz|tar\.bz2|txz)$", re.I)
SKIP_RE = re.compile(r"(^|/)(meta-inf|__macosx)/", re.I)


def is_interesting(name: str) -> bool:
    n = name.lower()
    if n.endswith(("_a", "_b")):
        n = n[:-2]
    return n in INTERESTING or n.startswith("my_")


def part_name(member: str) -> str:
    """'images/system.img.ext4.lz4' -> 'system', 'super.img_sparsechunk.3' -> 'super'."""
    b = os.path.basename(member.replace("\\", "/"))
    b = re.sub(r"_sparsechunk\.\d+$", "", b, flags=re.I)
    while True:
        nb = re.sub(r"\.(lz4|ext4|img|br|bin|raw|new\.dat|unsparse)$", "", b, flags=re.I)
        if nb == b:
            break
        b = nb
    return b or "image"


# ----------------------------------------------------------------------------- openers
class Opener:
    seekable = False
    size = None
    desc = ""

    def open(self):
        raise NotImplementedError


class FileOpener(Opener):
    seekable = True

    def __init__(self, path):
        self.path = path
        self.size = os.path.getsize(path)
        self.desc = os.path.basename(path)

    def open(self):
        return open(self.path, "rb")


class _OwnedSubFile(SubFile):
    def close(self):
        self.f.close()


class SliceOpener(Opener):
    """A byte range of a file on disk (tar member, stored zip member)."""
    seekable = True

    def __init__(self, path, offset, size, desc):
        self.path, self.offset, self.size, self.desc = path, offset, size, desc

    def open(self):
        return _OwnedSubFile(open(self.path, "rb"), self.offset, self.size)


class _ZipStream:
    def __init__(self, z, f):
        self.z, self.f = z, f

    def read(self, n=-1):
        return self.f.read(n)

    def seekable(self):
        return False

    def close(self):
        self.f.close()
        self.z.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class ZipMemberOpener(Opener):
    def __init__(self, path, info: zipfile.ZipInfo):
        self.path, self.member, self.size = path, info.filename, info.file_size
        self.desc = f"{os.path.basename(path)} > {info.filename}"

    def open(self):
        z = zipfile.ZipFile(self.path)
        return _ZipStream(z, z.open(self.member))


def zip_opener(path, info: zipfile.ZipInfo) -> Opener:
    if info.compress_type == zipfile.ZIP_STORED:
        with open(path, "rb") as fh:
            fh.seek(info.header_offset)
            lh = fh.read(30)
        import struct
        nl, el = struct.unpack("<HH", lh[26:30])
        return SliceOpener(path, info.header_offset + 30 + nl + el, info.file_size,
                           f"{os.path.basename(path)} > {info.filename}")
    return ZipMemberOpener(path, info)


class _Prepend:
    def __init__(self, data, f):
        self.data, self.f = data, f

    def read(self, n=-1):
        if self.data:
            if n is None or n < 0:
                out, self.data = self.data + self.f.read(), b""
                return out
            out, self.data = self.data[:n], self.data[n:]
            if len(out) < n:
                out += self.f.read(n - len(out))
            return out
        return self.f.read(n)

    def seekable(self):
        return False

    def close(self):
        self.f.close()


class OpenedImage:
    """Opens an image through its layers: [lz4] -> [sparse] -> raw stream."""

    def __init__(self, opener: Opener):
        self.opener = opener
        base = opener.open()
        self._base = base
        magic = base.read(4)
        self.lz4 = magic == LZ4_FRAME_MAGIC
        s = _Prepend(magic, base)
        if self.lz4:
            import lz4.frame
            s = lz4.frame.LZ4FrameFile(s, mode="rb")
            self._lz = s
        first = s.read(28)
        self.sparse = len(first) >= 4 and int.from_bytes(first[:4], "little") == SPARSE_MAGIC
        s = _Prepend(first, s)
        if self.sparse:
            self.reader = SparseReader(s)
            self.size = self.reader.size
        else:
            self.reader = lpunpack.RawStream(s)
            self.size = None if self.lz4 else opener.size
        self.random_access = opener.seekable and not self.lz4 and not self.sparse

    def close(self):
        try:
            if self.lz4:
                self._lz.close()
        finally:
            self._base.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def write_raw(opener: Opener, out_path: str, job=None, label=""):
    """Write the fully decoded raw image behind `opener` to out_path."""
    with OpenedImage(opener) as oi, open(out_path, "wb") as out:
        total = oi.size or 0
        if oi.sparse:
            oi.reader.write_to(out, job, label)
            out.seek(0, 2)
            if out.tell() < oi.size:
                out.truncate(oi.size)
        else:
            done = 0
            while True:
                data = oi.reader.read(CHUNK)
                if not data:
                    break
                out.write(data)
                done += len(data)
                if job:
                    job.check()
                    job.progress(done, total or done, label)


# ----------------------------------------------------------------------------- sources
@dataclass
class Item:
    name: str
    size: int | None
    kind: str
    origin: str
    source: "Source"
    key: str
    selected: bool = False
    logical: bool | None = None   # True = lives inside super (flashed from fastbootd)

    @property
    def size_text(self):
        return human(self.size) if self.size else "?"


class Source:
    def items(self) -> list[Item]:
        return []

    def extract(self, keys, outdir, job) -> list[tuple[str, str]]:
        raise NotImplementedError


class ImageSource(Source):
    def __init__(self, name, opener: Opener, kind, size):
        self.name, self.opener, self.kind, self.size = name, opener, kind, size

    def items(self):
        return [Item(self.name, self.size, self.kind, self.opener.desc, self, self.name,
                     is_interesting(self.name) and self.kind in FS_KINDS | {"raw", "f2fs"})]

    def extract(self, keys, outdir, job):
        # A plain raw image file on disk can be used in place - no copy needed.
        if isinstance(self.opener, FileOpener):
            with OpenedImage(self.opener) as oi:
                in_place = oi.random_access
            if in_place:
                return [(self.name, self.opener.path)]
        out = os.path.join(outdir, self.name + ".img")
        job.log(f"Unpacking {self.opener.desc} -> {os.path.basename(out)}")
        write_raw(self.opener, out, job, f"unpack {self.name}")
        return [(self.name, out)]


class SparseChunkSource(Source):
    def __init__(self, name, openers, kind, size):
        self.name, self.openers, self.kind, self.size = name, openers, kind, size

    def items(self):
        return [Item(self.name, self.size, self.kind,
                     f"{self.openers[0].desc} (+{len(self.openers) - 1} chunks)", self, self.name,
                     is_interesting(self.name))]

    def materialize(self, out, job):
        with open(out, "wb") as fh:
            size = 0
            for i, op in enumerate(self.openers):
                job.log(f"  sparse chunk {i + 1}/{len(self.openers)}: {op.desc}")
                with OpenedImage(op) as oi:
                    if not oi.sparse:
                        raise KitchenError(f"{op.desc} is not a sparse chunk")
                    oi.reader.write_to(fh, job, f"{self.name} chunk {i + 1}")
                    size = max(size, oi.size)
            fh.seek(0, 2)
            if fh.tell() < size:
                fh.truncate(size)

    def extract(self, keys, outdir, job):
        out = os.path.join(outdir, self.name + ".img")
        self.materialize(out, job)
        return [(self.name, out)]


class SuperSource(Source):
    def __init__(self, opener: Opener | None, meta: lpunpack.LpMetadata, origin: str,
                 chunks: SparseChunkSource | None = None):
        self.opener, self.meta, self.origin, self.chunks = opener, meta, origin, chunks

    def items(self):
        out = []
        for p in self.meta.partitions:
            if not p.extents:
                continue
            out.append(Item(p.out_name, p.size, "logical", f"{self.origin} > {p.name}", self,
                            p.out_name, is_interesting(p.out_name), logical=True))
        return out

    def extract(self, keys, outdir, job):
        names = set(keys)
        job.log(f"Extracting from super: {', '.join(sorted(names))}")
        if self.chunks:
            raw = os.path.join(outdir, "_super_raw.img")
            job.log("Merging super sparse chunks (temporary raw super)...")
            self.chunks.materialize(raw, job)
            try:
                with open(raw, "rb") as f:
                    return lpunpack.extract_seekable(f, self.meta, names, outdir, job)
            finally:
                os.remove(raw)
        with OpenedImage(self.opener) as oi:
            if oi.random_access:
                f = self.opener.open()
                try:
                    return lpunpack.extract_seekable(f, self.meta, names, outdir, job)
                finally:
                    f.close()
            head = oi.reader.read(8192)
            need = lpunpack.metadata_bytes_needed(head)
            if need > len(head):
                head += oi.reader.read(need - len(head))
            return lpunpack.extract_stream(oi.reader, head, self.meta, names, outdir, job)


class PayloadSource(Source):
    def __init__(self, path, member=None, origin=""):
        self.path, self.member = path, member
        self.origin = origin or os.path.basename(path)
        p = Payload(path, member)
        self.parts = [(pu.name, pu.size, p.is_incremental(pu)) for pu in p.partitions]
        self.dynamic = set(p.dynamic_partitions)
        p.close()

    def items(self):
        return [Item(n, s, "payload" + (" (delta!)" if inc else ""),
                     f"{self.origin} > {n}", self, n, is_interesting(n) and not inc,
                     logical=(n in self.dynamic) if self.dynamic else None)
                for n, s, inc in self.parts]

    def extract(self, keys, outdir, job):
        p = Payload(self.path, self.member)
        res = []
        try:
            for pu in p.partitions:
                if pu.name in keys:
                    out = os.path.join(outdir, pu.name + ".img")
                    job.log(f"payload.bin -> {pu.name}.img ({human(pu.size)})")
                    p.extract(pu, out, job)
                    res.append((pu.name, out))
        finally:
            p.close()
        return res


class SdatSource(Source):
    def __init__(self, name, tl_text, dat_opener: Opener, brotli: bool):
        self.name, self.tl_text, self.dat, self.brotli = name, tl_text, dat_opener, brotli
        self.size = image_size(tl_text)

    def items(self):
        return [Item(self.name, self.size, "new.dat" + (".br" if self.brotli else ""),
                     self.dat.desc, self, self.name, is_interesting(self.name))]

    def extract(self, keys, outdir, job):
        out = os.path.join(outdir, self.name + ".img")
        job.log(f"sdat2img: {self.dat.desc} -> {self.name}.img")
        f = self.dat.open()
        try:
            stream = BrotliStream(f) if self.brotli else f
            sdat2img(self.tl_text, stream, out, job, f"sdat2img {self.name}")
        finally:
            f.close()
        return [(self.name, out)]


# ----------------------------------------------------------------------------- scanning
class Scanner:
    def __init__(self, workdir: str, job):
        self.workdir = workdir
        self.job = job
        self.sources: list[Source] = []
        self.unpack_dir = os.path.join(workdir, "_firmware")
        self.ota_metadata: dict = {}   # META-INF/com/android/metadata of an OTA zip

    # entry point
    def scan(self, path: str) -> list[Item]:
        path = os.path.abspath(path)
        if os.path.isdir(path):
            self.scan_dir(path)
        else:
            self.scan_file(path)
        items = []
        seen = {}
        for s in self.sources:
            for it in s.items():
                base = it.name
                if base in seen:
                    seen[base] += 1
                    it.name = f"{base}_{seen[base]}"
                else:
                    seen[base] = 1
                items.append(it)
        return items

    def scan_dir(self, d: str, depth=0):
        entries = []
        work = os.path.normcase(os.path.abspath(self.workdir)) + os.sep
        inside_work = (os.path.normcase(os.path.abspath(d)) + os.sep).startswith(work)
        for root, dirs, files in os.walk(d):
            if not inside_work and (os.path.normcase(os.path.abspath(root)) + os.sep).startswith(work):
                dirs[:] = []
                continue
            rel_depth = os.path.relpath(root, d).count(os.sep)
            if rel_depth >= 3:
                dirs[:] = []
            for fn in files:
                entries.append(os.path.join(root, fn))
        self._scan_members(
            [(os.path.relpath(p, d).replace("\\", "/"), FileOpener(p)) for p in entries],
            nested_extract=lambda rel: os.path.join(d, rel),
            container=d)

    def scan_file(self, path: str):
        with open(path, "rb") as f:
            head = f.read(8192)
        kind = classify_file_head(head)
        low = path.lower()
        self.job.log(f"Scanning {os.path.basename(path)} [{kind}]")
        if kind == "zip":
            return self.scan_zip(path)
        if kind == "tar" or low.endswith((".tar", ".tar.md5")):
            return self.scan_tar(path)
        if kind in ("gzip", "xz") or low.endswith((".tgz", ".tar.gz", ".tar.xz", ".tar.bz2", ".txz")):
            return self.scan_compressed_tar(path)
        if kind == "payload":
            self.sources.append(PayloadSource(path))
            return
        self.scan_image(FileOpener(path), part_name(path))

    def scan_zip(self, path: str):
        z = zipfile.ZipFile(path)
        infos = [i for i in z.infolist() if not i.is_dir()]
        z.close()
        names = {i.filename: i for i in infos}
        meta = names.get("META-INF/com/android/metadata")
        if meta and not self.ota_metadata:
            with zipfile.ZipFile(path) as zz:
                text = zz.read(meta).decode("utf-8", "replace")
            self.ota_metadata = dict(l.split("=", 1) for l in text.splitlines() if "=" in l)
        # A/B OTA
        for i in infos:
            if os.path.basename(i.filename) == "payload.bin":
                if i.compress_type == zipfile.ZIP_STORED:
                    self.sources.append(PayloadSource(path, i.filename,
                                                      f"{os.path.basename(path)} > payload.bin"))
                else:
                    out = self._extract_member_zip(path, i)
                    self.sources.append(PayloadSource(out))
        members = []
        for i in infos:
            if SKIP_RE.search(i.filename) or os.path.basename(i.filename) == "payload.bin":
                continue
            members.append((i.filename, zip_opener(path, i)))

        def nested(rel):
            return self._extract_member_zip(path, names[rel])
        self._scan_members(members, nested, path)

    def scan_tar(self, path: str):
        members = []
        nested_infos = {}
        try:
            with tarfile.open(path, "r:") as t:
                for m in t:
                    if m.isfile():
                        op = SliceOpener(path, m.offset_data, m.size,
                                         f"{os.path.basename(path)} > {m.name}")
                        members.append((m.name, op))
                        nested_infos[m.name] = op
        except tarfile.ReadError as e:
            if not members:
                raise KitchenError(f"Cannot read tar archive: {e}")

        def nested(rel):
            op = nested_infos[rel]
            out = os.path.join(self.unpack_dir, os.path.basename(rel))
            os.makedirs(self.unpack_dir, exist_ok=True)
            with op.open() as src, open(out, "wb") as dst:
                copy_stream(src, dst, op.size, self.job, op.size, 0, f"extract {rel}")
            return out
        self._scan_members(members, nested, path)

    def scan_compressed_tar(self, path: str):
        """Compressed tarballs cannot be randomly accessed: pull image members out first."""
        stem = re.sub(r"\.(tgz|tar\.gz|tar\.xz|tar\.bz2|txz|gz|xz)$", "", os.path.basename(path), flags=re.I)
        out_dir = os.path.join(self.unpack_dir, stem)
        os.makedirs(out_dir, exist_ok=True)
        self.job.log(f"Decompressing image files from {os.path.basename(path)} (one pass)...")
        total = os.path.getsize(path)
        raw_f = open(path, "rb")
        try:
            with tarfile.open(fileobj=raw_f, mode="r|*") as t:
                for m in t:
                    self.job.check()
                    if not m.isfile():
                        continue
                    bn = os.path.basename(m.name)
                    if not (IMAGE_NAME_RE.search(bn) or NESTED_RE.search(bn)
                            or bn.endswith((".transfer.list", ".new.dat", ".new.dat.br"))):
                        continue
                    dst_path = os.path.join(out_dir, bn)
                    self.job.log(f"  {m.name} ({human(m.size)})")
                    src = t.extractfile(m)
                    with open(dst_path, "wb") as dst:
                        while True:
                            chunk = src.read(CHUNK)
                            if not chunk:
                                break
                            dst.write(chunk)
                            self.job.check()
                            self.job.progress(raw_f.tell(), total, f"decompress {bn}")
        finally:
            raw_f.close()
        self.scan_dir(out_dir)

    # shared logic for zip / tar / folder members
    def _scan_members(self, members, nested_extract, container):
        chunks: dict[str, list] = {}
        sdat = {}
        for rel, op in members:
            self.job.check()
            bn = os.path.basename(rel)
            low = bn.lower()
            if SKIP_RE.search(rel):
                continue
            m = re.search(r"_sparsechunk\.(\d+)$", low)
            if m:
                chunks.setdefault(part_name(rel), []).append((int(m.group(1)), op))
                continue
            if low.endswith((".transfer.list", ".new.dat", ".new.dat.br")):
                base = re.sub(r"\.(transfer\.list|new\.dat\.br|new\.dat)$", "", rel, flags=re.I)
                sdat.setdefault(base, {})[_sdat_kind(low)] = op
                continue
            if NESTED_RE.search(low):
                if isinstance(op, FileOpener):
                    self.scan_file(op.path)
                else:
                    self.job.log(f"Extracting nested archive {rel} ...")
                    self.scan_file(nested_extract(rel))
                continue
            if low == "payload.bin" and isinstance(op, FileOpener):
                self.sources.append(PayloadSource(op.path))
                continue
            if IMAGE_NAME_RE.search(low):
                try:
                    self.scan_image(op, part_name(rel))
                except KitchenError as e:
                    self.job.warn(f"Skipping {rel}: {e}")
                except Exception as e:  # corrupt / odd member - keep scanning the rest
                    self.job.warn(f"Skipping {rel}: {type(e).__name__}: {e}")
        for base, d in sdat.items():
            if "tl" in d and ("dat" in d or "br" in d):
                with d["tl"].open() as f:
                    tl = f.read().decode("utf-8", "replace")
                dat = d.get("br") or d["dat"]
                self.sources.append(SdatSource(part_name(base), tl, dat, "br" in d))
        for name, lst in chunks.items():
            lst.sort(key=lambda x: x[0])
            ops = [o for _, o in lst]
            self.scan_chunks(name, ops)

    def scan_chunks(self, name, ops):
        with OpenedImage(ops[0]) as oi:
            if not oi.sparse:
                raise KitchenError(f"{ops[0].desc}: sparsechunk is not a sparse image")
            head = oi.reader.read(8192)
            kind = classify_raw(head)
            size = oi.size
            if kind == "super":
                need = lpunpack.metadata_bytes_needed(head)
                head += oi.reader.read(need - len(head)) if need > len(head) else b""
        group = SparseChunkSource(name, ops, kind, size)
        if kind == "super":
            meta = lpunpack.parse_metadata(head)
            self.sources.append(SuperSource(None, meta, f"{ops[0].desc} (+chunks)", chunks=group))
        else:
            self.sources.append(group)

    def scan_image(self, op: Opener, name: str):
        with OpenedImage(op) as oi:
            head = oi.reader.read(8192)
            kind = classify_raw(head)
            size = oi.size
            if kind == "super":
                need = lpunpack.metadata_bytes_needed(head)
                if need > len(head):
                    head += oi.reader.read(need - len(head))
        if kind == "super":
            meta = lpunpack.parse_metadata(head)
            self.job.log(f"  super: {op.desc} -> "
                         f"{', '.join(p.out_name for p in meta.partitions if p.extents)}")
            self.sources.append(SuperSource(op, meta, op.desc))
            return
        if kind == "payload" and isinstance(op, FileOpener):
            self.sources.append(PayloadSource(op.path))
            return
        if kind == "zip":
            return
        self.sources.append(ImageSource(name, op, kind, size))

    def _extract_member_zip(self, path, info: zipfile.ZipInfo) -> str:
        os.makedirs(self.unpack_dir, exist_ok=True)
        out = os.path.join(self.unpack_dir, os.path.basename(info.filename))
        if os.path.exists(out) and os.path.getsize(out) == info.file_size:
            return out
        self.job.log(f"Extracting {info.filename} from zip ({human(info.file_size)})...")
        with zipfile.ZipFile(path) as z, z.open(info) as src, open(out, "wb") as dst:
            copy_stream(src, dst, info.file_size, self.job, info.file_size, 0,
                        f"extract {os.path.basename(info.filename)}")
        return out


def _sdat_kind(low: str) -> str:
    if low.endswith(".transfer.list"):
        return "tl"
    if low.endswith(".br"):
        return "br"
    return "dat"
