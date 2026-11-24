"""SQLite 관리자 암호·세션. 암호 및 브라우저 세션 토큰의 원문은 DB에 저장하지 않는다."""
import hashlib
import hmac
import json
import secrets
import time

from fastapi import HTTPException, Request


COOKIE = "court_session"
ITERATIONS = 600_000


def password_hash(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password, encoded):
    try:
        scheme, rounds, salt, expected = encoded.split("$")
        if scheme != "pbkdf2_sha256" or not 100_000 <= int(rounds) <= 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError, AttributeError):
        return False


class Auth:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        with store.db(True) as db:
            for statement in (
                "CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, created REAL NOT NULL, expires REAL NOT NULL)",
                "CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires)",
                "CREATE TABLE IF NOT EXISTS login_attempts (client_hash TEXT PRIMARY KEY, failures INTEGER NOT NULL, started REAL NOT NULL)",
            ):
                db.execute(statement)
            row = db.execute("SELECT value FROM meta WHERE key='admin_password_hash'").fetchone()
            if not row and settings.password:
                if not 16 <= len(settings.password) <= 256:
                    raise RuntimeError("관리자 비밀번호는 16~256자여야 합니다.")
                db.execute("INSERT INTO meta VALUES ('admin_password_hash',?)", (json.dumps(password_hash(settings.password)),))
            db.execute("INSERT OR IGNORE INTO meta VALUES ('auth_secret',?)", (json.dumps(secrets.token_hex(32)),))
        self.secret = store.get_meta("auth_secret").encode()

    @property
    def configured(self):
        return bool(self.store.get_meta("admin_password_hash"))

    def setup(self, password):
        if not 16 <= len(password) <= 256:
            raise HTTPException(422, "비밀번호는 16~256자여야 합니다.")
        with self.store.db(True) as db:
            if db.execute("SELECT 1 FROM meta WHERE key='admin_password_hash'").fetchone():
                raise HTTPException(409, "관리자 비밀번호가 이미 설정되어 있습니다.")
            db.execute("INSERT INTO meta VALUES ('admin_password_hash',?)", (json.dumps(password_hash(password)),))
        return self.new_session()

    def csrf_token(self, token):
        return hmac.new(self.secret, token.encode(), hashlib.sha256).hexdigest()

    def token(self, request):
        return request.cookies.get(COOKIE, "")

    def valid_session(self, token, now=None):
        if not 20 <= len(token) <= 128:
            return False
        now = time.time() if now is None else now
        with self.store.db() as db:
            return db.execute("SELECT 1 FROM sessions WHERE token_hash=? AND expires>?", (hashlib.sha256(token.encode()).hexdigest(), now)).fetchone() is not None

    def require(self, request: Request):
        token = self.token(request)
        if not self.valid_session(token):
            raise HTTPException(401, "로그인이 필요합니다.")
        return token

    def login(self, password, client, now=None):
        if not self.configured:
            raise HTTPException(409, "먼저 관리자 비밀번호를 설정하세요.")
        now = time.time() if now is None else now
        client_hash = hmac.new(self.secret, client.encode(), hashlib.sha256).hexdigest()
        with self.store.db(True) as db:
            db.execute("DELETE FROM login_attempts WHERE started<?", (now - 900,))
            row = db.execute("SELECT * FROM login_attempts WHERE client_hash=?", (client_hash,)).fetchone()
            if row and row["failures"] >= 5:
                raise HTTPException(429, "로그인 시도가 많습니다. 잠시 후 다시 시도하세요.", headers={"Retry-After": str(max(1, int(row["started"] + 900 - now)))})
            encoded = json.loads(db.execute("SELECT value FROM meta WHERE key='admin_password_hash'").fetchone()[0])
            success = verify_password(password, encoded)
            if success:
                db.execute("DELETE FROM login_attempts WHERE client_hash=?", (client_hash,))
            else:
                db.execute("INSERT INTO login_attempts VALUES (?,1,?) ON CONFLICT(client_hash) DO UPDATE SET failures=failures+1", (client_hash, now))
        if not success:
            raise HTTPException(401, "비밀번호가 올바르지 않습니다.")
        return self.new_session(now)

    def new_session(self, now=None):
        now = time.time() if now is None else now
        token = secrets.token_urlsafe(32)
        with self.store.db(True) as db:
            db.execute("DELETE FROM sessions WHERE expires<=?", (now,))
            db.execute("INSERT INTO sessions VALUES (?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), now, now + self.settings.session_days * 86400))
            db.execute("DELETE FROM sessions WHERE token_hash IN (SELECT token_hash FROM sessions ORDER BY created DESC LIMIT -1 OFFSET 100)")
        return token

    def logout(self, token):
        with self.store.db(True) as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))

    def change_password(self, current, new):
        if not 16 <= len(new) <= 256:
            raise HTTPException(422, "새 비밀번호는 16~256자여야 합니다.")
        with self.store.db(True) as db:
            encoded = json.loads(db.execute("SELECT value FROM meta WHERE key='admin_password_hash'").fetchone()[0])
            if not verify_password(current, encoded):
                raise HTTPException(403, "현재 비밀번호가 올바르지 않습니다.")
            db.execute("UPDATE meta SET value=? WHERE key='admin_password_hash'", (json.dumps(password_hash(new)),))
            db.execute("DELETE FROM sessions")
            db.execute("DELETE FROM login_attempts")

    def set_cookie(self, response, token, request):
        response.set_cookie(COOKIE, token, max_age=self.settings.session_days * 86400,
                            httponly=True, secure=self.settings.cookie_secure or request.url.scheme == "https" or request.headers.get("origin", "").startswith("https://"),
                            samesite="lax", path="/")

    @staticmethod
    def clear_cookie(response):
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="lax")
