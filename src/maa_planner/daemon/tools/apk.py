"""Minimal Android binary manifest reader; installation validates APK signatures."""

import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ApkInfo:
    package: str
    version_code: int
    version_name: str

    def __post_init__(self):
        if (
            not isinstance(self.package, str)
            or not self.package
            or not isinstance(self.version_name, str)
            or not self.version_name
        ):
            raise ValueError("无效的 APK 包名或版本名")
        if type(self.version_code) is not int or self.version_code < 0:
            raise ValueError("无效的 APK versionCode")


def read_apk(path: Path) -> ApkInfo:
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("AndroidManifest.xml")
        if info.file_size > 8 * 1024 * 1024:
            raise ValueError("APK manifest 过大")
        data = archive.read(info)
    return read_manifest(data)


def read_manifest(data: bytes) -> ApkInfo:
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("APK manifest 过大")
    try:
        return _read_manifest(data)
    except (IndexError, KeyError, struct.error, UnicodeError) as error:
        raise ValueError("损坏的 Android manifest") from error


def _read_manifest(data: bytes) -> ApkInfo:
    if len(data) < 8 or struct.unpack_from("<H", data)[0] != 3:
        raise ValueError("APK manifest 不是 Android binary XML")
    strings: list[str] = []
    offset = struct.unpack_from("<H", data, 2)[0]
    while offset + 8 <= len(data):
        kind, header, size = struct.unpack_from("<HHI", data, offset)
        if header < 8 or size < header or offset + size > len(data):
            raise ValueError("损坏的 APK XML chunk")
        if kind == 1:
            count, _, flags, start = struct.unpack_from("<IIII", data, offset + 8)
            if header < 28 or header + count * 4 > size:
                raise ValueError("损坏的 APK 字符串表")
            utf8 = bool(flags & 0x100)

            def length(pos: int) -> tuple[int, int]:
                if utf8:
                    value = data[pos]
                    return (
                        (((value & 0x7F) << 8) | data[pos + 1], pos + 2)
                        if value & 0x80
                        else (value, pos + 1)
                    )
                value = struct.unpack_from("<H", data, pos)[0]
                return (
                    (((value & 0x7FFF) << 16) | struct.unpack_from("<H", data, pos + 2)[0], pos + 4)
                    if value & 0x8000
                    else (value, pos + 2)
                )

            for index in range(count):
                pos = (
                    offset + start + struct.unpack_from("<I", data, offset + header + index * 4)[0]
                )
                n, pos = length(pos)
                if utf8:
                    n, pos = length(pos)
                end = pos + n * (1 if utf8 else 2)
                if end > offset + size:
                    raise ValueError("APK 字符串越界")
                strings.append(data[pos:end].decode("utf-8" if utf8 else "utf-16-le"))
        elif kind == 0x102:
            name = struct.unpack_from("<I", data, offset + 20)[0]
            if strings[name] == "manifest":
                attr_start, attr_size, count = struct.unpack_from("<HHH", data, offset + 24)
                if attr_size < 20 or 16 + attr_start + attr_size * count > size:
                    raise ValueError("损坏的 manifest attributes")
                attributes: dict[str, str | int] = {}
                for index in range(count):
                    pos = offset + 16 + attr_start + index * attr_size
                    _, key, raw = struct.unpack_from("<III", data, pos)
                    value_type = data[pos + 15]
                    value = struct.unpack_from("<I", data, pos + 16)[0]
                    attributes[strings[key]] = (
                        strings[raw]
                        if raw != 0xFFFFFFFF
                        else strings[value]
                        if value_type == 3
                        else value
                    )
                code = int(attributes["versionCode"]) | (
                    int(attributes.get("versionCodeMajor", 0)) << 32
                )
                return ApkInfo(str(attributes["package"]), code, str(attributes["versionName"]))
        offset += size
    raise ValueError("找不到 APK manifest 元数据")
