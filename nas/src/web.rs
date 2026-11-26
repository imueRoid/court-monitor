use crate::{
    releases::{version, Release, Updates},
    supervise::{terminate, Listen},
};
use anyhow::{bail, Context, Result};
use axum::{
    body::{to_bytes, Body},
    extract::{ConnectInfo, Path as RoutePath, Request, State},
    http::{header, HeaderMap, HeaderValue, StatusCode, Uri},
    response::{IntoResponse, Response},
    routing::get,
    Json, Router,
};
use reqwest::{redirect, Client};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::{
    env,
    net::SocketAddr,
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, Instant},
};
use subtle::ConstantTimeEq;
use tokio::{
    net::TcpListener,
    process::Command,
    signal::unix::{signal, SignalKind},
    sync::{watch, Mutex},
    time::{sleep, timeout},
};

struct App {
    backend: Client,
    updates: Updates,
    installed: String,
    release_cache: Mutex<Option<(Instant, Option<Release>)>>,
    staging: Mutex<()>,
    restart: watch::Sender<bool>,
    accepted: PathBuf,
}

async fn proxy(
    State(app): State<Arc<App>>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    request: Request,
) -> Response {
    let (request, body) = request.into_parts();
    let path = request
        .uri
        .path_and_query()
        .map_or("/", |part| part.as_str());
    let bytes = match to_bytes(body, 2_000_000).await {
        Ok(bytes) => bytes,
        Err(_) => return error(StatusCode::PAYLOAD_TOO_LARGE, "요청 파일이 너무 큽니다."),
    };
    let mut headers = request.headers;
    remove_proxy_headers(&mut headers);
    headers.insert(
        "x-forwarded-for",
        HeaderValue::from_str(&peer.ip().to_string()).unwrap(),
    );
    let response = app
        .backend
        .request(request.method, format!("http://localhost{path}"))
        .headers(headers)
        .body(bytes)
        .send()
        .await;
    match response {
        Ok(response) => {
            let status = response.status();
            let mut headers = response.headers().clone();
            remove_proxy_headers(&mut headers);
            let mut output = Response::new(Body::from_stream(response.bytes_stream()));
            *output.status_mut() = status;
            *output.headers_mut() = headers;
            output
        }
        Err(_) => error(
            StatusCode::SERVICE_UNAVAILABLE,
            "사건 관리 서비스를 시작하고 있습니다.",
        ),
    }
}

fn remove_proxy_headers(headers: &mut HeaderMap) {
    let connection = headers
        .get_all(header::CONNECTION)
        .iter()
        .filter_map(|header| header.to_str().ok())
        .flat_map(|value| value.split(','))
        .filter_map(|name| name.trim().parse::<header::HeaderName>().ok())
        .collect::<Vec<_>>();
    for name in connection {
        headers.remove(name);
    }
    for name in [
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "forwarded",
        "x-forwarded-for",
    ] {
        headers.remove(name);
    }
}

fn error(code: StatusCode, detail: &str) -> Response {
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

async fn authenticated(app: &App, headers: &HeaderMap) -> Result<String, Response> {
    let cookie = headers
        .get(header::COOKIE)
        .cloned()
        .ok_or_else(|| error(StatusCode::UNAUTHORIZED, "로그인이 필요합니다."))?;
    let response = app
        .backend
        .get("http://localhost/api/bootstrap")
        .header(header::COOKIE, cookie)
        .send()
        .await
        .map_err(|_| {
            error(
                StatusCode::SERVICE_UNAVAILABLE,
                "서비스에 연결할 수 없습니다.",
            )
        })?;
    if response.status() == StatusCode::UNAUTHORIZED {
        return Err(error(StatusCode::UNAUTHORIZED, "로그인이 필요합니다."));
    }
    #[derive(Deserialize)]
    struct Bootstrap {
        csrf_token: String,
    }
    response
        .error_for_status()
        .map_err(|_| {
            error(
                StatusCode::SERVICE_UNAVAILABLE,
                "서비스에 연결할 수 없습니다.",
            )
        })?
        .json::<Bootstrap>()
        .await
        .map(|response| response.csrf_token)
        .map_err(|_| {
            error(
                StatusCode::SERVICE_UNAVAILABLE,
                "로그인 상태를 확인할 수 없습니다.",
            )
        })
}

fn same_origin(headers: &HeaderMap) -> bool {
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

async fn check(State(app): State<Arc<App>>, headers: HeaderMap) -> Response {
    if let Err(response) = authenticated(&app, &headers).await {
        return response;
    }
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
                    return private(
                        Json(Status {
                            current: &app.installed,
                            latest: None,
                            status: "unavailable",
                        })
                        .into_response(),
                    )
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

async fn apply(
    State(app): State<Arc<App>>,
    headers: HeaderMap,
    Json(payload): Json<Apply>,
) -> Response {
    let csrf = match authenticated(&app, &headers).await {
        Ok(csrf) => csrf,
        Err(response) => return response,
    };
    if !same_origin(&headers)
        || !headers
            .get("x-csrf-token")
            .is_some_and(|token| bool::from(token.as_bytes().ct_eq(csrf.as_bytes())))
    {
        return error(
            StatusCode::FORBIDDEN,
            "요청 인증값이 올바르지 않습니다. 화면을 새로고침하세요.",
        );
    }
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
    let release = match app.updates.latest().await {
        Ok(Some(release))
            if release.version == payload.version
                && version(&release.version) > version(&app.installed)
                && release.runtime == app.updates.runtime =>
        {
            release
        }
        Ok(_) => {
            return error(
                StatusCode::CONFLICT,
                "업데이트 상태가 바뀌었습니다. 화면을 새로고침하세요.",
            )
        }
        Err(_) => {
            return error(
                StatusCode::BAD_GATEWAY,
                "GitHub에서 업데이트를 확인하지 못했습니다.",
            )
        }
    };
    if let Err(err) = app.updates.stage(&release).await {
        eprintln!("Could not stage release {}: {err:#}", release.version);
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

async fn wait_for_backend(backend: &Client, child: &mut tokio::process::Child) -> Result<()> {
    timeout(Duration::from_secs(30), async {
        loop {
            if let Some(status) = child.try_wait()? {
                bail!("court services stopped before startup ({status})");
            }
            if let Ok(response) = backend.get("http://localhost/health").send().await {
                if response.status().is_success() {
                    return Ok(());
                }
            }
            sleep(Duration::from_millis(100)).await;
        }
    })
    .await
    .context("court services did not become ready")?
}

pub async fn run(directory: &Path, listen: Listen) -> Result<bool> {
    let mut term = signal(SignalKind::terminate())?;
    let mut interrupt = signal(SignalKind::interrupt())?;
    let listener = TcpListener::bind((listen.host.as_str(), listen.port)).await?;
    let socket = directory.join("court.sock");
    let python = env::var_os("COURT_MONITOR_PYTHON").unwrap_or_else(|| "python".into());
    let mut backend_process = Command::new(python)
        .args([
            "-m",
            "uvicorn",
            "server.app:create_app",
            "--factory",
            "--uds",
        ])
        .arg(&socket)
        .args([
            "--workers",
            "1",
            "--no-access-log",
            "--proxy-headers",
            "--forwarded-allow-ips",
            "*",
        ])
        .kill_on_drop(true)
        .spawn()?;
    let backend = Client::builder()
        .unix_socket(socket)
        .no_proxy()
        .redirect(redirect::Policy::none())
        .timeout(Duration::from_secs(25))
        .build()?;
    tokio::select! {
        result = wait_for_backend(&backend, &mut backend_process) => result?,
        _ = term.recv() => { terminate(&mut backend_process).await; return Ok(false); },
        _ = interrupt.recv() => { terminate(&mut backend_process).await; return Ok(false); },
    }
    let (restart, mut requested) = watch::channel(false);
    let app = Arc::new(App {
        backend,
        updates: Updates::from_env()?,
        installed: env::var("COURT_MONITOR_VERSION").unwrap_or_else(|_| "development".into()),
        release_cache: Mutex::new(None),
        staging: Mutex::new(()),
        restart,
        accepted: directory.join("accepted"),
    });
    let router = Router::new()
        .route("/api/update", get(check).post(apply))
        .route("/static/{file}", get(static_file))
        .fallback(proxy)
        .with_state(app);
    tokio::fs::write(directory.join("ready"), b"").await?;
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
    let mut restart = false;
    tokio::select! {
        result = &mut server => { result??; },
        _ = term.recv() => {},
        _ = interrupt.recv() => {},
        _ = requested.changed() => { restart = *requested.borrow(); },
        result = backend_process.wait() => { bail!("court services stopped ({})", result?); },
    };
    if !server.is_finished() {
        shutdown.send_replace(true);
        if timeout(Duration::from_secs(10), &mut server).await.is_err() {
            server.abort();
        }
    }
    terminate(&mut backend_process).await;
    Ok(restart)
}
