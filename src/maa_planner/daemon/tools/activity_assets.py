"""Versioned, private bundle acquisition/export. Never touches an installed game."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import time
import urllib.request
import uuid
import zipfile
import zlib
from contextlib import ExitStack, contextmanager
from pathlib import PurePosixPath
from urllib.parse import quote, urlsplit

from maa_planner.daemon.adapters.windows import filesystem_path
from maa_planner.daemon.tools.cache_cleanup import mark_apk_used, touch_cache
from maa_planner.daemon.tools.locks import acquire, data_resource
from maa_planner.daemon.tools.storage import atomic_write, write_json
from maa_planner.schema.assets import validate_assets

NETWORK = "https://ak-conf.hypergryph.com/config/prod/official/network_config"
EXPORTERS = {
    "windows": (
        "https://github.com/aelurum/AssetStudioMod/releases/download/ak-v1.2.1/ArknightsStudioCLI-net8-Portable.v1.2.1.zip",
        "ee3fa9f3c616560276a5f49f8d50878d6ea413fdb9ac325e298cf98f58a718b8",
    ),
    "linux": (
        "https://github.com/ashleney/AssetStudio/releases/download/ak-v1.2.1/ArknightsStudioCLI-net6-linux64.v1.2.1.zip",
        "e64016c8af49c28c3e61cc783cf50b4fbde7b269919a72169b324f30a12ffb12",
    ),
}
OPTIONS = ["-g", "containerFull", "-t", "Sprite,AkPortraitSprite", "--log-level", "info"]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def fetch(url, limit, cancel):
    if cancel.is_set():
        raise InterruptedError("活动素材准备取消")
    request = urllib.request.Request(url, headers={"User-Agent": "maa-planner"})
    with urllib.request.urlopen(request, timeout=30) as response:
        final = urlsplit(response.geturl())
        if final.scheme != "https" or (
            urlsplit(url).hostname in {"ak-conf.hypergryph.com", "ak.hycdn.cn"}
            and response.geturl() != url
        ):
            raise ValueError("素材来源意外重定向")
        chunks, size = [], 0
        while True:
            if cancel.is_set():
                raise InterruptedError("活动素材准备取消")
            chunk = response.read(min(1024 * 1024, limit + 1 - size))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise ValueError("活动素材响应超过预算")


def official(url, host):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != host
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("活动素材来源离开官方主机")
    return url


def source_snapshot(get, cancel):
    raw_network = get(NETWORK, 1024 * 1024, cancel)
    network = json.loads(json.loads(raw_network)["content"])
    urls = network["configs"][network["funcVer"]]["network"]
    version_url = official(urls["hv"].replace("{0}", "Android"), "ak-conf.hypergryph.com")
    version_bytes = get(version_url, 1024 * 1024, cancel)
    version = json.loads(version_bytes)
    if not all(
        isinstance(version.get(k), str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}", version[k])
        for k in ("resVersion", "clientVersion")
    ):
        raise ValueError("游戏素材版本字段无效")
    base = official(
        urls["hu"].rstrip("/") + "/Android/assets/" + version["resVersion"] + "/", "ak.hycdn.cn"
    )
    manifest_bytes = get(base + "hot_update_list.json", 8 * 1024 * 1024, cancel)
    manifest = json.loads(manifest_bytes)
    if manifest.get("versionId") != version["resVersion"] or not isinstance(
        manifest.get("abInfos"), list
    ):
        raise ValueError("热更新清单版本与游戏资源版本不一致")
    entries = {}
    for entry in manifest["abInfos"]:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("name"), str)
            or entry["name"] in entries
        ):
            raise ValueError("热更新清单条目无效或重复")
        entries[entry["name"]] = entry
    return {
        "version": version,
        "base": base,
        "entries": entries,
        "source_hashes": {
            "network": sha(raw_network),
            "version": sha(version_bytes),
            "manifest": sha(manifest_bytes),
        },
        "checked_at": time.time(),
        "raw_sources": {
            "network": raw_network,
            "version": version_bytes,
            "manifest": manifest_bytes,
        },
    }


def safe_exporter(archive_bytes, destination):
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        seen, total = set(), 0
        for entry in archive.infolist():
            path = PurePosixPath(entry.filename)
            key = entry.filename.casefold()
            total += entry.file_size
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in entry.filename
                or ":" in entry.filename
                or key in seen
                or stat.S_ISLNK(entry.external_attr >> 16)
                or total > 512 * 1024 * 1024
            ):
                raise ValueError("导出器归档路径或展开大小无效")
            seen.add(key)
        archive.extractall(destination)
    return {
        p.relative_to(destination).as_posix(): sha(p.read_bytes())
        for p in destination.rglob("*")
        if p.is_file()
    }


def prepare_exporter(root, platform, get, cancel):
    url, checksum = EXPORTERS[platform]
    archives = root / "exporter-archives"
    archives.mkdir(exist_ok=True)
    archive_path = archives / (checksum + ".zip")
    if archive_path.exists() and archive_path.stat().st_size > 256 * 1024 * 1024:
        raise ValueError("导出器缓存归档过大")
    content = (
        archive_path.read_bytes() if archive_path.exists() else get(url, 256 * 1024 * 1024, cancel)
    )
    if len(content) > 256 * 1024 * 1024 or sha(content) != checksum:
        raise ValueError("导出器归档 SHA256 不匹配")
    if not archive_path.exists():
        atomic_write(archive_path, content)
    folder = root / "exporters" / checksum
    if folder.exists():
        # Derive expected hashes from the pinned archive, never from mutable
        # cache metadata that could authorize a different executable.
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            files = {
                entry.filename: sha(archive.read(entry))
                for entry in archive.infolist()
                if not entry.is_dir()
            }
        for path, expected in files.items():
            target = folder / path
            if (
                not target.resolve().is_relative_to(folder.resolve())
                or target.is_symlink()
                or sha(target.read_bytes()) != expected
            ):
                raise ValueError("导出器缓存被修改")
    else:
        folder.mkdir(parents=True)
        files = safe_exporter(content, folder)
    dlls = [folder / path for path in files if PurePosixPath(path).name == "ArknightsStudioCLI.dll"]
    if len(dlls) != 1:
        raise ValueError("导出器 DLL 不唯一或缺失")
    touch_cache(archive_path)
    touch_cache(folder)
    return dlls[0], checksum


def run_export(dll, input_folder, output_folder, log_path, timeout, cancel, *, max_output_bytes):
    import psutil

    dotnet = shutil.which("dotnet")
    if dotnet is None:
        raise ValueError("需要已安装的 .NET 8 dotnet；不会安装系统依赖或启动模拟器")
    environment = {**os.environ, "DOTNET_ROLL_FORWARD": "Major"}
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [dotnet, str(dll), str(input_folder), *OPTIONS, "-o", str(output_folder)],
            cwd=log_path.parent,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        began = time.monotonic()
        try:
            while process.poll() is None:
                if cancel.wait(0.1):
                    raise InterruptedError("活动素材导出取消")
                if (
                    output_folder.exists()
                    and sum(p.stat().st_size for p in output_folder.rglob("*") if p.is_file())
                    > max_output_bytes
                ):
                    raise ValueError("活动素材导出超过输出预算")
                if time.monotonic() - began >= timeout:
                    raise TimeoutError("活动素材导出超时")
            if process.returncode != 0:
                raise ValueError(f"活动素材导出失败，退出码 {process.returncode}")
        finally:
            if process.poll() is None:
                try:
                    parent = psutil.Process(process.pid)
                    children = parent.children(recursive=True)
                    for child in children:
                        child.terminate()
                    parent.terminate()
                    _, alive = psutil.wait_procs([*children, parent], timeout=2)
                    for child in alive:
                        child.kill()
                except psutil.NoSuchProcess:
                    pass
                process.wait()


def export_paths(root):
    output = {}
    for path in sorted(root.rglob("*.png")):
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("导出图片路径逃逸")
        relative = path.relative_to(root)
        if (
            len([f for f in path.parent.iterdir() if f.is_file()]) == 1
            or "#" in path.parent.as_posix()
        ):
            relative = relative.parent.parent / path.name
        key = relative.as_posix().lower()
        if key in output or ".." in relative.parts or relative.is_absolute():
            raise ValueError("导出图片路径冲突")
        output[key] = path
    return output


def hot_bundle(root, name, entry, source, remaining, get, cancel):
    if any(
        type(entry.get(k)) is not int or not 0 < entry[k] <= remaining
        for k in ("totalSize", "abSize")
    ):
        raise ValueError(f"bundle {name} 大小无效或超过本次预算")
    checksum = entry.get("md5")
    full_md5 = isinstance(checksum, str) and re.fullmatch("[a-fA-F0-9]{32}", checksum)
    opaque = (
        isinstance(checksum, str)
        and re.fullmatch("[a-f0-9]{4}", checksum)
        and re.fullmatch("anon/[a-f0-9]{32}\\.bin", name)
        and type(entry.get("cat")) is int
        and entry["cat"] == 1
        and type(entry.get("meta")) is int
        and entry["meta"] == 1
        and entry.get("hash") == checksum
    )
    if not full_md5 and not opaque:
        raise ValueError(f"bundle {name} 校验字段格式尚未支持")
    link, cached = None, None
    if full_md5:
        cache_file = root / "bundles" / (checksum.lower() + ".ab")
    else:
        # cat=1/meta=1 entries have an opaque four-hex revision token, not MD5.
        # Bind its CRC/SHA cache to this complete official manifest instead of
        # reusing a short token as a content address across resource versions.
        links = root / "anonymous-links"
        links.mkdir(exist_ok=True)
        identity = sha(
            json.dumps(
                {"name": name, "manifest": source["source_hashes"]["manifest"], "token": checksum},
                sort_keys=True,
            ).encode()
        )
        link = links / (identity + ".json")
        if link.exists():
            cached = json.loads(link.read_text(encoding="utf-8"))
            if not isinstance(cached.get("sha256"), str) or not re.fullmatch(
                "[a-f0-9]{64}", cached["sha256"]
            ):
                raise ValueError("匿名资源缓存身份无效")
            cache_file = root / "bundles" / (cached["sha256"] + ".ab")
        else:
            cache_file = None
    if cache_file is not None and cache_file.exists():
        if cache_file.stat().st_size != entry["abSize"]:
            raise ValueError("bundle 缓存大小无效")
        bundle = cache_file.read_bytes()
        crc = zlib.crc32(bundle)
    else:
        encoded = name.replace("/", "_").replace("#", "__").rsplit(".", 1)[0] + ".dat"
        content = get(source["base"] + quote(encoded, safe="[]_."), entry["totalSize"], cancel)
        if len(content) != entry["totalSize"]:
            raise ValueError("bundle 容器长度不符")
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            matches = [i for i in archive.infolist() if i.filename == name]
            if len(matches) != 1 or matches[0].file_size != entry["abSize"]:
                raise ValueError("bundle 归档缺失、重复或展开大小不符")
            bundle = archive.read(matches[0])  # zipfile verifies the archive CRC.
            crc = matches[0].CRC
    if len(bundle) != entry["abSize"] or (
        full_md5 and hashlib.md5(bundle).hexdigest().lower() != checksum.lower()
    ):
        raise ValueError("bundle 大小／MD5 不匹配")
    if cached is not None and (sha(bundle) != cached["sha256"] or crc != cached["crc32"]):
        raise ValueError("匿名资源缓存校验失败")
    if cache_file is None:
        cache_file = root / "bundles" / (sha(bundle) + ".ab")
    if not cache_file.exists():
        atomic_write(cache_file, bundle)
    if link is not None and cached is None:
        write_json(link, {"sha256": sha(bundle), "crc32": crc})
    touch_cache(cache_file)
    return (
        bundle,
        cache_file,
        {
            "source": "hot_update",
            "manifest_hash": source["source_hashes"]["manifest"],
            "bundle_hash": entry.get("hash"),
            "checksum_kind": "md5" if full_md5 else "opaque_revision_zip_crc",
            "checksum": checksum,
            "crc32": crc,
        },
    )


@contextmanager
def base_apk_source(settings, version, arguments, cancel):
    from maa_planner.daemon.constants import GAME_APK_HOST, GAME_LATEST_URL, GAME_PACKAGE
    from maa_planner.daemon.tools.apk import read_apk, read_manifest
    from maa_planner.daemon.tools.game_update import cache_resource, resolve_latest
    from maa_planner.daemon.tools.remote_apk import (
        MANIFEST_LIMIT,
        REQUEST_LIMIT,
        TRANSFER_LIMIT,
        RangeFile,
    )

    # Use a matching local artifact if available, without replacing it. A newer
    # cached APK is not a source for an older, already-pinned resource snapshot.
    with acquire([cache_resource(settings.game_cache)]):
        path = settings.game_cache / "latest.apk"
        try:
            info = read_apk(path) if path.is_file() else None
        except (ValueError, zipfile.BadZipFile, KeyError):
            info = None
        if (
            info is not None
            and info.package == GAME_PACKAGE
            and info.version_name == version["clientVersion"]
        ):
            descriptor = {
                "source": "apk_cache",
                "client_version": info.version_name,
                "version_code": info.version_code,
                "apk_size": path.stat().st_size,
                "apk_mtime_ns": path.stat().st_mtime_ns,
            }
            mark_apk_used(path)
            with zipfile.ZipFile(path) as archive:
                yield archive, descriptor
            return
    url = resolve_latest(GAME_LATEST_URL, allowed_host=GAME_APK_HOST)
    with RangeFile(
        url,
        allowed_host=GAME_APK_HOST,
        transfer_limit=TRANSFER_LIMIT,
        max_requests=4096 + arguments["max_bundle_bytes"] // REQUEST_LIMIT,
        large_reads=True,
        deadline_seconds=arguments["export_timeout"],
        cancel=cancel,
    ) as remote:
        with zipfile.ZipFile(remote) as archive:
            manifests = [
                entry for entry in archive.infolist() if entry.filename == "AndroidManifest.xml"
            ]
            if (
                len(manifests) != 1
                or manifests[0].file_size > MANIFEST_LIMIT
                or manifests[0].compress_size > REQUEST_LIMIT
            ):
                raise ValueError("基础 APK metadata 缺失或超过局部预算")
            info = read_manifest(archive.read(manifests[0]))
            if info.package != GAME_PACKAGE or info.version_name != version["clientVersion"]:
                raise ValueError("远端 APK 与固定的资源客户端版本不一致")
            # Directory/manifest retain the small metadata budget. Only after
            # identity validation are selected members allowed a larger budget.
            remote.transfer_limit = remote.transferred + arguments["max_bundle_bytes"]
            yield (
                archive,
                {
                    "source": "apk_range",
                    "client_version": info.version_name,
                    "version_code": info.version_code,
                    "apk_url": url,
                    "validator": remote.validator,
                },
            )


def prepare_assets(settings, arguments, cancel, *, get=fetch, export=run_export, platform=None):
    arguments = validate_assets(arguments)
    if cancel.is_set():
        raise InterruptedError("活动素材准备取消")
    platform = platform or ("windows" if os.name == "nt" else "linux")
    root = settings.cache_directory / "activity-assets"
    resource = data_resource(root)
    root = filesystem_path(root)
    root.mkdir(parents=True, exist_ok=True)
    with acquire([resource]), ExitStack() as base_stack:
        source = source_snapshot(get, cancel)
        (root / "metadata").mkdir(exist_ok=True)
        for name, content in source["raw_sources"].items():
            metadata = root / "metadata" / (source["source_hashes"][name] + ".json")
            if not metadata.exists():
                atomic_write(metadata, content)
            elif sha(metadata.read_bytes()) != source["source_hashes"][name]:
                raise ValueError("素材来源 metadata 缓存损坏")
        version = source["version"]
        requested = arguments["bundles"]
        selected = requested
        index_evidence = None
        if arguments["resolve_dependencies"]:
            from maa_planner.daemon.tools.asset_index import (
                MAX_INDEX_BYTES,
                AssetIndex,
                dependency_closure,
            )

            indices = [entry for name, entry in source["entries"].items() if name.endswith(".idx")]
            if len(indices) != 1:
                raise ValueError("官方资源索引缺失或不唯一")
            entry = indices[0]
            if (
                any(
                    type(entry.get(k)) is not int or not 0 < entry[k] <= MAX_INDEX_BYTES
                    for k in ("totalSize", "abSize")
                )
                or not isinstance(entry.get("md5"), str)
                or not re.fullmatch("[a-fA-F0-9]{32}", entry["md5"])
            ):
                raise ValueError("资源索引清单大小或校验无效")
            indices_root = root / "indices"
            indices_root.mkdir(exist_ok=True)
            index_path = indices_root / (entry["md5"].lower() + ".idx")
            if index_path.exists():
                if index_path.stat().st_size != entry["abSize"]:
                    raise ValueError("资源索引缓存长度不匹配")
                index_bytes = index_path.read_bytes()
            else:
                url = source["base"] + quote(entry["name"].rsplit(".", 1)[0] + ".dat", safe="[]_.")
                container = get(url, entry["totalSize"], cancel)
                if len(container) != entry["totalSize"]:
                    raise ValueError("资源索引容器长度不匹配")
                with zipfile.ZipFile(io.BytesIO(container)) as archive:
                    matches = [
                        item for item in archive.infolist() if item.filename == entry["name"]
                    ]
                    if len(matches) != 1 or matches[0].file_size != entry["abSize"]:
                        raise ValueError("资源索引归档无效")
                    index_bytes = archive.read(matches[0])
            if (
                len(index_bytes) != entry["abSize"]
                or hashlib.md5(index_bytes).hexdigest().lower() != entry["md5"].lower()
            ):
                raise ValueError("资源索引 MD5 不匹配")
            touch_cache(index_path)
            graph = AssetIndex(index_bytes).dependencies()
            selected = dependency_closure(graph, requested)
            if len(selected) > 1000:
                raise ValueError("资源依赖闭包超过 1000 个输入预算")
            if not index_path.exists():
                atomic_write(index_path, index_bytes)
            index_evidence = {
                "name": entry["name"],
                "md5": entry["md5"],
                "sha256": sha(index_bytes),
                "format": "prefix128-flatbuffers-reader1",
            }
        bundle_inputs: list[dict] = []
        (root / "bundles").mkdir(exist_ok=True)
        remaining = arguments["max_bundle_bytes"]
        base_archive, base_descriptor = None, None
        # Prefer a matching cached APK; otherwise read selected remote members.
        # Exporting art never installs the APK or starts an emulator.
        for name in selected:
            if cancel.is_set():
                raise InterruptedError("活动素材准备取消")
            entry = source["entries"].get(name)
            if entry is not None:
                bundle, cache_file, provenance = hot_bundle(
                    root, name, entry, source, remaining, get, cancel
                )
            else:
                if base_archive is None:
                    base_archive, base_descriptor = base_stack.enter_context(
                        base_apk_source(settings, version, arguments, cancel)
                    )
                paths = [
                    i for i in base_archive.infolist() if i.filename == "assets/AB/Android/" + name
                ]
                if len(paths) != 1 or not 0 < paths[0].file_size <= remaining:
                    raise ValueError("APK 基础资源缺失、重复或超过预算")
                member = paths[0]
                links = root / "base-links"
                links.mkdir(exist_ok=True)
                key = sha(
                    json.dumps(
                        {
                            "apk": base_descriptor,
                            "name": name,
                            "crc": member.CRC,
                            "size": member.file_size,
                        },
                        sort_keys=True,
                    ).encode()
                )
                link = links / (key + ".json")
                if link.exists():
                    cached_sha = json.loads(link.read_text(encoding="utf-8")).get("sha256")
                    if not isinstance(cached_sha, str) or not re.fullmatch(
                        "[a-f0-9]{64}", cached_sha
                    ):
                        raise ValueError("APK 资源缓存身份无效")
                    cache_file = root / "bundles" / (cached_sha + ".ab")
                    if cache_file.stat().st_size != member.file_size:
                        raise ValueError("APK 资源缓存长度不符")
                    bundle = cache_file.read_bytes()
                    if sha(bundle) != cached_sha or zlib.crc32(bundle) != member.CRC:
                        raise ValueError("APK 资源缓存校验失败")
                else:
                    bundle = base_archive.read(member)
                    cache_file = root / "bundles" / (sha(bundle) + ".ab")
                    if not cache_file.exists():
                        atomic_write(cache_file, bundle)
                    write_json(link, {"sha256": sha(bundle)})
                assert base_descriptor is not None
                provenance = dict(base_descriptor)
            remaining -= len(bundle)
            bundle_inputs.append(
                {
                    "name": name,
                    "sha256": sha(bundle),
                    "size": len(bundle),
                    "file": cache_file,
                    **provenance,
                }
            )
        base_stack.close()
        tool_hash = EXPORTERS[platform][1]
        export_key = sha(
            json.dumps(
                {
                    "inputs": [(i["name"], i["sha256"]) for i in bundle_inputs],
                    "exporter": tool_hash,
                    "options": OPTIONS,
                },
                sort_keys=True,
            ).encode()
        )
        manifest = {
            "activity_id": arguments["activity_id"],
            "client_version": version["clientVersion"],
            "res_version": version["resVersion"],
            "source_hashes": source["source_hashes"],
            "exporter_sha256": tool_hash,
            "export_options": OPTIONS,
            "export_key": export_key,
            "dependency_scope": "index_closure"
            if arguments["resolve_dependencies"]
            else "explicit_bundle_inputs",
            "requested_bundles": requested,
            "index": index_evidence,
            "inputs": [{k: v for k, v in entry.items() if k != "file"} for entry in bundle_inputs],
        }
        identity = sha(json.dumps(manifest, sort_keys=True).encode())
        destination = root / "snapshots" / identity

        def checked_assets(folder):
            previous = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            if folder != destination and previous.get("export_key") != export_key:
                raise ValueError("导出缓存版本不匹配")
            if previous["source"] != manifest and folder == destination:
                raise ValueError("私有素材版本证据不匹配")
            if sum(a["size"] for a in previous["assets"]) > arguments["max_output_bytes"]:
                raise ValueError("私有素材超过当前输出预算")
            for asset in previous["assets"]:
                path = folder / asset["path"]
                if (
                    not path.resolve().is_relative_to(folder.resolve())
                    or path.is_symlink()
                    or path.stat().st_size != asset["size"]
                    or sha(path.read_bytes()) != asset["sha256"]
                ):
                    raise ValueError("私有素材快照损坏")
            touch_cache(folder)
            return previous["assets"]

        if (destination / "READY").exists():
            assets = checked_assets(destination)
            return {
                "snapshot": str(destination),
                "identity": identity,
                "reused": True,
                "status": "exported",
                "asset_count": len(assets),
                "checked_at": source["checked_at"],
                "res_version": version["resVersion"],
                "client_version": version["clientVersion"],
                "dependency_scope": manifest["dependency_scope"],
            }
        job = root / "jobs" / uuid.uuid4().hex
        job.mkdir(parents=True)
        write_json(job / "source.json", manifest)
        export_cache = root / "exports" / export_key
        if (export_cache / "READY").exists():
            assets = checked_assets(export_cache)
        else:
            dll, _ = prepare_exporter(root, platform, get, cancel)
            (job / "input").mkdir()
            for entry in bundle_inputs:
                target = job / "input" / entry["name"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(entry["file"], target)
            export(
                dll,
                job / "input",
                job / "exported",
                job / "export.log",
                arguments["export_timeout"],
                cancel,
                max_output_bytes=arguments["max_output_bytes"],
            )
            if cancel.is_set():
                raise InterruptedError("活动素材发布前取消")
            assets, total = [], 0
            exported = export_paths(job / "exported")
            for name, path in exported.items():
                total += path.stat().st_size
                if total > arguments["max_output_bytes"]:
                    raise ValueError("导出图片超过预算")
                content = path.read_bytes()
                if (
                    len(content) < 33
                    or content[:8] != b"\x89PNG\r\n\x1a\n"
                    or content[12:16] != b"IHDR"
                ):
                    raise ValueError("导出图片格式无效")
                width, height = struct.unpack_from(">II", content, 16)
                if min(width, height) <= 0:
                    raise ValueError("导出图片尺寸无效")
                assets.append(
                    {
                        "id": name,
                        "path": "images/" + name,
                        "sha256": sha(content),
                        "size": len(content),
                        "width": width,
                        "height": height,
                    }
                )
            if not assets:
                raise ValueError("导出器没有产生 PNG，保留现场并拒绝空快照")
            staging = job / "export-cache"
            staging.mkdir()
            for asset, original in zip(assets, exported.values(), strict=True):
                target = staging / asset["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, target)
            write_json(
                staging / "manifest.json",
                {"source": manifest, "assets": assets, "export_key": export_key},
            )
            if cancel.is_set():
                raise InterruptedError("活动素材缓存发布前取消")
            write_json(staging / "READY", {"export_key": export_key})
            export_cache.parent.mkdir(exist_ok=True)
            staging.rename(export_cache)
        staging = job / "snapshot"
        staging.mkdir()
        for asset in assets:
            if cancel.is_set():
                raise InterruptedError("活动素材快照发布取消")
            target = staging / asset["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(export_cache / asset["path"], target)
        write_json(
            staging / "manifest.json",
            {
                "source": manifest,
                "assets": assets,
                "checked_at": source["checked_at"],
                "job": str(job),
            },
        )
        if cancel.is_set():
            raise InterruptedError("活动素材快照发布取消")
        write_json(staging / "READY", {"identity": identity})
        destination.parent.mkdir(exist_ok=True)
        staging.rename(destination)
        return {
            "snapshot": str(destination),
            "identity": identity,
            "reused": False,
            "status": "exported",
            "asset_count": len(assets),
            "checked_at": source["checked_at"],
            "res_version": version["resVersion"],
            "client_version": version["clientVersion"],
            "dependency_scope": manifest["dependency_scope"],
        }
