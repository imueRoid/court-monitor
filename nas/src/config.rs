use anyhow::{ensure, Context, Result};
use chrono_tz::Tz;
use std::{env, fs, path::PathBuf};

pub struct Settings {
    pub database: PathBuf,
    pub password: String,
    pub calendar_id: String,
    pub credentials_file: PathBuf,
    pub credentials_json: String,
    pub session_days: u32,
    pub cookie_secure: bool,
    pub timezone: Tz,
    pub base_poll_hours: i64,
    pub hearing_delay_minutes: i64,
    pub verdict_retry_minutes: i64,
    pub verdict_checks_per_day: u32,
    pub watch_start_hour: u32,
    pub watch_end_hour: u32,
    pub request_gap_seconds: u64,
}

fn number<T: std::str::FromStr>(name: &str, default: T) -> Result<T> {
    match env::var(name) {
        Ok(value) => value
            .parse()
            .ok()
            .with_context(|| format!("invalid value for {name}")),
        Err(_) => Ok(default),
    }
}

impl Settings {
    pub fn from_env() -> Result<Self> {
        let explicit_file = env::var("ADMIN_PASSWORD_FILE").unwrap_or_default();
        let mut password = env::var("ADMIN_PASSWORD").unwrap_or_default();
        if !explicit_file.is_empty() {
            password = fs::read_to_string(explicit_file.trim())
                .context("관리자 암호 secret 파일을 읽지 못했습니다.")?
                .trim()
                .to_owned();
        } else if password.is_empty() {
            password = fs::read_to_string("/run/secrets/admin_password")
                .unwrap_or_default()
                .trim()
                .to_owned();
        }
        ensure!(
            password.is_empty() || (8..=256).contains(&password.chars().count()),
            "관리자 암호는 8~256자여야 합니다."
        );
        let settings = Self {
            database: std::path::absolute(
                env::var_os("DATABASE_PATH").unwrap_or_else(|| "/data/cases.sqlite3".into()),
            )?,
            password,
            calendar_id: env::var("GOOGLE_CALENDAR_ID")
                .unwrap_or_default()
                .trim()
                .to_owned(),
            credentials_file: env::var_os("GOOGLE_CREDENTIALS_FILE")
                .unwrap_or_else(|| "/run/secrets/google_credentials".into())
                .into(),
            credentials_json: env::var("GOOGLE_CREDENTIALS_JSON")
                .unwrap_or_default()
                .trim()
                .to_owned(),
            session_days: number("SESSION_DAYS", 90)?,
            cookie_secure: env::var("COOKIE_SECURE").is_ok_and(|s| s.eq_ignore_ascii_case("true")),
            timezone: env::var("TZ")
                .unwrap_or_else(|_| "Asia/Seoul".into())
                .parse()
                .map_err(|_| anyhow::anyhow!("invalid TZ"))?,
            base_poll_hours: number("BASE_POLL_HOURS", 24)?,
            hearing_delay_minutes: number("HEARING_DELAY_MINUTES", 30)?,
            verdict_retry_minutes: number("VERDICT_RETRY_MINUTES", 30)?,
            verdict_checks_per_day: number("VERDICT_CHECKS_PER_DAY", 8)?,
            watch_start_hour: number("WATCH_START_HOUR", 9)?,
            watch_end_hour: number("WATCH_END_HOUR", 19)?,
            request_gap_seconds: number("REQUEST_GAP_SECONDS", 5)?,
        };
        ensure!(
            (1..=365).contains(&settings.session_days),
            "로그인 유지 기간은 1~365일이어야 합니다."
        );
        ensure!(
            settings.watch_start_hour < settings.watch_end_hour
                && settings.watch_end_hour <= 23
                && (1..=168).contains(&settings.base_poll_hours)
                && (1..=1440).contains(&settings.hearing_delay_minutes)
                && (10..=1440).contains(&settings.verdict_retry_minutes)
                && (1..=24).contains(&settings.verdict_checks_per_day)
                && (1..=3600).contains(&settings.request_gap_seconds),
            "조회 주기 설정이 허용 범위를 벗어났습니다."
        );
        Ok(settings)
    }
}
