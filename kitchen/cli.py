"""Minimal command-line front-end (useful for scripting / debugging)."""
from __future__ import annotations

import argparse

from .extractor import ExtractOptions
from .firmware import Scanner
from .pipeline import run
from .util import Job, KitchenError


def main(argv=None):
    ap = argparse.ArgumentParser(prog="AndroidKitchen --cli",
                                 description="Extract APKs and configs from stock firmware")
    ap.add_argument("firmware")
    ap.add_argument("workspace")
    ap.add_argument("--all", action="store_true", help="process every partition")
    ap.add_argument("--parts", help="comma separated partition names")
    ap.add_argument("--full", action="store_true", help="full filesystem dump")
    ap.add_argument("--keep-images", action="store_true")
    ap.add_argument("--list", action="store_true", help="only list partitions")
    ap.add_argument("--flash-script", action="store_true",
                    help="unpack ALL partitions and write flash_all.bat (no file extraction)")
    ap.add_argument("--current-slot-only", action="store_true",
                    help="flash script: write firmware to the current slot only")
    a = ap.parse_args(argv)

    job = Job(log=lambda m, lvl="info": print(("" if lvl == "info" else f"[{lvl}] ") + m))
    try:
        sc = Scanner(a.workspace, job)
        items = sc.scan(a.firmware)
        for it in items:
            print(f"  {'*' if it.selected else ' '} {it.name:16} {it.kind:12} {it.size_text:>10}  {it.origin}")
        if a.list:
            return 0
        if a.parts:
            want = set(a.parts.split(","))
            sel = [it for it in items if it.name in want]
        else:
            sel = [it for it in items if a.all or it.selected]
        if a.flash_script:
            sel = items if not a.parts else sel
            run(sel, a.workspace, ExtractOptions(), job, images_only=True,
                flash={"both_slots": not a.current_slot_only, "info": sc.ota_metadata,
                       "all_names": [i.name for i in items]})
            return 0
        run(sel, a.workspace, ExtractOptions(full=a.full), job, keep_images=a.keep_images)
    except KitchenError as e:
        print(f"error: {e}")
        return 1
    return 0
