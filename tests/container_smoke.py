"""GitHub Actions: 실제 Linux 이미지에서 인증·DB 재시작·OCR 런타임을 검증한다."""
import io
import json
import os
import secrets
import socket
import subprocess
import time

import requests


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
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "-p", f"127.0.0.1:{port}:8080",
               "-e", "COURT_AUTO_UPDATE=false", "-v", volume + ":/data", "court-monitor:test")
        wait_ready(port)
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
        assert session.get(base + "/api/cases", timeout=5).ok, "Session did not survive restart"
        ocr = """import io
from PIL import Image, ImageDraw, ImageFont
import ddddocr
image=Image.new('RGB',(180,60),'white')
ImageDraw.Draw(image).text((5,5),'123456',fill='black',font=ImageFont.load_default(size=32))
stream=io.BytesIO(); image.save(stream,format='PNG')
engine=ddddocr.DdddOcr(show_ad=False); engine.set_ranges('0123456789')
assert isinstance(engine.classification(stream.getvalue()),str)
print('Linux OCR model inference passed')
"""
        print(docker("exec", "-i", name, "python", "-", input=ocr).stdout.strip())
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
