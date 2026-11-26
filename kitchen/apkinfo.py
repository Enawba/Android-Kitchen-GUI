"""Read package name / version / SDK levels from an APK's binary AndroidManifest.xml."""
from __future__ import annotations

import struct
import zipfile

RES_STRING_POOL = 0x0001
RES_XML = 0x0003
RES_XML_START_ELEMENT = 0x0102
UTF8_FLAG = 1 << 8

TYPE_REFERENCE, TYPE_STRING, TYPE_INT_DEC, TYPE_INT_HEX, TYPE_BOOL = 0x01, 0x03, 0x10, 0x11, 0x12

# android: attribute resource ids (stable)
ATTR_IDS = {
    0x0101021B: "versionCode", 0x0101021C: "versionName", 0x0101020C: "minSdkVersion",
    0x01010270: "targetSdkVersion", 0x01010001: "label", 0x0101000F: "debuggable",
    0x01010572: "compileSdkVersion", 0x0101057B: "isSplitRequired",
}


def _strings(data: bytes, off: int):
    _t, hsize, _size, count, _style_count, flags, str_start, _style_start = struct.unpack_from(
        "<HHIIIIII", data, off)
    utf8 = bool(flags & UTF8_FLAG)
    offsets = struct.unpack_from(f"<{count}I", data, off + hsize)
    base = off + str_start
    out = []
    for so in offsets:
        p = base + so
        try:
            if utf8:
                n = data[p]
                p += 2 if n & 0x80 else 1
                n = data[p]
                if n & 0x80:
                    n = ((n & 0x7F) << 8) | data[p + 1]
                    p += 2
                else:
                    p += 1
                out.append(data[p:p + n].decode("utf-8", "replace"))
            else:
                n = struct.unpack_from("<H", data, p)[0]
                p += 2
                if n & 0x8000:
                    n = ((n & 0x7FFF) << 16) | struct.unpack_from("<H", data, p)[0]
                    p += 2
                out.append(data[p:p + n * 2].decode("utf-16-le", "replace"))
        except (IndexError, struct.error):
            out.append("")
    return out


def parse_axml(data: bytes) -> dict:
    """Return attributes of <manifest> and <uses-sdk> / <application> we care about."""
    info = {}
    if len(data) < 8 or struct.unpack_from("<H", data, 0)[0] != RES_XML:
        return info
    strings = []
    res_ids = []
    off = struct.unpack_from("<H", data, 2)[0]
    while off + 8 <= len(data):
        ctype, hsize, csize = struct.unpack_from("<HHI", data, off)
        if csize < 8:
            break
        if ctype == RES_STRING_POOL:
            strings = _strings(data, off)
        elif ctype == 0x0180:  # resource map
            n = (csize - hsize) // 4
            res_ids = list(struct.unpack_from(f"<{n}I", data, off + hsize))
        elif ctype == RES_XML_START_ELEMENT:
            _ns, name_i = struct.unpack_from("<iI", data, off + 16)
            attr_start, attr_size, attr_count = struct.unpack_from("<HHH", data, off + 24)
            tag = strings[name_i] if name_i < len(strings) else ""
            if tag in ("manifest", "uses-sdk", "application"):
                for k in range(attr_count):
                    a = off + 16 + attr_start + k * attr_size
                    _ans, aname, araw, _sz, _r, dtype, dval = struct.unpack_from("<iIiHBBI", data, a)
                    key = ATTR_IDS.get(res_ids[aname]) if aname < len(res_ids) else None
                    if key is None:
                        key = strings[aname] if aname < len(strings) else ""
                    if dtype == TYPE_STRING:
                        val = strings[dval] if dval < len(strings) else ""
                    elif araw >= 0 and araw < len(strings):
                        val = strings[araw]
                    elif dtype in (TYPE_INT_DEC, TYPE_INT_HEX):
                        val = dval if dtype == TYPE_INT_DEC else dval
                    elif dtype == TYPE_BOOL:
                        val = dval != 0
                    elif dtype == TYPE_REFERENCE:
                        val = f"@0x{dval:08x}"
                    else:
                        val = dval
                    if tag == "manifest" and key in ("package", "versionCode", "versionName",
                                                     "sharedUserId", "compileSdkVersion",
                                                     "split", "coreApp"):
                        info[key] = val
                    elif tag == "uses-sdk" and key in ("minSdkVersion", "targetSdkVersion"):
                        info[key] = val
                    elif tag == "application" and key in ("label", "debuggable"):
                        info["app_" + key] = val
            if tag == "application":
                break
        off += csize
    return info


def apk_info(path: str) -> dict:
    try:
        with zipfile.ZipFile(path) as z:
            data = z.read("AndroidManifest.xml")
        return parse_axml(data)
    except Exception as e:  # not every .apk-named file is a valid apk
        return {"error": str(e)}
