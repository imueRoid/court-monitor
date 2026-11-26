use crate::{
    config::Settings,
    court::Court,
    google::Google,
    ocr::Ocr,
    store::{now, Store},
};
use anyhow::Result;
use std::{sync::Arc, time::Duration};

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
            self.store
                .set_meta("courts", client.courts.keys().collect::<Vec<_>>())?;
            self.client = Some(client);
        }
        Ok(self.client.as_mut().expect("court client was initialized"))
    }

    async fn tick(&mut self) -> Result<()> {
        let at = now();
        self.store.set_meta("worker_heartbeat", at)?;
        if at - self.last_request < self.settings.request_gap_seconds as f64 {
            return Ok(());
        }
        let Some(job) = self.store.claim_case(at, &self.settings)? else {
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
        match result {
            Ok(observed) => self.store.complete(&job, &observed, at, &self.settings)?,
            Err(err) => {
                self.store
                    .fail_case(&job, &err.to_string(), at, &self.settings)?;
                self.client = None;
            }
        }
        Ok(())
    }
}

pub async fn court(store: Store, settings: Arc<Settings>) {
    let mut jobs = Worker {
        store,
        settings,
        client: None,
        reader: Ocr::default(),
        last_request: 0.0,
    };
    if jobs.client().await.is_err() {
        eprintln!("법원 목록을 불러오지 못했습니다. 사건 조회 시 다시 시도합니다.");
    }
    loop {
        if let Err(error) = jobs.tick().await {
            eprintln!("사건 조회 작업 오류: {error:#}");
        }
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
}

pub async fn calendar(google: Arc<Google>) {
    loop {
        if let Err(error) = google.sync_one().await {
            eprintln!("캘린더 작업 오류: {error:#}");
        }
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
}
