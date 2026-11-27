"""Windows release identity and update checks against the public GitHub release."""
import json
from pathlib import Path
import re
import sys
from urllib.request import urlopen


RELEASES = "https://github.com/imueRoid/court-monitor/releases"
MANIFEST = "court-monitor-windows-update.json"
INSTALLER = "CourtMonitorPC-Setup.exe"
VERSION_FILE = "desktop-version.txt"


def version_parts(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,4})(?:\.(?:0|[1-9][0-9]{0,4})){2}", value):
        raise ValueError("Invalid Windows release version")
    return tuple(map(int, value.split(".")))


def installed_version():
    if getattr(sys, "frozen", False):
        version = (Path(sys._MEIPASS) / VERSION_FILE).read_text(encoding="utf-8").strip()
        version_parts(version)
        return version
    return "1.0.0"


def check_update(current):
    url = f"{RELEASES}/latest/download/{MANIFEST}"
    with urlopen(url, timeout=8) as response:
        data = response.read(4097)
    if len(data) > 4096:
        raise ValueError("Windows update information is too large")
    release = json.loads(data)
    if not isinstance(release, dict) or release.get("installer") != INSTALLER:
        raise ValueError("Windows installer information is missing")
    latest = release.get("version")
    return latest if version_parts(latest) > version_parts(current) else None


def release_page(version):
    version_parts(version)
    return f"{RELEASES}/tag/app-v{version}"
