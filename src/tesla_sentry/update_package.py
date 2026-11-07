"""Portable ZIP update format shared by the producer and device-side validator."""
import hashlib
import io
import json
import re
import stat
import zipfile
import zlib

RUNTIME_FILES = ("sentry-viewer.py", "tesla-live-sync.py", "tesla-gadget.sh",
                 "tesla-sync.sh", "usb-selfheal.sh", "tesla-img.sh", "tsf-check.sh")
MAX_DOWNLOAD = 16 * 1024 * 1024
MAX_UNPACKED = 32 * 1024 * 1024
MAX_MEMBER = 8 * 1024 * 1024
MAX_MANIFEST = 128 * 1024
KIND = "tesla-sentry-update"


def version_key(version):
    if not isinstance(version, str) or len(version) > 32 or not re.fullmatch(r"\d+(?:\.\d+){1,3}", version):
        raise ValueError("更新包版本号无效")
    parts = tuple(int(n) for n in version.split("."))
    return parts + (0,) * (4 - len(parts))


def _json(data):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError("更新清单包含重复字段")
            out[key] = value
        return out
    result = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
    if not isinstance(result, dict):
        raise ValueError("更新清单须为 JSON 对象")
    return result


def read_zip(data):
    """Read only named members; never extract archive paths onto the filesystem."""
    if not data or len(data) > MAX_DOWNLOAD:
        raise ValueError("更新 ZIP 为空或超过 16 MiB")
    expected = set(RUNTIME_FILES) | {"manifest.json"}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            items = archive.infolist()
            names = [item.filename for item in items]
            if len(items) != len(expected) or set(names) != expected:
                raise ValueError("ZIP 文件清单不正确; 请使用 tools/package_update.py 生成更新包, 不要使用源码 ZIP")
            if sum(item.file_size for item in items) > MAX_UNPACKED:
                raise ValueError("ZIP 解压后超过 32 MiB")
            for item in items:
                limit = MAX_MANIFEST if item.filename == "manifest.json" else MAX_MEMBER
                kind = stat.S_IFMT(item.external_attr >> 16)
                if (item.orig_filename != item.filename or item.flag_bits & 1 or
                        kind not in (0, stat.S_IFREG) or item.file_size < 0 or item.file_size > limit or
                        item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    raise ValueError("ZIP 包含不支持的文件类型、加密文件或超大文件")
            manifest = _json(archive.read("manifest.json"))
            if manifest.get("kind") != KIND or manifest.get("format") != 1:
                raise ValueError("不支持的更新 ZIP 格式")
            version_key(manifest.get("version"))
            notes = manifest.get("notes", [])
            if not isinstance(notes, list) or len(notes) > 50 or any(not isinstance(n, str) or len(n) > 1000 for n in notes):
                raise ValueError("更新说明格式无效")
            records = manifest.get("files")
            if not isinstance(records, dict) or set(records) != set(RUNTIME_FILES):
                raise ValueError("更新清单缺少运行文件")
            files = {}
            for name in RUNTIME_FILES:
                content = archive.read(name)
                record = records[name]
                if (not isinstance(record, dict) or type(record.get("size")) is not int or
                        record["size"] != len(content) or
                        not re.fullmatch(r"[a-f0-9]{64}", str(record.get("sha256", ""))) or
                        hashlib.sha256(content).hexdigest() != record["sha256"]):
                    raise ValueError("更新文件校验失败: " + name)
                files[name] = {"sha256": record["sha256"], "content": content.decode("utf-8")}
    except (zipfile.BadZipFile, zipfile.LargeZipFile, UnicodeError, KeyError, RuntimeError, EOFError, zlib.error) as error:
        raise ValueError("更新 ZIP 损坏或无法读取") from error
    # Convert to the existing staged, atomic release installation contract.
    release = {"format": 1, "version": manifest["version"], "files": files}
    return manifest, json.dumps(release, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def make_zip(release, metadata):
    if release.get("format") != 1 or set(release.get("files", {})) != set(RUNTIME_FILES):
        raise ValueError("构建程序包文件清单无效")
    version = release.get("version")
    version_key(version)
    if metadata.get("version") != version:
        raise ValueError("版本清单与程序包不一致")
    manifest = {"kind": KIND, "format": 1, "version": version,
                "date": metadata.get("date", ""), "notes": metadata.get("notes", []), "files": {}}
    payloads = {}
    for name in RUNTIME_FILES:
        record = release["files"][name]
        data = record["content"].encode("utf-8")
        if hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise ValueError("构建文件校验失败: " + name)
        payloads[name] = data
        manifest["files"][name] = {"sha256": record["sha256"], "size": len(data)}
    payloads["manifest.json"] = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(payloads.items()):
            item = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.create_system = 3
            item.external_attr = 0o100644 << 16
            archive.writestr(item, data)
    data = output.getvalue()
    read_zip(data)  # Apply the device's exact validation before publishing any artifact.
    return data
