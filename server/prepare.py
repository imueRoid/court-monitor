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
    print("전용 캘린더 ID를 .env에, 서비스 계정 JSON을 secrets/google-service-account.json에 설정하세요.")
