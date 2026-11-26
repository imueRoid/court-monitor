use crate::releases::{version, Release, Updates, EXECUTABLE};
use anyhow::{bail, Context, Result};
use std::{
    env,
    path::{Path, PathBuf},
    process::ExitStatus,
    time::Duration,
};
use tokio::{
    process::{Child, Command},
    signal::unix::{signal, Signal, SignalKind},
    time::{interval, sleep, timeout},
};

pub const RESTART: i32 = 75;

#[derive(Clone)]
pub struct Listen {
    pub host: String,
    pub port: u16,
}

impl Listen {
    pub fn from_args(mut args: impl Iterator<Item = String>) -> Result<Self> {
        let mut listen = Self {
            host: "0.0.0.0".into(),
            port: 8080,
        };
        while let Some(arg) = args.next() {
            if arg == "--host" {
                listen.host = args.next().context("--host needs an address")?;
            } else if arg == "--port" {
                listen.port = args.next().context("--port needs a number")?.parse()?;
            } else if let Some(host) = arg.strip_prefix("--host=") {
                listen.host = host.into();
            } else if let Some(port) = arg.strip_prefix("--port=") {
                listen.port = port.parse()?;
            }
        }
        anyhow::ensure!(listen.port != 0, "port must be between 1 and 65535");
        Ok(listen)
    }
}

pub async fn terminate(child: &mut Child) {
    if let Some(pid) = child.id() {
        // These are our own children. Give SQLite workers time to release their leases.
        unsafe { libc::kill(pid as libc::pid_t, libc::SIGTERM) };
        if timeout(Duration::from_secs(25), child.wait())
            .await
            .is_err()
        {
            let _ = child.kill().await;
        }
    }
}

async fn stop(term: &mut Signal, interrupt: &mut Signal) {
    tokio::select! {
        _ = term.recv() => {},
        _ = interrupt.recv() => {},
    }
}

async fn check_on_boot(updates: &Updates, installed: &str) -> Result<()> {
    if env::var("COURT_AUTO_UPDATE").as_deref() == Ok("false")
        || !updates.marker("pending").is_empty()
    {
        return Ok(());
    }
    let newest = updates
        .history()
        .into_iter()
        .filter_map(|id| updates.installed(&id))
        .filter_map(|release| version(&release.version))
        .max()
        .max(version(installed));
    if let Some(release) = updates.latest().await? {
        if release.runtime == updates.runtime
            && version(&release.version) > newest
            && release.version != updates.marker("failed")
        {
            updates.stage(&release).await?;
        }
    }
    Ok(())
}

fn persist_active(updates: &Updates, release: &Release, history: &[String]) -> Result<()> {
    let mut keep = vec![release.version.clone()];
    if let Some(previous) = history.iter().find(|id| {
        **id != release.version
            && **id != updates.marker("failed")
            && updates.installed(id).is_some()
    }) {
        keep.push(previous.clone());
    }
    updates.write("history", &serde_json::to_string(&keep)?)?;
    updates.clear("pending")?;
    updates.prune(&keep);
    Ok(())
}

async fn ready(child: &mut Child, receipt: &Path) -> Result<()> {
    let mut checks = interval(Duration::from_millis(100));
    let deadline = sleep(Duration::from_secs(45));
    tokio::pin!(deadline);
    loop {
        tokio::select! {
            result = child.wait() => bail!("server exited before starting ({})", result?),
            _ = &mut deadline => bail!("server startup timed out"),
            _ = checks.tick() => {
                if receipt.is_file() {
                    return Ok(());
                }
            }
        }
    }
}

fn app_command(
    bundled: &Path,
    source: &Path,
    run_dir: &Path,
    listen: &Listen,
    release: Option<&Release>,
) -> Result<Command> {
    let mut command =
        Command::new(release.map_or_else(|| bundled.to_owned(), |_| source.join(EXECUTABLE)));
    command
        .args(["--serve"])
        .arg(run_dir)
        .args(["--host", &listen.host, "--port", &listen.port.to_string()])
        .current_dir(source)
        .kill_on_drop(true);
    if let Some(release) = release {
        command
            .env("COURT_MONITOR_VERSION", &release.version)
            .env("COURT_MONITOR_REVISION", &release.revision)
            .env("COURT_MONITOR_CREATED", &release.created);
    }
    for key in [
        "DATABASE_PATH",
        "ADMIN_PASSWORD_FILE",
        "GOOGLE_CREDENTIALS_FILE",
    ] {
        if let Some(value) = env::var_os(key).filter(|value| !value.is_empty()) {
            command.env(key, std::path::absolute(value)?);
        }
    }
    Ok(command)
}

pub async fn run(listen: Listen) -> Result<()> {
    let mut term = signal(SignalKind::terminate())?;
    let mut interrupt = signal(SignalKind::interrupt())?;
    let bundled = env::current_exe()?;
    let base = env::current_dir()?;
    let installed = env::var("COURT_MONITOR_VERSION").unwrap_or_default();
    let updates = Updates::from_env()?;
    let enabled = updates.enabled(&installed);
    if enabled {
        tokio::select! {
            result = check_on_boot(&updates, &installed) => {
                if let Err(error) = result {
                    eprintln!("Update check failed; starting the installed version: {error:#}");
                }
            }
            _ = stop(&mut term, &mut interrupt) => return Ok(()),
        }
    }

    loop {
        let history = if enabled {
            updates.history()
        } else {
            Vec::new()
        };
        let mut releases = Vec::new();
        if enabled {
            let pending = updates.marker("pending");
            let failed = updates.marker("failed");
            for id in std::iter::once(&pending).chain(history.iter()) {
                if id != &failed && version(id) > version(&installed) && !releases.contains(id) {
                    releases.push(id.clone());
                }
            }
        }
        let candidates = releases
            .iter()
            .filter_map(|id| updates.installed(id).map(Some))
            .chain([None]);
        let mut status: Option<ExitStatus> = None;
        for release in candidates {
            let run_dir = tempfile::tempdir()?;
            let source = release
                .as_ref()
                .map_or_else(|| base.clone(), |r| updates.directory(&r.version));
            let launched =
                app_command(&bundled, &source, run_dir.path(), &listen, release.as_ref())?.spawn();
            let start = if let Ok(mut child) = launched {
                let receipt: PathBuf = run_dir.path().join("ready");
                let started = tokio::select! {
                    result = ready(&mut child, &receipt) => result,
                    _ = stop(&mut term, &mut interrupt) => {
                        terminate(&mut child).await;
                        return Ok(());
                    },
                };
                if started.is_ok() {
                    if enabled {
                        let recorded = if let Some(release) = &release {
                            persist_active(&updates, release, &history)
                        } else {
                            updates
                                .clear("pending")
                                .and_then(|()| updates.clear("history"))
                        };
                        if let Err(error) = recorded {
                            eprintln!("Could not record the running release: {error:#}");
                        }
                    }
                    tokio::fs::write(run_dir.path().join("accepted"), b"").await?;
                    status = Some(tokio::select! {
                        result = child.wait() => result?,
                        _ = stop(&mut term, &mut interrupt) => {
                            terminate(&mut child).await;
                            return Ok(());
                        },
                    });
                    break;
                }
                terminate(&mut child).await;
                started
            } else {
                launched.map(|_| ()).map_err(Into::into)
            };
            if let Some(release) = release {
                eprintln!(
                    "Release {} could not start; falling back: {:#}",
                    release.version,
                    start.unwrap_err()
                );
                if version(&release.version) > version(&updates.marker("failed")) {
                    updates.write("failed", &release.version)?;
                }
                if updates.marker("pending") == release.version {
                    updates.clear("pending")?;
                }
            } else {
                return start;
            }
        }
        let status = status.context("no runnable NAS server")?;
        if status.code() != Some(RESTART) {
            anyhow::ensure!(status.success(), "NAS server terminated ({status})");
            return Ok(());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn existing_synology_command_keeps_the_host_port() {
        let args = "python -m uvicorn server.app:create_app --factory --host 0.0.0.0 --port 7708 --workers 1 --no-access-log";
        let listen = Listen::from_args(args.split_whitespace().map(str::to_owned)).unwrap();
        assert_eq!(listen.port, 7708);
        assert_eq!(listen.host, "0.0.0.0");
    }
}
