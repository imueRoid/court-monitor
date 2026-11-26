use anyhow::{anyhow, ensure, Context, Result};
use reqwest::{redirect, Client, StatusCode, Url};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    env, fs,
    io::{self, Cursor, Write},
    os::unix::fs::PermissionsExt,
    path::{Component, Path, PathBuf},
    time::Duration,
};
use zip::ZipArchive;

const REPO: &str = "https://github.com/imueRoid/court-monitor/releases";
const MANIFEST: &str = "court-monitor-update.json";
const PACKAGE: &str = "court-monitor-app-linux-amd64.zip";
const ARCHIVE_LIMIT: usize = 24 * 1024 * 1024;
const EXTRACT_LIMIT: u64 = 64 * 1024 * 1024;
pub const EXECUTABLE: &str = "court-monitor-nas";

pub fn version(value: &str) -> Option<[u64; 3]> {
    let mut sections = value.split('.');
    let parse = |s: &str| {
        let value = s.parse::<u64>().ok()?;
        (value.to_string() == s).then_some(value)
    };
    let numbers = [
        parse(sections.next()?)?,
        parse(sections.next()?)?,
        parse(sections.next()?)?,
    ];
    sections.next().is_none().then_some(numbers)
}

fn hash(value: &str, digits: usize) -> bool {
    value.len() == digits
        && value
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Release {
    pub version: String,
    pub revision: String,
    pub created: String,
    pub runtime: String,
    pub sha256: String,
}

impl Release {
    fn validate(&self) -> Result<()> {
        ensure!(
            version(&self.version).is_some()
                && hash(&self.revision, 40)
                && hash(&self.runtime, 64)
                && hash(&self.sha256, 64)
                && self.created.len() <= 40
                && self.created.is_ascii()
                && !self.created.chars().any(char::is_control),
            "invalid release manifest"
        );
        Ok(())
    }
}

#[derive(Clone)]
pub struct Updates {
    root: PathBuf,
    pub runtime: String,
    client: Client,
}

impl Updates {
    pub fn from_env() -> Result<Self> {
        let database = env::var_os("DATABASE_PATH").unwrap_or_else(|| "/data/cases.sqlite3".into());
        let database = std::path::absolute(database)?;
        let root = database
            .parent()
            .context("DATABASE_PATH must point to a file")?
            .join("app-updates");
        let client = Client::builder()
            .connect_timeout(Duration::from_secs(4))
            .timeout(Duration::from_secs(15))
            .redirect(redirect::Policy::custom(|attempt| {
                if approved_url(attempt.url()) && attempt.previous().len() < 4 {
                    attempt.follow()
                } else {
                    attempt.error("unapproved release download location")
                }
            }))
            .build()?;
        Ok(Self {
            root,
            runtime: env::var("COURT_MONITOR_RUNTIME").unwrap_or_default(),
            client,
        })
    }

    pub fn enabled(&self, installed: &str) -> bool {
        env::var("COURT_UPDATES_ENABLED").as_deref() != Ok("false")
            && version(installed).is_some()
            && hash(&self.runtime, 64)
    }

    async fn download(&self, url: &str, limit: usize) -> Result<Option<Vec<u8>>> {
        let mut response = self
            .client
            .get(url)
            .send()
            .await
            .map_err(reqwest::Error::without_url)?;
        if response.status() == StatusCode::NOT_FOUND {
            return Ok(None);
        }
        response = response
            .error_for_status()
            .map_err(reqwest::Error::without_url)?;
        ensure!(
            response.content_length().is_none_or(|n| n <= limit as u64),
            "download too large"
        );
        let mut data = Vec::new();
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(reqwest::Error::without_url)?
        {
            ensure!(data.len() + chunk.len() <= limit, "download too large");
            data.extend_from_slice(&chunk);
        }
        Ok(Some(data))
    }

    pub async fn latest(&self) -> Result<Option<Release>> {
        let url = format!("{REPO}/latest/download/{MANIFEST}");
        self.download(&url, 16 * 1024)
            .await?
            .map(|data| {
                let release: Release = serde_json::from_slice(&data)?;
                release.validate()?;
                Ok(release)
            })
            .transpose()
    }

    pub async fn stage(&self, release: &Release) -> Result<()> {
        release.validate()?;
        ensure!(release.runtime == self.runtime, "incompatible runtime");
        let url = format!("{REPO}/download/app-v{}/{PACKAGE}", release.version);
        let data = self
            .download(&url, ARCHIVE_LIMIT)
            .await?
            .context("release package is missing")?;
        let updates = self.clone();
        let release = release.clone();
        tokio::task::spawn_blocking(move || updates.install(&release, data)).await?
    }

    fn install(&self, release: &Release, data: Vec<u8>) -> Result<()> {
        ensure!(
            hex::encode(Sha256::digest(&data)) == release.sha256,
            "release checksum mismatch"
        );
        let releases = self.root.join("releases");
        fs::create_dir_all(&releases)?;
        let target = releases.join(&release.version);
        if self.installed(&release.version).as_ref() != Some(release) {
            ensure!(
                !target.exists(),
                "a different package already uses this release version"
            );
            let directory = tempfile::tempdir_in(&releases)?;
            unpack(data, directory.path())?;
            fs::write(
                directory.path().join("release.json"),
                serde_json::to_vec(release)?,
            )?;
            fs::rename(directory.path(), &target)?;
        }
        if self.marker("failed") == release.version {
            self.clear("failed")?;
        }
        self.write("pending", &release.version)
    }

    pub fn installed(&self, id: &str) -> Option<Release> {
        version(id)?;
        let directory = self.directory(id);
        let release: Release =
            serde_json::from_slice(&fs::read(directory.join("release.json")).ok()?).ok()?;
        (release.validate().is_ok()
            && release.version == id
            && release.runtime == self.runtime
            && directory.join(EXECUTABLE).is_file())
        .then_some(release)
    }

    pub fn directory(&self, id: &str) -> PathBuf {
        self.root.join("releases").join(id)
    }

    pub fn marker(&self, name: &str) -> String {
        fs::read_to_string(self.root.join(name)).unwrap_or_default()
    }

    pub fn history(&self) -> Vec<String> {
        serde_json::from_str::<Vec<String>>(&self.marker("history"))
            .unwrap_or_default()
            .into_iter()
            .take(2)
            .collect()
    }

    pub fn write(&self, name: &str, value: &str) -> Result<()> {
        fs::create_dir_all(&self.root)?;
        let mut file = tempfile::NamedTempFile::new_in(&self.root)?;
        file.write_all(value.as_bytes())?;
        file.as_file().sync_all()?;
        file.persist(self.root.join(name))?;
        Ok(())
    }

    pub fn clear(&self, name: &str) -> Result<()> {
        match fs::remove_file(self.root.join(name)) {
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
            result => Ok(result?),
        }
    }

    pub fn prune(&self, keep: &[String]) {
        if let Ok(entries) = fs::read_dir(self.root.join("releases")) {
            for entry in entries.flatten() {
                let name = entry.file_name().to_string_lossy().into_owned();
                if version(&name).is_some() && !keep.contains(&name) {
                    let _ = fs::remove_dir_all(entry.path());
                }
            }
        }
    }
}

fn approved_url(url: &Url) -> bool {
    url.scheme() == "https"
        && url.port_or_known_default() == Some(443)
        && url.username().is_empty()
        && url.password().is_none()
        && matches!(
            url.host_str(),
            Some(
                "github.com"
                    | "release-assets.githubusercontent.com"
                    | "objects.githubusercontent.com"
            )
        )
}

fn allowed(path: &Path) -> bool {
    let parts = path.components().collect::<Vec<_>>();
    if parts
        .iter()
        .any(|part| !matches!(part, Component::Normal(_)))
    {
        return false;
    }
    let suffix = path.extension().and_then(|ext| ext.to_str());
    match parts.as_slice() {
        [Component::Normal(file)] => matches!(
            file.to_str(),
            Some(EXECUTABLE | "court_client.py" | "case_summary.py" | "captcha_ocr.py")
        ),
        [Component::Normal(folder), Component::Normal(_)] => {
            folder == &"server" && suffix == Some("py")
        }
        [Component::Normal(folder), Component::Normal(subfolder), Component::Normal(_)] => {
            folder == &"server"
                && subfolder == &"static"
                && matches!(suffix, Some("html" | "css" | "js"))
        }
        _ => false,
    }
}

fn unpack(data: Vec<u8>, target: &Path) -> Result<()> {
    let mut archive = ZipArchive::new(Cursor::new(data))?;
    ensure!(archive.len() <= 100, "too many files in release");
    let mut remaining = EXTRACT_LIMIT;
    for index in 0..archive.len() {
        let entry = archive.by_index(index)?;
        let relative = entry
            .enclosed_name()
            .filter(|path| allowed(path))
            .ok_or_else(|| anyhow!("unexpected file in release"))?;
        ensure!(
            entry.is_file() && !entry.is_symlink() && entry.size() <= remaining,
            "invalid release file"
        );
        let path = target.join(&relative);
        fs::create_dir_all(path.parent().expect("extract target has a parent"))?;
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)?;
        let size = entry.size();
        let copied = io::copy(&mut io::Read::take(entry, size + 1), &mut file)?;
        ensure!(size == copied, "release file size mismatch");
        remaining -= copied;
        if relative == Path::new(EXECUTABLE) {
            file.set_permissions(fs::Permissions::from_mode(0o755))?;
        }
    }
    for required in [
        EXECUTABLE,
        "server/app.py",
        "server/static/index.html",
        "court_client.py",
        "case_summary.py",
        "captcha_ocr.py",
    ] {
        ensure!(
            target.join(required).is_file(),
            "incomplete release package"
        );
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use zip::{write::SimpleFileOptions, ZipWriter};

    fn archive(extra: Option<&str>) -> Vec<u8> {
        let mut writer = ZipWriter::new(Cursor::new(Vec::new()));
        for path in [
            EXECUTABLE,
            "server/app.py",
            "server/static/index.html",
            "court_client.py",
            "case_summary.py",
            "captcha_ocr.py",
        ]
        .into_iter()
        .chain(extra)
        {
            writer
                .start_file(path, SimpleFileOptions::default())
                .unwrap();
            writer.write_all(b"app fixture").unwrap();
        }
        writer.finish().unwrap().into_inner()
    }

    #[test]
    fn validates_download_then_stages_an_executable_update() {
        let directory = tempfile::tempdir().unwrap();
        let updates = Updates {
            root: directory.path().into(),
            runtime: "a".repeat(64),
            client: Client::new(),
        };
        let package = archive(None);
        let mut release = Release {
            version: "1.0.42".into(),
            revision: "b".repeat(40),
            created: "2026-01-01T00:00:00Z".into(),
            runtime: updates.runtime.clone(),
            sha256: "c".repeat(64),
        };
        assert!(updates.install(&release, package.clone()).is_err());
        assert!(updates.marker("pending").is_empty());
        release.sha256 = hex::encode(Sha256::digest(&package));
        updates.install(&release, package).unwrap();
        assert_eq!(updates.marker("pending"), "1.0.42");
        assert_eq!(updates.installed("1.0.42"), Some(release));
        assert_eq!(
            updates
                .directory("1.0.42")
                .join(EXECUTABLE)
                .metadata()
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o755
        );
    }

    #[test]
    fn rejects_paths_and_files_outside_the_app() {
        for path in [
            "../outside",
            "server/../credentials.key",
            "server/static/secret.json",
            "credentials.key",
        ] {
            let directory = tempfile::tempdir().unwrap();
            assert!(unpack(archive(Some(path)), directory.path()).is_err());
        }
    }
}
