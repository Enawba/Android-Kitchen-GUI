"""Smoke test: builds synthetic firmware packages around small ext4 images and runs
the whole pipeline on them.

    python tests/smoke_test.py <dir-with-ext4.bin-and-ext4_symlink_test.bin>

The ext4 sample images come from fox-it/dissect.extfs (tests/data, gunzipped).
"""
import io
import lzma
import os
import struct
import sys
import tarfile
import tempfile
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kitchen.extractor import ExtractOptions  # noqa: E402
from kitchen.firmware import Scanner  # noqa: E402
from kitchen.pipeline import run  # noqa: E402
from kitchen.util import Job  # noqa: E402

BS = 4096


# --------------------------------------------------------------------------- builders
def make_sparse(raw: bytes) -> bytes:
    assert len(raw) % BS == 0
    blocks = [raw[i:i + BS] for i in range(0, len(raw), BS)]
    chunks = []
    i = 0
    while i < len(blocks):
        b = blocks[i]
        j = i
        if b == bytes(BS):
            while j < len(blocks) and blocks[j] == bytes(BS):
                j += 1
            chunks.append(struct.pack("<HHII", 0xCAC3, 0, j - i, 12))
        elif b == b[:4] * (BS // 4):
            while j < len(blocks) and blocks[j] == b:
                j += 1
            chunks.append(struct.pack("<HHII", 0xCAC2, 0, j - i, 16) + b[:4])
        else:
            while j < len(blocks) and blocks[j] != bytes(BS) and j - i < 64:
                j += 1
            data = b"".join(blocks[i:j])
            chunks.append(struct.pack("<HHII", 0xCAC1, 0, j - i, 12 + len(data)) + data)
        i = j
    hdr = struct.pack("<IHHHHIIII", 0xED26FF3A, 1, 0, 28, 12, BS, len(blocks), len(chunks), 0)
    return hdr + b"".join(chunks)


def make_super(parts: dict) -> bytes:
    """parts: name -> raw bytes. Second partition is split into two extents."""
    max_size = 65536
    data_start = 1 << 20
    pentries, eentries = b"", b""
    layout = []
    cursor = data_start
    ext_index = 0
    for idx, (name, raw) in enumerate(parts.items()):
        sectors = len(raw) // 512
        if idx == 1:  # split in two non-adjacent extents
            half = (sectors // 2) & ~7
            exts = [(half, cursor), (sectors - half, cursor + half * 512 + BS * 4)]
        else:
            exts = [(sectors, cursor)]
        first = ext_index
        for ns, phys in exts:
            eentries += struct.pack("<QIQI", ns, 0, phys // 512, 0)
            layout.append((phys, raw[:ns * 512] if phys == exts[0][1] else raw[exts[0][0] * 512:]))
            ext_index += 1
        pentries += name.encode().ljust(36, b"\0") + struct.pack("<IIII", 0, first, len(exts), 0)
        cursor = exts[-1][1] + exts[-1][0] * 512 + BS * 8
    # empty _b slot entry to exercise slot-suffix handling
    pentries += b"system_b".ljust(36, b"\0") + struct.pack("<IIII", 0, ext_index, 0, 0)
    groups = b"default".ljust(36, b"\0") + struct.pack("<IQ", 0, 0)
    devices = struct.pack("<QIIQ", data_start // 512, 0, 0, cursor) + b"super".ljust(36, b"\0") + \
        struct.pack("<I", 0)
    tables = pentries + eentries + groups + devices
    np = len(parts) + 1
    ne = ext_index
    hdr = struct.pack("<IHHI", 0x414C5030, 10, 2, 256) + bytes(32) + struct.pack("<I", len(tables)) \
        + bytes(32)
    hdr += struct.pack("<III", 0, np, 52)
    hdr += struct.pack("<III", np * 52, ne, 24)
    hdr += struct.pack("<III", np * 52 + ne * 24, 1, 48)
    hdr += struct.pack("<III", np * 52 + ne * 24 + 48, 1, 64)
    hdr = hdr.ljust(256, b"\0")
    geom = struct.pack("<II", 0x616C4467, 52) + bytes(32) + struct.pack("<III", max_size, 2, BS)
    img = bytearray(cursor)
    img[4096:4096 + len(geom)] = geom
    img[8192:8192 + len(geom)] = geom
    img[12288:12288 + len(hdr) + len(tables)] = hdr + tables
    for phys, raw in layout:
        img[phys:phys + len(raw)] = raw
    img += bytes((-len(img)) % BS)
    return bytes(img)


def pb_varint(v):
    out = b""
    while True:
        b = v & 0x7F
        v >>= 7
        out += bytes([b | (0x80 if v else 0)])
        if not v:
            return out


def pb_field(no, wt, val):
    key = pb_varint((no << 3) | wt)
    if wt == 0:
        return key + pb_varint(val)
    return key + pb_varint(len(val)) + val


def make_payload(parts: dict) -> bytes:
    blob = b""
    manifest = pb_field(3, 0, BS)
    for name, raw in parts.items():
        nblocks = len(raw) // BS
        ops = b""
        half = nblocks // 2
        # op1: xz first half, op2: plain REPLACE second half (minus last block), op3: ZERO
        segs = [(8, 0, half, lzma.compress(raw[:half * BS])),
                (0, half, nblocks - half - 1, raw[half * BS:(nblocks - 1) * BS]),
                (6, nblocks - 1, 1, b"")]
        for typ, start, cnt, data in segs:
            ext = pb_field(1, 0, start) + pb_field(2, 0, cnt)
            op = pb_field(1, 0, typ)
            if data:
                op += pb_field(2, 0, len(blob)) + pb_field(3, 0, len(data))
                blob += data
            op += pb_field(6, 2, ext)
            ops += pb_field(8, 2, op)
        info = pb_field(1, 0, len(raw))
        pu = pb_field(1, 2, name.encode()) + pb_field(7, 2, info) + ops
        manifest += pb_field(13, 2, pu)
    hdr = b"CrAU" + struct.pack(">QQI", 2, len(manifest), 0)
    return hdr + manifest + blob


def make_sdat(raw: bytes):
    import brotli
    nblocks = len(raw) // BS
    tl = f"4\n{nblocks}\n0\n0\nerase 2,0,{nblocks}\nnew 4,0,{nblocks // 2},{nblocks // 2},{nblocks}\n"
    return tl, brotli.compress(raw)


# --------------------------------------------------------------------------- checks
def run_case(label, fw_path, work, expect_parts):
    print(f"\n===== {label} =====")
    job = Job(log=lambda m, l="info": print(f"  [{l}] {m}"))
    sc = Scanner(work, job)
    items = sc.scan(fw_path)
    for it in items:
        print(f"  item: {it.name:12} {it.kind:10} {it.size_text:>10}  sel={it.selected}  {it.origin}")
    sel = [it for it in items if it.name in expect_parts]
    assert {it.name for it in sel} == set(expect_parts), [it.name for it in items]
    opts = ExtractOptions(full=True, config_patterns=["*test_file", "*.ext"])
    res = run(sel, work, opts, job, keep_images=False)
    assert len(res) == len(expect_parts), res
    for r in res:
        assert not r.errors, r.errors
    return res


def verify_outputs(work, ref_images):
    from kitchen.ext4 import Ext4Reader
    for part, ref in ref_images.items():
        fs = Ext4Reader(ref)
        for e in fs.walk():
            if e.is_file:
                p = os.path.join(work, "extracted", part, *e.path.split("/"))
                with open(p, "rb") as f:
                    assert f.read() == fs.read_bytes(e), p
        fs.close()
        assert os.path.exists(os.path.join(work, "config", f"{part}_fs_config"))
    print("  outputs verified OK")


def main():
    data = sys.argv[1]
    sys_raw = open(os.path.join(data, "ext4.bin"), "rb").read()
    ven_raw = open(os.path.join(data, "ext4_symlink_test.bin"), "rb").read()
    tmp = tempfile.mkdtemp(prefix="kitchen_test_")
    print("temp:", tmp)
    ref = {"system": os.path.join(data, "ext4.bin"),
           "vendor": os.path.join(data, "ext4_symlink_test.bin")}

    # 1) Samsung style: AP.tar.md5 containing super.img.lz4 (sparse super)
    import lz4.frame
    sup = make_super({"system_a": sys_raw, "vendor_a": ven_raw})
    sup_sparse = make_sparse(sup)
    ap = os.path.join(tmp, "AP_TEST.tar.md5")
    with tarfile.open(ap, "w") as t:
        for name, blob in [("super.img.lz4", lz4.frame.compress(sup_sparse)),
                           ("boot.img.lz4", lz4.frame.compress(b"ANDROID!" + bytes(8184)))]:
            ti = tarfile.TarInfo(name)
            ti.size = len(blob)
            t.addfile(ti, io.BytesIO(blob))
    with open(ap, "ab") as f:
        f.write(b"0123456789abcdef  AP_TEST.tar\n")
    w = os.path.join(tmp, "w1")
    run_case("Samsung tar.md5 / lz4 / sparse super", ap, w, ["system", "vendor"])
    verify_outputs(w, ref)

    # 2) A/B OTA zip with stored payload.bin
    ota = os.path.join(tmp, "ota.zip")
    with zipfile.ZipFile(ota, "w") as z:
        z.writestr(zipfile.ZipInfo("payload.bin"), make_payload({"system": sys_raw,
                                                                 "vendor": ven_raw}))
        z.writestr("payload_properties.txt", "FILE_HASH=x\n")
    w = os.path.join(tmp, "w2")
    run_case("A/B OTA payload.bin", ota, w, ["system", "vendor"])
    verify_outputs(w, ref)

    # 3) Block OTA: system.new.dat.br + vendor raw sparse image in a zip (deflated)
    tl, br = make_sdat(sys_raw)
    old = os.path.join(tmp, "old_ota.zip")
    with zipfile.ZipFile(old, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("system.transfer.list", tl)
        z.writestr("system.new.dat.br", br)
        z.writestr("vendor.img", make_sparse(ven_raw))
    w = os.path.join(tmp, "w3")
    run_case("Block OTA new.dat.br + sparse img", old, w, ["system", "vendor"])
    verify_outputs(w, ref)

    # 4) Raw super image file on disk (random access path)
    rs = os.path.join(tmp, "super.img")
    with open(rs, "wb") as f:
        f.write(sup)
    w = os.path.join(tmp, "w4")
    run_case("raw super.img", rs, w, ["system", "vendor"])
    verify_outputs(w, ref)

    print("\nALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
