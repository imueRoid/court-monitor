from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import re
import secrets
import time
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from court_client import CaseNumber, CourtError
from .config import Settings
from .build_info import get_build_info
from .auth import Auth
from .google_oauth import CALLBACK, OAuthError
from .domain import next_hearing, has_order, progress_interval
from .store import Store
from .worker import Worker


STATIC = Path(__file__).parent / "static"


class CaseInput(BaseModel):
    court: str = Field(min_length=2, max_length=100)
    case_number: str = Field(min_length=5, max_length=30)
    party: str = Field(min_length=2, max_length=100)

    @field_validator("case_number")
    @classmethod
    def check_case(cls, value):
        try:
            return str(CaseNumber.parse(value))
        except CourtError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("court", "party")
    @classmethod
    def check_text(cls, value, info):
        value = re.sub(r"\s+", "", value) if info.field_name == "party" else value.strip()
        if len(value) < 2:
            raise ValueError("두 글자 이상 입력하세요.")
        return value


class CaseImport(BaseModel):
    cases: list[CaseInput] = Field(min_length=1, max_length=1000)


class ActiveInput(BaseModel):
    active: bool


class CaseDelete(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=10000)

    @field_validator("ids")
    @classmethod
    def check_ids(cls, values):
        if any(not re.fullmatch(r"[a-f0-9]{32}", value) for value in values):
            raise ValueError("올바른 사건 ID가 아닙니다.")
        return values


class LoginInput(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class PasswordInput(BaseModel):
    current: str = Field(min_length=1, max_length=256)
    new: str = Field(min_length=8, max_length=256)


class GoogleClientInput(BaseModel):
    config: dict


class GoogleStartInput(BaseModel):
    redirect_uri: str = Field(max_length=2048)


def create_app(settings=None, *, start_worker=True, worker_factory=Worker):
    settings = settings or Settings.from_env()
    build = get_build_info()
    store = Store(settings.database)
    store.resume_no_hearing_cancelled_cases(settings)
    auth = Auth(store, settings)
    worker = worker_factory(store, settings)
    google = worker.calendar.connection

    def csrf(request: Request, x_csrf_token: str = Header(default="")):
        token = auth.require(request)
        if not secrets.compare_digest(x_csrf_token.encode(), auth.csrf_token(token).encode()):
            raise HTTPException(403, "요청 인증값이 올바르지 않습니다. 화면을 새로고침하세요.")

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            worker.start()
        yield
        if start_worker:
            worker.stop()

    app = FastAPI(title="사건 일정 관리", version=build["version"], lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.worker, app.state.settings, app.state.auth = store, worker, settings, auth

    @app.middleware("http")
    async def headers(request, call_next):
        if request.headers.get("content-length", "0").isdigit() and int(request.headers.get("content-length", "0")) > 2_000_000:
            return JSONResponse({"detail": "요청 파일이 너무 큽니다."}, status_code=413)
        if request.method == "POST" and request.headers.get("origin"):
            from urllib.parse import urlsplit
            if urlsplit(request.headers["origin"]).netloc != request.url.netloc:
                return JSONResponse({"detail": "다른 사이트에서 보낸 요청은 허용되지 않습니다."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer" if request.url.path == CALLBACK else "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    def health():
        return {"status": "ok", **build}

    @app.get("/guide/google")
    def google_guide():
        return FileResponse(STATIC / "google-guide.html")

    @app.get("/about")
    def about():
        return FileResponse(STATIC / "home.html")

    @app.get("/privacy")
    @app.get("/privacy.html")
    def privacy():
        return FileResponse(STATIC / "privacy.html")

    @app.get("/terms")
    def terms():
        return FileResponse(STATIC / "terms.html")

    @app.get("/")
    def index(request: Request):
        token = auth.token(request)
        if token and auth.configured and auth.valid_session(token):
            return FileResponse(STATIC / "index.html")
        return about()

    @app.get("/login")
    def login_page(request: Request):
        if not auth.configured:
            return RedirectResponse("/setup", status_code=303)
        if auth.valid_session(auth.token(request)):
            return RedirectResponse("/", status_code=303)
        return FileResponse(STATIC / "login.html")

    @app.get("/setup")
    def setup_page():
        if auth.configured:
            return RedirectResponse("/login", status_code=303)
        return FileResponse(STATIC / "setup.html")

    @app.post("/api/setup")
    def setup(payload: LoginInput, request: Request):
        token = auth.setup(payload.password)
        response = JSONResponse({"configured": True})
        auth.set_cookie(response, token, request)
        return response

    @app.post("/api/login")
    def login(payload: LoginInput, request: Request):
        token = auth.login(payload.password, request.client.host if request.client else "unknown")
        response = JSONResponse({"logged_in": True})
        auth.set_cookie(response, token, request)
        return response

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    api = APIRouter(prefix="/api", dependencies=[Depends(auth.require)])

    @api.post("/logout", dependencies=[Depends(csrf)])
    def logout(request: Request):
        auth.logout(auth.token(request))
        response = JSONResponse({"logged_out": True})
        auth.clear_cookie(response)
        return response

    @api.post("/password", dependencies=[Depends(csrf)])
    def change_password(payload: PasswordInput):
        auth.change_password(payload.current, payload.new)
        response = JSONResponse({"changed": True, "login_required": True})
        auth.clear_cookie(response)
        return response

    @api.get("/bootstrap")
    def bootstrap(request: Request):
        return {"csrf_token": auth.csrf_token(auth.token(request)), "timezone": settings.timezone,
                "session_days": settings.session_days, "build": build,
                "google": google.status(),
                "courts": store.get_meta("courts", []), "calendar_configured": worker.calendar.configured,
                "verdict_checks_per_day": settings.verdict_checks_per_day,
                "verdict_retry_minutes": settings.verdict_retry_minutes,
                "watch_hours": [settings.watch_start_hour, settings.watch_end_hour]}

    @api.get("/google/status")
    def google_status():
        return google.status()

    @api.post("/google/client", dependencies=[Depends(csrf)])
    def google_client(payload: GoogleClientInput):
        try:
            google.configure(payload.config)
        except OAuthError as exc:
            raise HTTPException(422, str(exc)) from exc
        return google.status()

    @api.post("/google/start", dependencies=[Depends(csrf)])
    def google_start(payload: GoogleStartInput, request: Request):
        from urllib.parse import urlsplit
        if urlsplit(payload.redirect_uri).netloc.lower() != request.url.netloc.lower():
            raise HTTPException(422, "현재 접속한 주소의 콜백 URL을 사용하세요.")
        try:
            return {"url": google.begin(payload.redirect_uri, auth.token(request))}
        except OAuthError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.post("/google/calendar", dependencies=[Depends(csrf)])
    def google_calendar_retry():
        try:
            google.prepare_calendar()
        except OAuthError as exc:
            google.store.set_meta("google_error", str(exc))
            raise HTTPException(422, str(exc)) from exc
        return google.status()

    @api.post("/google/disconnect", dependencies=[Depends(csrf)])
    def google_disconnect():
        google.disconnect()
        return google.status()

    @app.get(CALLBACK, dependencies=[Depends(auth.require)])
    def google_callback(request: Request, state: str = "", code: str = "", error: str = ""):
        try:
            if error:
                google.cancel(state, auth.token(request))
                return RedirectResponse("/?google=cancelled",status_code=303)
            if not code or len(code) > 4096:
                raise OAuthError("Google 인증 코드가 없습니다. 다시 연결하세요.")
            google.finish(state, code, auth.token(request))
            return RedirectResponse("/?google=connected",status_code=303)
        except OAuthError as exc:
            google.store.set_meta("google_error", str(exc))
            return RedirectResponse("/?google=error",status_code=303)

    @api.get("/cases")
    def cases(q: str = Query("", max_length=200), page: int = Query(1, ge=1),
              page_size: int = Query(10, ge=1, le=100), scope: Literal["all", "ongoing", "finished"] = "all"):
        now = datetime.now(timezone.utc)
        result = store.case_page(q, page, page_size, now=now, timezone_name=settings.timezone, scope=scope)
        for record in result["cases"]:
            upcoming = next_hearing(record["observed"], now, settings.timezone)
            record["next_hearing"] = asdict(upcoming) if upcoming else None
            record["show_progress"] = not has_order(record["observed"].get("summary", {}))
            record["progress_interval"] = progress_interval(record["observed"], now, settings.timezone) if record["show_progress"] else None
        return {**result, "worker_heartbeat": store.get_meta("worker_heartbeat"),
                "calendar_configured": worker.calendar.configured}

    @api.post("/cases", dependencies=[Depends(csrf)])
    def add_case(case: CaseInput):
        return {"id": store.add_case(case.court, case.case_number, case.party)}

    @api.post("/cases/import", dependencies=[Depends(csrf)])
    def import_cases(payload: CaseImport):
        ids = [store.add_case(case.court, case.case_number, case.party) for case in payload.cases]
        return {"ids": ids, "count": len(set(ids))}

    @api.post("/cases/delete", dependencies=[Depends(csrf)])
    def delete_cases(payload: CaseDelete):
        return {"deleted": store.delete_cases(payload.ids)}

    @api.post("/cases/{key}/refresh", dependencies=[Depends(csrf)])
    def refresh(key: str):
        if not store.request_refresh(key):
            raise HTTPException(404, "등록된 사건이 없습니다.")
        return {"queued": True}

    @api.post("/cases/{key}/detail-refresh", dependencies=[Depends(csrf)])
    def detail_refresh(key: str):
        result = store.request_detail_refresh(key)
        if result is None:
            raise HTTPException(404, "등록된 사건이 없습니다.")
        return result

    @api.post("/cases/{key}/active", dependencies=[Depends(csrf)])
    def active(key: str, payload: ActiveInput):
        if not store.set_active(key, payload.active):
            raise HTTPException(404, "등록된 사건이 없습니다.")
        return {"active": payload.active}

    @api.get("/cases/{key}/events")
    def events(key: str):
        return {"events": store.case_events(key)}

    @api.get("/logs")
    def logs():
        return {"logs": store.logs()}

    @api.get("/export")
    def export():
        return {"exported_at": time.time(), "cases": [{"query": {k: row[k] for k in ("court", "case_number", "party")},
            "summary": row["observed"].get("summary", {}), "last_checked": row["last_checked"]} for row in store.list_cases()]}

    app.include_router(api)
    return app
