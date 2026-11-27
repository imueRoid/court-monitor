use crate::{
    api,
    auth::Auth,
    config::Settings,
    diagnostics::{error_kind, Journal, Source},
    google::Google,
    releases::{version, Release, Updates},
    store::Store,
    supervise::Listen,
    worker,
};
use anyhow::{Context, Result};
use axum::{
    extract::{DefaultBodyLimit, Path as RoutePath, State},
    http::{header, HeaderMap, HeaderValue, StatusCode, Uri},
    middleware,
    response::{IntoResponse, Response},
    routing::get,
    Json, Router,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    env,
    net::SocketAddr,
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::{
    net::TcpListener,
    signal::unix::{signal, SignalKind},
    sync::{watch, Mutex},
    time::timeout,
};

pub struct App {
    pub settings: Arc<Settings>,
    pub auth: Arc<Auth>,
    pub store: Store,
    pub google: Arc<Google>,
    pub journal: Journal,
    pub build: Value,
    database_health: watch::Receiver<(Instant, bool)>,
    updates: Updates,
    installed: String,
    release_cache: Mutex<Option<(Instant, Option<Release>)>>,
    staging: Mutex<()>,
    restart: watch::Sender<bool>,
    accepted: PathBuf,
}

impl App {
    pub fn database_healthy(&self) -> bool {
        let (checked, healthy) = *self.database_health.borrow();
        healthy && checked.elapsed() < Duration::from_secs(6)
    }
}

async fn monitor_database(store: Store, health: watch::Sender<(Instant, bool)>, journal: Journal) {
    let mut was_healthy = true;
    loop {
        let store = store.clone();
        let result = tokio::task::spawn_blocking(move || store.health_check())
            .await
            .map_err(anyhow::Error::from)
            .and_then(|result| result);
        let healthy = result.is_ok();
        health.send_replace((Instant::now(), healthy));
        if healthy != was_healthy {
            let message = match result {
                Ok(()) => "Database health recovered".into(),
                Err(error) => format!("Database health check failed: {}", error_kind(&error)),
            };
            journal.record(Source::Server, message);
            was_healthy = healthy;
        }
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
}

pub fn error(code: StatusCode, detail: &str) -> Response {
    private((code, Json(json!({"detail": detail}))).into_response())
}

fn private(mut response: Response) -> Response {
    response
        .headers_mut()
        .insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    response
}

async fn static_file(RoutePath(file): RoutePath<String>) -> Response {
    if !file
        .bytes()
        .all(|ch| ch.is_ascii_alphanumeric() || b"._-".contains(&ch))
    {
        return StatusCode::NOT_FOUND.into_response();
    }
    let mime = match file.rsplit_once('.').map(|(_, ext)| ext) {
        Some("js") => "text/javascript; charset=utf-8",
        Some("css") => "text/css; charset=utf-8",
        Some("html") => "text/html; charset=utf-8",
        _ => return StatusCode::NOT_FOUND.into_response(),
    };
    match tokio::fs::read(Path::new("server/static").join(file)).await {
        Ok(body) => (
            [(header::CONTENT_TYPE, mime), (header::CACHE_CONTROL, "no-cache"),
             (header::X_CONTENT_TYPE_OPTIONS, "nosniff"), (header::X_FRAME_OPTIONS, "DENY"),
             (header::CONTENT_SECURITY_POLICY, "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")],
            body,
        ).into_response(),
        Err(_) => StatusCode::NOT_FOUND.into_response(),
    }
}

pub fn same_origin(headers: &HeaderMap) -> bool {
    let Some(origin) = headers.get(header::ORIGIN) else {
        return true;
    };
    origin
        .to_str()
        .ok()
        .and_then(|value| value.parse::<Uri>().ok())
        .and_then(|uri| {
            uri.authority()
                .map(|authority| authority.as_str().to_owned())
        })
        .is_some_and(|origin| {
            headers
                .get(header::HOST)
                .is_some_and(|host| host.as_bytes() == origin.as_bytes())
        })
}

#[derive(Serialize)]
struct Status<'a> {
    current: &'a str,
    latest: Option<String>,
    status: &'static str,
}

async fn check(State(app): State<Arc<App>>) -> Response {
    let (latest, status) = if !app.updates.enabled(&app.installed) {
        (None, "disabled")
    } else {
        let mut cache = app.release_cache.lock().await;
        if cache
            .as_ref()
            .is_none_or(|(checked, _)| checked.elapsed() >= Duration::from_secs(300))
        {
            match app.updates.latest().await {
                Ok(latest) => *cache = Some((Instant::now(), latest)),
                Err(_) => {
                    if let Some((checked, Some(_))) = cache.as_mut() {
                        *checked = Instant::now();
                    } else {
                        return private(
                            Json(Status {
                                current: &app.installed,
                                latest: None,
                                status: "unavailable",
                            })
                            .into_response(),
                        );
                    }
                }
            }
        }
        match cache.as_ref().and_then(|(_, latest)| latest.as_ref()) {
            Some(latest) if version(&latest.version) > version(&app.installed) => {
                let status = if latest.runtime != app.updates.runtime {
                    "image_required"
                } else if latest.version == app.updates.marker("failed") {
                    "failed"
                } else {
                    "available"
                };
                (Some(latest.version.clone()), status)
            }
            _ => (None, "current"),
        }
    };
    private(
        Json(Status {
            current: &app.installed,
            latest,
            status,
        })
        .into_response(),
    )
}

#[derive(Deserialize)]
struct Apply {
    version: String,
}

async fn apply(State(app): State<Arc<App>>, Json(payload): Json<Apply>) -> Response {
    let Ok(_guard) = app.staging.try_lock() else {
        return error(StatusCode::CONFLICT, "업데이트를 이미 준비하고 있습니다.");
    };
    if !app.updates.enabled(&app.installed)
        || !app.accepted.is_file()
        || !app.updates.marker("pending").is_empty()
        || *app.restart.borrow()
    {
        return error(
            StatusCode::CONFLICT,
            "현재 업데이트를 적용할 수 없습니다. 잠시 후 다시 시도하세요.",
        );
    }
    let latest = match app.updates.latest().await {
        Ok(latest) => latest,
        Err(_) => {
            return error(
                StatusCode::BAD_GATEWAY,
                "GitHub에서 업데이트를 확인하지 못했습니다.",
            )
        }
    };
    *app.release_cache.lock().await = Some((Instant::now(), latest.clone()));
    let Some(release) =
        latest.filter(|release| version(&release.version) > version(&app.installed))
    else {
        return error(StatusCode::CONFLICT, "현재 적용할 새 버전이 없습니다.");
    };
    if release.runtime != app.updates.runtime {
        return error(
            StatusCode::CONFLICT,
            "Docker 이미지 업데이트가 필요합니다. 기존 프로젝트에서 새 이미지를 받아 컨테이너를 재생성하세요.",
        );
    }
    if release.version != payload.version {
        return error(
            StatusCode::CONFLICT,
            "업데이트 상태가 바뀌었습니다. 새 버전을 다시 확인하세요.",
        );
    }
    if let Err(err) = app.updates.stage(&release).await {
        app.journal.record(
            Source::Server,
            format!(
                "Could not stage release {}: {}",
                release.version,
                error_kind(&err)
            ),
        );
        return error(
            StatusCode::BAD_GATEWAY,
            "업데이트 파일을 내려받거나 검증하지 못했습니다.",
        );
    }
    app.restart.send_replace(true);
    private(
        (
            StatusCode::ACCEPTED,
            Json(json!({"version": release.version})),
        )
            .into_response(),
    )
}

pub async fn run(directory: &Path, listen: Listen, journal: &Journal) -> Result<bool> {
    let mut term = signal(SignalKind::terminate())?;
    let mut interrupt = signal(SignalKind::interrupt())?;
    let listener = TcpListener::bind((listen.host.as_str(), listen.port)).await?;
    let (restart, mut requested) = watch::channel(false);
    let installed = env::var("COURT_MONITOR_VERSION").unwrap_or_else(|_| "development".into());
    let settings = Arc::new(Settings::from_env()?);
    let store = Store::open(&settings.database)?;
    store.resume_no_hearing_cancelled_cases(crate::store::now(), &settings)?;
    let auth = Arc::new(Auth::new(store.clone(), &settings)?);
    let google = Arc::new(Google::new(store.clone(), settings.clone())?);
    let (database_health, last_database_check) = watch::channel((Instant::now(), true));
    tokio::fs::metadata("server/static/index.html")
        .await
        .context("application assets missing")?;
    let app = Arc::new(App {
        store: store.clone(),
        settings: settings.clone(),
        auth,
        google: google.clone(),
        journal: journal.clone(),
        database_health: last_database_check,
        build: json!({"version":installed,
           "revision":env::var("COURT_MONITOR_REVISION").unwrap_or_else(|_|"unknown".into()),
           "created":env::var("COURT_MONITOR_CREATED").unwrap_or_default()}),
        updates: Updates::from_env()?,
        installed,
        release_cache: Mutex::new(None),
        staging: Mutex::new(()),
        restart,
        accepted: directory.join("accepted"),
    });
    let protected = api::protected()
        .route("/api/update", get(check).post(apply))
        .route_layer(middleware::from_fn_with_state(app.clone(), api::authorize));
    let router = Router::new()
        .route("/static/{file}", get(static_file))
        .merge(api::public())
        .merge(protected)
        .with_state(app)
        .layer(DefaultBodyLimit::max(2_000_000))
        .layer(middleware::from_fn(api::security));
    let (shutdown, mut stop_server) = watch::channel(false);
    let mut server = tokio::spawn(async move {
        axum::serve(
            listener,
            router.into_make_service_with_connect_info::<SocketAddr>(),
        )
        .with_graceful_shutdown(async move {
            let _ = stop_server.changed().await;
        })
        .await
    });
    tokio::fs::write(directory.join("ready"), b"").await?;
    let database = tokio::spawn(monitor_database(
        store.clone(),
        database_health,
        journal.clone(),
    ));
    let mut court = tokio::spawn(worker::court(store, settings, journal.clone()));
    let mut calendar = tokio::spawn(worker::calendar(google, journal.clone()));
    let mut restart = false;
    let result = tokio::select! {
        result=&mut server=>unexpected_exit(
            result.map_err(Into::into).and_then(|result| result.map_err(Into::into)),
            "HTTP server",
            journal,
        ),
        result=&mut court=>unexpected_exit(result.map_err(Into::into), "Court worker", journal),
        result=&mut calendar=>unexpected_exit(result.map_err(Into::into), "Calendar worker", journal),
        _=term.recv()=>Ok(()),
        _=interrupt.recv()=>Ok(()),
        _=requested.changed()=>{restart=*requested.borrow(); Ok(())},
    };
    database.abort();
    court.abort();
    calendar.abort();
    if !server.is_finished() {
        shutdown.send_replace(true);
        if timeout(Duration::from_secs(10), &mut server).await.is_err() {
            server.abort();
        }
    }
    result?;
    Ok(restart)
}

fn unexpected_exit(result: Result<()>, task: &str, journal: &Journal) -> Result<()> {
    let reason = match &result {
        Ok(()) => "task ended".into(),
        Err(error) => error_kind(error),
    };
    journal.record(
        Source::Server,
        format_args!("{task} stopped unexpectedly: {reason}"),
    );
    result?;
    anyhow::bail!("{task} stopped unexpectedly")
}
