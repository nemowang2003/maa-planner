import hashlib
import io
import json
import zipfile
from types import SimpleNamespace

from maa_planner.daemon.tools import activity_assets as assets
from tests.helpers.png import PNG

ARGUMENTS = {
    "activity_id": "act_test",
    "resolve_dependencies": False,
    "bundles": ["activity/test.ab"],
    "max_bundle_bytes": 1024 * 1024,
    "max_output_bytes": 1024 * 1024,
    "export_timeout": 30,
}


def zip_bytes(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, data in files:
            archive.writestr(name, data)
    return stream.getvalue()


def scenario(tmp_path, monkeypatch, platform):
    bundle = b"UnityFS fake test data"
    container = zip_bytes([("activity/test.ab", bundle)])
    tool = zip_bytes([("tool/ArknightsStudioCLI.dll", b"tool")])
    checksum = assets.sha(tool)
    monkeypatch.setitem(
        assets.EXPORTERS, platform, ("https://github.com/example/tool.zip", checksum)
    )
    version = ["one"]
    calls, exports = [], []

    def get(url, limit, cancel):
        calls.append(url)
        if url == assets.NETWORK:
            return json.dumps(
                {
                    "content": json.dumps(
                        {
                            "funcVer": "test",
                            "configs": {
                                "test": {
                                    "network": {
                                        "hv": "https://ak-conf.hypergryph.com/{0}/version",
                                        "hu": "https://ak.hycdn.cn/assets",
                                    }
                                }
                            },
                        }
                    )
                }
            ).encode()
        if url.endswith("/version"):
            return json.dumps({"resVersion": version[0], "clientVersion": "2.7.91"}).encode()
        if url.endswith("hot_update_list.json"):
            return json.dumps(
                {
                    "versionId": version[0],
                    "abInfos": [
                        {
                            "name": "activity/test.ab",
                            "md5": hashlib.md5(bundle).hexdigest(),
                            "hash": "same-bundle",
                            "totalSize": len(container),
                            "abSize": len(bundle),
                        }
                    ],
                }
            ).encode()
        if url.endswith("tool.zip"):
            return tool
        return container

    def export(dll, source, destination, log, timeout, cancel, *, max_output_bytes):
        exports.append(source)
        assert dll.read_bytes() == b"tool"
        assert next(source.rglob("*.ab")).read_bytes() == bundle
        (destination / "dir/item").mkdir(parents=True)
        (destination / "dir/item/button.png").write_bytes(PNG)

    settings = SimpleNamespace(cache_directory=tmp_path, game_cache=tmp_path / "apk")
    return settings, get, export, calls, exports, version
