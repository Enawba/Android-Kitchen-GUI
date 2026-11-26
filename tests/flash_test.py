"""Flash-script test: synthetic OnePlus-style full OTA -> flash_all.bat -> run it against a
fake fastboot that records every command (and fails once to exercise the retry path).

    python tests/flash_test.py
"""
import os
import re
import subprocess
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from smoke_test import pb_field  # noqa: E402

from kitchen.cli import main as cli_main  # noqa: E402

BS = 4096
PHYSICAL = ["abl", "boot", "dtbo", "init_boot", "modem", "recovery", "vbmeta",
            "vbmeta_system", "vendor_boot", "xbl", "xbl_config", "tz", "splash"]
SINGLE_SLOT = {"splash"}   # the fake device reports has-slot:splash = no
LOGICAL = ["system", "system_ext", "product", "vendor", "odm", "my_product", "my_stock"]

FAKE_FASTBOOT = r"""@echo off
setlocal
set "LOG=%~dp0fb_log.txt"
set "STATE=%~dp0fb_state.txt"
>>"%LOG%" echo %*
if "%1"=="devices" goto devices
if "%1"=="getvar" goto getvar
if "%1 %2"=="reboot fastboot" >"%STATE%" echo yes
if "%1 %2"=="reboot bootloader" >"%STATE%" echo no
echo %* | findstr /c:"flash xbl_a " >nul || goto ok
if exist "%~dp0fb_failed_once" goto ok
>"%~dp0fb_failed_once" echo 1
echo FAILED (remote: simulated failure) 1>&2
exit /b 1
:ok
exit /b 0
:devices
echo FAKE123	fastboot
exit /b 0
:getvar
set "US=no"
if exist "%STATE%" set /p US=<"%STATE%"
echo %2 | findstr /b /c:"has-slot:" >nul || goto gv_vars
rem like the OnePlus 12 ABL: the bootloader does not answer has-slot, fastbootd does
if "%US%"=="no" 1>&2 echo getvar:%2 FAILED remote: unknown variable
if "%US%"=="no" exit /b 1
if "%2"=="has-slot:splash" 1>&2 echo %2: no
if not "%2"=="has-slot:splash" 1>&2 echo %2: yes
exit /b 0
:gv_vars
if "%2"=="is-userspace" 1>&2 echo is-userspace: %US%
if "%2"=="product" 1>&2 echo product: waffle
if "%2"=="current-slot" 1>&2 echo current-slot: a
if "%2"=="unlocked" 1>&2 echo unlocked: yes
echo Finished. Total time: 0.001s 1>&2
exit /b 0
"""


def make_payload():
    blob, manifest = b"", pb_field(3, 0, BS)
    for name in PHYSICAL + LOGICAL:
        data = (name.encode() * 4096)[:BS * 2]
        op = pb_field(1, 0, 0) + pb_field(2, 0, len(blob)) + pb_field(3, 0, len(data)) + \
            pb_field(6, 2, pb_field(1, 0, 0) + pb_field(2, 0, 2))
        blob += data
        manifest += pb_field(13, 2, pb_field(1, 2, name.encode()) +
                             pb_field(7, 2, pb_field(1, 0, len(data))) + pb_field(8, 2, op))
    group = pb_field(1, 2, b"qti_dynamic_partitions") + pb_field(2, 0, 1 << 30)
    for n in LOGICAL:
        group += pb_field(3, 2, n.encode())
    manifest += pb_field(15, 2, pb_field(1, 2, group))
    import struct
    return b"CrAU" + struct.pack(">QQI", 2, len(manifest), 0) + manifest + blob


def main():
    tmp = tempfile.mkdtemp(prefix="kflash_")
    ota = os.path.join(tmp, "PJD110_15.0.0.1(CN01).zip")
    with zipfile.ZipFile(ota, "w") as z:
        z.writestr(zipfile.ZipInfo("payload.bin"), make_payload())
        z.writestr("META-INF/com/android/metadata",
                   "ota-type=AB\npre-device=OP595DL1\npost-build=OnePlus/PJD110/OP595DL1:16/TEST\n")
    ws = os.path.join(tmp, "ws")
    assert cli_main([ota, ws, "--flash-script"]) == 0
    bat = os.path.join(ws, "flash_all.bat")
    print(open(os.path.join(ws, "flash_plan.txt")).read())

    # 1) dry run: no device needed, prints the commands
    def answers_file(text):
        # cmd's set /p swallows a whole pipe buffer; a redirected file is read line by line
        fp = os.path.join(tmp, "answers.txt")
        with open(fp, "w", newline="\r\n") as f:
            f.write(text)
        return open(fp, "rb")

    r = subprocess.run(["cmd", "/c", bat, "--dry-run"], stdin=answers_file("YES\nN\n"),
                       capture_output=True, text=True, cwd=ws, timeout=60)
    dry = [l.strip() for l in r.stdout.splitlines() if "[dry-run]" in l]
    print(f"dry-run: {len(dry)} commands, rc={r.returncode}")
    assert r.returncode == 0, r.stdout[-2000:]

    # 2) real run against the fake fastboot
    fake = os.path.join(tmp, "fake", "fastboot.cmd")
    os.makedirs(os.path.dirname(fake))
    with open(fake, "w", newline="\r\n") as f:
        f.write(FAKE_FASTBOOT)
    env = dict(os.environ, KITCHEN_FASTBOOT=fake)
    answers = "YES\nYES\nR\nY\n"   # confirm, right phone, retry xbl, wipe
    r = subprocess.run(["cmd", "/c", bat], stdin=answers_file(answers), capture_output=True,
                       text=True, cwd=ws, env=env, timeout=180)
    print(r.stdout[-1500:])
    log = [l.strip() for l in open(os.path.join(tmp, "fake", "fb_log.txt"))]
    assert r.returncode == 0, "script failed"

    # ordering + slot checks
    events = []   # (log index, partition, line)
    for i, l in enumerate(log):
        m = re.search(r"(?:^| )flash (\S+)", l)
        if m:
            events.append((i, m.group(1), l))
    print("\n".join(l for _, _, l in events))
    to_fbd = log.index("reboot fastboot")
    to_bl = max(i for i, l in enumerate(log) if l == "reboot bootloader")
    assert to_bl > to_fbd
    flashed = {}   # partition (with suffix) -> log index of last flash
    for i, p, _ in events:
        flashed[p] = i
    # never --slot=all (OnePlus ABL can't answer has-slot for it)
    assert not any("--slot" in l for _, _, l in events)
    # phase 1: only recovery, from the bootloader, both slots by name
    assert flashed["recovery_a"] < to_fbd and flashed["recovery_b"] < to_fbd
    assert [p for i, p, _ in events if i < to_fbd] == ["recovery_a", "recovery_b"]
    # phase 2: boot chain + firmware in fastbootd, both slots by name
    for p in ["boot", "init_boot", "vendor_boot", "dtbo", "vbmeta", "vbmeta_system",
              "abl", "xbl", "xbl_config", "tz"]:
        for s in ("_a", "_b"):
            assert to_fbd < flashed[p + s] < to_bl, f"{p}{s} must be flashed in fastbootd"
    # boot chain comes before the rest of the firmware
    assert flashed["vbmeta_system_b"] < flashed["abl_a"]
    # single-slot partition falls back to its plain name
    assert "splash" in flashed and "splash_a" not in flashed
    # phase 3: logical partitions, plain names, in fastbootd
    for p in LOGICAL:
        assert to_fbd < flashed[p] < to_bl, p
    # phase 4: modem from the bootloader after fastbootd
    assert flashed["modem_a"] > to_bl and flashed["modem_b"] > to_bl
    # retry path, cow cleanup, wipe, reboot
    assert sum(1 for _, p, _ in events if p == "xbl_a") == 2, "xbl_a retry expected"
    assert "Flashing xbl_a failed" in r.stdout
    assert any("delete-logical-partition system_a-cow" in l for l in log)
    assert "-w" in log and log[-1] == "reboot", log[-3:]
    expected = 2 * (len(PHYSICAL) - len(SINGLE_SLOT)) + len(SINGLE_SLOT) + len(LOGICAL) + 1
    assert len(events) == expected, (len(events), expected)
    print("\nFLASH SCRIPT TEST PASSED")


if __name__ == "__main__":
    main()
