use crate::{
    auth::{token, Login},
    court,
    diagnostics::{console, error_kind},
    google::{SharedClaim, SharedRefresh, SharedStart, CALLBACK, SHARED_PREFIX},
    store::{CaseInput, CaseScope, Store},
    web::{error, same_origin, App},
};
use anyhow::Result;
use axum::{
    extract::{ConnectInfo, DefaultBodyLimit, Path, Query, Request, State},
    http::{header, HeaderMap, HeaderValue, StatusCode},
    middleware::Next,
    response::{IntoResponse, Redirect, Response},
    routing::{get, post},
    Extension, Json, Router,
};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{collections::HashSet, net::SocketAddr, sync::Arc};
use subtle::ConstantTimeEq;

pub type Reply<T> = std::result::Result<T, Failure>;

pub struct Failure(Response);
impl IntoResponse for Failure {
    fn into_response(self) -> Response {
        self.0
    }
}

impl From<anyhow::Error> for Failure {
    fn from(err: anyhow::Error) -> Self {
        console(format!("서버 데이터 처리 오류: {}\n", error_kind(&err)));
        fail(
            StatusCode::INTERNAL_SERVER_ERROR,
            "서버의 데이터를 처리하지 못했습니다.",
        )
    }
}

fn fail(code: StatusCode, message: &str) -> Failure {
    Failure(error(code, message))
}
fn invalid(message: &str) -> Failure {
    fail(StatusCode::UNPROCESSABLE_ENTITY, message)
}

pub async fn blocking<T: Send + 'static>(
    function: impl FnOnce() -> Result<T> + Send + 'static,
) -> Reply<T> {
    let result = tokio::task::spawn_blocking(function)
        .await
        .map_err(|err| Failure::from(anyhow::Error::from(err)))?;
    result.map_err(Into::into)
}

#[derive(Clone)]
pub struct Session(pub String);

pub async fn authorize(State(app): State<Arc<App>>, mut request: Request, next: Next) -> Response {
    let session = token(request.headers()).to_owned();
    let check = session.clone();
    let auth = app.auth.clone();
    match blocking(move || auth.valid(&check)).await {
        Ok(false) => return error(StatusCode::UNAUTHORIZED, "로그인이 필요합니다."),
        Err(f) => return f.into_response(),
        Ok(true) => {}
    }
    if request.method() != axum::http::Method::GET && request.method() != axum::http::Method::HEAD {
        let csrf = app.auth.csrf(&session);
        if !request
            .headers()
            .get("x-csrf-token")
            .is_some_and(|h| bool::from(h.as_bytes().ct_eq(csrf.as_bytes())))
        {
            return error(
                StatusCode::FORBIDDEN,
                "요청 인증값이 올바르지 않습니다. 화면을 새로고침하세요.",
            );
        }
    }
    request.extensions_mut().insert(Session(session));
    next.run(request).await
}

pub async fn security(request: Request, next: Next) -> Response {
    let path = request.uri().path().to_owned();
    let mut response =
        if request.method() == axum::http::Method::POST && !same_origin(request.headers()) {
            error(
                StatusCode::FORBIDDEN,
                "다른 사이트에서 보낸 요청은 허용되지 않습니다.",
            )
        } else {
            next.run(request).await
        };
    let headers = response.headers_mut();
    for (key,value) in [
        (header::X_CONTENT_TYPE_OPTIONS,"nosniff"),(header::X_FRAME_OPTIONS,"DENY"),
        (header::REFERRER_POLICY,if path==CALLBACK {"no-referrer"} else {"same-origin"}),
        (header::CONTENT_SECURITY_POLICY,"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"),
        (header::CACHE_CONTROL,if path.starts_with("/static/") {"no-cache"} else {"no-store"}),
    ] {headers.insert(key,HeaderValue::from_static(value));}
    response
}

pub fn public() -> Router<Arc<App>> {
    let shared = Router::new()
        .route("/oauth/shared/start", post(broker_start))
        .route("/oauth/shared/claim", post(broker_claim))
        .route("/oauth/shared/refresh", post(broker_refresh))
        .layer(DefaultBodyLimit::max(16 * 1024));
    Router::new()
        .merge(shared)
        .route(CALLBACK, get(google_callback))
        .route("/health", get(health))
        .route("/", get(index))
        .route("/about", get(about))
        .route("/privacy", get(privacy))
        .route("/privacy.html", get(privacy))
        .route("/terms", get(terms))
        .route("/login", get(login_page))
        .route("/setup", get(setup_page))
        .route("/guide/google", get(guide))
        .route("/api/setup", post(setup))
        .route("/api/login", post(login))
}

pub fn protected() -> Router<Arc<App>> {
    Router::new()
        .route("/api/bootstrap", get(bootstrap))
        .route("/api/logout", post(logout))
        .route("/api/password", post(password))
        .route("/api/cases", get(cases).post(add))
        .route("/api/cases/import", post(import))
        .route("/api/cases/delete", post(delete))
        .route("/api/cases/{id}/refresh", post(refresh))
        .route("/api/cases/{id}/detail-refresh", post(detail_refresh))
        .route("/api/cases/{id}/active", post(active))
        .route("/api/cases/{id}/events", get(events))
        .route("/api/logs", get(logs))
        .route("/api/diagnostics", get(diagnostics))
        .route("/api/export", get(export))
        .route("/api/google/status", get(google_status))
        .route("/api/google/client", post(google_client))
        .route("/api/google/start", post(google_start))
        .route("/api/google/shared/complete", post(google_shared_complete))
        .route("/api/google/calendar", post(google_calendar))
        .route("/api/google/disconnect", post(google_disconnect))
}

async fn health(State(app): State<Arc<App>>) -> (StatusCode, Json<Value>) {
    let mut build = app.build.clone();
    let (code, status) = if app.database_healthy() {
        (StatusCode::OK, "ok")
    } else {
        (StatusCode::SERVICE_UNAVAILABLE, "database_unavailable")
    };
    build["status"] = json!(status);
    (code, Json(build))
}

async fn page_html(name: &str) -> Response {
    match tokio::fs::read(std::path::Path::new("server/static").join(name)).await {
        Ok(body) => ([(header::CONTENT_TYPE, "text/html; charset=utf-8")], body).into_response(),
        Err(_) => StatusCode::NOT_FOUND.into_response(),
    }
}

async fn guide() -> Response {
    page_html("google-guide.html").await
}

async fn about() -> Response {
    page_html("home.html").await
}

async fn privacy() -> Response {
    page_html("privacy.html").await
}

async fn terms() -> Response {
    page_html("terms.html").await
}

async fn index(State(app): State<Arc<App>>, headers: HeaderMap) -> Reply<Response> {
    let token = token(&headers).to_owned();
    let auth = app.auth.clone();
    if !token.is_empty() && blocking(move || Ok(auth.configured()? && auth.valid(&token)?)).await? {
        return Ok(page_html("index.html").await);
    }
    Ok(about().await)
}

async fn login_page(State(app): State<Arc<App>>, headers: HeaderMap) -> Reply<Response> {
    let auth = app.auth.clone();
    let token = token(&headers).to_owned();
    let option = blocking(move || Ok((auth.configured()?, auth.valid(&token)?))).await?;
    Ok(match option {
        (false, _) => Redirect::to("/setup").into_response(),
        (_, true) => Redirect::to("/").into_response(),
        _ => page_html("login.html").await,
    })
}

async fn setup_page(State(app): State<Arc<App>>) -> Reply<Response> {
    let auth = app.auth.clone();
    Ok(if blocking(move || auth.configured()).await? {
        Redirect::to("/login").into_response()
    } else {
        page_html("setup.html").await
    })
}

#[derive(Deserialize)]
struct LoginInput {
    password: String,
}

fn with_cookie(app: &App, headers: &HeaderMap, value: Value, token: &str) -> Response {
    let mut response = Json(value).into_response();
    response
        .headers_mut()
        .insert(header::SET_COOKIE, app.auth.cookie(token, headers));
    response
}

async fn setup(
    State(app): State<Arc<App>>,
    headers: HeaderMap,
    Json(input): Json<LoginInput>,
) -> Reply<Response> {
    if !(8..=256).contains(&input.password.chars().count()) {
        return Err(invalid("비밀번호는 8~256자여야 합니다."));
    }
    let auth = app.auth.clone();
    let session = blocking(move || auth.setup(&input.password))
        .await?
        .ok_or_else(|| {
            fail(
                StatusCode::CONFLICT,
                "관리자 비밀번호가 이미 설정되어 있습니다.",
            )
        })?;
    Ok(with_cookie(
        &app,
        &headers,
        json!({"configured":true}),
        &session,
    ))
}

async fn login(
    State(app): State<Arc<App>>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Json(input): Json<LoginInput>,
) -> Reply<Response> {
    if !(1..=256).contains(&input.password.chars().count()) {
        return Err(invalid("관리자 비밀번호를 입력하세요."));
    }
    let auth = app.auth.clone();
    match blocking(move || auth.login(&input.password, &peer.ip().to_string())).await? {
        Login::Allowed(token) => Ok(with_cookie(
            &app,
            &headers,
            json!({"logged_in":true}),
            &token,
        )),
        Login::Incorrect => Err(fail(
            StatusCode::UNAUTHORIZED,
            "비밀번호가 올바르지 않습니다.",
        )),
        Login::MissingPassword => Err(fail(
            StatusCode::CONFLICT,
            "먼저 관리자 비밀번호를 설정하세요.",
        )),
        Login::Locked(seconds) => {
            let mut response = error(
                StatusCode::TOO_MANY_REQUESTS,
                "로그인 시도가 많습니다. 잠시 후 다시 시도하세요.",
            );
            response
                .headers_mut()
                .insert(header::RETRY_AFTER, HeaderValue::from(seconds));
            Ok(response)
        }
    }
}

async fn logout(
    State(app): State<Arc<App>>,
    Extension(session): Extension<Session>,
    headers: HeaderMap,
) -> Reply<Response> {
    let auth = app.auth.clone();
    blocking(move || auth.logout(&session.0)).await?;
    Ok(with_cookie(&app, &headers, json!({"logged_out":true}), ""))
}

#[derive(Deserialize)]
struct Password {
    current: String,
    new: String,
}

async fn password(
    State(app): State<Arc<App>>,
    headers: HeaderMap,
    Json(input): Json<Password>,
) -> Reply<Response> {
    if !(8..=256).contains(&input.new.chars().count())
        || !(1..=256).contains(&input.current.chars().count())
    {
        return Err(invalid("새 비밀번호는 8~256자여야 합니다."));
    }
    let auth = app.auth.clone();
    if !blocking(move || auth.change(&input.current, &input.new)).await? {
        return Err(fail(
            StatusCode::FORBIDDEN,
            "현재 비밀번호가 올바르지 않습니다.",
        ));
    }
    Ok(with_cookie(
        &app,
        &headers,
        json!({"changed":true,"login_required":true}),
        "",
    ))
}

async fn bootstrap(
    State(app): State<Arc<App>>,
    Extension(session): Extension<Session>,
) -> Reply<Json<Value>> {
    blocking(move|| {
        let s=&app.settings;
        Ok(Json(json!({"csrf_token":app.auth.csrf(&session.0),"timezone":s.timezone.name(),"session_days":s.session_days,
            "build":app.build,"diagnostics_available":true,"google":app.google.status()?,"courts":app.store.meta::<Vec<String>>("courts")?.unwrap_or_default(),
            "calendar_configured":app.google.configured()?,"verdict_checks_per_day":s.verdict_checks_per_day,
            "verdict_retry_minutes":s.verdict_retry_minutes,"watch_hours":[s.watch_start_hour,s.watch_end_hour]})))
    }).await
}

#[derive(Deserialize)]
struct Filter {
    #[serde(default)]
    q: String,
    #[serde(default)]
    scope: CaseScope,
    page: Option<usize>,
    page_size: Option<usize>,
}

async fn cases(State(app): State<Arc<App>>, Query(q): Query<Filter>) -> Reply<Json<Value>> {
    let size = q.page_size.unwrap_or(10);
    let page = q.page.unwrap_or(1);
    if !(1..=100).contains(&size) || page == 0 || q.q.chars().count() > 200 {
        return Err(invalid("검색 조건을 확인하세요."));
    }
    Ok(Json(
        blocking(move || {
            app.store.page(
                &q.q,
                page,
                size,
                q.scope,
                app.settings.timezone,
                app.google.configured()?,
            )
        })
        .await?,
    ))
}

fn validated(mut case: CaseInput) -> Reply<CaseInput> {
    if case.case_number.chars().count() > 30
        || case.court.chars().count() > 100
        || case.party.chars().count() > 100
    {
        return Err(invalid("사건번호·법원·당사자명의 길이를 확인하세요."));
    }
    case.case_number = court::number(&case.case_number).map_err(|err| invalid(&err.to_string()))?;
    case.court = case.court.trim().to_owned();
    case.party = case.party.chars().filter(|c| !c.is_whitespace()).collect();
    if case.court.chars().count() < 2 || case.party.chars().count() < 2 {
        return Err(invalid("두 글자 이상 입력하세요."));
    }
    Ok(case)
}

async fn add(State(app): State<Arc<App>>, Json(case): Json<CaseInput>) -> Reply<Json<Value>> {
    let case = validated(case)?;
    let ids = blocking(move || app.store.import(&[case])).await?;
    Ok(Json(json!({"id":ids[0]})))
}

#[derive(Deserialize)]
struct Import {
    cases: Vec<CaseInput>,
}

async fn import(State(app): State<Arc<App>>, Json(input): Json<Import>) -> Reply<Json<Value>> {
    if !(1..=1000).contains(&input.cases.len()) {
        return Err(invalid("가져올 사건은 1~1,000건이어야 합니다."));
    }
    let cases = input
        .cases
        .into_iter()
        .map(validated)
        .collect::<Reply<Vec<_>>>()?;
    let ids = blocking(move || app.store.import(&cases)).await?;
    let count = ids.iter().collect::<HashSet<_>>().len();
    Ok(Json(json!({"ids":ids,"count":count})))
}

#[derive(Deserialize)]
struct Delete {
    ids: Vec<String>,
}

async fn delete(State(app): State<Arc<App>>, Json(input): Json<Delete>) -> Reply<Json<Value>> {
    if !(1..=10000).contains(&input.ids.len())
        || input.ids.iter().any(|id| {
            id.len() != 32
                || id
                    .bytes()
                    .any(|ch| !(ch.is_ascii_digit() || (b'a'..=b'f').contains(&ch)))
        })
    {
        return Err(invalid("올바른 사건 ID가 아닙니다."));
    }
    Ok(Json(
        json!({"deleted":blocking(move||app.store.delete(&input.ids)).await?}),
    ))
}

fn missing() -> Failure {
    fail(StatusCode::NOT_FOUND, "등록된 사건이 없습니다.")
}

async fn refresh(State(app): State<Arc<App>>, Path(id): Path<String>) -> Reply<Json<Value>> {
    if !blocking(move || app.store.refresh(&id)).await? {
        return Err(missing());
    }
    Ok(Json(json!({"queued":true})))
}

async fn detail_refresh(State(app): State<Arc<App>>, Path(id): Path<String>) -> Reply<Json<Value>> {
    blocking(move || app.store.detail_refresh(&id))
        .await?
        .map(Json)
        .ok_or_else(missing)
}

#[derive(Deserialize)]
struct Active {
    active: bool,
}

async fn active(
    State(app): State<Arc<App>>,
    Path(id): Path<String>,
    Json(input): Json<Active>,
) -> Reply<Json<Value>> {
    if !blocking(move || app.store.active(&id, input.active)).await? {
        return Err(missing());
    }
    Ok(Json(json!({"active":input.active})))
}

async fn events(State(app): State<Arc<App>>, Path(id): Path<String>) -> Reply<Json<Value>> {
    Ok(Json(
        json!({"events":blocking(move||app.store.events(&id)).await?}),
    ))
}

async fn logs(State(app): State<Arc<App>>) -> Reply<Json<Value>> {
    Ok(Json(
        json!({"logs":blocking(move||app.store.logs()).await?}),
    ))
}

async fn diagnostics(State(app): State<Arc<App>>) -> Reply<Json<Value>> {
    let log = blocking(move || Ok(app.journal.latest()?)).await?;
    Ok(Json(json!({"log": log})))
}

async fn export(State(app): State<Arc<App>>) -> Reply<Json<Value>> {
    Ok(Json(blocking(move || app.store.export()).await?))
}

async fn google_status(State(app): State<Arc<App>>) -> Reply<Json<Value>> {
    Ok(Json(blocking(move || app.google.status()).await?))
}

#[derive(Deserialize)]
struct ClientInput {
    config: Value,
}

async fn google_client(
    State(app): State<Arc<App>>,
    Json(input): Json<ClientInput>,
) -> Reply<Json<Value>> {
    app.google
        .configure(input.config)
        .await
        .map(Json)
        .map_err(|err| invalid(&err.to_string()))
}

#[derive(Deserialize)]
struct GoogleStart {
    #[serde(default)]
    redirect_uri: String,
    #[serde(default)]
    method: String,
}

async fn google_start(
    State(app): State<Arc<App>>,
    Extension(session): Extension<Session>,
    headers: HeaderMap,
    Json(input): Json<GoogleStart>,
) -> Reply<Json<Value>> {
    if input.method == "shared" || (input.method.is_empty() && input.redirect_uri.is_empty()) {
        let url = app
            .google
            .begin_shared(&session.0)
            .await
            .map_err(|err| invalid(&err.to_string()))?;
        return Ok(Json(json!({"url":url,"method":"shared"})));
    }
    if !input.method.is_empty() && input.method != "direct" {
        return Err(invalid("Google 연결 방법을 선택하세요."));
    }
    let allowed = input
        .redirect_uri
        .parse::<axum::http::Uri>()
        .ok()
        .and_then(|url| url.authority().cloned())
        .zip(headers.get(header::HOST))
        .is_some_and(|(host, header)| {
            host.as_str()
                .as_bytes()
                .eq_ignore_ascii_case(header.as_bytes())
        });
    if !allowed {
        return Err(invalid("현재 접속한 주소의 콜백 URL을 사용하세요."));
    }
    let result = blocking(move || Ok(app.google.begin(&input.redirect_uri, &session.0))).await?;
    let url = result.map_err(|err| invalid(&err.to_string()))?;
    Ok(Json(json!({"url":url,"method":"direct"})))
}

async fn google_shared_complete(
    State(app): State<Arc<App>>,
    Extension(session): Extension<Session>,
) -> Reply<Json<Value>> {
    let result = app
        .google
        .finish_shared(&session.0)
        .await
        .map_err(|err| invalid(&err.to_string()))?;
    Ok(Json(match result {
        Some(status) => json!({"status":"connected","google":status}),
        None => json!({"status":"pending"}),
    }))
}

async fn google_calendar(State(app): State<Arc<App>>) -> Reply<Json<Value>> {
    match app.google.prepare().await {
        Ok(status) => Ok(Json(status)),
        Err(err) => {
            save_google_error(&app.store, &err.to_string()).await?;
            Err(invalid(&err.to_string()))
        }
    }
}

async fn google_disconnect(State(app): State<Arc<App>>) -> Reply<Json<Value>> {
    Ok(Json(app.google.disconnect().await?))
}

#[derive(Default, Deserialize)]
#[serde(default)]
struct GoogleCallback {
    state: String,
    code: String,
    error: String,
}

async fn google_callback(
    State(app): State<Arc<App>>,
    headers: HeaderMap,
    Query(q): Query<GoogleCallback>,
) -> Reply<Response> {
    let denied = !q.error.is_empty();
    if q.state.starts_with(SHARED_PREFIX) {
        broker_host(&app, &headers)?;
        let (status, message) = match app.google.broker_callback(&q.state, &q.code, denied).await {
            Ok(true) => (
                StatusCode::OK,
                "Google 승인이 완료되었습니다. 이 창을 닫고 앱으로 돌아가세요.",
            ),
            Ok(false) => (
                StatusCode::OK,
                "Google 승인이 취소되었습니다. 앱으로 돌아가 승인 대기를 중단하거나 다시 시작하세요.",
            ),
            Err(_) => (
                StatusCode::BAD_REQUEST,
                "Google 승인을 완료하지 못했습니다. 앱으로 돌아가 다시 연결하세요.",
            ),
        };
        return Ok((status, message).into_response());
    }
    let session = token(&headers).to_owned();
    let check = session.clone();
    let auth = app.auth.clone();
    if !blocking(move || auth.valid(&check)).await? {
        return Err(fail(StatusCode::UNAUTHORIZED, "로그인이 필요합니다."));
    }
    match app
        .google
        .callback(&q.state, &q.code, denied, &session)
        .await
    {
        Ok(()) => Ok(Redirect::to(if denied {
            "/?google=cancelled"
        } else {
            "/?google=connected"
        })
        .into_response()),
        Err(err) => {
            save_google_error(&app.store, &err.to_string()).await?;
            Ok(Redirect::to("/?google=error").into_response())
        }
    }
}

fn broker_host(app: &App, headers: &HeaderMap) -> Reply<()> {
    let host = headers
        .get(header::HOST)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<axum::http::uri::Authority>().ok());
    if app.google.broker_enabled()
        && host.is_some_and(|value| {
            value.host().eq_ignore_ascii_case("court.imue.one")
                && value.port_u16().is_none_or(|port| port == 443)
        })
    {
        return Ok(());
    }
    Err(fail(
        StatusCode::NOT_FOUND,
        "공통 인증 서버를 사용할 수 없습니다.",
    ))
}

fn broker_failure() -> Failure {
    invalid("공통 인증 요청을 처리하지 못했습니다.")
}

async fn broker_start(
    State(app): State<Arc<App>>,
    headers: HeaderMap,
    Json(input): Json<SharedStart>,
) -> Reply<Json<Value>> {
    broker_host(&app, &headers)?;
    let result = blocking(move || Ok(app.google.broker_start(input))).await?;
    result.map(Json).map_err(|_| broker_failure())
}

async fn broker_claim(
    State(app): State<Arc<App>>,
    headers: HeaderMap,
    Json(input): Json<SharedClaim>,
) -> Reply<Json<Value>> {
    broker_host(&app, &headers)?;
    let result = blocking(move || Ok(app.google.broker_claim(input))).await?;
    result.map(Json).map_err(|_| broker_failure())
}

async fn broker_refresh(
    State(app): State<Arc<App>>,
    headers: HeaderMap,
    Json(input): Json<SharedRefresh>,
) -> Reply<Json<Value>> {
    broker_host(&app, &headers)?;
    app.google
        .broker_refresh(input)
        .await
        .map(Json)
        .map_err(|_| broker_failure())
}

async fn save_google_error(store: &Store, message: &str) -> Reply<()> {
    let store = store.clone();
    let message = message.to_owned();
    blocking(move || store.set_meta("google_error", message)).await
}
