mod api;
mod auth;
mod config;
mod court;
mod domain;
mod google;
mod ocr;
mod releases;
mod store;
mod supervise;
mod web;
mod worker;

use anyhow::Result;
use std::{env, path::PathBuf};

#[tokio::main]
async fn main() -> Result<()> {
    let mut args = env::args().skip(1);
    let mode = args.next();
    if mode.as_deref() == Some("--healthcheck") {
        let port: u16 = args.next().unwrap_or_else(|| "8080".into()).parse()?;
        reqwest::Client::builder()
            .no_proxy()
            .timeout(std::time::Duration::from_secs(3))
            .build()?
            .get(format!("http://127.0.0.1:{port}/health"))
            .send()
            .await?
            .error_for_status()?;
    } else if mode.as_deref() == Some("--ocr-smoke") {
        let image = std::fs::read(
            args.next()
                .ok_or_else(|| anyhow::anyhow!("missing image"))?,
        )?;
        println!("{}", ocr::Ocr::default().recognize(&image)?);
    } else if mode.as_deref() == Some("--serve") {
        let run_dir = PathBuf::from(args.next().expect("missing run directory"));
        let options = supervise::Listen::from_args(args)?;
        if web::run(&run_dir, options).await? {
            std::process::exit(supervise::RESTART);
        }
    } else {
        let options = supervise::Listen::from_args(env::args().skip(1))?;
        supervise::run(options).await?;
    }
    Ok(())
}
