"""Generate a Windows fastboot flash script (flash_all.bat) for unpacked stock firmware.

Order used (matches known-working OnePlus 12 / OPPO / realme ColorOS fastboot scripts):
  1. bootloader: only `recovery` (each slot by name) so that the next step boots a stock
     fastbootd. Many OnePlus bootloaders do not answer `getvar has-slot:<part>` for
     init_boot / vendor_boot / dtbo, so nothing else is flashed from the bootloader early.
  2. fastbootd: the boot chain (boot, init_boot, vendor_boot, dtbo, vbmeta*) and every
     other physical firmware partition, both slots.
  3. fastbootd: the logical partitions that live inside super (current slot).
  4. bootloader: `modem` (OnePlus flashes it from the bootloader), optional data wipe,
     reboot.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

# Flashed from the bootloader BEFORE fastbootd (gives a stock fastbootd).
BOOTLOADER_FIRST = ["recovery"]
# Used instead when the firmware has no recovery partition (recovery lives in boot images).
BOOTLOADER_FIRST_NO_RECOVERY = ["boot", "vendor_boot", "init_boot"]
# Flashed first inside fastbootd, in this order; the rest of the firmware follows.
FASTBOOTD_FIRST = ["boot", "init_boot", "vendor_boot", "dtbo",
                   "vbmeta", "vbmeta_system", "vbmeta_vendor"]
# Flashed from the bootloader AFTER fastbootd.
BOOTLOADER_LAST = ["modem"]

# Fallback when the firmware does not say which partitions are dynamic.
KNOWN_LOGICAL = {"system", "system_ext", "product", "vendor", "odm", "vendor_dlkm", "odm_dlkm",
                 "system_dlkm", "mi_ext", "cust", "oem", "preload", "prism", "optics"}

# Never put into a script: these are per-device and must not be overwritten.
NEVER_FLASH = {"persist", "modemst1", "modemst2", "fsg", "fsc", "userdata", "metadata",
               "frp", "devinfo", "misc", "ocdt", "oplusreserve1", "super"}

_NAME_OK = re.compile(r"^[A-Za-z0-9_.\-]+$")


@dataclass
class FlashImage:
    name: str      # partition name as fastboot knows it (no slot suffix)
    path: str      # image file
    logical: bool


@dataclass
class FlashPlan:
    bootloader: list   # bootloader, before fastbootd
    firmware: list     # fastbootd, physical (boot chain first)
    logical: list      # fastbootd, inside super
    late: list         # bootloader, after fastbootd
    skipped: list      # (name, reason)


def is_logical(name: str, flag) -> bool:
    if flag is not None:
        return bool(flag)
    n = name.lower()
    return n in KNOWN_LOGICAL or n.startswith("my_")


def make_plan(images: list[FlashImage]) -> FlashPlan:
    bl, fw, lg, late, skipped = [], [], [], [], []
    by_name = {}
    for im in images:
        if im.name.lower() in NEVER_FLASH:
            skipped.append((im.name, "device-specific partition, never flashed"))
            continue
        if not _NAME_OK.match(im.name):
            skipped.append((im.name, "unusual partition name"))
            continue
        by_name[im.name] = im

    def take(names, into):
        for n in names:
            if n in by_name and not by_name[n].logical:
                into.append(by_name.pop(n))

    take(BOOTLOADER_FIRST if "recovery" in by_name else BOOTLOADER_FIRST_NO_RECOVERY, bl)
    take(BOOTLOADER_LAST, late)
    take(FASTBOOTD_FIRST, fw)
    for n in sorted(by_name):
        im = by_name[n]
        (lg if im.logical else fw).append(im)
    # system first, then the rest alphabetically (cosmetic, order does not matter in super)
    lg.sort(key=lambda i: (i.name != "system", i.name))
    return FlashPlan(bl, fw, lg, late, skipped)


def _echo(text: str) -> str:
    """A batch `echo` line with special characters escaped."""
    if not text:
        return "echo."
    t = text.replace("^", "^^").replace("%", "%%")
    for ch in "&<>|":
        t = t.replace(ch, "^" + ch)
    return "echo " + t


def _img_ref(path: str, images_dir: str) -> str:
    ap, ad = os.path.abspath(path), os.path.abspath(images_dir)
    if os.path.normcase(os.path.dirname(ap)) == os.path.normcase(ad):
        return "%IMG%\\" + os.path.basename(ap)
    return ap.replace("%", "%%")


def generate(workspace: str, images: list[FlashImage], info: dict | None = None,
             both_slots: bool = True, incomplete: list | None = None) -> tuple[str, FlashPlan]:
    """Write <workspace>/flash_all.bat (+ flash_plan.txt). Returns (script path, plan)."""
    info = info or {}
    plan = make_plan(images)
    images_dir = os.path.join(workspace, "images")
    device = info.get("pre-device", "") or info.get("product", "")
    build = info.get("post-build", "") or info.get("version", "")

    L = []
    a = L.append
    a("@echo off")
    a("setlocal EnableExtensions DisableDelayedExpansion")
    a("title Flash stock firmware")
    a('cd /d "%~dp0"')
    a('set "IMG=%~dp0images"')
    a('set "DRY="')
    a('if /i "%~1"=="--dry-run" set "DRY=1"')
    a("")
    a("rem ---- locate fastboot -------------------------------------------------------")
    a('set "FASTBOOT="')
    a('if defined KITCHEN_FASTBOOT set "FASTBOOT=%KITCHEN_FASTBOOT%"')
    a('if not defined FASTBOOT if exist "%~dp0platform-tools\\fastboot.exe" '
      'set "FASTBOOT=%~dp0platform-tools\\fastboot.exe"')
    a('if not defined FASTBOOT if exist "%~dp0fastboot.exe" set "FASTBOOT=%~dp0fastboot.exe"')
    a('if not defined FASTBOOT for /f "delims=" %%F in (\'where fastboot 2^>nul\') do '
      'if not defined FASTBOOT set "FASTBOOT=%%F"')
    a("if defined FASTBOOT goto :have_fastboot")
    a(_echo("fastboot.exe was not found."))
    a(_echo("Download Android SDK Platform-Tools from"))
    a(_echo("  https://developer.android.com/tools/releases/platform-tools"))
    a(_echo("and unzip it next to this script (so platform-tools\\fastboot.exe exists),"))
    a(_echo("or add it to PATH."))
    a("goto :fail")
    a(":have_fastboot")
    a("")
    a("rem ---- check that every image is present -------------------------------------")
    a('set "MISSING="')
    for im in plan.bootloader + plan.firmware + plan.logical + plan.late:
        ref = _img_ref(im.path, images_dir)
        a(f'if not exist "{ref}" echo   missing: {os.path.basename(im.path)} & set "MISSING=1"')
    a("if defined MISSING goto :fail")
    a("")
    a("cls")
    a(_echo("=" * 74))
    a(_echo("  STOCK FIRMWARE FLASH SCRIPT  (generated by Android Kitchen)"))
    a(_echo("=" * 74))
    if build:
        a(_echo(f"  Build     : {build}"))
    if device:
        a(_echo(f"  Made for  : {device}"))
    a(_echo(f"  Partitions: {len(plan.bootloader) + len(plan.late)} from bootloader, "
            f"{len(plan.firmware)} firmware + {len(plan.logical)} logical from fastbootd"))
    a(_echo(f"  Slots     : {'firmware written to BOTH slots' if both_slots else 'current slot only'}"))
    a("echo.")
    if incomplete:
        a(_echo("  !! WARNING: this script was generated from an INCOMPLETE selection."))
        a(_echo("  !! Not included: " + ", ".join(incomplete)[:200]))
        a(_echo("  !! Flashing a partial firmware can leave the phone unbootable."))
        a("echo.")
    a(_echo("  READ BEFORE CONTINUING:"))
    a(_echo("   - The phone must be in BOOTLOADER (fastboot) mode, connected by USB."))
    a(_echo("   - ANTI-ROLLBACK: never flash firmware older than what the phone has run."))
    a(_echo("     A lower anti-rollback level can HARD BRICK the device."))
    a(_echo("   - Make sure this firmware matches your exact model / region."))
    a(_echo("   - Keep the bootloader UNLOCKED until stock has booted successfully."))
    a(_echo("   - Back up your data: switching ROMs normally requires a data wipe."))
    a(_echo("   - Run with --dry-run to only print the commands."))
    a("echo.")
    a('set "ANS="')
    a('set /p "ANS=Type YES to continue: "')
    a('if /i not "%ANS%"=="YES" goto :cancel')
    a("")
    a("rem ---- device checks ---------------------------------------------------------")
    a('if defined DRY set "SLOT=a"')
    a("if defined DRY goto :checks_done")
    a(_echo("Waiting for a device in fastboot mode..."))
    a("call :wait_device || goto :fail")
    a("call :getvar is-userspace USERSPACE")
    a('if /i not "%USERSPACE%"=="yes" goto :in_bootloader')
    a(_echo("Device is in fastbootd - rebooting to the bootloader first..."))
    a("call :fb reboot bootloader")
    a("call :sleep 8")
    a("call :wait_device || goto :fail")
    a(":in_bootloader")
    a("call :getvar product PRODUCT")
    a("call :getvar current-slot SLOT")
    a("call :getvar unlocked UNLOCKED")
    if not both_slots:
        a("if defined SLOT goto :slot_ok")
        a(_echo("The phone did not report its current slot - cannot flash current-slot-only."))
        a("goto :fail")
        a(":slot_ok")
    a("echo.")
    a("echo   Connected device : %PRODUCT%   (current slot: %SLOT%, unlocked: %UNLOCKED%)")
    if device:
        a(_echo(f"  Firmware made for: {device}"))
    a('if /i "%UNLOCKED%"=="no" echo   !! The bootloader reports LOCKED - flashing will fail.')
    a('set "ANS="')
    a('set /p "ANS=Is this the right phone? Type YES to start flashing: "')
    a('if /i not "%ANS%"=="YES" goto :cancel')
    a(":checks_done")
    a("")

    a("rem ---- phase 1: bootloader ---------------------------------------------------")
    a("echo.")
    a(_echo("=== Phase 1/4: " + ", ".join(i.name for i in plan.bootloader)
            + " (bootloader) ==="))
    def physical(im):
        # Explicit slot names instead of --slot=all: fastboot aborts --slot=all when the
        # bootloader does not answer "has-slot:<part>" (OnePlus ABL does this for
        # init_boot / vendor_boot / dtbo ...), but flashing <part>_a / <part>_b works.
        # :flash_ab / :flash_cur ask "has-slot" themselves and fall back to the plain
        # name for single-slot partitions.
        ref = _img_ref(im.path, images_dir)
        sub = ":flash_ab" if both_slots else ":flash_cur"
        a(f'call {sub} {im.name} "{ref}" || goto :abort')

    for im in plan.bootloader:
        physical(im)
    a("")
    a("rem ---- phase 2: fastbootd, firmware ------------------------------------------")
    a("echo.")
    a(_echo("=== Rebooting to fastbootd ==="))
    a("call :fb reboot fastboot")
    a("if not defined DRY call :sleep 10")
    a("call :wait_userspace || goto :fail")
    a(_echo(f"=== Phase 2/4: boot chain + firmware ({len(plan.firmware)}) in fastbootd ==="))
    for im in plan.firmware:
        physical(im)
    a("")
    a("rem ---- phase 3: fastbootd, logical partitions in super ------------------------")
    a("echo.")
    a(_echo(f"=== Phase 3/4: logical partitions in super ({len(plan.logical)}) ==="))
    for im in plan.logical:
        a(f"call :delcow {im.name}")
    for im in plan.logical:
        a(f'call :flash {im.name} "{_img_ref(im.path, images_dir)}" "" || goto :abort')
    a("")
    a("rem ---- phase 4: back to the bootloader ---------------------------------------")
    a("echo.")
    a(_echo("=== Rebooting to the bootloader ==="))
    a("call :fb reboot bootloader")
    a("if not defined DRY call :sleep 8")
    a("call :wait_bootloader || goto :fail")
    if plan.late:
        a(_echo("=== Phase 4/4: " + ", ".join(i.name for i in plan.late) + " (bootloader) ==="))
        for im in plan.late:
            physical(im)
    a("")
    a("rem ---- wipe + reboot ---------------------------------------------------------")
    a('set "WIPEASK=0"')
    a(":ask_wipe")
    a("set /a WIPEASK+=1")
    a("if %WIPEASK% gtr 5 goto :no_answer")
    a("echo.")
    a(_echo("Wipe data (factory reset)? Strongly recommended when coming from another ROM."))
    a('set "ANS="')
    a('set /p "ANS=Wipe data? [Y/N]: "')
    a('if /i "%ANS%"=="Y" goto :do_wipe')
    a('if /i "%ANS%"=="N" goto :reboot')
    a("goto :ask_wipe")
    a(":do_wipe")
    a("call :fb -w")
    a(":reboot")
    a("call :fb reboot")
    a("echo.")
    a(_echo("All done. First boot can take several minutes."))
    a(_echo("Do NOT relock the bootloader until stock has booted and works."))
    a("if not defined DRY pause")
    a("exit /b 0")
    a("")

    a("rem ============================ subroutines ==================================")
    a(":fb")
    a("if defined DRY echo     [dry-run] fastboot %* & exit /b 0")
    a('call "%FASTBOOT%" %*')
    a("exit /b %errorlevel%")
    a("")
    a(":flash")
    a("rem %1 = partition  %2 = image  %3 = extra fastboot options")
    a('set "TRIES=0"')
    a(":flash_again")
    a("echo [flash] %~1")
    a('call :fb %~3 flash %~1 "%~2"')
    a("if not errorlevel 1 exit /b 0")
    a("echo [!] Flashing %~1 failed.")
    a(":flash_ask")
    a("set /a TRIES+=1")
    a("if %TRIES% gtr 5 exit /b 1")
    a('set "ANS="')
    a('set /p "ANS=    R = retry, S = skip this partition, A = abort: "')
    a('if /i "%ANS%"=="R" goto :flash_again')
    a('if /i "%ANS%"=="S" exit /b 0')
    a('if /i "%ANS%"=="A" exit /b 1')
    a("goto :flash_ask")
    a("")
    a(":flash_ab")
    a("rem physical partition -> both slots (plain name if the device says it has no slots)")
    a("if defined DRY goto :fab_both")
    a("call :hasslot %~1")
    a('if /i "%HASSLOT%"=="no" goto :fab_single')
    a(":fab_both")
    a('call :flash %~1_a "%~2" || exit /b 1')
    a('call :flash %~1_b "%~2" || exit /b 1')
    a("exit /b 0")
    a(":fab_single")
    a('call :flash %~1 "%~2"')
    a("exit /b %errorlevel%")
    a("")
    a(":flash_cur")
    a("rem physical partition -> current slot only")
    a("if defined DRY goto :fc_slot")
    a("call :hasslot %~1")
    a('if /i "%HASSLOT%"=="no" goto :fc_single')
    a(":fc_slot")
    a('call :flash %~1_%SLOT% "%~2"')
    a("exit /b %errorlevel%")
    a(":fc_single")
    a('call :flash %~1 "%~2"')
    a("exit /b %errorlevel%")
    a("")
    a(":hasslot")
    a("rem HASSLOT = yes / no / empty (device did not answer)")
    a('set "HASSLOT="')
    a('for /f "tokens=3 delims=: " %%a in (\'call "%FASTBOOT%" getvar has-slot:%~1 2^>^&1 '
      '^| findstr /b /c:"has-slot:%~1:"\') do set "HASSLOT=%%a"')
    a("exit /b 0")
    a("")
    a(":delcow")
    a("if defined DRY exit /b 0")
    a('call "%FASTBOOT%" delete-logical-partition %~1_a-cow >nul 2>&1')
    a('call "%FASTBOOT%" delete-logical-partition %~1_b-cow >nul 2>&1')
    a("exit /b 0")
    a("")
    a(":getvar")
    a('set "%~2="')
    a('for /f "tokens=1,2 delims=: " %%a in (\'call "%FASTBOOT%" getvar %~1 2^>^&1\') do '
      'if /i "%%a"=="%~1" set "%~2=%%b"')
    a("exit /b 0")
    a("")
    a(":wait_device")
    a('set "WAITS=0"')
    a(":wd_loop")
    a('for /f "tokens=2" %%a in (\'call "%FASTBOOT%" devices 2^>nul\') do '
      'if /i "%%a"=="fastboot" exit /b 0')
    a("set /a WAITS+=1")
    a("if %WAITS% geq 90 echo Timed out waiting for the device. & exit /b 1")
    a("call :sleep 2")
    a("goto :wd_loop")
    a("")
    a(":wait_userspace")
    a("if defined DRY exit /b 0")
    a('set "WAITS=0"')
    a(":wu_loop")
    a("call :getvar is-userspace USERSPACE")
    a('if /i "%USERSPACE%"=="yes" exit /b 0')
    a("set /a WAITS+=1")
    a("if %WAITS% geq 60 echo Device did not enter fastbootd. & exit /b 1")
    a("call :sleep 2")
    a("goto :wu_loop")
    a("")
    a(":wait_bootloader")
    a("if defined DRY exit /b 0")
    a('set "WAITS=0"')
    a(":wb_loop")
    a("call :wait_device || exit /b 1")
    a("call :getvar is-userspace USERSPACE")
    a('if /i not "%USERSPACE%"=="yes" exit /b 0')
    a("set /a WAITS+=1")
    a("if %WAITS% geq 60 echo Device did not return to the bootloader. & exit /b 1")
    a("call :sleep 2")
    a("goto :wb_loop")
    a("")
    a(":sleep")
    a("set /a PINGS=%~1+1")
    a("ping -n %PINGS% 127.0.0.1 >nul")
    a("exit /b 0")
    a("")
    a(":no_answer")
    a(_echo("No answer received - the phone was NOT wiped and NOT rebooted."))
    a(_echo("Run 'fastboot -w' (recommended) and then 'fastboot reboot' yourself."))
    a("exit /b 1")
    a(":abort")
    a("echo.")
    a(_echo("Aborted. The phone is now in a partially flashed state - do NOT reboot to system."))
    a(_echo("Fix the problem (cable/port/fastboot version) and run this script again."))
    a("if not defined DRY pause")
    a("exit /b 1")
    a(":cancel")
    a(_echo("Cancelled - nothing was flashed."))
    a("exit /b 1")
    a(":fail")
    a("if not defined DRY pause")
    a("exit /b 1")

    path = os.path.join(workspace, "flash_all.bat")
    with open(path, "w", encoding="ascii", errors="replace", newline="\r\n") as f:
        f.write("\n".join(L) + "\n")

    with open(os.path.join(workspace, "flash_plan.txt"), "w", encoding="utf-8") as f:
        f.write(f"Build: {build or '?'}\nDevice: {device or '?'}\n"
                f"Firmware partitions to both slots: {both_slots}\n\n")
        for title, lst in (("Phase 1 - bootloader", plan.bootloader),
                           ("Phase 2 - fastbootd (firmware)", plan.firmware),
                           ("Phase 3 - fastbootd (logical / super)", plan.logical),
                           ("Phase 4 - bootloader", plan.late)):
            f.write(f"{title}:\n")
            for im in lst:
                f.write(f"  {im.name:24} {os.path.basename(im.path)}\n")
            f.write("\n")
        if plan.skipped:
            f.write("Skipped:\n")
            for n, why in plan.skipped:
                f.write(f"  {n:24} {why}\n")
        if incomplete:
            f.write("\nNOT INCLUDED (not selected when unpacking):\n  " + ", ".join(incomplete) + "\n")
    return path, plan


def regenerate(workspace: str) -> tuple[str, FlashPlan]:
    """Rebuild flash_all.bat from an existing flash_plan.txt + images folder
    (no need to unpack the firmware again)."""
    plan_path = os.path.join(workspace, "flash_plan.txt")
    images_dir = os.path.join(workspace, "images")
    info, both, images, phase = {}, True, [], None
    with open(plan_path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if line.startswith("Build: "):
                info["post-build"] = line[7:].strip().replace("?", "")
            elif line.startswith("Device: "):
                info["pre-device"] = line[8:].strip().replace("?", "")
            elif line.startswith("Firmware partitions to both slots:"):
                both = line.split(":", 1)[1].strip() == "True"
            elif line.startswith("Phase "):
                phase = "logical" if "logical" in line else "physical"
            elif not line.strip() or not line.startswith("  "):
                phase = None if not line.startswith("Phase ") else phase
            elif phase:
                name, fn = line.split()[:2]
                images.append(FlashImage(name, os.path.join(images_dir, fn), phase == "logical"))
    if not images:
        raise ValueError(f"no partitions found in {plan_path}")
    return generate(workspace, images, info, both)
