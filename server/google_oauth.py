"""각 NAS가 자신의 OAuth 클라이언트·토큰을 보관한다. 공통 인증 서버는 사용하지 않는다."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
import threading
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
import requests


SCOPE = "https://www.googleapis.com/auth/calendar.app.created"
CALLBACK = "/oauth/google/callback"


class OAuthError(Exception):
    pass


class Vault:
    def __init__(self, store):
        self.store = store
        path = Path(store.path).parent / "credentials.key"
        if not path.exists():
            with store.db() as db:
                encrypted = db.execute("SELECT 1 FROM meta WHERE value LIKE '%enc:v1:%' LIMIT 1").fetchone()
            if encrypted:
                raise RuntimeError("Google 인증정보 암호화 키가 없습니다. DB와 credentials.key를 함께 복원하세요.")
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(Fernet.generate_key())
        self.fernet = Fernet(path.read_bytes())

    def encode(self, value):
        return "enc:v1:" + self.fernet.encrypt(json.dumps(value).encode()).decode()

    def decode(self, value):
        try:
            return json.loads(self.fernet.decrypt(value.removeprefix("enc:v1:").encode()))
        except (InvalidToken, ValueError, AttributeError) as exc:
            raise OAuthError("Google 인증정보를 복호화하지 못했습니다. DB와 암호화 키의 백업을 확인하세요.") from exc

    def get(self, key, default=None):
        value = self.store.get_meta(key)
        return self.decode(value) if value else default

    def set(self, key, value):
        self.store.set_meta(key, self.encode(value))


def check_redirect(uri):
    try:
        if not isinstance(uri, str) or len(uri) > 2048:
            raise ValueError()
        parsed = urlsplit(uri)
        parsed.port
    except ValueError as exc:
        raise OAuthError("올바른 콜백 URI를 입력하세요.") from exc
    loopback = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path != CALLBACK:
        raise OAuthError("돌아올 주소는 안내 화면의 콜백 주소와 정확히 같아야 합니다.")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise OAuthError("Google 연결에는 HTTPS 주소가 필요합니다. localhost SSH 터널도 사용할 수 있습니다. 상세 안내를 확인하세요.")
    if not parsed.hostname:
        raise OAuthError("올바른 접속 주소를 입력하세요.")
    return uri


class GoogleConnection:
    def __init__(self, store, timezone="Asia/Seoul", flow_factory=Flow.from_client_config, session_factory=AuthorizedSession):
        self.store, self.timezone = store, timezone
        self.vault = Vault(store)
        self.flow_factory, self.session_factory = flow_factory, session_factory
        self._lock = threading.RLock()
        with store.db(True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS google_oauth_states (
                state_hash TEXT PRIMARY KEY, session_hash TEXT NOT NULL, expires REAL NOT NULL,
                verifier TEXT NOT NULL, redirect_uri TEXT NOT NULL, generation INTEGER NOT NULL)""")

    @property
    def mode(self):
        return self.store.get_meta("google_mode") == "oauth"

    @property
    def generation(self):
        return self.store.get_meta("google_generation", 0)

    @property
    def calendar_id(self):
        return self.store.get_meta("google_calendar", {}).get("id", "") if self.mode else ""

    @property
    def connected(self):
        return self.mode and bool(self.calendar_id and self.store.get_meta("google_tokens"))

    def status(self):
        return {"client_configured": bool(self.store.get_meta("google_client")),
                "authorized": bool(self.store.get_meta("google_tokens") or self.store.get_meta("google_pending_tokens")) and self.mode,
                "connected": self.connected, "calendar_id": self.calendar_id,
                "calendar_name": self.store.get_meta("google_calendar", {}).get("summary", ""),
                "error": self.store.get_meta("google_error", "")}

    def configure(self, config):
        with self._lock:
            return self._configure(config)

    def _configure(self, config):
        web = config.get("web", {})
        if not isinstance(web, dict):
            raise OAuthError("웹 애플리케이션 OAuth 클라이언트 JSON을 선택하세요.")
        client_id, secret = web.get("client_id", ""), web.get("client_secret", "")
        uris = web.get("redirect_uris", [])
        if not isinstance(client_id, str) or not client_id.endswith(".apps.googleusercontent.com") or not isinstance(secret, str) or not 1 <= len(secret) <= 512:
            raise OAuthError("Google Cloud에서 받은 웹 애플리케이션 OAuth 클라이언트 JSON을 선택하세요. 서비스 계정 키와 데스크톱 클라이언트는 사용할 수 없습니다.")
        if not isinstance(uris, list) or not uris or len(uris) > 50:
            raise OAuthError("OAuth 클라이언트에 승인된 리디렉션 URI를 추가하고 JSON을 다시 내려받으세요.")
        for uri in uris:
            check_redirect(uri)
        # 업로드한 JSON의 임의 URL로 토큰이나 비밀값이 전송되지 않도록 Google 주소를 고정한다.
        client = {"client_id": client_id, "client_secret": secret, "redirect_uris": uris,
                  "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth", "token_uri": "https://oauth2.googleapis.com/token"}
        old = self.vault.get("google_client")
        if old != client:
            with self.store.db(True) as db:
                for key in ("google_tokens", "google_calendar", "google_pending_tokens"):
                    db.execute("DELETE FROM meta WHERE key=?", (key,))
                db.execute("DELETE FROM google_oauth_states")
            self.store.set_meta("google_generation", self.generation + 1)
        self.vault.set("google_client", client)
        self.store.set_meta("google_mode", "oauth")
        self.store.set_meta("google_error", "")

    def begin(self, redirect_uri, session_token, now=None):
        check_redirect(redirect_uri)
        client = self.vault.get("google_client")
        if not client:
            raise OAuthError("먼저 상세 안내에 따라 OAuth 클라이언트 JSON을 등록하세요.")
        if redirect_uri not in client["redirect_uris"]:
            raise OAuthError("현재 접속 주소가 JSON의 승인된 리디렉션 URI에 없습니다. 안내 화면의 콜백 주소를 Google Cloud에 추가하고 JSON을 다시 등록하세요.")
        now = time.time() if now is None else now
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        flow = self.flow_factory({"web":client}, scopes=[SCOPE], state=state, code_verifier=verifier)
        flow.redirect_uri = redirect_uri
        url, _ = flow.authorization_url(access_type="offline", prompt="consent select_account")
        with self.store.db(True) as db:
            db.execute("DELETE FROM google_oauth_states WHERE expires<=? OR session_hash=?", (now, hashlib.sha256(session_token.encode()).hexdigest()))
            db.execute("INSERT INTO google_oauth_states VALUES (?,?,?,?,?,?)", (hashlib.sha256(state.encode()).hexdigest(),
                hashlib.sha256(session_token.encode()).hexdigest(), now + 600, self.vault.encode(verifier), redirect_uri, self.generation))
        return url

    def _consume(self, state, session_token, now=None):
        now = time.time() if now is None else now
        if not 20 <= len(state) <= 128:
            raise OAuthError("Google 연결 요청이 올바르지 않습니다. 연결 버튼을 다시 누르세요.")
        with self.store.db(True) as db:
            row = db.execute("SELECT * FROM google_oauth_states WHERE state_hash=? AND session_hash=? AND expires>?",
                (hashlib.sha256(state.encode()).hexdigest(), hashlib.sha256(session_token.encode()).hexdigest(), now)).fetchone()
            if not row or row["generation"] != self.generation:
                raise OAuthError("연결 요청이 만료되었거나 다른 기기의 요청입니다. 다시 연결하세요.")
            db.execute("DELETE FROM google_oauth_states WHERE state_hash=?", (row["state_hash"],))
            return dict(row)

    def finish(self, state, code, session_token, now=None):
        with self._lock:
            return self._finish(state, code, session_token, now)

    def _finish(self, state, code, session_token, now=None):
        row = self._consume(state, session_token, now)
        client = self.vault.get("google_client")
        flow = self.flow_factory({"web":client}, scopes=[SCOPE], state=state, code_verifier=self.vault.decode(row["verifier"]))
        flow.redirect_uri = row["redirect_uri"]
        try:
            flow.fetch_token(code=code, timeout=30)
            credentials = flow.credentials
            if not credentials.refresh_token:
                raise OAuthError("장기 연결 권한을 받지 못했습니다. Google 연결을 다시 승인하세요.")
            if credentials.granted_scopes is not None and SCOPE not in credentials.granted_scopes:
                raise OAuthError("사건 전용 캘린더 권한을 승인하지 않았습니다.")
        except OAuthError:
            raise
        except Exception as exc:
            raise OAuthError("Google 인증을 완료하지 못했습니다. OAuth 클라이언트·동의 화면 설정을 확인하고 다시 연결하세요.") from exc
        if row["generation"] != self.generation:
            raise OAuthError("연결 설정이 변경되었습니다. 다시 연결하세요.")
        self.vault.set("google_pending_tokens", json.loads(credentials.to_json()))
        return self.prepare_calendar()

    def cancel(self, state, session_token):
        self._consume(state, session_token)
        self.store.set_meta("google_error", "Google 연결이 취소되었습니다. 연결 버튼으로 다시 시도할 수 있습니다.")

    def prepare_calendar(self):
        with self._lock:
            return self._prepare_calendar()

    def _prepare_calendar(self):
        pending = self.vault.get("google_pending_tokens")
        if not pending:
            raise OAuthError("Google 계정에 먼저 연결하세요.")
        credentials = Credentials.from_authorized_user_info(pending, scopes=[SCOPE])
        session = self.session_factory(credentials)
        previous = self.store.get_meta("google_calendar", {})
        try:
            calendar = None
            if previous.get("id"):
                from urllib.parse import quote
                response = session.get("https://www.googleapis.com/calendar/v3/calendars/" + quote(previous["id"],safe=""), timeout=30)
                if response.ok:
                    calendar = response.json()
                elif response.status_code not in (403,404):
                    raise OAuthError("기존 사건 캘린더를 확인하지 못했습니다. 잠시 후 캘린더 준비를 다시 시도하세요.")
            if not calendar:
                response = session.post("https://www.googleapis.com/calendar/v3/calendars", json={"summary":"사건 관리", "timeZone":self.timezone,
                    "description":"Court Monitor가 관리하는 사건 전용 캘린더"}, timeout=30)
                if not response.ok:
                    raise OAuthError("전용 캘린더를 만들지 못했습니다. Google Calendar API 사용 설정과 권한 승인을 확인한 뒤 다시 시도하세요.")
                calendar = response.json()
            if not isinstance(calendar.get("id"), str) or not calendar["id"]:
                raise OAuthError("Google에서 올바른 캘린더 ID를 받지 못했습니다.")
            self.vault.set("google_tokens", json.loads(credentials.to_json()))
            self.store.set_meta("google_calendar", {"id":calendar["id"],"summary":calendar.get("summary","사건 관리")})
            self.store.set_meta("google_mode", "oauth")
            self.store.set_meta("google_generation", self.generation + 1)
            self.store.set_meta("google_error", "")
            with self.store.db(True) as db:
                db.execute("DELETE FROM meta WHERE key='google_pending_tokens'")
            self.store.set_calendar_target(calendar["id"])
            return calendar["id"]
        except OAuthError:
            raise
        except Exception as exc:
            raise OAuthError("Google Calendar와 통신하지 못했습니다. 캘린더 준비를 다시 시도하세요.") from exc
        finally:
            session.close()

    def credentials(self):
        return Credentials.from_authorized_user_info(self.vault.get("google_tokens"), scopes=[SCOPE])

    def persist_refresh(self, credentials, generation):
        encoded = self.vault.encode(json.loads(credentials.to_json()))
        with self.store.db(True) as db:
            row = db.execute("SELECT value FROM meta WHERE key='google_generation'").fetchone()
            if row and json.loads(row[0]) == generation:
                db.execute("UPDATE meta SET value=? WHERE key='google_tokens'", (json.dumps(encoded),))

    def disconnect(self):
        with self._lock:
            self._disconnect()

    def _disconnect(self):
        with self.store.db(True) as db:
            for key in ("google_tokens", "google_pending_tokens"):
                db.execute("DELETE FROM meta WHERE key=?", (key,))
            db.execute("DELETE FROM google_oauth_states")
        self.store.set_meta("google_generation", self.generation + 1)
        self.store.set_meta("google_error", "")
