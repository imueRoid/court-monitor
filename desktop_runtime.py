"""Native PC runtime: SQLite, per-user Windows encryption and optional startup."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import hashlib
import html
import json
import os
from pathlib import Path
import subprocess
import sys
import re

from google_auth_oauthlib.flow import InstalledAppFlow

from court_client import CaseNumber, CourtError
from server.config import Settings
from server.google_oauth import GoogleConnection, OAuthError, SCOPE


APP_NAME = "Court Monitor PC"
VERSION = "1.0.0"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "CourtMonitorDesktop"


def data_directory():
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "CourtMonitorPC"


def launch_command():
    if getattr(sys, "frozen", False):
        return [sys.executable, "--startup"]
    interpreter = Path(sys.executable).with_name("pythonw.exe")
    return [str(interpreter if interpreter.exists() else Path(sys.executable)),
            str(Path(__file__).with_name("desktop_monitor.py")), "--startup"]


class Startup:
    def enabled(self):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                command = winreg.QueryValueEx(key, RUN_NAME)[0]
            return command == subprocess.list2cmdline(launch_command())
        except FileNotFoundError:
            return False

    def set(self, enabled):
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if enabled:
                winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(launch_command()))
            else:
                try:
                    winreg.DeleteValue(key, RUN_NAME)
                except FileNotFoundError:
                    pass


def save_preferences(store, startup, *, auto_start, background):
    previous = startup.enabled()
    startup.set(auto_start)
    try:
        store.set_meta("desktop_preferences", {"auto_start": bool(auto_start), "background": bool(background)})
    except Exception:
        startup.set(previous)
        raise


class SingleInstance:
    def __init__(self, folder):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        name = RUN_NAME
        if Path(folder).resolve() != data_directory().resolve():
            name += "-" + hashlib.sha256(str(Path(folder).resolve()).encode()).hexdigest()[:16]
        self.kernel = kernel
        self.handle = kernel.CreateMutexW(None, False, name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.already_running = ctypes.get_last_error() == 183

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]


def protect_bytes(value, *, decrypt=False):
    """DPAPI binds secrets to the current Windows user and machine."""
    raw = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(raw, ctypes.POINTER(ctypes.c_byte)))
    output = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        method = crypt.CryptUnprotectData
        method.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                           ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        success = method(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        method = crypt.CryptProtectData
        method.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
                           ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        success = method(ctypes.byref(source), APP_NAME, None, None, None, 1, ctypes.byref(output))
    if not success:
        raise OAuthError("현재 Windows 사용자로 Google 인증정보를 읽거나 저장하지 못했습니다. Google 계정을 다시 연결하세요.")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))


class WindowsVault:
    def __init__(self, store):
        self.store = store

    def encode(self, value):
        return "dpapi:v1:" + base64.b64encode(protect_bytes(json.dumps(value).encode())).decode("ascii")

    def decode(self, value):
        try:
            if not value.startswith("dpapi:v1:"):
                raise ValueError()
            return json.loads(protect_bytes(base64.b64decode(value[9:], validate=True), decrypt=True))
        except (ValueError, TypeError) as exc:
            raise OAuthError("PC용 Google 인증정보가 아닙니다. 이 PC에서 Google 계정을 다시 연결하세요.") from exc

    def get(self, key, default=None):
        value = self.store.get_meta(key)
        return self.decode(value) if value else default

    def set(self, key, value):
        self.store.set_meta(key, self.encode(value))


class DesktopGoogleConnection(GoogleConnection):
    def __init__(self, store, timezone="Asia/Seoul", *, flow_factory=InstalledAppFlow.from_client_config,
                 vault_factory=WindowsVault, **kwargs):
        super().__init__(store, timezone, vault_factory=vault_factory, **kwargs)
        self.desktop_flow_factory = flow_factory

    def status(self):
        return {**super().status(), "client_configured": bool(self.store.get_meta("desktop_google_client"))}

    def configure(self, config):
        installed = config.get("installed", {})
        client_id, secret = installed.get("client_id", ""), installed.get("client_secret", "")
        if not isinstance(client_id, str) or not client_id.endswith(".apps.googleusercontent.com") or not isinstance(secret, str) or not 1 <= len(secret) <= 512:
            raise OAuthError("Google Cloud에서 유형을 '데스크톱 앱'으로 만든 OAuth JSON을 선택하세요. NAS용 웹 클라이언트 JSON은 사용할 수 없습니다.")
        client = {"client_id": client_id, "client_secret": secret,
                  "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                  "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": ["http://localhost"]}
        with self._lock:
            self.vault.set("desktop_google_client", client)
            self.store.set_meta("google_generation", self.generation + 1)
            self.store.set_meta("google_mode", "oauth")
            self.store.set_meta("google_error", "")
            with self.store.db(True) as db:
                db.execute("DELETE FROM meta WHERE key='google_pending_tokens'")

    def authorize(self):
        with self._lock:
            client = self.vault.get("desktop_google_client")
            generation = self.generation
        if not client:
            raise OAuthError("먼저 설정 탭에서 데스크톱 앱 OAuth JSON을 등록하세요.")
        try:
            flow = self.desktop_flow_factory({"installed": client}, scopes=[SCOPE], autogenerate_code_verifier=True)
            credentials = flow.run_local_server(host="127.0.0.1", bind_addr="127.0.0.1", port=0,
                timeout_seconds=180, authorization_prompt_message="", open_browser=True,
                success_message="Google Calendar connected. You can close this window and return to Court Monitor PC.",
                access_type="offline", prompt="consent select_account")
            if not credentials.refresh_token or credentials.granted_scopes is not None and SCOPE not in credentials.granted_scopes:
                raise OAuthError("장기 연결과 사건 전용 캘린더 권한을 승인하세요.")
            with self._lock:
                if generation != self.generation:
                    raise OAuthError("연결 설정이 변경되었습니다. 다시 연결하세요.")
                self.vault.set("google_pending_tokens", json.loads(credentials.to_json()))
                return self.prepare_calendar()
        except OAuthError:
            raise
        except Exception as exc:
            raise OAuthError("Google 인증이 취소되었거나 3분이 지났습니다. Google Cloud의 API·테스트 사용자 설정을 확인하고 다시 연결하세요.") from exc


def validate_case(court, number, party):
    court, party = court.strip(), "".join(party.split())
    if not 2 <= len(court) <= 100 or not 2 <= len(party) <= 100:
        raise CourtError("법원명과 당사자명을 두 글자 이상 입력하세요.")
    return court, str(CaseNumber.parse(number)), party


def pc_settings(folder):
    return Settings(database=Path(folder) / "cases.sqlite3")


def render_guide(markdown):
    text = html.escape(markdown)
    text = re.sub(r"\[([^\]]+)\]\((https://[^\s)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"^#{1,3} (.+)$", r"<h2>\1</h2>", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    return '<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>사건 일정 관리 PC 안내</title><style>body{font-family:system-ui,"Malgun Gothic",sans-serif;background:#f3f6f8;color:#172a36;margin:0}main{max-width:960px;margin:32px auto;background:white;border-radius:12px;padding:32px;white-space:pre-wrap;line-height:1.8;overflow-wrap:anywhere}h2{white-space:normal;color:#17664e}a{color:#185d78}code{background:#edf4f0;padding:2px 4px}@media(max-width:600px){main{padding:18px;margin:12px}}</style><main>' + text + '</main></html>'
