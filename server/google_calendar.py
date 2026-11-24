import json
from urllib.parse import quote

from google.auth.transport.requests import AuthorizedSession
from google.auth.exceptions import GoogleAuthError
from google.oauth2 import service_account
import requests
from .google_oauth import GoogleConnection, OAuthError


class CalendarError(Exception):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


class GoogleCalendar:
    def __init__(self, settings, store=None):
        self.settings = settings
        self.session = None
        self.connection = GoogleConnection(store, settings.timezone) if store else None
        self.session_generation = None
        self.oauth_credentials = None

    @property
    def calendar_id(self):
        if self.connection and self.connection.mode:
            return self.connection.calendar_id
        return self.settings.calendar_id

    @property
    def configured(self):
        if self.connection and self.connection.mode:
            return self.connection.connected
        if not self.settings.calendar_id:
            return False
        try:
            return self._credential_info().get("type") == "service_account"
        except (OSError, ValueError, AttributeError):
            return False

    def _credential_info(self):
        content = self.settings.credentials_json or self.settings.credentials_file.read_text("utf-8")
        return json.loads(content)

    def _session(self):
        generation = self.connection.generation if self.connection else None
        if self.connection and self.session and self.session_generation != generation:
            self.session.close()
            self.session, self.oauth_credentials = None, None
        if self.session is None:
            if not self.configured:
                raise CalendarError("전용 캘린더 ID와 서비스 계정 자격증명 파일을 설정하세요.")
            try:
                if self.connection and self.connection.mode:
                    credentials = self.connection.credentials()
                    self.oauth_credentials = credentials
                else:
                    credentials = service_account.Credentials.from_service_account_info(self._credential_info(),
                        scopes=["https://www.googleapis.com/auth/calendar.events"])
                self.session = AuthorizedSession(credentials)
                self.session_generation = generation
            except (ValueError, OSError, OAuthError) as exc:
                raise CalendarError("Google 인증정보를 읽지 못했습니다. 관리 화면에서 연결 설정을 확인하세요.") from exc
        return self.session

    def upsert(self, event_id, payload):
        session = self._session()
        base = "https://www.googleapis.com/calendar/v3/calendars/" + quote(self.calendar_id, safe="") + "/events"
        params = {"sendUpdates": "none"}
        patch = dict(payload)
        # 종일 일정과 시간 지정 일정 사이를 바꿀 때 기존 날짜 표현을 제거한다.
        for field in ("start", "end"):
            if field in payload:
                value = dict(payload[field])
                if "dateTime" in value:
                    value["date"] = None
                elif "date" in value:
                    value.update(dateTime=None, timeZone=None)
                patch[field] = value
        try:
            response = session.patch(base + "/" + event_id, json=patch, params=params, timeout=30)
            if response.status_code == 404:
                response = session.post(base, json={**payload, "id": event_id}, params=params, timeout=30)
                if response.status_code == 409:
                    response = session.patch(base + "/" + event_id, json=patch, params=params, timeout=30)
        except GoogleAuthError as exc:
            raise CalendarError("Google 인증 갱신에 실패했습니다. 관리 화면에서 Google 계정을 다시 연결하세요.") from exc
        except requests.RequestException as exc:
            raise CalendarError("Google Calendar 서버에 연결하지 못했습니다.") from exc
        finally:
            if self.oauth_credentials and self.connection:
                self.connection.persist_refresh(self.oauth_credentials, self.session_generation)
        if not response.ok:
            message = {401: "Google 인증을 확인하세요.", 403: "전용 캘린더의 서비스 계정 쓰기 권한 또는 API 사용량을 확인하세요.",
                       404: "전용 캘린더 ID와 공유 설정을 확인하세요.", 429: "Google Calendar 요청 한도에 도달했습니다."}.get(response.status_code, "Google Calendar 일정을 갱신하지 못했습니다.")
            retry = response.headers.get("Retry-After", "")
            raise CalendarError(message + f" (HTTP {response.status_code})", int(retry) if retry.isdigit() else 0)
        return response.json()
