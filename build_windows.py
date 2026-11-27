"""Build the native Windows app; compile installer/windows.iss with Inno Setup next."""
import argparse
from pathlib import Path
import shutil
import tempfile
import ddddocr
import PyInstaller.__main__
from desktop_monitor import tray_image
from desktop_updates import VERSION_FILE, installed_version, version_parts

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default=installed_version())
    args = parser.parse_args()
    version_parts(args.version)
    root = Path(__file__).resolve().parent
    assets = root / "build/desktop-assets"
    assets.mkdir(parents=True, exist_ok=True)
    icon = assets / "court-monitor.ico"
    tray_image().save(icon, sizes=[(16,16), (24,24), (32,32), (48,48), (64,64)])
    model = Path(ddddocr.__file__).parent / "common_old.onnx"
    # A new staging directory avoids OneDrive locks when replacing build folders.
    with tempfile.TemporaryDirectory(prefix="court-monitor-build-") as staging:
        version_file = Path(staging) / VERSION_FILE
        version_file.write_text(args.version + "\n", encoding="utf-8")
        PyInstaller.__main__.run([
            "--noconfirm", "--onedir", "--windowed", "--name", "CourtMonitorPC", "--distpath", staging,
            "--icon", str(icon), "--copy-metadata", "ddddocr", "--copy-metadata", "google-auth-oauthlib",
            "--collect-data", "tzdata", "--hidden-import", "pystray._win32",
            "--add-data", f"{model};ddddocr", "--add-data", f"{root / 'WINDOWS-PC.md'};.",
            "--add-data", f"{version_file};.",
            "--exclude-module", "fastapi", "--exclude-module", "uvicorn", "--exclude-module", "starlette",
            "--exclude-module", "playwright", "--exclude-module", "pytest",
            str(root / "desktop_monitor.py"),
        ])
        shutil.copytree(Path(staging) / "CourtMonitorPC", root / "dist/CourtMonitorPC", dirs_exist_ok=True)
    print("Windows app ready: " + str(root / "dist/CourtMonitorPC/CourtMonitorPC.exe"))
