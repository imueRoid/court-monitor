use crate::{
    config::Settings,
    court::Court,
    diagnostics::{error_kind, Journal, Source},
    google::Google,
    ocr::Ocr,
    store::{now, Store},
};
use anyhow::Result;
use std::{
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::task::spawn_blocking;

struct Worker {
    store: Store,
    settings: Arc<Settings>,
    client: Option<Court>,
    reader: Ocr,
    last_request: f64,
}

impl Worker {
    async fn client(&mut self) -> Result<&mut Court> {
        if self.client.is_none() {
            let client = Court::connect().await?;
            let courts = client.courts.keys().cloned().collect::<Vec<_>>();
            let store = self.store.clone();
            spawn_blocking(move || store.set_meta("courts", courts)).await??;
            self.client = Some(client);
        }
        Ok(self.client.as_mut().expect("court client was initialized"))
    }

    async fn tick(&mut self) -> Result<()> {
        let at = now();
        let should_claim = at - self.last_request >= self.settings.request_gap_seconds as f64;
        let store = self.store.clone();
        let settings = self.settings.clone();
        let job = spawn_blocking(move || {
            store.set_meta("worker_heartbeat", at)?;
            if should_claim {
                store.claim_case(at, &settings)
            } else {
                Ok(None)
            }
        })
        .await??;
        let Some(job) = job else {
            return Ok(());
        };
        self.last_request = at;
        let mut reader = std::mem::take(&mut self.reader);
        let result = match self.client().await {
            Ok(client) => {
                client
                    .lookup(&job.court, &job.number, &job.party, &mut reader)
                    .await
            }
            Err(err) => Err(err),
        };
        self.reader = reader;
        if result.is_err() {
            self.client = None;
        }
        let store = self.store.clone();
        let settings = self.settings.clone();
        spawn_blocking(move || match result {
            Ok(observed) => store.complete(&job, observed, at, &settings),
            Err(error) => store.fail_case(&job, &error.to_string(), at, &settings),
        })
        .await?
    }
}

async fn report(
    journal: &Journal,
    operation: &str,
    error: &anyhow::Error,
    last: &mut Option<Instant>,
) {
    if last.is_some_and(|at| at.elapsed() < Duration::from_secs(60)) {
        return;
    }
    *last = Some(Instant::now());
    let journal = journal.clone();
    let message = format!("{operation}: {}", error_kind(error));
    let _ = spawn_blocking(move || journal.record(Source::Server, message)).await;
}

pub async fn court(store: Store, settings: Arc<Settings>, journal: Journal) {
    let mut jobs = Worker {
        store,
        settings,
        client: None,
        reader: Ocr::default(),
        last_request: 0.0,
    };
    let mut last_error = None;
    if let Err(error) = jobs.client().await {
        eprintln!("법원 목록을 불러오지 못했습니다. 사건 조회 시 다시 시도합니다.");
        report(
            &journal,
            "Court directory could not be loaded",
            &error,
            &mut last_error,
        )
        .await;
        last_error = None;
    }
    loop {
        if let Err(error) = jobs.tick().await {
            eprintln!("사건 조회 작업 오류: {error:#}");
            report(&journal, "Court worker error", &error, &mut last_error).await;
        }
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
}

pub async fn calendar(google: Arc<Google>, journal: Journal) {
    let mut last_error = None;
    loop {
        if let Err(error) = google.sync_one().await {
            eprintln!("캘린더 작업 오류: {error:#}");
            report(&journal, "Calendar worker error", &error, &mut last_error).await;
        }
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
}
