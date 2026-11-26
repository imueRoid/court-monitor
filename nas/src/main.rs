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

use anyhow::{ensure, Context, Result};
use std::{
    env,
    path::{Path, PathBuf},
    time::Duration,
};

fn legacy_health_port(mut args: impl Iterator<Item = String>) -> Result<u16> {
    const PREFIX: &str = "import urllib.request; urllib.request.urlopen('http://127.0.0.1:";
    const SUFFIX: &str = "/health',timeout=3)";
    const UNSUPPORTED: &str = "only the previous Court Monitor health check is supported";

    ensure!(args.next().as_deref() == Some("-c"), "{UNSUPPORTED}");
    let script = args.next().context(UNSUPPORTED)?;
    let port = script
        .strip_prefix(PREFIX)
        .and_then(|s| s.strip_suffix(SUFFIX))
        .context(UNSUPPORTED)?;
    ensure!(
        args.next().is_none() && port.bytes().all(|c| c.is_ascii_digit()),
        "{UNSUPPORTED}"
    );
    let port = port.parse::<u16>()?;
    ensure!(port != 0, "health check requires a nonzero port");
    Ok(port)
}

async fn healthcheck(port: u16) -> Result<()> {
    reqwest::Client::builder()
        .no_proxy()
        .timeout(Duration::from_secs(3))
        .build()?
        .get(format!("http://127.0.0.1:{port}/health"))
        .send()
        .await?
        .error_for_status()?;
    Ok(())
}

#[tokio::main]
async fn main() -> Result<()> {
    let mut args = env::args();
    // The previous Synology Compose calls its health check via "python -c".
    if args.next().is_some_and(|name| {
        Path::new(&name)
            .file_name()
            .is_some_and(|name| name == "python")
    }) {
        return healthcheck(legacy_health_port(args)?).await;
    }
    let mode = args.next();
    if mode.as_deref() == Some("--healthcheck") {
        let port: u16 = args.next().unwrap_or_else(|| "8080".into()).parse()?;
        healthcheck(port).await?;
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unchanged_synology_health_check_uses_the_configured_port() {
        for port in [7708, 8080, 8088] {
            let script = format!("import urllib.request; urllib.request.urlopen('http://127.0.0.1:{port}/health',timeout=3)");
            assert_eq!(
                legacy_health_port(["-c".into(), script].into_iter()).unwrap(),
                port
            );
        }
    }

    #[test]
    fn legacy_health_command_accepts_only_the_known_loopback_check() {
        for script in [
            "print(1)",
            "import urllib.request; urllib.request.urlopen('http://example.test:7708/health',timeout=3)",
            "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7708/admin',timeout=3)",
            "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7708/health',timeout=3); print(1)",
            "import urllib.request; urllib.request.urlopen('http://127.0.0.1:0/health',timeout=3)",
        ] {
            assert!(legacy_health_port(["-c".into(), script.into()].into_iter()).is_err());
        }
    }
}
