use super::*;
use serde::{Deserialize, Serialize};
use tokio::sync::Semaphore;

const ORIGIN: &str = "https://court.imue.one";
const PREFIX: &str = "/oauth/shared";
pub const SHARED_PREFIX: &str = "shared.";
const FLOW_TTL: f64 = 600.0;
const RESULT_LIMIT: i64 = 512;

pub(super) struct Shared {
    enabled: bool,
    origin: Url,
    requests: Semaphore,
}

impl Shared {
    pub(super) fn from_env() -> Result<Self> {
        Ok(Self {
            enabled: std::env::var("COURT_GOOGLE_BROKER_ENABLED").as_deref() == Ok("true"),
            origin: Url::parse(ORIGIN)?,
            requests: Semaphore::new(8),
        })
    }
}

#[derive(Serialize, Deserialize)]
pub struct SharedStart {
    pub claim_hash: String,
}

#[derive(Serialize, Deserialize)]
pub struct SharedClaim {
    pub flow: String,
    pub claim: String,
}

#[derive(Serialize, Deserialize)]
pub struct SharedRefresh {
    pub grant: String,
}

#[derive(Serialize, Deserialize)]
struct Flow {
    id: String,
    claim_hash: String,
    verifier: String,
    client_id: String,
    expires: f64,
}

#[derive(Serialize, Deserialize)]
struct Pending {
    flow: String,
    claim: String,
    session_hash: String,
    generation: i64,
    expires: f64,
}

fn token_id(value: &str) -> bool {
    value.len() == 43
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

fn claim_hash(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}

fn only_calendar_scope(result: &Value) -> Result<()> {
    if let Some(scopes) = result["scope"].as_str() {
        let mut scopes = scopes.split_whitespace();
        ensure!(
            scopes.next() == Some(SCOPE) && scopes.next().is_none(),
            "사건 전용 캘린더 권한을 확인해 주세요."
        );
    }
    Ok(())
}

impl Google {
    pub fn broker_enabled(&self) -> bool {
        self.shared.enabled
    }

    fn shared_client(&self) -> Result<Value> {
        ensure!(
            self.broker_enabled(),
            "공통 인증 서버를 사용할 수 없습니다."
        );
        let client = self.secret(&self.store.connection()?, "google_client")?;
        let callback = self.shared.origin.join(CALLBACK)?;
        ensure!(
            client["redirect_uris"]
                .as_array()
                .is_some_and(|urls| urls.iter().any(|url| url == callback.as_str())),
            "공통 인증 서버의 Google OAuth 콜백 설정을 확인하세요."
        );
        Ok(client)
    }

    pub fn broker_start(&self, input: SharedStart) -> Result<Value> {
        let _request = self
            .shared
            .requests
            .try_acquire()
            .context("공통 인증 서버가 처리 중입니다.")?;
        ensure!(
            claim_hash(&input.claim_hash),
            "유효한 인증 요청이 필요합니다."
        );
        let client = self.shared_client()?;
        let flow = Flow {
            id: random_token::<32>()?,
            claim_hash: input.claim_hash,
            verifier: random_token::<64>()?,
            client_id: text(&client["client_id"]).to_owned(),
            expires: now() + FLOW_TTL,
        };
        let state = format!(
            "{SHARED_PREFIX}{}",
            self.encrypt(&serde_json::to_value(&flow)?)?
        );
        let challenge = URL_SAFE_NO_PAD.encode(hex::decode(hash(&flow.verifier))?);
        let callback = self.shared.origin.join(CALLBACK)?;
        let mut url = Url::parse(AUTH)?;
        url.query_pairs_mut().extend_pairs([
            ("client_id", flow.client_id.as_str()),
            ("response_type", "code"),
            ("redirect_uri", callback.as_str()),
            ("scope", SCOPE),
            ("access_type", "offline"),
            ("include_granted_scopes", "false"),
            ("prompt", "consent select_account"),
            ("state", &state),
            ("code_challenge", &challenge),
            ("code_challenge_method", "S256"),
        ]);
        Ok(json!({"flow":flow.id,"url":url.as_str()}))
    }

    pub async fn broker_callback(&self, state: &str, code: &str, denied: bool) -> Result<bool> {
        let _request = self
            .shared
            .requests
            .try_acquire()
            .context("공통 인증 서버가 처리 중입니다.")?;
        ensure!(state.len() <= 4096, "유효한 인증 요청이 필요합니다.");
        let encrypted = state
            .strip_prefix(SHARED_PREFIX)
            .context("유효한 인증 요청이 필요합니다.")?;
        let flow: Flow = serde_json::from_value(self.decode(encrypted)?)?;
        let client = block_in_place(|| self.shared_client())?;
        ensure!(
            now() < flow.expires && text(&client["client_id"]) == flow.client_id,
            "인증 요청이 만료되었거나 변경되었습니다."
        );
        if denied {
            // Declined requests have no tokens to relay. The installation can cancel and retry.
            return Ok(false);
        }
        ensure!(
            !code.is_empty() && code.len() <= 4096,
            "유효한 Google 인증 코드가 필요합니다."
        );
        let callback = self.shared.origin.join(CALLBACK)?;
        let result = self
            .token_request(&[
                ("grant_type", "authorization_code"),
                ("code", code),
                ("redirect_uri", callback.as_str()),
                ("client_id", &flow.client_id),
                ("client_secret", text(&client["client_secret"])),
                ("code_verifier", &flow.verifier),
            ])
            .await?;
        let response =
            self.broker_token(&flow.client_id, text(&result["refresh_token"]), &result)?;
        block_in_place(|| {
            self.store.write(|db| {
                let active = self.secret(db, "google_client")?;
                ensure!(
                    flow.expires > now() && active == client,
                    "인증 요청이 만료되었거나 변경되었습니다."
                );
                db.execute(
                    "DELETE FROM google_shared_results WHERE expires<=?1",
                    [now()],
                )?;
                let count =
                    db.query_row("SELECT count(*) FROM google_shared_results", [], |r| {
                        r.get::<_, i64>(0)
                    })?;
                ensure!(count < RESULT_LIMIT, "공통 인증 서버가 처리 중입니다.");
                let inserted = db.execute(
                    "INSERT OR IGNORE INTO google_shared_results VALUES (?1,?2,?3,?4)",
                    params![
                        flow.id,
                        flow.claim_hash,
                        flow.expires,
                        self.encrypt(&response)?
                    ],
                )?;
                ensure!(inserted == 1, "이미 처리한 인증 요청입니다.");
                Ok(true)
            })
        })
    }

    pub fn broker_claim(&self, input: SharedClaim) -> Result<Value> {
        let _request = self
            .shared
            .requests
            .try_acquire()
            .context("공통 인증 서버가 처리 중입니다.")?;
        ensure!(
            self.broker_enabled() && token_id(&input.flow) && token_id(&input.claim),
            "유효한 인증 요청이 필요합니다."
        );
        let claim_hash = hash(&input.claim);
        let available = self.store.connection()?.query_row(
            "SELECT EXISTS(SELECT 1 FROM google_shared_results WHERE id=?1 AND claim_hash=?2 AND expires>?3 AND result IS NOT NULL)",
            params![input.flow, claim_hash, now()], |row| row.get::<_, bool>(0),
        )?;
        if !available {
            return Ok(Value::Null);
        }
        let result = self.store.write(|db| {
            let args = params![input.flow, claim_hash, now()];
            let encrypted = db.query_row(
                "SELECT result FROM google_shared_results WHERE id=?1 AND claim_hash=?2 AND expires>?3 AND result IS NOT NULL",
                args, |row| row.get::<_, String>(0),
            ).optional()?;
            let Some(encoded) = encrypted else { return Ok(Value::Null); };
            let result = self.decode(&encoded)?;
            db.execute("UPDATE google_shared_results SET result=NULL WHERE id=?1", [&input.flow])?;
            Ok(result)
        })?;
        Ok(result)
    }

    pub async fn clean_broker_results(&self) {
        loop {
            let store = self.store.clone();
            let _ = tokio::task::spawn_blocking(move || {
                store
                    .connection()?
                    .execute(
                        "DELETE FROM google_shared_results WHERE expires<=?1",
                        [now()],
                    )
                    .map_err(anyhow::Error::from)
            })
            .await;
            tokio::time::sleep(Duration::from_secs(60)).await;
        }
    }

    fn broker_token(&self, client_id: &str, refresh: &str, response: &Value) -> Result<Value> {
        only_calendar_scope(response)?;
        ensure!(
            !refresh.is_empty(),
            "Google 장기 연결 권한을 받지 못했습니다."
        );
        let grant = self
            .encrypt(&json!({"kind":"calendar_grant","client_id":client_id,"refresh":refresh}))?;
        Ok(
            json!({"access_token":text(&response["access_token"]),"expires_in":lifetime(response),"shared_grant":grant}),
        )
    }

    pub async fn broker_refresh(&self, input: SharedRefresh) -> Result<Value> {
        let _request = self
            .shared
            .requests
            .try_acquire()
            .context("공통 인증 서버가 처리 중입니다.")?;
        ensure!(
            self.broker_enabled() && input.grant.len() <= 8192,
            "공통 인증 토큰이 유효하지 않습니다."
        );
        let grant = self.decode(&input.grant)?;
        let client = block_in_place(|| self.shared_client())?;
        let id = text(&client["client_id"]);
        let refresh = text(&grant["refresh"]);
        ensure!(
            grant["kind"] == "calendar_grant" && grant["client_id"] == id && !refresh.is_empty(),
            "공통 인증 토큰이 유효하지 않습니다."
        );
        let result = self
            .token_request(&[
                ("grant_type", "refresh_token"),
                ("client_id", id),
                ("client_secret", text(&client["client_secret"])),
                ("refresh_token", refresh),
            ])
            .await?;
        let rotated = text(&result["refresh_token"]);
        self.broker_token(
            id,
            if rotated.is_empty() { refresh } else { rotated },
            &result,
        )
    }

    async fn shared_post(&self, operation: &str, data: &impl Serialize) -> Result<Value> {
        let url = self.shared.origin.join(&format!("{PREFIX}/{operation}"))?;
        let response = self
            .client
            .post(url)
            .timeout(Duration::from_secs(12))
            .json(data)
            .send()
            .await
            .map_err(|error| error.without_url())
            .context("공통 인증 서버에 연결하지 못했습니다.")?;
        ensure!(
            response.status().is_success(),
            "공통 인증 요청을 처리하지 못했습니다. 잠시 후 다시 시도하세요."
        );
        Ok(response.json().await?)
    }

    pub async fn begin_shared(&self, session: &str) -> Result<String> {
        let claim = random_token::<32>()?;
        let request = SharedStart {
            claim_hash: hash(&claim),
        };
        let version = block_in_place(|| generation(&self.store.connection()?))?;
        let response = self.shared_post("start", &request).await?;
        let flow = text(&response["flow"]);
        let url = Url::parse(text(&response["url"]))?;
        let google = Url::parse(AUTH)?;
        ensure!(
            token_id(flow)
                && url.scheme() == google.scheme()
                && url.host_str() == google.host_str()
                && url.path() == google.path()
                && url.port().is_none()
                && url.username().is_empty()
                && url.password().is_none(),
            "공통 인증 서버의 응답을 확인하세요."
        );
        let pending = Pending {
            flow: flow.to_owned(),
            claim,
            session_hash: hash(session),
            generation: version,
            expires: now() + FLOW_TTL,
        };
        let _gate = self.gate.lock().await;
        block_in_place(|| {
            self.store.write(|db| {
                ensure!(
                    generation(db)? == version,
                    "연결 설정이 변경되었습니다. 다시 연결하세요."
                );
                db.execute(
                    "DELETE FROM google_oauth_states WHERE session_hash=?1",
                    [&pending.session_hash],
                )?;
                self.save(db, "google_shared_pending", &serde_json::to_value(pending)?)
            })
        })?;
        Ok(url.into())
    }

    pub async fn finish_shared(&self, session: &str) -> Result<Option<Value>> {
        let stored =
            block_in_place(|| self.secret(&self.store.connection()?, "google_shared_pending"))?;
        let pending: Pending = serde_json::from_value(stored.clone())
            .context("공통 인증 요청이 없습니다. 다시 시작하세요.")?;
        ensure!(
            pending.expires > now() && pending.session_hash == hash(session),
            "공통 인증 요청이 만료되었거나 다른 로그인에서 시작되었습니다."
        );
        let request = SharedClaim {
            flow: pending.flow,
            claim: pending.claim,
        };
        let result = self.shared_post("claim", &request).await?;
        if result.is_null() {
            return Ok(None);
        }
        let grant = text(&result["shared_grant"]);
        ensure!(
            !grant.is_empty() && grant.len() <= 8192 && !text(&result["access_token"]).is_empty(),
            "공통 인증 서버의 응답을 확인하세요."
        );
        let mut tokens = json!({"shared_grant":grant});
        merge_token(&mut tokens, &result)?;
        let _gate = self.gate.lock().await;
        block_in_place(|| {
            self.store.write(|db| {
                ensure!(
                    pending.generation == generation(db)?
                        && self.secret(db, "google_shared_pending")? == stored,
                    "연결 설정이 변경되었습니다. 다시 연결하세요."
                );
                self.save(db, "google_pending_tokens", &tokens)?;
                db.execute("DELETE FROM meta WHERE key='google_shared_pending'", [])?;
                Ok(())
            })
        })?;
        self.prepare_locked().await?;
        block_in_place(|| self.status()).map(Some)
    }

    pub(super) async fn request_shared_refresh(&self, grant: &str) -> Result<Value> {
        let result = self
            .shared_post(
                "refresh",
                &SharedRefresh {
                    grant: grant.to_owned(),
                },
            )
            .await?;
        ensure!(
            !text(&result["shared_grant"]).is_empty() && !text(&result["access_token"]).is_empty(),
            "공통 인증 서버의 응답을 확인하세요."
        );
        Ok(result)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{
        extract::{Form, State},
        http::{header, HeaderMap, StatusCode},
        routing::post,
        Json, Router,
    };
    use std::collections::HashMap;
    use tokio::net::TcpListener;

    const CLIENT_ID: &str = "fixture.apps.googleusercontent.com";
    const CLIENT_SECRET: &str = "fixture-secret";
    const REFRESH_TOKEN: &str = "fixture-refresh";

    #[tokio::test(flavor = "multi_thread")]
    async fn relays_shared_approval_and_refresh_between_two_installations() -> Result<()> {
        let fixture = tempfile::tempdir()?;
        let owner_dir = fixture.path().join("operator");
        let local_dir = fixture.path().join("installation");
        fs::create_dir_all(&owner_dir)?;
        fs::create_dir_all(&local_dir)?;
        let settings = Arc::new(Settings::from_env()?);
        let listener = TcpListener::bind("127.0.0.1:0").await?;
        let base = Url::parse(&format!("http://{}", listener.local_addr()?))?;
        let callback = format!("{ORIGIN}{CALLBACK}");
        let client = Client::builder().no_proxy().build()?;
        let mut owner = Google::new(
            Store::open(&owner_dir.join("cases.sqlite3"))?,
            settings.clone(),
        )?;
        owner.shared.enabled = true;
        owner.client = client.clone();
        owner.token_endpoint = base.join("/token")?.into();
        owner
            .configure(json!({"web":{"client_id":CLIENT_ID,"client_secret":CLIENT_SECRET,"redirect_uris":[callback]}}))
            .await?;
        let owner = Arc::new(owner);
        let server = Router::new()
            .route(
                "/oauth/shared/start",
                post(|State(owner): State<Arc<Google>>, Json(input): Json<SharedStart>| async move {
                    block_in_place(|| owner.broker_start(input))
                        .map(Json)
                        .map_err(|_| StatusCode::BAD_REQUEST)
                }),
            )
            .route(
                "/oauth/shared/claim",
                post(|State(owner): State<Arc<Google>>, Json(input): Json<SharedClaim>| async move {
                    block_in_place(|| owner.broker_claim(input))
                        .map(Json)
                        .map_err(|_| StatusCode::BAD_REQUEST)
                }),
            )
            .route(
                "/oauth/shared/refresh",
                post(|State(owner): State<Arc<Google>>, Json(input): Json<SharedRefresh>| async move {
                    owner
                        .broker_refresh(input)
                        .await
                        .map(Json)
                        .map_err(|_| StatusCode::BAD_REQUEST)
                }),
            )
            .with_state(owner.clone())
            .route(
                "/token",
                post(|Form(form): Form<HashMap<String, String>>| async move {
                    assert_eq!(form["client_id"], CLIENT_ID);
                    assert_eq!(form["client_secret"], CLIENT_SECRET);
                    let response = match form["grant_type"].as_str() {
                        "authorization_code" => {
                            assert_eq!(form["redirect_uri"], format!("{ORIGIN}{CALLBACK}"));
                            assert_eq!(form["code"], "google-fixture-code");
                            assert_eq!(form["code_verifier"].len(), 86);
                            json!({"access_token":"first-access","refresh_token":REFRESH_TOKEN,"expires_in":3600,"scope":SCOPE})
                        }
                        "refresh_token" => {
                            assert_eq!(form["refresh_token"], REFRESH_TOKEN);
                            json!({"access_token":"new-access","expires_in":3600,"scope":SCOPE})
                        }
                        other => panic!("unexpected Google grant type {other}"),
                    };
                    Json(response)
                }),
            )
            .route(
                "/calendars",
                post(|headers: HeaderMap| async move {
                    assert_eq!(headers[header::AUTHORIZATION], "Bearer first-access");
                    Json(json!({"id":"local-case-calendar","summary":"사건 관리"}))
                }),
            );
        let server = tokio::spawn(async move { axum::serve(listener, server).await });
        let local_store = Store::open(&local_dir.join("cases.sqlite3"))?;
        let mut local = Google::new(local_store.clone(), settings)?;
        local.shared.origin = base.clone();
        local.client = client;
        local.calendars_endpoint = base.join("/calendars")?.into();
        assert_eq!(local.status()?["connection_method"], "shared");
        assert!(!local.status()?["client_configured"].as_bool().unwrap());
        let approval = Url::parse(&local.begin_shared("local-admin").await?)?;
        let query: HashMap<_, _> = approval.query_pairs().into_owned().collect();
        assert_eq!(query["redirect_uri"], callback);
        assert_eq!(query["scope"], SCOPE);
        assert_eq!(query["code_challenge_method"], "S256");
        assert!(local.finish_shared("another-session").await.is_err());
        assert!(local.finish_shared("local-admin").await?.is_none());
        assert!(owner
            .broker_callback("shared.invalid", "code", false)
            .await
            .is_err());
        assert!(
            owner
                .broker_callback(&query["state"], "google-fixture-code", false)
                .await?
        );
        let before: Pending = serde_json::from_value(
            local.secret(&local_store.connection()?, "google_shared_pending")?,
        )?;
        assert!(owner
            .broker_claim(SharedClaim {
                flow: before.flow.clone(),
                claim: random_token::<32>()?
            })?
            .is_null());
        let status = local
            .finish_shared("local-admin")
            .await?
            .expect("authorized calendar");
        assert_eq!(status["calendar_id"], "local-case-calendar");
        assert_eq!(status["connected"], true);
        assert_eq!(status["connection_method"], "shared");
        assert!(owner
            .broker_claim(SharedClaim {
                flow: before.flow,
                claim: before.claim
            })?
            .is_null());
        let credentials = local.secret(&local_store.connection()?, "google_tokens")?;
        assert!(credentials["refresh_token"].is_null());
        assert!(credentials["client_secret"].is_null());
        assert!(credentials["shared_grant"].is_string());
        assert_eq!(
            local.access(Some("google_tokens"), true).await?,
            "new-access"
        );
        assert_eq!(local.disconnect().await?["connected"], false);
        assert!(local
            .secret(&local_store.connection()?, "google_tokens")?
            .is_null());
        server.abort();
        Ok(())
    }
}
