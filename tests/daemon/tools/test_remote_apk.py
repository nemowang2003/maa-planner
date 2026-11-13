import io
import re
import zipfile

import pytest

from maa_planner.daemon.tools import remote_apk
from maa_planner.daemon.tools.apk import ApkInfo
from tests.helpers.apk import binary_manifest

URL = "https://ak.hycdn.cn/test/app.apk"
INFO = ApkInfo("com.hypergryph.arknights", 200, "2.7.81")


def make_zip(*, first=True, descriptor=False, payload=None):
    class Unseekable(io.BytesIO):
        def seekable(self):
            return False

        def seek(self, *args):
            raise OSError("unseekable")

    output = Unseekable() if descriptor else io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        if not first:
            archive.writestr("classes.dex", b"x" * 40000)
        archive.writestr(
            "AndroidManifest.xml",
            payload if payload is not None else binary_manifest(INFO),
            compress_type=zipfile.ZIP_DEFLATED,
        )
        archive.writestr("large.bin", b"x" * 200000)
    return output.getvalue()


class Response(io.BytesIO):
    def __init__(self, body, status, headers, *, read_log=None):
        super().__init__(body)
        self.status = status
        self.headers = headers
        self.read_log = read_log

    def geturl(self):
        return URL

    def read(self, size=-1):
        assert size >= 0, "must never consume an unlimited response body"
        if self.read_log is not None:
            self.read_log.append(size)
        return super().read(size)


def range_server(monkeypatch, data, *, edit=None):
    calls = []

    def open(request, timeout):
        headers = {k.lower(): v for k, v in request.header_items()}
        match = re.fullmatch(r"bytes=(\d+)-(\d+)", headers["range"])
        assert match is not None
        start, stop = map(int, match.groups())
        stop = min(stop, len(data) - 1)
        response = Response(
            data[start : stop + 1],
            206,
            {"Content-Range": f"bytes {start}-{stop}/{len(data)}", "ETag": '"release-a"'},
        )
        calls.append((headers, start, stop))
        if edit:
            edit(response, len(calls))
        return response

    monkeypatch.setattr(remote_apk.urllib.request, "urlopen", open)
    return calls


@pytest.mark.parametrize("first,descriptor", [(True, False), (False, False), (True, True)])
def test_bounded_remote_manifest_handles_layout_and_descriptors(monkeypatch, first, descriptor):
    data = make_zip(first=first, descriptor=descriptor)
    calls = range_server(monkeypatch, data)
    assert remote_apk.read_remote_apk(URL, allowed_host="ak.hycdn.cn") == INFO
    assert sum(end - start + 1 for _, start, end in calls) < len(data) // 2
    if first and not descriptor:
        assert len(calls) == 1
        assert calls[0][2] == 16383
    else:
        assert any("if-range" in headers for headers, _, _ in calls[1:])


def test_ignored_range_does_not_read_full_body(monkeypatch):
    reads = []
    monkeypatch.setattr(
        remote_apk.urllib.request,
        "urlopen",
        lambda *a, **kw: Response(b"huge APK", 200, {}, read_log=reads),
    )
    with pytest.raises(remote_apk.MetadataError, match="不会下载完整"):
        remote_apk.read_remote_apk(URL, allowed_host="ak.hycdn.cn")
    assert reads == []


@pytest.mark.parametrize("change", ["range", "length", "validator"])
def test_invalid_or_changed_range_fails_closed(monkeypatch, change):
    def edit(response, call):
        if change == "range":
            response.headers["Content-Range"] = "bytes 1-3/200000"
        if change == "length":
            response.truncate(1)
        if change == "validator" and call > 1:
            response.headers["ETag"] = '"release-b"'

    range_server(monkeypatch, make_zip(first=False), edit=edit)
    with pytest.raises(remote_apk.MetadataError):
        remote_apk.read_remote_apk(URL, allowed_host="ak.hycdn.cn")


def test_corrupt_manifest_crc_is_rejected(monkeypatch):
    data = bytearray(make_zip())
    data[14] ^= 1
    range_server(monkeypatch, bytes(data))
    with pytest.raises(remote_apk.MetadataError, match="CRC"):
        remote_apk.read_remote_apk(URL, allowed_host="ak.hycdn.cn")


def test_excessive_directory_does_not_trigger_full_download(monkeypatch):
    calls = range_server(monkeypatch, make_zip(first=False))
    monkeypatch.setattr(remote_apk, "TRANSFER_LIMIT", 16500)
    with pytest.raises(remote_apk.MetadataError, match="预算"):
        remote_apk.read_remote_apk(URL, allowed_host="ak.hycdn.cn")
    assert sum(end - start + 1 for _, start, end in calls) <= 16500


def test_apk_host_is_supplied_by_configuration():
    remote_apk.validate_url(
        "https://mirror.example.test/app.apk", allowed_host="mirror.example.test"
    )
    with pytest.raises(ValueError, match="非预期"):
        remote_apk.validate_url("https://ak.hycdn.cn/app.apk", allowed_host="mirror.example.test")


def test_only_explicit_asset_reader_can_chunk_larger_reads_within_its_budget(monkeypatch):
    data = b"x" * (3 * remote_apk.REQUEST_LIMIT)
    calls = range_server(monkeypatch, data)
    with remote_apk.RangeFile(URL, allowed_host="ak.hycdn.cn") as metadata:
        with pytest.raises(remote_apk.MetadataError, match="预算"):
            metadata.read(2 * remote_apk.REQUEST_LIMIT)
    with remote_apk.RangeFile(
        URL, allowed_host="ak.hycdn.cn", transfer_limit=len(data), large_reads=True, max_requests=32
    ) as assets:
        assert assets.read(2 * remote_apk.REQUEST_LIMIT) == data[: 2 * remote_apk.REQUEST_LIMIT]
        assert assets.transferred <= len(data)
    assert all(end - start + 1 <= remote_apk.REQUEST_LIMIT for _, start, end in calls)
