"""배포 전 한 번 실행한다. 기존 암호·자격증명·환경설정을 덮어쓰지 않는다."""
from pathlib import Path
import os
import secrets


def prepare(root):
    root = Path(root)
    folder = root / "secrets"
    folder.mkdir(exist_ok=True, mode=0o700)
    if os.name == "posix":
        folder.chmod(0o700)
    files = {folder / "admin-password.txt": secrets.token_urlsafe(32) + "\n",
             folder / "google-service-account.json": "{}\n"}
    for path, content in files.items():
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            if os.name == "posix":
                path.chmod(0o644)
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        if os.name == "posix":
            path.chmod(0o644)
    environment = root / ".env"
    if not environment.exists():
        environment.write_text((root / ".env.example").read_text("utf-8"), encoding="utf-8")
    return folder / "admin-password.txt"


if __name__ == "__main__":
    path = prepare(Path(__file__).resolve().parents[1])
    print("준비 완료. 관리자 암호 파일:", path)
    print("Google Calendar 연동은 앱의 설정 → Google Calendar 연결에서 진행하세요.")
    print("기존 Google 서비스 계정으로 운영하는 경우 server/README.md의 호환 설정을 참고하세요.")
