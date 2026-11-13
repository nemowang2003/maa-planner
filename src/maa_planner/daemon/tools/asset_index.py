"""Bounded reader for the observed official prefix128/FlatBuffers index.

Only the bundle names/dependency-index vector is consumed. Layout changes fail
closed; the opaque prefix is retained with the source evidence.
"""

import struct
from pathlib import PurePosixPath

MAX_INDEX_BYTES = 64 * 1024 * 1024
MAX_BUNDLES = 100000


class AssetIndex:
    def __init__(self, content: bytes):
        if not 160 <= len(content) <= MAX_INDEX_BYTES:
            raise ValueError("资源索引长度无效")
        self.data = content

    def unpack(self, pattern, offset):
        size = struct.calcsize(pattern)
        if type(offset) is not int or offset < 128 or offset + size > len(self.data):
            raise ValueError("资源索引越界")
        return struct.unpack_from(pattern, self.data, offset)[0]

    def pointer(self, field):
        value = self.unpack("<I", field)
        target = field + value
        if value < 4 or target + 4 > len(self.data):
            raise ValueError("资源索引指针无效")
        return target

    def fields(self, table, maximum):
        vtable = table - self.unpack("<i", table)
        length = self.unpack("<H", vtable)
        object_size = self.unpack("<H", vtable + 2)
        if (
            length < 4
            or length % 2
            or (length - 4) // 2 > maximum
            or table + object_size > len(self.data)
        ):
            raise ValueError("资源索引表结构发生变化")
        fields = []
        for offset in range(4, length, 2):
            relative = self.unpack("<H", vtable + offset)
            if relative and not 4 <= relative < object_size:
                raise ValueError("资源索引字段无效")
            fields.append(table + relative if relative else None)
        return fields + [None] * (maximum - len(fields))

    def vector(self, field, maximum):
        if field is None:
            return []
        vector = self.pointer(field)
        count = self.unpack("<I", vector)
        if count > maximum or vector + 4 + count * 4 > len(self.data):
            raise ValueError("资源索引向量无效或超过预算")
        return [vector + 4 + i * 4 for i in range(count)]

    def name(self, field):
        if field is None:
            raise ValueError("资源索引缺少 bundle 名称")
        target = self.pointer(field)
        size = self.unpack("<I", target)
        if (
            not 1 <= size <= 512
            or target + 4 + size >= len(self.data)
            or self.data[target + 4 + size] != 0
        ):
            raise ValueError("资源索引名称长度无效")
        name = self.data[target + 4 : target + 4 + size].decode("utf-8")
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or any(c in name for c in '\\:<>"|?*')
            or any(ord(c) < 32 for c in name)
        ):
            raise ValueError("资源索引名称包含目录逃逸或无效字符")
        return name

    def dependencies(self):
        root = self.pointer(128)
        reserved, bundles, assets = self.fields(root, 3)
        if reserved is not None and self.unpack("<I", reserved) != 0:
            raise ValueError("当前资源索引布局尚未识别")
        records = self.vector(bundles, MAX_BUNDLES)
        self.vector(assets, 1000000)  # Check the unrelated asset vector's boundary.
        if not records:
            raise ValueError("资源索引没有 bundle")
        names, references, reference_count = [], [], 0
        for record in records:
            table = self.pointer(record)
            name, _, _, dependencies = self.fields(table, 4)
            names.append(self.name(name))
            positions = self.vector(dependencies, len(records))
            reference_count += len(positions)
            if reference_count > 1000000:
                raise ValueError("资源索引总依赖数超过预算")
            refs = [self.unpack("<I", p) for p in positions]
            if any(index >= len(records) for index in refs):
                raise ValueError("资源索引依赖目标不存在")
            references.append(refs)
        if len(set(names)) != len(names) or len({name.casefold() for name in names}) != len(names):
            raise ValueError("资源索引 bundle 名称不唯一")
        # References are positions in the bundle vector, not the per-source ID
        # scalar (which overlaps between APK and hot-update entries).
        return {
            name: tuple(names[index] for index in refs)
            for name, refs in zip(names, references, strict=True)
        }


def dependency_closure(graph, roots):
    output, pending, visited = [], list(reversed(roots)), set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        if name not in graph:
            raise ValueError(f"资源索引未包含请求的 bundle: {name}")
        visited.add(name)
        output.append(name)
        pending.extend(reversed(graph[name]))
    return output
