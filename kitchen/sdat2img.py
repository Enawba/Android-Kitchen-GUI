"""Convert block-based OTA images (*.new.dat[.br] + *.transfer.list) to raw images."""
from __future__ import annotations

from .util import KitchenError

BLOCK = 4096


def _rangeset(s: str):
    nums = [int(x) for x in s.split(",")]
    if nums[0] != len(nums) - 1:
        raise KitchenError("Bad range set in transfer list")
    return [(nums[i], nums[i + 1]) for i in range(1, len(nums), 2)]


def parse_transfer_list(text: str):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    version = int(lines[0])
    total_blocks = int(lines[1])
    body = lines[4:] if version >= 2 else lines[2:]
    cmds = []
    max_block = total_blocks
    for line in body:
        parts = line.split(" ")
        cmd = parts[0]
        if cmd in ("new", "erase", "zero"):
            rs = _rangeset(parts[1])
            cmds.append((cmd, rs))
            for _, e in rs:
                max_block = max(max_block, e)
        elif cmd in ("move", "bsdiff", "imgdiff", "stash", "free"):
            raise KitchenError("Incremental block OTA (transfer list with diffs) is not supported")
        elif cmd[0].isdigit():
            continue
        else:
            raise KitchenError(f"Unknown transfer list command: {cmd}")
    return version, max_block, cmds


class BrotliStream:
    def __init__(self, f):
        import brotli
        self.f, self.d, self.buf = f, brotli.Decompressor(), b""

    def read(self, n):
        while len(self.buf) < n:
            chunk = self.f.read(1 << 20)
            if not chunk:
                break
            self.buf += self.d.process(chunk)
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def close(self):
        self.f.close()


def image_size(transfer_list_text: str) -> int:
    return parse_transfer_list(transfer_list_text)[1] * BLOCK


def sdat2img(transfer_list_text: str, dat_stream, out_path: str, job=None, label=""):
    """dat_stream must already be brotli-decoded if needed (see BrotliStream)."""
    _ver, max_block, cmds = parse_transfer_list(transfer_list_text)
    total = sum(e - s for c, rs in cmds if c == "new" for s, e in rs) or 1
    done = 0
    with open(out_path, "wb") as out:
        out.truncate(max_block * BLOCK)
        for cmd, rs in cmds:
            if cmd != "new":
                continue
            for s, e in rs:
                out.seek(s * BLOCK)
                for blk in range(s, e, 256):
                    n = min(256, e - blk)
                    data = dat_stream.read(n * BLOCK)
                    if len(data) != n * BLOCK:
                        raise KitchenError("new.dat ended early")
                    out.write(data)
                    done += n
                    if job:
                        job.check()
                        job.progress(done, total, label)
