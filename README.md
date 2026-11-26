# Android Kitchen (Windows GUI)

A Windows GUI tool that unpacks **stock Android firmware** and pulls out the **APKs**
and **config files**, plus `fs_config` / `file_contexts`. It's inspired by
[ravindu644/Android_Image_Tools](https://github.com/ravindu644/Android_Image_Tools),
which is Linux-only and needs root and `mount`. This tool is written in pure Python,
so it needs no WSL, Cygwin, root or extra binaries.

## Quick start

* **Prebuilt:** run `dist\AndroidKitchen.exe`.
* **From source:** install Python 3.10+, then double-click `run.bat`
  (it installs `lz4`, `brotli` and `zstandard`).
* **Build the exe yourself:** run `build.bat`. The result is `dist\AndroidKitchen.exe`.

1. **File...** / **Folder...**: pick the firmware. Workspace is the output folder; it
   defaults to `<firmware>_kitchen` next to the firmware.
2. **Scan firmware**: lists every partition image found. Partitions that hold apps and
   configs (system, system_ext, product, vendor, odm, my_*, ...) are ticked for you.
3. Check the **Options** tab, then click **Extract selected**.
4. The **APKs** tab lists every APK found, with its package name, version and SDK levels.
   Double-click a row to show the file in Explorer.

## Supported input

| Firmware | Example |
|---|---|
| Samsung | `AP_*.tar.md5`, `*.tar` with `super.img.lz4`, `*.img.lz4` |
| Xiaomi / Redmi / POCO fastboot ROM | `*.tgz`, `*.tar.gz` (`images/super.img`) |
| A/B OTA, recovery ROMs | `.zip` with `payload.bin` (full OTA only) |
| Older OTA ROMs | `.zip` with `system.new.dat.br` + `system.transfer.list` |
| Google Pixel factory image | `.zip` (the nested `image-*.zip` is handled) |
| Motorola | `super.img_sparsechunk.N`, `system.img_sparsechunk.N` |
| Anything already unpacked | a folder of `.img` files, or a single `.img` / `super.img` |

* **Image layers:** lz4 → Android sparse → raw. `super.img` (dynamic partitions) is
  extracted in one streaming pass, without writing the multi-GB raw super to disk first.
* **Filesystems:** ext4 (extents, indirect blocks, inline data, xattrs) and EROFS (plain,
  inline, chunked, and compressed with LZ4 / LZMA / DEFLATE / Zstd, including big
  pclusters, ztailpacking and fragments).
* **Not supported:** incremental (delta) OTAs, F2FS, Huawei `UPDATE.APP`, encrypted
  firmware such as some Oppo/Realme `.ozip` / `.ofp` files.

## Output (in the workspace)

```
apks\<partition>\...        collected APK files (and APEX / JAR if enabled), original paths kept
configs\<partition>\...     config files matching the patterns (build.prop, *.rc, etc\*.xml, vintf, permissions, ...)
extracted\<partition>\...   full filesystem dump (only if enabled)
config\<part>_fs_config     uid gid mode [capabilities] for every path
config\<part>_file_contexts SELinux label for every path
config\<part>_symlinks.txt  symlink list (Windows can't store Android symlinks)
config\<part>_info.txt      filesystem type, image size, counts
images\<part>.img           raw partition images (kept only if "Keep raw images" is ticked)
apk_list.csv                partition, path, package, version, SDK, privileged, size
```

You can edit the config patterns in the Options tab. They are matched against the
lower-cased path inside the partition, and `*` matches anything, including `/`.

## Flash script (going back to stock)

**Build flash script** (bottom bar) unpacks *every* partition of a full firmware into
`workspace\images` and writes two files:

* `flash_all.bat`: a fastboot script that flashes everything in the order known to work on
  OnePlus / OPPO / realme ColorOS phones (checked against a working OnePlus 12 ColorOS 17
  fastboot ROM):
  1. **bootloader:** only `recovery`, so the phone reboots into a stock fastbootd.
  2. **fastbootd:** the boot chain (`boot`, `init_boot`, `vendor_boot`, `dtbo`, `vbmeta*`)
     and all other firmware partitions, written to both slots by default (option on the
     Options tab).
  3. **fastbootd:** the logical partitions inside `super`, after removing leftover
     `*-cow` snapshot partitions. These go to the current slot; the script never
     switches slots.
  4. **bootloader:** `modem`, then an optional data wipe (`fastboot -w`) and a reboot.

  Slots are flashed by explicit name (`boot_a`, `boot_b`). The script asks fastbootd
  `has-slot` for each partition and uses the plain name for single-slot ones. It never
  uses `--slot=all`: OnePlus bootloaders don't answer `has-slot` for `init_boot`,
  `vendor_boot` and others, which makes `--slot=all` abort.
* **Rebuild a script without unpacking again:**
  `python -c "from kitchen.flashscript import regenerate; regenerate(r'<workspace>')"`
  rebuilds `flash_all.bat` from the workspace's `flash_plan.txt` and `images\`.
* `flash_plan.txt`: the same plan in plain text, so you can check it before flashing.

Which partitions are logical comes from the payload's dynamic-partition metadata (or the
super image's own metadata). It isn't guessed. The script never flashes per-device
partitions (`persist`, `modemst1/2`, `fsg`, `devinfo`, `frp`, …).

Safety features in the script:

* It shows the connected device's `product`, current slot and lock state next to the
  device the OTA was built for, and asks you to confirm before flashing.
* It checks that every image file exists first.
* If a flash fails you can retry, skip or abort; nothing continues silently.
* `flash_all.bat --dry-run` prints every command without touching the phone.

`fastboot.exe` is taken from `platform-tools\` next to the script, then the script's own
folder, then `PATH`, or from the `KITCHEN_FASTBOOT` environment variable.

> ⚠ **Anti-rollback:** never flash firmware older than what the phone has already run.
> Keep the bootloader unlocked until stock boots, and use a FULL OTA for your exact
> model and region.

CLI: `python AndroidKitchen.py --cli <ota.zip> <workspace> --flash-script [--current-slot-only]`

## Command line

```
python AndroidKitchen.py --cli <firmware> <workspace> [--list] [--all] [--parts system,vendor] [--full] [--keep-images]
```

## Notes

* Disk space: the selected partition images are unpacked into the workspace first and
  deleted afterwards (unless kept). A Samsung `system` image alone can be 6 GB or more.
* Paths longer than 260 characters work, because the tool uses `\\?\` long paths.
* Settings are saved to `%APPDATA%\AndroidKitchen\settings.json`.

## Project layout

```
AndroidKitchen.py        entry point (GUI, or --cli)
kitchen\gui.py           Tkinter user interface
kitchen\firmware.py      container scanning (zip / tar / tgz / folders / nested archives)
kitchen\sparse.py        Android sparse image decoder (streaming)
kitchen\lpunpack.py      super.img / LP metadata parser and extractor
kitchen\payload.py       payload.bin dumper (no protobuf dependency)
kitchen\sdat2img.py      new.dat(.br) + transfer.list converter
kitchen\ext4.py          read-only ext4 reader
kitchen\erofs.py         read-only EROFS reader
kitchen\extractor.py     walks a filesystem, collects APKs and configs, writes fs_config and file_contexts
kitchen\apkinfo.py       binary AndroidManifest.xml parser
tests\smoke_test.py      builds synthetic firmware packages and runs the whole pipeline
tests\erofs_test.py      checks the EROFS reader against images made by mkfs.erofs
```

## Tests

* `python tests\smoke_test.py <dir>` wraps sample ext4 images in Samsung tar.md5,
  payload.bin, new.dat.br and raw super packages, then checks the extracted files
  byte for byte. The sample images are from `fox-it/dissect.extfs` (`tests/data`, gunzipped).
* `python tests\erofs_test.py <mkfs.erofs.exe> <scratch-dir>` builds a varied tree
  (sizes at block edges, incompressible data, duplicates, 400-entry directories,
  Unicode and 200-character names, fs_config ownership, capabilities and SELinux labels).
  It then makes one image per option set and compares every file. The 25 option sets
  are: uncompressed; lz4 / lz4hc / lzma / deflate / zstd / mixed algorithms; big
  pclusters (64K–256K); ztailpacking; fragments and all-fragments; dedupe; legacy and
  compact indexes; compact and extended inodes; noinline; chunk-based files;
  compressed directories; max-extent-bytes. All pass with erofs-utils 1.9.4
  (Windows build from sekaiacg/erofs-tools). That build can't make 16 KB-block
  images, so those are untested.
* Read speed on a 466 MB lz4hc / 256K-pcluster image: about 540 MB/s.
* `python tests\flash_test.py` builds a OnePlus-style full OTA and generates
  `flash_all.bat`. It runs the script once with `--dry-run`, then for real against a fake
  `fastboot` that logs every call and fails once. It checks the bootloader/fastbootd
  order, which partitions use `--slot=all`, the cow cleanup, the retry path, the wipe and
  the reboot.

## License

[MIT](LICENSE) © enawba
