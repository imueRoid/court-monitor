"""Build the tested NAS application release and identify its compatible runtime."""
import argparse
from hashlib import file_digest, sha256
import json
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "court-monitor-app-linux-amd64.zip"
MANIFEST = "court-monitor-update.json"
RUNTIME_FILES = (
    ".dockerignore", "Dockerfile", "requirements-server.lock.txt",
    "nas/Cargo.toml", "nas/Cargo.lock", "nas/src/main.rs",
    "nas/src/releases.rs", "nas/src/supervise.rs",
    "scripts/package_update.py",
)
APPLICATION_FILES = ("court_client.py", "case_summary.py", "captcha_ocr.py")


def runtime_id():
    digest = sha256()
    for name in RUNTIME_FILES:
        digest.update(name.encode() + b"\0")
        digest.update(sha256((ROOT / name).read_bytes()).digest())
    return digest.hexdigest()


def package(output, binary, version, revision, created):
    output.mkdir(parents=True, exist_ok=True)
    archive = output / PACKAGE
    sources = [ROOT / name for name in APPLICATION_FILES]
    sources.extend((ROOT / "server").glob("*.py"))
    sources.extend(path for path in (ROOT / "server/static").iterdir()
                   if path.suffix in {".html", ".css", ".js"})
    with ZipFile(archive, "w", ZIP_DEFLATED) as compressed:
        compressed.write(binary, "court-monitor-nas")
        for path in sorted(sources):
            compressed.write(path, path.relative_to(ROOT).as_posix())
    with archive.open("rb") as stream:
        digest = file_digest(stream, "sha256").hexdigest()
    manifest = {"version": version, "revision": revision, "created": created,
                "runtime": runtime_id(), "sha256": digest}
    (output / MANIFEST).write_text(json.dumps(manifest) + "\n", encoding="utf-8")


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--runtime-id", action="store_true")
    cli.add_argument("--output", type=Path)
    cli.add_argument("--binary", type=Path)
    cli.add_argument("--version")
    cli.add_argument("--revision")
    cli.add_argument("--created")
    args = cli.parse_args()
    if args.runtime_id:
        print(runtime_id())
    elif all((args.output, args.binary, args.version, args.revision, args.created)):
        package(args.output, args.binary, args.version, args.revision, args.created)
    else:
        cli.error("release packaging requires --output, --binary, --version, --revision and --created")
