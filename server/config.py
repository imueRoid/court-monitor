from dataclasses import dataclass, field
from pathlib import Path
import os


@dataclass
class Settings:
    database: Path = Path("/data/cases.sqlite3")
    username: str = "admin"
    password: str = field(default="", repr=False)
    calendar_id: str = ""
    credentials_file: Path = Path("/run/secrets/google_credentials")
    credentials_json: str = field(default="", repr=False)
    session_days: int = 90
    cookie_secure: bool = False
    timezone: str = "Asia/Seoul"
    base_poll_hours: int = 24
    hearing_delay_minutes: int = 30
    verdict_retry_minutes: int = 30
    verdict_checks_per_day: int = 8
    watch_start_hour: int = 9
    watch_end_hour: int = 19
    request_gap_seconds: int = 5

    @classmethod
    def from_env(cls):
        explicit_file = os.environ.get("ADMIN_PASSWORD_FILE", "").strip()
        password = os.environ.get("ADMIN_PASSWORD", "")
        password_file = Path(explicit_file or "/run/secrets/admin_password")
        if explicit_file or not password:
            try:
                password = password_file.read_text("utf-8").strip()
            except OSError as exc:
                if explicit_file:
                    raise RuntimeError("관리자 암호 secret 파일을 읽지 못했습니다.") from exc
        if password and not 16 <= len(password) <= 256:
            raise RuntimeError("관리자 암호는 16자 이상이어야 합니다.")
        settings = cls(database=Path(os.environ.get("DATABASE_PATH", "/data/cases.sqlite3")),
            username=os.environ.get("ADMIN_USER", "admin"), password=password,
            calendar_id=os.environ.get("GOOGLE_CALENDAR_ID", "").strip(),
            credentials_file=Path(os.environ.get("GOOGLE_CREDENTIALS_FILE", "/run/secrets/google_credentials")),
            credentials_json=os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip(),
            session_days=int(os.environ.get("SESSION_DAYS", "90")),
            cookie_secure=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
            timezone=os.environ.get("TZ", "Asia/Seoul"))
        if not 1 <= settings.session_days <= 365:
            raise RuntimeError("로그인 유지 기간은 1~365일이어야 합니다.")
        for name in ("base_poll_hours", "hearing_delay_minutes", "verdict_retry_minutes", "verdict_checks_per_day",
                     "watch_start_hour", "watch_end_hour", "request_gap_seconds"):
            setattr(settings, name, int(os.environ.get(name.upper(), getattr(settings, name))))
        if not 0 <= settings.watch_start_hour < settings.watch_end_hour <= 23:
            raise RuntimeError("선고 확인 시간대를 확인하세요.")
        if not (1 <= settings.base_poll_hours <= 168 and 1 <= settings.verdict_checks_per_day <= 24
                and 10 <= settings.verdict_retry_minutes <= 1440 and 1 <= settings.hearing_delay_minutes <= 1440
                and 1 <= settings.request_gap_seconds <= 3600):
            raise RuntimeError("조회 주기 설정이 허용 범위를 벗어났습니다.")
        return settings

