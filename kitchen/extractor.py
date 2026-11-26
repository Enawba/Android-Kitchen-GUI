"""Walk a partition filesystem and extract APKs, config files, metadata or everything."""
from __future__ import annotations

import fnmatch
import os
import re
import stat
from dataclasses import dataclass, field

from .apkinfo import apk_info
from .fsbase import XATTR_CAPS, XATTR_SELINUX, capabilities_from_xattr, open_fs
from .util import human, long_path, safe_name

DEFAULT_CONFIG_PATTERNS = [
    "*.prop",
    "*.rc",
    "*etc/*.xml",
    "*etc/*.conf",
    "*etc/*.cfg",
    "*etc/*.json",
    "*etc/*.txt",
    "*etc/*.ini",
    "*etc/*fstab*",
    "*etc/permissions/*",
    "*etc/sysconfig/*",
    "*etc/default-permissions/*",
    "*etc/vintf/*",
    "*etc/selinux/*",
    "*etc/init/*",
    "*manifest*.xml",
    "*compatibility_matrix*.xml",
    "*/overlay/*.xml",
]


@dataclass
class ExtractOptions:
    full: bool = False            # dump the complete filesystem
    apks: bool = True             # collect *.apk
    apex: bool = False            # collect *.apex / *.capex
    jars: bool = False            # collect framework *.jar
    configs: bool = True          # collect config files matching patterns
    metadata: bool = True         # write fs_config / file_contexts / symlink list
    apk_info: bool = True         # parse AndroidManifest for package / version
    config_patterns: list = field(default_factory=lambda: list(DEFAULT_CONFIG_PATTERNS))
    max_config_size: int = 32 * 1024 * 1024


@dataclass
class PartitionResult:
    partition: str
    fs_type: str = ""
    files: int = 0
    dirs: int = 0
    symlinks: int = 0
    apks: list = field(default_factory=list)
    configs: int = 0
    other: int = 0
    errors: list = field(default_factory=list)


_RE_SPECIAL = re.compile(r"([.^$*+?()\[\]{}|\\])")


def _fc_escape(path: str) -> str:
    return _RE_SPECIAL.sub(r"\\\1", path)


def _out_path(base: str, rel: str) -> str:
    parts = [safe_name(p) for p in rel.split("/") if p]
    return os.path.join(base, *parts)


def _open_out(path: str):
    lp = long_path(path)
    os.makedirs(os.path.dirname(lp), exist_ok=True)
    return open(lp, "wb")


def extract_partition(img_path: str, part: str, workspace: str, opts: ExtractOptions,
                      job) -> PartitionResult:
    res = PartitionResult(part)
    fs = open_fs(img_path)
    res.fs_type = fs.fs_type
    job.log(f"[{part}] {fs.fs_type} filesystem - walking tree ...")
    full_dir = os.path.join(workspace, "extracted", part)
    apk_dir = os.path.join(workspace, "apks", part)
    cfg_dir = os.path.join(workspace, "configs", part)
    meta_dir = os.path.join(workspace, "config")
    patterns = [p.strip().lower() for p in opts.config_patterns if p.strip()]

    fs_config, file_contexts, symlinks = [], [], []
    try:
        entries = list(fs.walk())
        total = len(entries) or 1
        for idx, e in enumerate(entries):
            if idx % 200 == 0:
                job.check()
                job.progress(idx, total, f"{part}: {e.path[-60:]}")
            rel = e.path
            low = rel.lower()
            mount_rel = f"{part}/{rel}" if rel else part

            xattrs = {}
            if opts.metadata:
                try:
                    xattrs = fs.xattrs(e)
                except Exception as ex:  # pragma: no cover - corrupt xattrs
                    res.errors.append(f"xattr {rel}: {ex}")
                caps = capabilities_from_xattr(xattrs.get(XATTR_CAPS, b""))
                line = f"{mount_rel} {e.uid} {e.gid} {stat.S_IMODE(e.mode):04o}"
                if caps:
                    line += f" capabilities=0x{caps:x}"
                fs_config.append(line)
                ctx = xattrs.get(XATTR_SELINUX, b"").rstrip(b"\0").decode("utf-8", "replace")
                if ctx:
                    file_contexts.append(f"/{_fc_escape(mount_rel)} {ctx}")
                    if not rel:
                        file_contexts.append(f"/{_fc_escape(part)}/ {ctx}")

            if e.is_dir:
                res.dirs += 1
                if opts.full:
                    os.makedirs(long_path(_out_path(full_dir, rel)), exist_ok=True)
                continue
            if e.is_link:
                res.symlinks += 1
                try:
                    target = fs.readlink(e)
                except Exception as ex:
                    target = f"<error {ex}>"
                symlinks.append(f"/{mount_rel} -> {target}")
                continue
            if not e.is_file:
                res.other += 1
                continue
            res.files += 1

            targets = []
            is_apk = opts.apks and low.endswith(".apk")
            is_apex = opts.apex and low.endswith((".apex", ".capex"))
            is_jar = opts.jars and low.endswith(".jar")
            if opts.full:
                targets.append(_out_path(full_dir, rel))
            if is_apk or is_apex or is_jar:
                targets.append(_out_path(apk_dir, rel))
            is_cfg = False
            if opts.configs and e.size <= opts.max_config_size and \
                    any(fnmatch.fnmatchcase(low, p) for p in patterns):
                is_cfg = True
                targets.append(_out_path(cfg_dir, rel))
            if not targets:
                continue
            try:
                outs = [_open_out(t) for t in targets]
                try:
                    for chunk in fs.iter_data(e):
                        for o in outs:
                            o.write(chunk)
                finally:
                    for o in outs:
                        o.close()
            except Exception as ex:
                msg = f"{rel}: {type(ex).__name__}: {ex}"
                res.errors.append(msg)
                job.warn(f"[{part}] failed to extract {msg}")
                continue
            if is_cfg:
                res.configs += 1
            if is_apk or is_apex or is_jar:
                saved = targets[1] if opts.full else targets[0]
                rec = {"partition": part, "path": "/" + mount_rel, "file": saved,
                       "size": e.size, "type": low.rsplit(".", 1)[-1],
                       "privileged": "/priv-app/" in "/" + low}
                if is_apk and opts.apk_info:
                    rec.update(apk_info(long_path(saved)))
                res.apks.append(rec)
        job.progress(total, total, f"{part}: done")
    finally:
        fs.close()

    if opts.metadata:
        os.makedirs(meta_dir, exist_ok=True)
        with open(os.path.join(meta_dir, f"{part}_fs_config"), "w", encoding="utf-8",
                  newline="\n") as f:
            f.write("\n".join(fs_config) + "\n")
        with open(os.path.join(meta_dir, f"{part}_file_contexts"), "w", encoding="utf-8",
                  newline="\n") as f:
            f.write("\n".join(dict.fromkeys(file_contexts)) + "\n")
        with open(os.path.join(meta_dir, f"{part}_symlinks.txt"), "w", encoding="utf-8",
                  newline="\n") as f:
            f.write("\n".join(symlinks) + ("\n" if symlinks else ""))
        with open(os.path.join(meta_dir, f"{part}_info.txt"), "w", encoding="utf-8") as f:
            f.write(f"partition={part}\nfilesystem={res.fs_type}\nimage={img_path}\n"
                    f"image_size={os.path.getsize(img_path)}\nfiles={res.files}\n"
                    f"directories={res.dirs}\nsymlinks={res.symlinks}\n")
    job.log(f"[{part}] {res.files} files, {res.dirs} dirs, {res.symlinks} symlinks | "
            f"{len(res.apks)} APK/APEX/JAR, {res.configs} config files"
            + (f" | {len(res.errors)} errors" if res.errors else ""),
            "ok" if not res.errors else "warn")
    return res


def write_apk_index(workspace: str, apks: list):
    import csv
    cols = ["partition", "path", "package", "versionName", "versionCode", "minSdkVersion",
            "targetSdkVersion", "privileged", "size", "type", "file"]
    path = os.path.join(workspace, "apk_list.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in apks:
            w.writerow(r)
    return path


__all__ = ["ExtractOptions", "PartitionResult", "extract_partition", "write_apk_index",
           "DEFAULT_CONFIG_PATTERNS", "human"]
