use crate::{
    config::Settings,
    store::{hash, meta, now, random, set_meta, Store},
};
use anyhow::Result;
use axum::http::{header, HeaderMap, HeaderValue};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use hmac::{Hmac, Mac};
use pbkdf2::pbkdf2_hmac;
use rusqlite::{params, Connection, OptionalExtension};
use sha2::Sha256;
use subtle::ConstantTimeEq;

const ROUNDS: u32 = 600_000;

pub struct Auth {
    store: Store,
    secret: String,
    max_age: u32,
    secure: bool,
}

pub enum Login {
    Allowed(String),
    MissingPassword,
    Incorrect,
    Locked(u32),
}

pub fn token(headers: &HeaderMap) -> &str {
    headers
        .get_all(header::COOKIE)
        .iter()
        .filter_map(|v| v.to_str().ok())
        .flat_map(|v| v.split(';'))
        .filter_map(|part| part.trim().split_once('='))
        .find_map(|(name, value)| (name == "court_session").then_some(value))
        .unwrap_or("")
}

pub fn random_token<const N: usize>() -> Result<String> {
    Ok(URL_SAFE_NO_PAD.encode(random::<N>()?))
}

fn derive(password: &str, salt: &[u8], rounds: u32) -> [u8; 32] {
    let mut output = [0; 32];
    pbkdf2_hmac::<Sha256>(password.as_bytes(), salt, rounds, &mut output);
    output
}

fn password_hash(password: &str) -> Result<String> {
    let salt = random::<16>()?;
    Ok(format!(
        "pbkdf2_sha256${ROUNDS}${}${}",
        hex::encode(salt),
        hex::encode(derive(password, &salt, ROUNDS))
    ))
}

fn verify(password: &str, encoded: &str) -> bool {
    let parts: Vec<_> = encoded.split('$').collect();
    let ["pbkdf2_sha256", rounds, salt, digest] = parts.as_slice() else {
        return false;
    };
    let Ok(rounds) = rounds.parse::<u32>() else {
        return false;
    };
    let (Ok(salt), Ok(digest)) = (hex::decode(salt), hex::decode(digest)) else {
        return false;
    };
    (100_000..=2_000_000).contains(&rounds)
        && !salt.is_empty()
        && bool::from(
            derive(password, &salt, rounds)
                .as_slice()
                .ct_eq(digest.as_slice()),
        )
}

impl Auth {
    pub fn new(store: Store, settings: &Settings) -> Result<Self> {
        let secret = store.write(|db| {
            if meta::<String>(db, "admin_password_hash")?.is_none() && !settings.password.is_empty()
            {
                set_meta(
                    db,
                    "admin_password_hash",
                    password_hash(&settings.password)?,
                )?;
            }
            if let Some(secret) = meta(db, "auth_secret")? {
                return Ok(secret);
            }
            let secret = hex::encode(random::<32>()?);
            set_meta(db, "auth_secret", &secret)?;
            Ok(secret)
        })?;
        Ok(Self {
            store,
            secret,
            max_age: settings.session_days * 86_400,
            secure: settings.cookie_secure,
        })
    }

    pub fn configured(&self) -> Result<bool> {
        Ok(self.store.meta::<String>("admin_password_hash")?.is_some())
    }

    pub fn valid(&self, token: &str) -> Result<bool> {
        if !(20..=128).contains(&token.len()) {
            return Ok(false);
        }
        let db = self.store.connection()?;
        Ok(db.query_row(
            "SELECT EXISTS(SELECT 1 FROM sessions WHERE token_hash=?1 AND expires>?2)",
            params![hash(token), now()],
            |r| r.get(0),
        )?)
    }

    pub fn csrf(&self, token: &str) -> String {
        let mut mac = Hmac::<Sha256>::new_from_slice(self.secret.as_bytes())
            .expect("HMAC allows any key length");
        mac.update(token.as_bytes());
        hex::encode(mac.finalize().into_bytes())
    }

    fn create_session(&self, db: &Connection, time: f64) -> Result<String> {
        let token = random_token::<32>()?;
        db.execute("DELETE FROM sessions WHERE expires<=?1", [time])?;
        db.execute(
            "INSERT INTO sessions VALUES (?1,?2,?3)",
            params![hash(&token), time, time + f64::from(self.max_age)],
        )?;
        db.execute("DELETE FROM sessions WHERE token_hash IN (SELECT token_hash FROM sessions ORDER BY created DESC LIMIT -1 OFFSET 100)",[])?;
        Ok(token)
    }

    pub fn setup(&self, password: &str) -> Result<Option<String>> {
        self.store.write(|db| {
            if meta::<String>(db, "admin_password_hash")?.is_some() {
                return Ok(None);
            }
            set_meta(db, "admin_password_hash", password_hash(password)?)?;
            self.create_session(db, now()).map(Some)
        })
    }

    pub fn login(&self, password: &str, client: &str) -> Result<Login> {
        self.store.write(|db| {
            let Some(encoded) : Option<String> = meta(db,"admin_password_hash")? else {return Ok(Login::MissingPassword);};
            let time = now();
            let client = self.csrf(client);
            db.execute("DELETE FROM login_attempts WHERE started<?1",[time-900.0])?;
            let attempt = db.query_row("SELECT failures,started FROM login_attempts WHERE client_hash=?1",[&client],
                |r| Ok((r.get::<_,u32>(0)?,r.get::<_,f64>(1)?))).optional()?;
            if let Some((_,started)) = attempt.filter(|(failures,_)| *failures>=5) {
                return Ok(Login::Locked((started+900.0-time).max(1.0) as u32));
            }
            if verify(password,&encoded) {
                db.execute("DELETE FROM login_attempts WHERE client_hash=?1",[&client])?;
                Ok(Login::Allowed(self.create_session(db,time)?))
            } else {
                db.execute("INSERT INTO login_attempts VALUES (?1,1,?2) ON CONFLICT(client_hash) DO UPDATE SET failures=failures+1",
                    params![client,time])?;
                Ok(Login::Incorrect)
            }
        })
    }

    pub fn logout(&self, token: &str) -> Result<()> {
        self.store.write(|db| {
            db.execute("DELETE FROM sessions WHERE token_hash=?1", [hash(token)])?;
            Ok(())
        })
    }

    pub fn change(&self, current: &str, new: &str) -> Result<bool> {
        self.store.write(|db| {
            let encoded: Option<String> = meta(db, "admin_password_hash")?;
            if !encoded.is_some_and(|encoded| verify(current, &encoded)) {
                return Ok(false);
            }
            set_meta(db, "admin_password_hash", password_hash(new)?)?;
            db.execute("DELETE FROM sessions", [])?;
            db.execute("DELETE FROM login_attempts", [])?;
            Ok(true)
        })
    }

    pub fn cookie(&self, token: &str, headers: &HeaderMap) -> HeaderValue {
        let secure = self.secure
            || headers
                .get("x-forwarded-proto")
                .is_some_and(|h| h == "https")
            || headers
                .get(header::ORIGIN)
                .is_some_and(|h| h.as_bytes().starts_with(b"https://"));
        let secure = if secure { "; Secure" } else { "" };
        let seconds = if token.is_empty() { 0 } else { self.max_age };
        HeaderValue::from_str(&format!(
            "court_session={token}; Path=/; Max-Age={seconds}; HttpOnly; SameSite=Lax{secure}"
        ))
        .expect("session tokens are URL-safe")
    }
}
