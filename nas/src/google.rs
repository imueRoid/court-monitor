use crate::{
    auth::random_token,
    config::Settings,
    store::{calendar_target, hash, meta, now, set_meta, Event, Store},
};
use anyhow::{anyhow, ensure, Context, Result};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use chrono::{DateTime, NaiveDateTime, Utc};
use fernet::Fernet;
use reqwest::{redirect, Client, Method, RequestBuilder, Response, StatusCode, Url};
use ring::signature::{RsaKeyPair, RSA_PKCS1_SHA256};
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};
use std::{fs, io::Write, os::unix::fs::OpenOptionsExt, sync::Arc, time::Duration};
use tokio::{sync::Mutex, task::block_in_place};

pub const CALLBACK: &str = "/oauth/google/callback";
#[path = "shared_google.rs"]
mod shared;
pub use shared::{SharedClaim, SharedRefresh, SharedStart, SHARED_PREFIX};
const AUTH: &str = "https://accounts.google.com/o/oauth2/v2/auth";
const TOKEN: &str = "https://oauth2.googleapis.com/token";
const CALENDARS: &str = "https://www.googleapis.com/calendar/v3/calendars";
const SCOPE: &str = "https://www.googleapis.com/auth/calendar.app.created";
const SERVICE_SCOPE: &str = "https://www.googleapis.com/auth/calendar.events";

pub struct Google {
    store: Store,
    settings: Arc<Settings>,
    vault: Fernet,
    client: Client,
    token_endpoint: String,
    calendars_endpoint: String,
    gate: Mutex<()>,
    service_token: Mutex<Option<(String, f64)>>,
    shared: shared::Shared,
}

fn value(db: &Connection, key: &str) -> Result<Value> {
    Ok(meta(db, key)?.unwrap_or(Value::Null))
}

fn text(value: &Value) -> &str {
    value.as_str().unwrap_or("")
}

fn generation(db: &Connection) -> Result<i64> {
    Ok(meta(db, "google_generation")?.unwrap_or(0))
}

fn redirect_url(input: &str) -> Result<Url> {
    ensure!(input.len() <= 2048, "올바른 콜백 URI를 입력하세요.");
    let url = Url::parse(input).map_err(|_| anyhow!("올바른 콜백 URI를 입력하세요."))?;
    ensure!(
        url.username().is_empty()
            && url.password().is_none()
            && url.query().is_none()
            && url.fragment().is_none()
            && url.path() == CALLBACK
            && url.host().is_some(),
        "돌아올 주소는 안내 화면의 콜백 주소와 정확히 같아야 합니다."
    );
    let local = matches!(url.host_str(), Some("localhost" | "127.0.0.1" | "[::1]"));
    ensure!(
        url.scheme() == "https" || url.scheme() == "http" && local,
        "Google 연결에는 HTTPS 주소가 필요합니다. localhost SSH 터널도 사용할 수 있습니다."
    );
    Ok(url)
}

impl Google {
    pub fn new(store: Store, settings: Arc<Settings>) -> Result<Self> {
        let path = store
            .path
            .parent()
            .context("database directory missing")?
            .join("credentials.key");
        let key = store.write(|db| {
            if !path.exists() {
                let encrypted: bool = db.query_row(
                    "SELECT EXISTS(SELECT 1 FROM meta WHERE value LIKE '%enc:v1:%')",
                    [],
                    |r| r.get(0),
                )?;
                ensure!(
                    !encrypted,
                    "Google 인증정보 암호화 키가 없습니다. DB와 credentials.key를 함께 복원하세요."
                );
                let mut file = fs::OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .mode(0o600)
                    .open(&path)?;
                file.write_all(Fernet::generate_key().as_bytes())?;
                file.sync_all()?;
            }
            Ok(fs::read_to_string(path)?)
        })?;
        let vault =
            Fernet::new(key.trim()).context("Google 인증정보 암호화 키를 읽지 못했습니다.")?;
        let client = Client::builder()
            .timeout(Duration::from_secs(30))
            .redirect(redirect::Policy::none())
            .build()?;
        Ok(Self {
            store,
            settings,
            vault,
            client,
            token_endpoint: TOKEN.to_owned(),
            calendars_endpoint: CALENDARS.to_owned(),
            gate: Mutex::new(()),
            service_token: Mutex::new(None),
            shared: shared::Shared::from_env()?,
        })
    }

    fn decode(&self, value: &str) -> Result<Value> {
        let encrypted = value
            .strip_prefix("enc:v1:")
            .context("저장된 Google 인증정보 형식을 확인하세요.")?;
        let decoded = self.vault.decrypt(encrypted).map_err(|_| {
            anyhow!("Google 인증정보를 복호화하지 못했습니다. DB와 암호화 키의 백업을 확인하세요.")
        })?;
        Ok(serde_json::from_slice(&decoded)?)
    }

    fn encrypt(&self, data: &Value) -> Result<String> {
        Ok(format!(
            "enc:v1:{}",
            self.vault.encrypt(&serde_json::to_vec(data)?)
        ))
    }

    fn secret(&self, db: &Connection, key: &str) -> Result<Value> {
        let encoded: Option<String> = meta(db, key)?;
        encoded.map(|s| self.decode(&s)).unwrap_or(Ok(Value::Null))
    }

    fn save(&self, db: &Connection, key: &str, data: &Value) -> Result<()> {
        set_meta(db, key, self.encrypt(data)?)
    }

    pub fn status(&self) -> Result<Value> {
        let db = self.store.connection()?;
        let mode = value(&db, "google_mode")? == "oauth";
        let calendar = value(&db, "google_calendar")?;
        let id = if mode { text(&calendar["id"]) } else { "" };
        let tokens = !value(&db, "google_tokens")?.is_null();
        let pending = !value(&db, "google_pending_tokens")?.is_null();
        let client = !value(&db, "google_client")?.is_null();
        let method = meta::<String>(&db, "google_connection_method")?
            .unwrap_or_else(|| if client { "direct" } else { "shared" }.into());
        Ok(
            json!({"client_configured":client,"shared_available":true,"connection_method":method,
            "authorized":(mode&&tokens)||pending,"pending_authorization":pending,
            "connected":!id.is_empty()&&tokens,"calendar_id":id,
            "calendar_name":text(&calendar["summary"]),"error":text(&value(&db,"google_error")?)}),
        )
    }

    pub fn configured(&self) -> Result<bool> {
        let db = self.store.connection()?;
        if value(&db, "google_mode")? == "oauth" {
            return Ok(!text(&value(&db, "google_calendar")?["id"]).is_empty()
                && !value(&db, "google_tokens")?.is_null());
        }
        Ok(!self.settings.calendar_id.is_empty()
            && self
                .service_info()
                .is_ok_and(|c| c["type"] == "service_account"))
    }

    pub async fn configure(&self, raw: Value) -> Result<Value> {
        let _gate = self.gate.lock().await;
        block_in_place(|| self.configure_client(raw))
    }

    fn configure_client(&self, raw: Value) -> Result<Value> {
        let web = &raw["web"];
        let id = text(&web["client_id"]);
        let secret = text(&web["client_secret"]);
        ensure!(
            id.ends_with(".apps.googleusercontent.com")
                && !secret.is_empty()
                && secret.chars().count() <= 512,
            "Google Cloud에서 받은 웹 애플리케이션 OAuth 클라이언트 JSON을 선택하세요."
        );
        let uris = web["redirect_uris"]
            .as_array()
            .filter(|a| !a.is_empty() && a.len() <= 50)
            .context(
                "OAuth 클라이언트에 승인된 리디렉션 URI를 추가하고 JSON을 다시 내려받으세요.",
            )?;
        for uri in uris {
            redirect_url(text(uri))?;
        }
        let client = json!({"client_id":id,"client_secret":secret,"redirect_uris":uris,"auth_uri":AUTH,"token_uri":TOKEN});
        self.store.write(|db| {
            if self.secret(db,"google_client")?!=client {
                db.execute("DELETE FROM meta WHERE key IN ('google_tokens','google_calendar','google_pending_tokens','google_shared_pending','google_connection_method')",[])?;
                db.execute("DELETE FROM google_oauth_states",[])?;
                set_meta(db,"google_generation",generation(db)?+1)?;
            }
            self.save(db,"google_client",&client)?;
            set_meta(db,"google_mode","oauth")?;
            set_meta(db,"google_error","")
        })?;
        self.status()
    }

    pub fn begin(&self, uri: &str, session: &str) -> Result<String> {
        redirect_url(uri)?;
        self.store.write(|db| {
            let client = self.secret(db, "google_client")?;
            ensure!(
                !client.is_null(),
                "먼저 상세 안내에 따라 OAuth 클라이언트 JSON을 등록하세요."
            );
            ensure!(
                client["redirect_uris"]
                    .as_array()
                    .is_some_and(|a| a.iter().any(|x| x == uri)),
                "현재 접속 주소가 JSON의 승인된 리디렉션 URI에 없습니다."
            );
            let state = random_token::<32>()?;
            let verifier = random_token::<64>()?;
            let challenge = URL_SAFE_NO_PAD.encode(hex::decode(hash(&verifier))?);
            let time = now();
            let session = hash(session);
            db.execute(
                "DELETE FROM google_oauth_states WHERE expires<=?1 OR session_hash=?2",
                params![time, session],
            )?;
            db.execute("DELETE FROM meta WHERE key='google_shared_pending'", [])?;
            db.execute(
                "INSERT INTO google_oauth_states VALUES (?1,?2,?3,?4,?5,?6)",
                params![
                    hash(&state),
                    session,
                    time + 600.0,
                    self.encrypt(&json!(verifier))?,
                    uri,
                    generation(db)?
                ],
            )?;
            let mut url = Url::parse(AUTH)?;
            url.query_pairs_mut().extend_pairs([
                ("client_id", text(&client["client_id"])),
                ("response_type", "code"),
                ("redirect_uri", uri),
                ("scope", SCOPE),
                ("access_type", "offline"),
                ("prompt", "consent select_account"),
                ("state", &state),
                ("code_challenge", &challenge),
                ("code_challenge_method", "S256"),
            ]);
            Ok(url.into())
        })
    }

    fn consume(&self, state: &str, session: &str) -> Result<(String, String, Value, i64)> {
        ensure!(
            (20..=128).contains(&state.len()),
            "Google 연결 요청이 올바르지 않습니다. 연결 버튼을 다시 누르세요."
        );
        self.store.write(|db| {
            let state = hash(state);
            let (encrypted, uri, version) = db
                .query_row(
                    "SELECT verifier,redirect_uri,generation FROM google_oauth_states
                WHERE state_hash=?1 AND session_hash=?2 AND expires>?3",
                    params![state, hash(session), now()],
                    |r| {
                        Ok((
                            r.get::<_, String>(0)?,
                            r.get::<_, String>(1)?,
                            r.get::<_, i64>(2)?,
                        ))
                    },
                )
                .optional()?
                .filter(|(_, _, version)| generation(db).ok() == Some(*version))
                .context("연결 요청이 만료되었거나 다른 기기의 요청입니다. 다시 연결하세요.")?;
            db.execute(
                "DELETE FROM google_oauth_states WHERE state_hash=?1",
                [state],
            )?;
            let verifier = self.decode(&encrypted)?;
            Ok((
                text(&verifier).to_owned(),
                uri,
                self.secret(db, "google_client")?,
                version,
            ))
        })
    }

    pub async fn callback(
        &self,
        state: &str,
        code: &str,
        denied: bool,
        session: &str,
    ) -> Result<()> {
        let _gate = self.gate.lock().await;
        if denied {
            return block_in_place(|| {
                self.consume(state, session)?;
                self.store.set_meta(
                    "google_error",
                    "Google 연결이 취소되었습니다. 연결 버튼으로 다시 시도할 수 있습니다.",
                )
            });
        }
        ensure!(
            !code.is_empty() && code.len() <= 4096,
            "Google 인증 코드가 없습니다. 다시 연결하세요."
        );
        let (verifier, uri, client, version) = block_in_place(|| self.consume(state, session))?;
        let id = text(&client["client_id"]);
        let secret = text(&client["client_secret"]);
        let result=self.token_request(&[("grant_type","authorization_code"),("code",code),("redirect_uri",&uri),
            ("client_id",id),("client_secret",secret),("code_verifier",&verifier)]).await
            .context("Google 인증을 완료하지 못했습니다. OAuth 클라이언트·동의 화면 설정을 확인하고 다시 연결하세요.")?;
        let refresh = text(&result["refresh_token"]);
        ensure!(
            !refresh.is_empty(),
            "장기 연결 권한을 받지 못했습니다. Google 연결을 다시 승인하세요."
        );
        ensure!(
            result["scope"]
                .as_str()
                .is_none_or(|scope| scope.split_whitespace().any(|s| s == SCOPE)),
            "사건 전용 캘린더 권한을 승인하지 않았습니다."
        );
        let mut credentials = json!({"refresh_token":refresh,"token_uri":TOKEN,"client_id":id,"client_secret":secret,"scopes":[SCOPE]});
        merge_token(&mut credentials, &result)?;
        block_in_place(|| {
            self.store.write(|db| {
                ensure!(
                    generation(db)? == version,
                    "연결 설정이 변경되었습니다. 다시 연결하세요."
                );
                self.save(db, "google_pending_tokens", &credentials)
            })
        })?;
        self.prepare_locked().await
    }

    pub async fn disconnect(&self) -> Result<Value> {
        let _gate = self.gate.lock().await;
        block_in_place(|| self.clear_connection())
    }

    fn clear_connection(&self) -> Result<Value> {
        self.store.write(|db| {
            db.execute(
                "DELETE FROM meta WHERE key IN ('google_tokens','google_pending_tokens','google_shared_pending')",
                [],
            )?;
            db.execute("DELETE FROM google_oauth_states", [])?;
            set_meta(db, "google_generation", generation(db)? + 1)?;
            set_meta(db, "google_error", "")
        })?;
        self.status()
    }

    pub async fn prepare(&self) -> Result<Value> {
        let _gate = self.gate.lock().await;
        self.prepare_locked().await?;
        block_in_place(|| self.status())
    }

    async fn prepare_locked(&self) -> Result<()> {
        let original =
            block_in_place(|| self.store.meta::<Value>("google_calendar"))?.unwrap_or_default();
        let mut calendar = Value::Null;
        let key = "google_pending_tokens";
        if !text(&original["id"]).is_empty() {
            let response = self
                .send(
                    Method::GET,
                    calendar_url(&self.calendars_endpoint, Some(text(&original["id"])), None)?,
                    None,
                    Some(key),
                )
                .await?;
            if response.status().is_success() {
                calendar = response.json().await?;
            } else {
                ensure!(matches!(response.status(),StatusCode::FORBIDDEN|StatusCode::NOT_FOUND),"기존 사건 캘린더를 확인하지 못했습니다. 잠시 후 캘린더 준비를 다시 시도하세요.");
            }
        }
        if calendar.is_null() {
            let body = json!({"summary":"사건 관리","timeZone":self.settings.timezone.name(),"description":"Court Monitor가 관리하는 사건 전용 캘린더"});
            let response = self
                .send(
                    Method::POST,
                    calendar_url(&self.calendars_endpoint, None, None)?,
                    Some(&body),
                    Some(key),
                )
                .await?;
            ensure!(response.status().is_success(),"전용 캘린더를 만들지 못했습니다. Google Calendar API 사용 설정과 권한을 확인하세요.");
            calendar = response.json().await?;
        }
        block_in_place(|| self.save_calendar(&calendar))
    }

    fn save_calendar(&self, calendar: &Value) -> Result<()> {
        let id = text(&calendar["id"]);
        let key = "google_pending_tokens";
        ensure!(
            !id.is_empty(),
            "Google에서 올바른 캘린더 ID를 받지 못했습니다."
        );
        let name = calendar["summary"].as_str().unwrap_or("사건 관리");
        self.store.write(|db| {
            let pending = self.secret(db, key)?;
            self.save(db, "google_tokens", &pending)?;
            let method = if pending["shared_grant"].is_string() {
                "shared"
            } else {
                "direct"
            };
            set_meta(db, "google_connection_method", method)?;
            set_meta(db, "google_calendar", json!({"id":id,"summary":name}))?;
            set_meta(db, "google_mode", "oauth")?;
            set_meta(db, "google_generation", generation(db)? + 1)?;
            set_meta(db, "google_error", "")?;
            db.execute("DELETE FROM meta WHERE key='google_pending_tokens'", [])?;
            calendar_target(db, id)
        })
    }

    async fn token_request(&self, form: &[(&str, &str)]) -> Result<Value> {
        let response = self
            .client
            .post(&self.token_endpoint)
            .form(form)
            .send()
            .await
            .map_err(|e| e.without_url())
            .context("Google 인증과 통신하지 못했습니다.")?;
        ensure!(
            response.status().is_success(),
            "Google 인증이 실패했습니다. 연결을 다시 확인하세요."
        );
        let result: Value = response.json().await?;
        ensure!(
            !text(&result["access_token"]).is_empty(),
            "유효한 Google 인증 토큰을 받지 못했습니다."
        );
        Ok(result)
    }

    async fn access(&self, key: Option<&str>, refresh: bool) -> Result<String> {
        if let Some(key) = key {
            let mut tokens = block_in_place(|| self.secret(&self.store.connection()?, key))?;
            let token = text(&tokens["token"]);
            let expiry = expiry(text(&tokens["expiry"]));
            if !refresh && !token.is_empty() && expiry > now() + 120.0 {
                return Ok(token.to_owned());
            }
            if let Some(grant) = tokens["shared_grant"].as_str() {
                let result = self.request_shared_refresh(grant).await?;
                tokens["shared_grant"] = result["shared_grant"].clone();
                merge_token(&mut tokens, &result)?;
                block_in_place(|| self.store.write(|db| self.save(db, key, &tokens)))?;
                return Ok(text(&tokens["token"]).to_owned());
            }
            let stored_refresh = text(&tokens["refresh_token"]);
            ensure!(!stored_refresh.is_empty(), "Google 계정에 먼저 연결하세요.");
            let result = self
                .token_request(&[
                    ("grant_type", "refresh_token"),
                    ("refresh_token", stored_refresh),
                    ("client_id", text(&tokens["client_id"])),
                    ("client_secret", text(&tokens["client_secret"])),
                ])
                .await?;
            merge_token(&mut tokens, &result)?;
            block_in_place(|| self.store.write(|db| self.save(db, key, &tokens)))?;
            return Ok(text(&tokens["token"]).to_owned());
        }
        let mut cached = self.service_token.lock().await;
        if let Some((token, expiry)) = cached
            .as_ref()
            .filter(|(_, expiry)| !refresh && *expiry > now() + 120.0)
        {
            let _ = expiry;
            return Ok(token.clone());
        }
        let account = block_in_place(|| self.service_info())?;
        ensure!(
            account["type"] == "service_account",
            "Google 서비스 계정 인증정보를 확인하세요."
        );
        let claims = json!({"iss":text(&account["client_email"]),"scope":SERVICE_SCOPE,"aud":TOKEN,"iat":now() as u64,"exp":now() as u64+3600});
        let unsigned = format!(
            "{}.{}",
            URL_SAFE_NO_PAD.encode(br#"{"alg":"RS256","typ":"JWT"}"#),
            URL_SAFE_NO_PAD.encode(serde_json::to_vec(&claims)?)
        );
        let key = pem::parse(text(&account["private_key"]))
            .context("Google 서비스 계정 키를 읽지 못했습니다.")?;
        let key = RsaKeyPair::from_pkcs8(key.contents())
            .map_err(|_| anyhow!("Google 서비스 계정 키 형식이 올바르지 않습니다."))?;
        let mut signature = vec![0; key.public().modulus_len()];
        key.sign(
            &RSA_PKCS1_SHA256,
            &ring::rand::SystemRandom::new(),
            unsigned.as_bytes(),
            &mut signature,
        )
        .map_err(|_| anyhow!("Google 서비스 계정 인증 서명에 실패했습니다."))?;
        let assertion = format!("{unsigned}.{}", URL_SAFE_NO_PAD.encode(signature));
        let result = self
            .token_request(&[
                ("grant_type", "urn:ietf:params:oauth:grant-type:jwt-bearer"),
                ("assertion", &assertion),
            ])
            .await?;
        let token = text(&result["access_token"]).to_owned();
        *cached = Some((token.clone(), now() + f64::from(lifetime(&result))));
        Ok(token)
    }

    fn service_info(&self) -> Result<Value> {
        let raw = if self.settings.credentials_json.is_empty() {
            fs::read_to_string(&self.settings.credentials_file)?
        } else {
            self.settings.credentials_json.clone()
        };
        Ok(serde_json::from_str(&raw)?)
    }

    async fn send(
        &self,
        method: Method,
        url: Url,
        body: Option<&Value>,
        key: Option<&str>,
    ) -> Result<Response> {
        let create = |token: String| -> RequestBuilder {
            let request = self
                .client
                .request(method.clone(), url.clone())
                .bearer_auth(token);
            if let Some(body) = body {
                request.json(body)
            } else {
                request
            }
        };
        let mut response = create(self.access(key, false).await?)
            .send()
            .await
            .map_err(|e| e.without_url())?;
        if response.status() == StatusCode::UNAUTHORIZED {
            response = create(self.access(key, true).await?)
                .send()
                .await
                .map_err(|e| e.without_url())?;
        }
        Ok(response)
    }

    pub async fn sync_one(&self) -> Result<bool> {
        let _gate = self.gate.lock().await;
        let Some((target, key, event)) = block_in_place(|| self.claim_sync())? else {
            return Ok(false);
        };
        let result = self.upsert(&target, &event, key).await;
        block_in_place(|| self.finish_sync(&event, result))?;
        Ok(true)
    }

    fn claim_sync(&self) -> Result<Option<(String, Option<&'static str>, Event)>> {
        if !self.configured()? {
            return Ok(None);
        }
        let oauth = self.store.meta::<String>("google_mode")?.as_deref() == Some("oauth");
        let target = if oauth {
            text(
                &self
                    .store
                    .meta::<Value>("google_calendar")?
                    .unwrap_or_default()["id"],
            )
            .to_owned()
        } else {
            self.settings.calendar_id.clone()
        };
        let key = oauth.then_some("google_tokens");
        self.store.write(|db| calendar_target(db, &target))?;
        Ok(self
            .store
            .claim_event(now())?
            .map(|event| (target, key, event)))
    }

    fn finish_sync(&self, event: &Event, result: Result<Option<(StatusCode, u32)>>) -> Result<()> {
        match result {
            Ok(None) => self.store.event_synced(event, now()),
            Ok(Some((status, delay))) => {
                let message = match status {
                    StatusCode::UNAUTHORIZED => "Google 인증을 확인하세요.",
                    StatusCode::FORBIDDEN => {
                        "전용 캘린더의 쓰기 권한 또는 API 사용량을 확인하세요."
                    }
                    StatusCode::NOT_FOUND => "전용 캘린더 ID와 공유 설정을 확인하세요.",
                    StatusCode::TOO_MANY_REQUESTS => "Google Calendar 요청 한도에 도달했습니다.",
                    _ => "Google Calendar 일정을 갱신하지 못했습니다.",
                };
                self.store
                    .event_failed(event, &format!("{message} (HTTP {status})"), now(), delay)
            }
            Err(error) => self.store.event_failed(event, &error.to_string(), now(), 0),
        }
    }

    async fn upsert(
        &self,
        target: &str,
        event: &Event,
        key: Option<&str>,
    ) -> Result<Option<(StatusCode, u32)>> {
        let mut payload: Value = serde_json::from_str(&event.payload)?;
        let mut patch = payload.clone();
        for field in ["start", "end"] {
            if let Some(value) = patch.get_mut(field) {
                if value.get("dateTime").is_some() {
                    value["date"] = Value::Null;
                } else if value.get("date").is_some() {
                    value["dateTime"] = Value::Null;
                    value["timeZone"] = Value::Null;
                }
            }
        }
        let mut update = calendar_url(
            &self.calendars_endpoint,
            Some(target),
            Some(Some(&event.id)),
        )?;
        update.query_pairs_mut().append_pair("sendUpdates", "none");
        let mut response = self
            .send(Method::PATCH, update.clone(), Some(&patch), key)
            .await?;
        if response.status() == StatusCode::NOT_FOUND {
            let mut create = calendar_url(&self.calendars_endpoint, Some(target), Some(None))?;
            create.query_pairs_mut().append_pair("sendUpdates", "none");
            payload["id"] = json!(event.id);
            response = self.send(Method::POST, create, Some(&payload), key).await?;
            if response.status() == StatusCode::CONFLICT {
                response = self.send(Method::PATCH, update, Some(&patch), key).await?;
            }
        }
        let status = response.status();
        let retry = response
            .headers()
            .get("retry-after")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.parse().ok())
            .unwrap_or(0);
        Ok((!status.is_success()).then_some((status, retry)))
    }
}

fn calendar_url(base: &str, calendar: Option<&str>, events: Option<Option<&str>>) -> Result<Url> {
    let mut url = Url::parse(base)?;
    {
        let mut path = url
            .path_segments_mut()
            .map_err(|_| anyhow!("invalid Calendar URL"))?;
        if let Some(calendar) = calendar {
            path.push(calendar);
        }
        if let Some(event) = events {
            path.push("events");
            if let Some(event) = event {
                path.push(event);
            }
        }
    }
    Ok(url)
}

fn lifetime(value: &Value) -> u32 {
    value["expires_in"].as_u64().unwrap_or(3600).clamp(1, 86400) as u32
}

fn expiry(value: &str) -> f64 {
    DateTime::parse_from_rfc3339(value)
        .map(|d| d.timestamp())
        .or_else(|_| {
            NaiveDateTime::parse_from_str(value, "%Y-%m-%dT%H:%M:%S%.f")
                .map(|d| d.and_utc().timestamp())
        })
        .unwrap_or_default() as f64
}

fn merge_token(tokens: &mut Value, replacement: &Value) -> Result<()> {
    tokens["token"] = replacement["access_token"].clone();
    tokens["expiry"] = json!(DateTime::<Utc>::from_timestamp(
        now() as i64 + i64::from(lifetime(replacement)),
        0
    )
    .context("invalid OAuth expiry")?
    .to_rfc3339_opts(chrono::SecondsFormat::Secs, true));
    if !text(&replacement["refresh_token"]).is_empty() {
        tokens["refresh_token"] = replacement["refresh_token"].clone();
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        domain::{Hearing, Observation},
        store::CaseInput,
    };
    use axum::{extract::Request, http::StatusCode, response::IntoResponse, Router};
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    };
    use tokio::net::TcpListener;

    #[tokio::test(flavor = "multi_thread")]
    async fn exchanges_oauth_code_preserves_calendar_and_retries_duplicate_event() -> Result<()> {
        let fixture = tempfile::tempdir()?;
        let settings = Arc::new(Settings::from_env()?);
        let store = Store::open(&fixture.path().join("cases.sqlite3"))?;
        let calls = Arc::new(AtomicUsize::new(0));
        let requests = calls.clone();
        let mock = Router::new().fallback(move |request: Request| {
            let calls = requests.clone();
            async move {
                let path = request.uri().path();
                match (request.method().as_str(), path) {
                    ("POST", "/token") => axum::Json(
                        json!({"access_token":"fixture-access","refresh_token":"fixture-refresh",
                        "expires_in":3600,"scope":SCOPE}),
                    )
                    .into_response(),
                    ("GET", "/calendars/prior-calendar") => {
                        axum::Json(json!({"id":"prior-calendar","summary":"기존 캘린더"}))
                            .into_response()
                    }
                    ("POST", "/calendars/prior-calendar/events") => {
                        (StatusCode::CONFLICT, "already exists").into_response()
                    }
                    ("PATCH", path) if path.starts_with("/calendars/prior-calendar/events/") => {
                        if calls.fetch_add(1, Ordering::SeqCst) == 0 {
                            StatusCode::NOT_FOUND.into_response()
                        } else {
                            axum::Json(json!({"id":"updated"})).into_response()
                        }
                    }
                    _ => StatusCode::BAD_REQUEST.into_response(),
                }
            }
        });
        let listener = TcpListener::bind("127.0.0.1:0").await?;
        let base = format!("http://{}", listener.local_addr()?);
        let server = tokio::spawn(async move { axum::serve(listener, mock).await });
        let mut google = Google::new(store.clone(), settings.clone())?;
        google.client = Client::builder().no_proxy().build()?;
        google.token_endpoint = format!("{base}/token");
        google.calendars_endpoint = format!("{base}/calendars");
        let uri = "https://example.test/oauth/google/callback";
        google.configure(json!({"web":{"client_id":"fixture.apps.googleusercontent.com","client_secret":"fixture-only","redirect_uris":[uri]}})).await?;
        store.set_meta(
            "google_calendar",
            json!({"id":"prior-calendar","summary":"기존 캘린더"}),
        )?;
        let url = Url::parse(&google.begin(uri, "first-session")?)?;
        let state = url
            .query_pairs()
            .find(|(key, _)| key == "state")
            .unwrap()
            .1
            .into_owned();
        assert!(google
            .callback(&state, "fake-code", false, "different-session")
            .await
            .is_err());
        google
            .callback(&state, "fake-code", false, "first-session")
            .await?;
        assert_eq!(google.status()?["calendar_id"], "prior-calendar");
        assert!(google
            .callback(&state, "fake-code", false, "first-session")
            .await
            .is_err());

        let ids = store.import(&[CaseInput {
            court: "가상법원".into(),
            case_number: "2024가합1".into(),
            party: "가상당사자".into(),
        }])?;
        let time = now();
        let case = store.claim_case(time, &settings)?.expect("queued case");
        assert_eq!(case.id, ids[0]);
        let observed = Observation {
            hearings: vec![Hearing {
                day: "2099-10-08".into(),
                kind: "변론".into(),
                ..Default::default()
            }],
            ..Default::default()
        };
        store.complete(&case, observed, time, &settings)?;
        assert!(google.sync_one().await?);
        assert_eq!(calls.load(Ordering::SeqCst), 2);
        assert!(store.events(&case.id)?[0]["synced_at"].is_number());
        server.abort();
        Ok(())
    }
}
