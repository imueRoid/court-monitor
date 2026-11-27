use crate::config::database_path;
use chrono::{SecondsFormat, Utc};
use std::{
    fmt::Display,
    fs::{self, File, OpenOptions},
    io::{self, Read, Write},
    os::unix::fs::OpenOptionsExt,
    path::{Path, PathBuf},
    sync::{mpsc, OnceLock},
    thread,
};

const FILE_LIMIT: u64 = 128 * 1024;
const DISPLAY_LINES: usize = 200;
const QUEUE_LIMIT: usize = 128;

pub fn console(line: String) {
    static STDERR: OnceLock<mpsc::SyncSender<String>> = OnceLock::new();
    let writer = STDERR.get_or_init(|| {
        let (sender, receiver) = mpsc::sync_channel::<String>(QUEUE_LIMIT);
        let _ = thread::Builder::new()
            .name("diagnostics-stderr".into())
            .spawn(move || {
                for line in receiver {
                    let _ = io::stderr().write_all(line.as_bytes());
                }
            });
        sender
    });
    let _ = writer.try_send(line);
}

#[derive(Clone, Copy)]
pub enum Source {
    Supervisor,
    Server,
}

impl Source {
    fn name(self) -> &'static str {
        match self {
            Self::Supervisor => "supervisor",
            Self::Server => "server",
        }
    }
}

#[derive(Clone)]
pub struct Journal {
    directory: PathBuf,
    writer: mpsc::SyncSender<(Source, String)>,
}

fn allow_missing<T: Default>(result: io::Result<T>) -> io::Result<T> {
    match result {
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(T::default()),
        result => result,
    }
}

impl Journal {
    pub fn from_env() -> io::Result<Self> {
        let path = database_path()?;
        let directory = path.parent().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidInput, "database directory missing")
        })?;
        Ok(Self::start(directory.join("diagnostics")))
    }

    fn start(directory: PathBuf) -> Self {
        let (writer, receiver) = mpsc::sync_channel::<(Source, String)>(QUEUE_LIMIT);
        let output = directory.clone();
        let _ = thread::Builder::new()
            .name("diagnostics-file".into())
            .spawn(move || {
                for (source, line) in receiver {
                    if let Err(error) = append(&output, source, &line) {
                        console(format!("Could not write diagnostics: {}\n", error.kind()));
                    }
                }
            });
        Self { directory, writer }
    }

    /// A stalled log destination must not hold up the HTTP server or supervisor.
    pub fn record(&self, source: Source, message: impl Display) {
        let stamp = Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true);
        let line = format!("{stamp} [{}] {message}\n", source.name());
        let _ = self.writer.try_send((source, line.clone()));
        console(line);
    }

    pub fn latest(&self) -> io::Result<String> {
        let mut entries = Vec::new();
        for source in [Source::Supervisor, Source::Server] {
            for previous in [true, false] {
                let path = path(&self.directory, source, previous);
                let text = allow_missing(read(&path))?;
                entries.extend(text.lines().map(str::to_owned));
            }
        }
        entries.sort();
        Ok(entries[entries.len().saturating_sub(DISPLAY_LINES)..].join("\n"))
    }
}

fn path(directory: &Path, source: Source, previous: bool) -> PathBuf {
    let suffix = if previous { "log.1" } else { "log" };
    directory.join(format!("{}.{suffix}", source.name()))
}

fn append(directory: &Path, source: Source, line: &str) -> io::Result<()> {
    fs::create_dir_all(directory)?;
    let current = path(directory, source, false);
    let length = allow_missing(fs::metadata(&current).map(|meta| meta.len()))?;
    if length + line.len() as u64 > FILE_LIMIT {
        fs::rename(&current, path(directory, source, true))?;
    }
    OpenOptions::new()
        .append(true)
        .create(true)
        .mode(0o600)
        .open(current)?
        .write_all(line.as_bytes())
}

fn read(path: &Path) -> io::Result<String> {
    let mut data = Vec::new();
    File::open(path)?.take(FILE_LIMIT).read_to_end(&mut data)?;
    Ok(String::from_utf8_lossy(&data).into_owned())
}

pub fn error_kind(error: &anyhow::Error) -> String {
    if let Some(rusqlite::Error::SqliteFailure(code, _)) = error.downcast_ref() {
        format!("SQLite {:?} ({})", code.code, code.extended_code)
    } else if let Some(error) = error.downcast_ref::<io::Error>() {
        format!("I/O {}", error.kind())
    } else if let Some(error) = error.downcast_ref::<tokio::task::JoinError>() {
        if error.is_panic() {
            "task panicked"
        } else {
            "task cancelled"
        }
        .into()
    } else if let Some(error) = error.downcast_ref::<reqwest::Error>() {
        if error.is_timeout() {
            "network timeout".into()
        } else if let Some(status) = error.status() {
            format!("HTTP {status}")
        } else {
            "network request failed".into()
        }
    } else {
        "operation failed".into()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rotates_logs_and_shows_recent_entries_from_both_processes() -> io::Result<()> {
        let temp = tempfile::tempdir()?;
        let journal = Journal::start(temp.path().to_path_buf());
        append(
            temp.path(),
            Source::Supervisor,
            &"a".repeat(FILE_LIMIT as usize),
        )?;
        append(temp.path(), Source::Supervisor, "restart\n")?;
        append(temp.path(), Source::Server, "worker recovered\n")?;
        assert_eq!(
            fs::metadata(path(temp.path(), Source::Supervisor, true))?.len(),
            FILE_LIMIT
        );
        let recent = journal.latest()?;
        assert!(recent.ends_with("restart\nworker recovered"));
        Ok(())
    }
}
