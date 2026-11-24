"""GitHub Actions: 실제 Linux 이미지에서 인증·DB 재시작·OCR 런타임을 검증한다."""
import io
import json
import secrets
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
    password = secrets.token_urlsafe(32)
    try:
        docker("volume", "create", volume)
        docker("run", "-d", "--name", name, "--read-only", "--tmpfs", "/tmp:size=256m,mode=1777",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "-p", "127.0.0.1::8080",
               "-v", volume + ":/data", "-e", "ADMIN_PASSWORD=" + password, "court-monitor:test")
        mapping = json.loads(docker("inspect", name).stdout)[0]["NetworkSettings"]["Ports"]["8080/tcp"][0]
        port = mapping["HostPort"]
        wait_ready(port)
        base = f"http://127.0.0.1:{port}"
        session = requests.Session()
        assert session.get(base + "/api/cases", timeout=5).status_code == 401
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
    finally:
        docker("rm", "-f", name, check=False)
        docker("volume", "rm", volume, check=False)


if __name__ == "__main__":
    main()
