"""Read APK metadata with bounded HTTP ranges, never a full-download fallback."""

from __future__ import annotations

import io
import re
import struct
import time
import urllib.request
import zipfile
import zlib
from urllib.parse import urlsplit

from maa_planner.daemon.tools.apk import ApkInfo, read_manifest

MANIFEST_LIMIT = 8 * 1024 * 1024
TRANSFER_LIMIT = 2 * 1024 * 1024
REQUEST_LIMIT = 1024 * 1024


class MetadataError(ValueError):
    pass


def validate_url(url: str, *, allowed_host: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != allowed_host
        or not parsed.path.endswith(".apk")
    ):
        raise ValueError(f"官网返回了非预期 APK 地址: {url}")


class RangeFile(io.RawIOBase):
    """Small seekable view for zipfile; the transfer budget includes all requests."""

    def __init__(
        self,
        url: str,
        *,
        allowed_host: str,
        transfer_limit=None,
        request_limit=None,
        max_requests=16,
        deadline_seconds=30,
        large_reads=False,
        cancel=None,
    ):
        super().__init__()
        validate_url(url, allowed_host=allowed_host)
        self.transfer_limit = TRANSFER_LIMIT if transfer_limit is None else transfer_limit
        self.request_limit = REQUEST_LIMIT if request_limit is None else request_limit
        self.max_requests = max_requests
        self.large_reads = large_reads
        self.cancel = cancel
        self.url = url
        self.position = 0
        self.size: int | None = None
        self.transferred = 0
        self.deadline = time.monotonic() + deadline_seconds
        self.validator: str | None = None
        self.blocks: list[tuple[int, bytes]] = []
        self._fetch(0, 16 * 1024)

    def _fetch(self, start: int, size: int) -> bytes:
        if self.cancel is not None and self.cancel.is_set():
            raise InterruptedError("Range 读取取消")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or len(self.blocks) >= self.max_requests:
            raise MetadataError("远端 metadata 读取超时或请求次数超限")
        if size > self.request_limit or self.transferred + size > self.transfer_limit:
            raise MetadataError("远端 metadata 超过局部读取预算；不会下载完整 APK")
        headers = {
            "Range": f"bytes={start}-{start + size - 1}",
            "Accept-Encoding": "identity",
            "User-Agent": "maa-planner",
        }
        if self.validator:
            headers["If-Range"] = self.validator
        with urllib.request.urlopen(
            urllib.request.Request(self.url, headers=headers), timeout=min(20, remaining)
        ) as response:
            # Inspect headers before reading any body, especially a server ignoring Range.
            if response.status != 206:
                raise MetadataError("CDN 不支持一致的 Range 读取；不会下载完整 APK")
            if response.geturl() != self.url:
                raise MetadataError("读取期间 APK 地址发生变化，请重新检查")
            if response.headers.get("Content-Encoding", "identity") != "identity":
                raise MetadataError("Range 响应使用了额外内容编码")
            match = re.fullmatch(
                r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", "")
            )
            if match is None:
                raise MetadataError("CDN 缺少有效 Content-Range")
            begin, end, total = map(int, match.groups())
            if total <= 0 or begin != start or end != min(start + size, total) - 1:
                raise MetadataError("CDN 返回了错误的字节范围")
            if self.size is not None and self.size != total:
                raise MetadataError("Range 读取期间 APK 长度发生变化")
            validator = response.headers.get("ETag")
            if not validator or validator.startswith("W/"):
                validator = response.headers.get("Last-Modified")
            if self.validator and validator != self.validator:
                raise MetadataError("Range 读取期间 APK 标识发生变化")
            body = response.read(end - begin + 2)
            if len(body) != end - begin + 1:
                raise MetadataError("CDN 字节范围响应长度不符")
        self.size = total
        self.validator = validator
        self.transferred += len(body)
        self.blocks.append((start, body))
        return body

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        assert self.size is not None
        position = (
            offset
            if whence == io.SEEK_SET
            else self.position + offset
            if whence == io.SEEK_CUR
            else self.size + offset
            if whence == io.SEEK_END
            else -1
        )
        if position < 0:
            raise MetadataError("无效的 ZIP seek")
        self.position = position
        return position

    def read(self, size: int = -1) -> bytes:
        assert self.size is not None
        size = min(self.size - self.position, size if size >= 0 else self.size - self.position)
        if size <= 0:
            return b""
        if self.large_reads and size > self.request_limit:
            chunks = []
            while size:
                chunk = self.read(min(size, self.request_limit))
                if not chunk:
                    break
                chunks.append(chunk)
                size -= len(chunk)
            return b"".join(chunks)
        for begin, data in self.blocks:
            if begin <= self.position and self.position + size <= begin + len(data):
                offset = self.position - begin
                self.position += size
                return data[offset : offset + size]
        body = self._fetch(self.position, size)
        self.position += len(body)
        return body


def _first_manifest(remote: RangeFile) -> bytes | None:
    header = remote.read(30)
    if len(header) != 30 or header[:4] != b"PK\x03\x04":
        return None
    _, _, flags, method, _, _, crc, compressed, plain, nlen, elen = struct.unpack(
        "<IHHHHHIIIHH", header
    )
    name = remote.read(nlen)
    if name != b"AndroidManifest.xml" or flags & 9 or compressed == 0xFFFFFFFF:
        return None
    if plain > MANIFEST_LIMIT or compressed > REQUEST_LIMIT:
        raise MetadataError("APK manifest 超过读取上限")
    remote.seek(elen, io.SEEK_CUR)
    content = remote.read(compressed)
    if len(content) != compressed:
        raise MetadataError("APK manifest 不完整")
    if method == zipfile.ZIP_STORED:
        data = content
    elif method == zipfile.ZIP_DEFLATED:
        decompressor = zlib.decompressobj(-15)
        data = decompressor.decompress(content, MANIFEST_LIMIT + 1)
        if not decompressor.eof or decompressor.unused_data:
            raise MetadataError("APK manifest 压缩数据不完整或超限")
    else:
        return None
    if len(data) != plain or zlib.crc32(data) != crc:
        raise MetadataError("APK manifest 长度或 CRC 不匹配")
    return data


def read_remote_apk(url: str, *, allowed_host: str) -> ApkInfo:
    try:
        with RangeFile(url, allowed_host=allowed_host) as remote:
            data = _first_manifest(remote)
            if data is None:
                # Standard ZIP directory handles moved manifests, descriptors and ZIP64.
                with zipfile.ZipFile(remote) as archive:
                    info = archive.getinfo("AndroidManifest.xml")
                    if info.file_size > MANIFEST_LIMIT or info.compress_size > REQUEST_LIMIT:
                        raise MetadataError("APK manifest 超过读取上限")
                    with archive.open(info) as entry:
                        data = entry.read(MANIFEST_LIMIT + 1)
            return read_manifest(data)
    except (zipfile.BadZipFile, KeyError, zlib.error, struct.error) as error:
        raise MetadataError("无法通过局部读取解析 APK manifest") from error
