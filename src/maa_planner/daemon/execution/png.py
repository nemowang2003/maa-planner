"""Bounded structural PNG validation for immutable private resource attachments."""

import base64
import binascii
import re
import struct

MAX_PNG_BYTES = 2 * 1024 * 1024


def image_name(name):
    if not isinstance(name, str) or not re.fullmatch(r"Planner@[A-Za-z0-9_@-]{1,120}\.png", name):
        raise ValueError("私有图片必须使用 Planner@ 名称且不能包含路径")
    return name


def validate_png(data: bytes) -> bytes:
    if not 0 < len(data) <= MAX_PNG_BYTES or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("PNG 无效或超过 2 MiB")
    offset, kinds = 8, []
    while offset + 12 <= len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        end = offset + length + 12
        if end > len(data):
            raise ValueError("PNG 块截断")
        kind = data[offset + 4 : offset + 8]
        content = data[offset + 8 : end - 4]
        crc = struct.unpack_from(">I", data, end - 4)[0]
        if binascii.crc32(kind + content) & 0xFFFFFFFF != crc:
            raise ValueError("PNG 校验失败")
        if not kinds:
            if kind != b"IHDR" or length != 13:
                raise ValueError("PNG 缺少 IHDR")
            width, height = struct.unpack_from(">II", content)
            if not 0 < width <= 4096 or not 0 < height <= 4096 or width * height > 4194304:
                raise ValueError("PNG 尺寸超过私有模板预算")
        kinds.append(kind)
        offset = end
        if kind == b"IEND":
            if length or offset != len(data) or b"IDAT" not in kinds:
                raise ValueError("PNG 结束块无效")
            return data
    raise ValueError("PNG 缺少 IEND")


def encode_png(data: bytes) -> str:
    return base64.b64encode(validate_png(data)).decode("ascii")


def decode_png(value: str) -> bytes:
    if not isinstance(value, str) or len(value) > (MAX_PNG_BYTES + 2) // 3 * 4:
        raise ValueError("PNG 附件必须为有界 base64")
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("PNG base64 无效") from error
    return validate_png(data)
