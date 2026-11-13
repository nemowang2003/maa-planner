"""Small flatbuffer fixture with bundle-vector dependency references."""

import struct


def index_bytes(records):
    data = bytearray(128)
    data += struct.pack("<I", 16) + b"\0\0" + struct.pack("<HHHHH", 10, 12, 0, 4, 8)
    root = len(data)
    data += struct.pack("<iII", 10, 8, 0)
    vector = len(data)
    data += struct.pack("<I", len(records)) + bytes(len(records) * 4)
    for i, (name, dependencies) in enumerate(records):
        data += struct.pack("<HHHHHH", 12, 16, 4, 0, 8, 12)
        table = len(data)
        data += struct.pack("<iIII", 12, 0, 0, 4)
        data += struct.pack("<I", len(dependencies))
        data += b"".join(struct.pack("<I", dependency) for dependency in dependencies)
        string = len(data)
        encoded = name.encode()
        data += struct.pack("<I", len(encoded)) + encoded + b"\0"
        data += bytes(-len(data) % 4)
        field = vector + 4 + i * 4
        struct.pack_into("<I", data, field, table - field)
        struct.pack_into("<I", data, table + 4, string - table - 4)
    field = root + 8
    struct.pack_into("<I", data, field, len(data) - field)
    data += bytes(4)
    return bytes(data)
