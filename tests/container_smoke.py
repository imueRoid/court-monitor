"""GitHub Actions: 실제 Linux 이미지에서 인증·DB 재시작·OCR 런타임을 검증한다."""
import io
import json
import os
import secrets
import shlex
import socket
import subprocess
import time

import requests
from PIL import Image, ImageDraw, ImageFont


LEGACY_COMMAND = ["python", "-m", "uvicorn", "server.app:create_app", "--factory",
                  "--host", "0.0.0.0", "--port", "7708", "--workers", "1", "--no-access-log"]
LEGACY_HEALTH = ["python", "-c",
                 "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7708/health',timeout=3)"]


def docker(*args, input=None, check=True):
    return subprocess.run(["docker", *args], input=input, text=True, capture_output=True, check=check)


def wait_ready(port):
    for _ in range(90):
        try:
            if requests.get(f"http://127.0.0.1:{port}/health", timeout=2).ok:
                return
        except requests.RequestException:
            pass
        time.sleep(1)
    raise AssertionError("Container did not become ready")


def wait_healthy(name):
    for _ in range(30):
        status = docker("inspect", "--format", "{{.State.Health.Status}}", name).stdout.strip()
        if status == "healthy":
            return
        if status == "unhealthy":
            break
        time.sleep(1)
    raise AssertionError("The previous Synology health check failed")


def main():
    name = "court-monitor-ci-" + secrets.token_hex(4)
    volume = name + "-data"
    password = secrets.token_urlsafe(6)  # 실제 이미지에서도 최소 길이인 8자로 최초 설정·로그인을 검사한다.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    try:
        docker("volume", "create", volume)
        docker("run", "-d", "--name", name, "--read-only", "--tmpfs", "/tmp:size=256m,mode=1777",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "-p", f"127.0.0.1:{port}:7708",
               "--health-cmd", shlex.join(LEGACY_HEALTH), "--health-interval", "2s",
               "-e", "COURT_AUTO_UPDATE=false", "-v", volume + ":/data", "court-monitor:test", *LEGACY_COMMAND)
        wait_ready(port)
        wait_healthy(name)
        docker("exec", name, *LEGACY_HEALTH)
        docker("exec", name, "court-monitor-nas", "--healthcheck", "7708")
        sqlite = docker("exec", name, "court-monitor-nas", "--sqlite-version").stdout.strip()
        assert tuple(map(int, sqlite.split("."))) >= (3, 54, 1), sqlite
        base = f"http://127.0.0.1:{port}"
        health = requests.get(base + "/health", timeout=5).json()
        expected = os.environ["EXPECTED_VERSION"]
        assert health["version"] == expected
        labels = json.loads(docker("image", "inspect", "--format", "{{json .Config.Labels}}", "court-monitor:test").stdout)
        assert labels["org.opencontainers.image.version"] == health["version"]
        assert labels["org.opencontainers.image.revision"] == health["revision"]
        session = requests.Session()
        assert session.get(base + "/api/cases", timeout=5).status_code == 401
        assert "처음 설정" in session.get(base + "/login", timeout=5).text
        assert session.post(base + "/api/setup", json={"password": password}, timeout=10).ok
        bootstrap = session.get(base + "/api/bootstrap", timeout=5).json()
        assert bootstrap["build"]["version"] == expected
        assert session.post(base + "/api/logout", json={}, headers={"X-CSRF-Token":bootstrap["csrf_token"]}, timeout=5).ok
        assert "관리자 로그인" in session.get(base + "/login", timeout=5).text
        assert session.post(base + "/api/login", json={"password": password}, timeout=10).ok
        assert session.get(base + "/api/bootstrap", timeout=5).json()["session_days"] == 90
        docker("restart", name)
        wait_ready(port)
        wait_healthy(name)
        assert session.get(base + "/api/cases", timeout=5).ok, "Session did not survive restart"
        # A generated image checks the model without querying a real case.
        image = Image.new("RGB", (200, 64), "white")
        ImageDraw.Draw(image).text((10, 5), "123456", fill="black", font=ImageFont.load_default(size=40))
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        ocr = subprocess.run(["docker", "exec", "-i", name, "/bin/sh", "-c",
            "cat >/tmp/ocr-test.png && court-monitor-nas --ocr-smoke /tmp/ocr-test.png"],
            input=stream.getvalue(), capture_output=True, check=True)
        assert ocr.stdout.decode().strip() == "123456", ocr
        assert docker("exec", name, "python", "-c", "print(1)", check=False).returncode != 0
        docker("exec", name, "/bin/sh", "-c", "! command -v python3")
        print("Container authentication, persistent SQLite session and OCR checks passed")
    except Exception:
        logs = docker("logs", "--tail", "100", name, check=False)
        print(logs.stdout)
        print(logs.stderr)
        print(docker("inspect", "--format", "{{json .State}}", name, check=False).stdout)
        raise
    finally:
        docker("rm", "-f", name, check=False)
        docker("volume", "rm", volume, check=False)


if __name__ == "__main__":
    main()
