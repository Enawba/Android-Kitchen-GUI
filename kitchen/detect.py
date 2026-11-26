"""Magic-number based detection of images and containers."""
from __future__ import annotations

import struct

LZ4_FRAME_MAGIC = b"\x04\x22\x4d\x18"
LZ4_LEGACY_MAGIC = b"\x02\x21\x4c\x18"
GZIP_MAGIC = b"\x1f\x8b"
ZIP_MAGIC = b"PK\x03\x04"
XZ_MAGIC = b"\xfd7zXZ\x00"
BZ2_MAGIC = b"BZh"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def classify_raw(head: bytes) -> str:
    """Classify a raw (non-sparse, uncompressed) image from its first ~8 KiB."""
    if len(head) >= 4100 and struct.unpack_from("<I", head, 4096)[0] == 0x616C4467:
        return "super"
    if len(head) >= 1084 and struct.unpack_from("<H", head, 1080)[0] == 0xEF53:
        return "ext4"
    if len(head) >= 1028:
        m = struct.unpack_from("<I", head, 1024)[0]
        if m == 0xE0F5E1E2:
            return "erofs"
        if m == 0xF2F52010:
            return "f2fs"
    if head[:8] == b"ANDROID!":
        return "boot"
    if head[:8] == b"VNDRBOOT":
        return "vendor_boot"
    if head[:4] == b"CrAU":
        return "payload"
    if head[:4] == b"hsqs":
        return "squashfs"
    if head[:4] == b"AVB0":
        return "vbmeta"
    if head[:4] == ZIP_MAGIC:
        return "zip"
    return "raw"


def classify_file_head(head: bytes) -> str:
    """Classify a file from its first bytes (containers + images)."""
    if head[:4] == LZ4_FRAME_MAGIC or head[:4] == LZ4_LEGACY_MAGIC:
        return "lz4"
    if head[:4] == b"\x3a\xff\x26\xed":
        return "sparse"
    if head[:4] == ZIP_MAGIC:
        return "zip"
    if head[:2] == GZIP_MAGIC:
        return "gzip"
    if head[:6] == XZ_MAGIC:
        return "xz"
    if head[:4] == ZSTD_MAGIC:
        return "zstd"
    if len(head) >= 263 and head[257:262] == b"ustar":
        return "tar"
    return classify_raw(head)


def sniff_fs(path: str) -> str:
    with open(path, "rb") as f:
        head = f.read(8192)
    k = classify_file_head(head)
    return k
