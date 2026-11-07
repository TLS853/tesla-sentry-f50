#!/usr/bin/env python3
"""Bundle importable Python modules and browser resources into standalone launchers."""
import argparse
import ast
import base64
import hashlib
import io
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src/tesla_sentry"
RUNTIME = ("sentry-viewer.py", "tesla-live-sync.py", "tesla-gadget.sh", "tesla-sync.sh",
           "usb-selfheal.sh", "tesla-img.sh", "tsf-check.sh")


def outputs(version=None):
    source_version = ast.literal_eval(next(n.value for n in ast.parse((PACKAGE / "__init__.py").read_text()).body
                                    if isinstance(n, ast.Assign) and n.targets[0].id == "__version__"))
    version = version or source_version
    resources = {}
    for fp in sorted(PACKAGE.rglob("*")):
        if not fp.is_file() or "__pycache__" in fp.parts or fp.suffix not in (".py", ".html", ".css", ".js", ".json"):
            continue
        text = fp.read_text(encoding="utf-8")
        if fp.name == "__init__.py":
            text = text.replace('__version__ = "' + source_version + '"', '__version__ = "' + version + '"')
        if fp.suffix == ".py":
            ast.parse(text, filename=str(fp), feature_version=(3, 7))
            compile(text, str(fp), "exec")
        resources["tesla_sentry/" + fp.relative_to(PACKAGE).as_posix()] = text.encode()
    for name in RUNTIME:
        if name.endswith(".sh"):
            resources["tesla_sentry/web/runtime/" + name] = (ROOT / "scripts" / name).read_bytes()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(resources.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    encoded = base64.b85encode(stream.getvalue()).decode("ascii")
    payload = "\n".join("    " + repr(encoded[i:i + 100]) for i in range(0, len(encoded), 100))
    template = (ROOT / "tools/launcher.py.in").read_text(encoding="utf-8")
    result = {}
    for name, entry in (("sentry-viewer.py", "viewer"), ("tesla-live-sync.py", "sync")):
        content = template.replace("@@VERSION@@", version).replace("@@ENTRYPOINT@@", entry).replace("@@PAYLOAD@@", payload)
        compile(content, name, "exec")
        result[name] = content
    files = {}
    for name in RUNTIME:
        content = result[name] if name in result else (ROOT / "scripts" / name).read_text(encoding="utf-8")
        files[name] = {"sha256": hashlib.sha256(content.encode()).hexdigest(), "content": content}
    bundle = json.dumps({"format": 1, "version": version, "files": files}, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    result["dist/release.json"] = bundle
    manifest = json.loads((ROOT / "version.json").read_text(encoding="utf-8"))
    manifest.update(version=version, bundle="dist/release.json", sha256=hashlib.sha256(bundle.encode()).hexdigest())
    result["version.json"] = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if generated artifacts are stale")
    args = parser.parse_args()
    stale = []
    for path, content in outputs().items():
        fp = ROOT / path
        if not fp.exists() or fp.read_text(encoding="utf-8") != content:
            stale.append(path)
            if not args.check:
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(content, encoding="utf-8")
    if args.check and stale:
        print("Stale build outputs: " + ", ".join(stale), file=sys.stderr)
        return 1
    print("Build verified" if args.check else "Built modular standalone launchers and release bundle")
    return 0


if __name__ == "__main__":
    sys.exit(main())
