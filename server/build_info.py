"""Container build identity, shared by health checks and the web UI."""
import os

VERSION_SERIES = "1.0"


def get_build_info():
    return {
        "version": os.environ.get("COURT_MONITOR_VERSION", "development"),
        "revision": os.environ.get("COURT_MONITOR_REVISION", "unknown"),
        "created": os.environ.get("COURT_MONITOR_CREATED", ""),
    }
