"""End-to-end job: firmware items -> raw images -> extracted APKs/configs."""
from __future__ import annotations

import os
import shutil
import time

from .detect import sniff_fs
from .extractor import ExtractOptions, extract_partition, write_apk_index
from .firmware import Item
from .util import Cancelled, KitchenError, human


def run(items: list[Item], workspace: str, opts: ExtractOptions, job,
        keep_images: bool = True, images_only: bool = False, flash: dict | None = None):
    """flash: None, or {"both_slots": bool, "info": dict, "all_names": [...]} to also
    write flash_all.bat for the unpacked images (images are then always kept)."""
    t0 = time.time()
    if flash is not None:
        keep_images = True
    images_dir = os.path.join(workspace, "images")
    tmp_dir = os.path.join(images_dir, "_tmp")
    os.makedirs(tmp_dir, exist_ok=True)

    # ---- stage 1: get raw partition images ------------------------------------------
    groups = {}
    for it in items:
        groups.setdefault(id(it.source), (it.source, []))[1].append(it)
    images = []          # (name, path, created_by_us)
    logical_flag = {}
    for src, its in groups.values():
        job.check()
        by_key = {it.key: it for it in its}
        try:
            produced = src.extract(list(by_key), tmp_dir, job)
        except (Cancelled, KeyboardInterrupt):
            raise
        except Exception as e:
            job.error(f"Failed to unpack {', '.join(by_key)}: {type(e).__name__}: {e}")
            continue
        for key, path in produced:
            name = by_key[key].name if key in by_key else key
            if key in by_key:
                logical_flag[name] = by_key[key].logical
            if os.path.dirname(os.path.abspath(path)) == os.path.abspath(tmp_dir):
                final = os.path.join(images_dir, name + ".img")
                if os.path.exists(final):
                    os.remove(final)
                shutil.move(path, final)
                images.append((name, final, True))
            else:
                images.append((name, path, False))  # used in place (user's own file)
            job.log(f"Image ready: {name} ({human(os.path.getsize(images[-1][1]))})", "ok")
    try:
        os.rmdir(tmp_dir)
    except OSError:
        pass

    if flash is not None:
        from .flashscript import FlashImage, generate, is_logical
        fimgs = [FlashImage(n, p, is_logical(n, logical_flag.get(n))) for n, p, _c in images]
        done = {n for n, _p, _c in images}
        incomplete = [n for n in flash.get("all_names", []) if n not in done]
        script, plan = generate(workspace, fimgs, flash.get("info"), flash.get("both_slots", True),
                                incomplete)
        job.log(f"Flash script written: {script}", "ok")
        job.log(f"  bootloader: {', '.join(i.name for i in plan.bootloader) or '-'}")
        job.log(f"  firmware  : {', '.join(i.name for i in plan.firmware) or '-'}")
        job.log(f"  logical   : {', '.join(i.name for i in plan.logical) or '-'}")
        job.log(f"  bootloader (after): {', '.join(i.name for i in plan.late) or '-'}")
        for n, why in plan.skipped:
            job.warn(f"  not in script: {n} ({why})")
        if incomplete:
            job.warn(f"  Flash script is INCOMPLETE - missing: {', '.join(incomplete)}")

    results = []
    if images_only:
        job.log(f"Done: {len(images)} images in {images_dir}", "ok")
        return results

    # ---- stage 2: filesystem extraction -----------------------------------------------
    all_apks = []
    for name, path, created in images:
        job.check()
        kind = sniff_fs(path)
        if kind not in ("ext4", "erofs"):
            job.log(f"[{name}] {kind} image - no filesystem to extract (image kept)", "warn")
            continue
        try:
            r = extract_partition(path, name, workspace, opts, job)
            results.append(r)
            all_apks.extend(r.apks)
        except (Cancelled, KeyboardInterrupt):
            raise
        except KitchenError as e:
            job.error(f"[{name}] {e}")
            continue
        except Exception as e:
            job.error(f"[{name}] unexpected error: {type(e).__name__}: {e}")
            continue
        if created and not keep_images:
            try:
                os.remove(path)
                job.log(f"[{name}] removed intermediate image")
            except OSError:
                pass

    if all_apks:
        p = write_apk_index(workspace, all_apks)
        job.log(f"APK index written: {p}", "ok")
    dt = time.time() - t0
    job.log(f"Finished in {dt / 60:.1f} min - {len(all_apks)} APK/APEX/JAR, "
            f"{sum(r.configs for r in results)} config files from {len(results)} partitions.",
            "ok")
    return results
