import struct

from maa_planner.daemon.tools.apk import ApkInfo


def binary_manifest(info: ApkInfo) -> bytes:
    strings = ["manifest", "package", "versionCode", "versionName", info.package, info.version_name]
    encoded = [bytes([len(s), len(s)]) + s.encode() + b"\0" for s in strings]
    content = b"".join(encoded)
    offsets = []
    pos = 0
    for value in encoded:
        offsets.append(pos)
        pos += len(value)
    pool = struct.pack(
        "<HHIIIIII",
        1,
        28,
        28 + 4 * len(strings) + len(content),
        len(strings),
        0,
        0x100,
        28 + 4 * len(strings),
        0,
    )
    pool += struct.pack("<" + "I" * len(offsets), *offsets) + content
    attrs = b"".join(
        struct.pack("<IIIHBBI", 0xFFFFFFFF, key, raw, 8, 0, kind, value)
        for key, raw, kind, value in [
            (1, 4, 3, 4),
            (2, 0xFFFFFFFF, 0x10, info.version_code),
            (3, 5, 3, 5),
        ]
    )
    node = struct.pack("<HHIII", 0x102, 16, 36 + len(attrs), 1, 0xFFFFFFFF)
    node += struct.pack("<IIHHHHHH", 0xFFFFFFFF, 0, 20, 20, 3, 0, 0, 0) + attrs
    return struct.pack("<HHI", 3, 8, 8 + len(pool) + len(node)) + pool + node
