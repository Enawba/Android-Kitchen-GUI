"""EROFS reader conformance test against images built by erofs-utils' mkfs.erofs.

    python tests/erofs_test.py <path-to-mkfs.erofs.exe> <scratch-dir>

Builds a varied source tree, makes one image per mkfs option set, and checks that
every file, symlink, mode, uid/gid and xattr read by kitchen.erofs matches.
"""
import hashlib
import os
import random
import shutil
import stat
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from kitchen.erofs import ErofsReader  # noqa: E402
from kitchen.fsbase import capabilities_from_xattr  # noqa: E402

CONFIGS = [
    ("plain", []),
    ("lz4", ["-zlz4"]),
    ("lz4hc", ["-zlz4hc,level=9"]),
    ("lz4hc-bigpcl", ["-zlz4hc", "-C65536"]),
    ("lz4hc-bigpcl256k", ["-zlz4hc,level=12", "-C262144"]),
    ("lz4-ztailpacking", ["-zlz4hc", "-Eztailpacking"]),
    ("lz4-bigpcl-ztail", ["-zlz4hc", "-C131072", "-Eztailpacking"]),
    ("lz4-fragments", ["-zlz4hc", "-Efragments"]),
    ("lz4-all-fragments", ["-zlz4hc", "-Eall-fragments"]),
    ("lz4-dedupe", ["-zlz4hc", "-Ededupe"]),
    ("lz4-frag-dedupe", ["-zlz4hc", "-C65536", "-Efragments,dedupe"]),
    ("lz4-legacy", ["-zlz4hc", "-Elegacy-compress"]),
    ("lz4-legacy-bigpcl", ["-zlz4hc", "-Elegacy-compress", "-C65536"]),
    ("lz4-extended-inodes", ["-zlz4", "-Eforce-inode-extended"]),
    ("lz4-noinline", ["-zlz4", "-Enoinline_data"]),
    ("lzma", ["-zlzma"]),
    ("lzma-bigpcl", ["-zlzma,level=9", "-C131072"]),
    ("deflate", ["-zdeflate"]),
    ("deflate-bigpcl", ["-zdeflate,level=9", "-C65536"]),
    ("zstd", ["-zzstd"]),
    ("zstd-bigpcl-ztail", ["-zzstd,level=15", "-C131072", "-Eztailpacking"]),
    ("multi-alg", ["-zlz4hc:lzma"]),
    ("chunked", ["--chunksize=65536"]),
    ("chunked-4k", ["--chunksize=4096"]),
    ("lz4-maxextent", ["-zlz4hc", "--max-extent-bytes=16384"]),
    ("dir-compress", ["-zlz4hc", "--zD=1"]),
    ("android-like", ["-zlz4hc,level=9", "-C262144", "-Eztailpacking,fragments,dedupe"]),
    ("lz4-erofs-default-v1", ["-zlz4hc", "-Eforce-inode-compact"]),
]


def build_tree(root):
    rnd = random.Random(1234)
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root)
    files = {}

    def put(rel, data):
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        files[rel] = data

    words = [bytes(rnd.choice("abcdefghijklmnop") * rnd.randint(1, 9), "ascii") for _ in range(400)]

    def text(n):
        out = bytearray()
        while len(out) < n:
            out += rnd.choice(words) + b" "
        return bytes(out[:n])

    put("empty", b"")
    put("tiny.txt", b"hi\n")
    put("build.prop", b"ro.build.id=TEST\nro.product.model=Kitchen\n" * 3)
    for n in (1, 100, 4095, 4096, 4097, 8191, 8192, 12345, 65536, 65537, 300000):
        put(f"sizes/text_{n}", text(n))
        put(f"sizes/rand_{n}", rnd.randbytes(n))
    # partly compressible: random runs interleaved with text
    mixed = b"".join(rnd.randbytes(3000) + text(9000) for _ in range(300))
    put("app/Big/Big.apk", mixed)                    # ~3.6 MB
    put("app/Zeros/zeros.bin", bytes(5 << 20))        # highly compressible
    put("app/Rand/rand.bin", rnd.randbytes(2 << 20))  # incompressible
    # duplicated content (exercises dedupe)
    blk = text(200000)
    put("dup/a.bin", blk + rnd.randbytes(5000))
    put("dup/b.bin", blk + rnd.randbytes(7000))
    put("dup/c.bin", rnd.randbytes(100) + blk)
    # big directory spanning multiple dir blocks with long and unicode names
    for k in range(400):
        put(f"etc/permissions/privapp-permissions-com.example.package{k:04d}.xml",
            b"<permissions>" + str(k).encode() + b"</permissions>\n")
    put("etc/very_long_" + "n" * 200, b"long name")
    put("etc/ünicode_中文.txt", "unicode".encode())
    for d in range(30):
        put("/".join(["deep"] + [f"d{i}" for i in range(d % 8)] + [f"f{d}"]), text(d * 77 + 1))
    os.makedirs(os.path.join(root, "emptydir"))
    return files


FS_OVERRIDES = {
    "": "0 0 0755",
    "build.prop": "0 0 0600",
    "app": "0 0 0751",
    "app/Big/Big.apk": "1000 1000 0644",
    "tiny.txt": "1000 2000 0640 capabilities=0x1000",
    "sizes": "0 2000 0750",
}


def make_fs_config(root):
    """Android's canned fs_config must list every path (mount point prefixed)."""
    out = ["/ 0 0 0755"]
    for d, _dirs, fns in os.walk(root):
        rel = os.path.relpath(d, root).replace(os.sep, "/")
        rel = "" if rel == "." else rel
        entries = [(rel, True)] + [((rel + "/" if rel else "") + fn, False) for fn in fns]
        for r, is_dir in entries:
            spec = FS_OVERRIDES.get(r, "0 0 0755" if is_dir else "0 0 0644")
            out.append(("system/" + r if r else "system") + " " + spec)
    return "\n".join(out) + "\n"
FILE_CONTEXTS = """\
/system(/.*)? u:object_r:system_file:s0
/system/build\\.prop u:object_r:system_build_prop:s0
/system/app(/.*)? u:object_r:system_app_file:s0
"""


def sha(b):
    return hashlib.sha1(b).hexdigest()


def check_image(img, files, android_meta):
    r = ErofsReader(img)
    seen = {}
    try:
        for e in r.walk():
            if e.is_file:
                seen[e.path] = r.read_bytes(e)
            if android_meta:
                x = r.xattrs(e)
                if e.path == "build.prop":
                    assert stat.S_IMODE(e.mode) == 0o600, oct(e.mode)
                    ctx = x.get("security.selinux", b"").rstrip(b"\0")
                    assert ctx == b"u:object_r:system_build_prop:s0", x
                if e.path == "app/Big/Big.apk":
                    assert (e.uid, e.gid) == (1000, 1000), (e.uid, e.gid)
                    assert x.get("security.selinux", b"").startswith(b"u:object_r:system_app_file"), x
                if e.path == "tiny.txt":
                    assert (e.uid, e.gid, stat.S_IMODE(e.mode)) == (1000, 2000, 0o640)
                    assert capabilities_from_xattr(x.get("security.capability", b"")) == 0x1000, x
    finally:
        r.close()
    missing = set(files) - set(seen)
    extra = set(seen) - set(files)
    bad = [p for p in files if p in seen and seen[p] != files[p]]
    return missing, extra, bad


def main():
    mkfs, scratch = sys.argv[1], sys.argv[2]
    src = os.path.join(scratch, "erofs_src")
    files = build_tree(src)
    total_bytes = sum(len(v) for v in files.values())
    print(f"source tree: {len(files)} files, {total_bytes / 1e6:.1f} MB")
    meta_dir = os.path.join(scratch, "erofs_meta")
    os.makedirs(meta_dir, exist_ok=True)
    fsc = os.path.join(meta_dir, "fs_config")
    fcx = os.path.join(meta_dir, "file_contexts")
    with open(fsc, "w", newline="\n", encoding="utf-8") as f:
        f.write(make_fs_config(src))
    with open(fcx, "w", newline="\n", encoding="utf-8") as f:
        f.write(FILE_CONTEXTS)

    tmp = os.path.join(scratch, "tmp")
    os.makedirs(tmp, exist_ok=True)
    env = dict(os.environ, TMPDIR=tmp.replace("\\", "/"))  # Cygwin mkfs needs a temp dir
    failures = 0
    only = set(sys.argv[3:])
    for name, args in CONFIGS:
        if only and name not in only:
            continue
        img = os.path.join(scratch, f"erofs_{name}.img")
        if os.path.exists(img):
            os.remove(img)
        android = ["--mount-point=/system", f"--fs-config-file={fsc}", f"--file-contexts={fcx}",
                   "-T0"]
        cmd = [mkfs, "--quiet", *args, *android, img, src]
        p = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if p.returncode != 0:
            print(f"[FAIL] {name}: mkfs failed: {(p.stderr or p.stdout).strip()[-300:]}")
            failures += 1
            continue
        android_ok = True
        t = time.time()
        try:
            missing, extra, bad = check_image(img, files, android_ok)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[FAIL] {name}: {type(e).__name__}: {e}")
            failures += 1
            continue
        ok = not (missing or extra or bad)
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name:22} {os.path.getsize(img) / 1e6:6.1f} MB "
              f"{time.time() - t:5.1f}s meta={'yes' if android_ok else 'no'}"
              + ("" if ok else f" missing={sorted(missing)[:3]} extra={sorted(extra)[:3]} "
                               f"bad({len(bad)})={bad[:5]}"))
    print("\nALL EROFS TESTS PASSED" if not failures else f"\n{failures} CONFIG(S) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
