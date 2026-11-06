use serde::{Deserialize, Serialize};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

const MAX_CONFIG_BYTES: usize = 1024 * 1024;
const MAX_ROOTS: usize = 64;
const MAX_PATH_BYTES: usize = 4096;

#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct Config {
    pub interval_seconds: u64,
    pub selection: String,
    pub folders: Vec<PathBuf>,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            interval_seconds: 300,
            selection: "shuffle".into(),
            folders: vec![],
        }
    }
}
impl Config {
    pub fn load(path: &Path) -> Result<Self, String> {
        let bytes = read_bytes(path)?;
        let text =
            std::str::from_utf8(&bytes).map_err(|e| format!("Invalid configuration UTF-8: {e}"))?;
        let config: Self =
            toml::from_str(text).map_err(|e| format!("Invalid configuration: {e}"))?;
        config.validate_and_normalize(path)
    }

    fn validate_limits(&self) -> Result<(), String> {
        if !(1..=31_536_000).contains(&self.interval_seconds) {
            return Err("interval_seconds must be between 1 and 31536000".into());
        }
        if !matches!(self.selection.as_str(), "shuffle" | "sequential") {
            return Err("selection must be \"shuffle\" or \"sequential\"".into());
        }
        if self.folders.is_empty() {
            return Err("Configure at least one folder".into());
        }
        if self.folders.len() > MAX_ROOTS {
            return Err("Configure at most 64 folders".into());
        }
        Ok(())
    }

    pub fn validate_and_normalize(&self, config_path: &Path) -> Result<Config, String> {
        self.validate_limits()?;
        let mut config = self.clone();
        let absolute_config = std::path::absolute(config_path).map_err(|e| e.to_string())?;
        let parent = absolute_config
            .parent()
            .ok_or("Configuration needs a parent directory")?;
        for folder in &mut config.folders {
            let text = folder.to_str().ok_or("Folder paths must be UTF-8")?;
            if text.starts_with("file:") {
                *folder = decode_file_url(text)?;
            }
            check_path(folder)?;
            if let Ok(rest) = folder.strip_prefix("~") {
                *folder =
                    PathBuf::from(std::env::var_os("HOME").ok_or("HOME is unset")?).join(rest);
            }
            if !folder.is_absolute() {
                *folder = parent.join(&*folder);
            }
            *folder = normalize_root(folder)?;
            check_path(folder)?;
        }
        config.folders.sort();
        let mut identities = std::collections::BTreeSet::new();
        config
            .folders
            .retain(|folder| identities.insert(root_identity(folder)));
        serialize_prepared(&config)?;
        Ok(config)
    }
}

// Pure validation only: persistence must keep exactly the configuration prepared
// by the caller, even if filesystem identities change before the commit.
fn serialize_prepared(config: &Config) -> Result<Vec<u8>, String> {
    config.validate_limits()?;
    for folder in &config.folders {
        check_path(folder)?;
        if !folder.is_absolute() {
            return Err("Prepared folder paths must be absolute".into());
        }
    }
    let bytes = toml::to_string(config)
        .map_err(|e| e.to_string())?
        .into_bytes();
    if bytes.len() > MAX_CONFIG_BYTES {
        return Err("Configuration exceeds 1 MiB".into());
    }
    Ok(bytes)
}

fn check_path(path: &Path) -> Result<(), String> {
    let bytes = path.as_os_str().as_bytes();
    if bytes.is_empty()
        || bytes.len() > MAX_PATH_BYTES
        || bytes.contains(&0)
        || path.to_str().is_none()
    {
        return Err(
            "Folder paths must be nonempty UTF-8, without NUL, and at most 4096 bytes".into(),
        );
    }
    Ok(())
}

fn normalize_root(path: &Path) -> Result<PathBuf, String> {
    normalize_root_with(
        path,
        |path| fs::canonicalize(path),
        |prefix| {
            fs::symlink_metadata(prefix).map(|metadata| {
                if metadata.file_type().is_symlink() {
                    RootComponent::Symlink
                } else if metadata.is_dir() {
                    RootComponent::Directory
                } else {
                    RootComponent::Other
                }
            })
        },
    )
}

#[derive(Clone, Copy)]
enum RootComponent {
    Directory,
    Symlink,
    Other,
}

fn unavailable_path(error: &std::io::Error) -> bool {
    matches!(
        error.kind(),
        std::io::ErrorKind::NotFound | std::io::ErrorKind::PermissionDenied
    ) || matches!(
        error.raw_os_error(),
        Some(
            libc::EIO
                | libc::ESTALE
                | libc::ENODEV
                | libc::ENXIO
                | libc::ETIMEDOUT
                | libc::ENOTCONN
        )
    )
}

// Inject only filesystem observations, so offline/error cases need no mounts or
// permissions changes in tests. Never collapse `..` after failed resolution:
// neither a missing component nor an inaccessible symlink has a known parent.
fn normalize_root_with(
    path: &Path,
    canonicalize: impl FnOnce(&Path) -> std::io::Result<PathBuf>,
    mut inspect: impl FnMut(&Path) -> std::io::Result<RootComponent>,
) -> Result<PathBuf, String> {
    let error = match canonicalize(path) {
        Ok(resolved) => return Ok(resolved),
        Err(error) => error,
    };
    let reject = |reason: &str| {
        format!(
            "Cannot safely resolve folder {}: {reason} (canonicalization: {error})",
            path.display()
        )
    };
    if !path.is_absolute()
        || path
            .components()
            .any(|component| component == std::path::Component::ParentDir)
    {
        return Err(reject(
            "unresolved path must be absolute and contain no '..'",
        ));
    }
    if !unavailable_path(&error) {
        return Err(reject("filesystem resolution failed"));
    }
    let mut prefix = PathBuf::new();
    for component in path.components() {
        prefix.push(component.as_os_str());
        match inspect(&prefix) {
            Ok(RootComponent::Directory) => {}
            Ok(RootComponent::Symlink) => {
                return Err(reject("unresolved path traverses a symlink"));
            }
            Ok(RootComponent::Other) => return Err(reject("path traverses a non-directory")),
            Err(error) if unavailable_path(&error) => {
                // Keep the spelling, not a partially canonicalized prefix. An
                // offline configured root can then recover at the same path.
                return Ok(path.to_path_buf());
            }
            Err(_) => return Err(reject("cannot inspect path components")),
        }
    }
    // The path may have become available between the two observations. Keeping
    // its spelling is still safer than inventing a different resolved identity.
    Ok(path.to_path_buf())
}

fn root_identity(path: &Path) -> PathBuf {
    fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf())
}

// Deliberately not a general URL parser: only local absolute file URLs are accepted.
fn decode_file_url(url: &str) -> Result<PathBuf, String> {
    let rest = url
        .strip_prefix("file://")
        .ok_or("Expected file:/// or file://localhost/ URL")?;
    let path = if rest.starts_with('/') {
        rest
    } else {
        rest.strip_prefix("localhost")
            .filter(|path| path.starts_with('/'))
            .ok_or("File URL authority must be empty or localhost")?
    };
    if path.contains(['?', '#']) {
        return Err(
            "File URLs must not contain a query or fragment; percent-encode literal characters"
                .into(),
        );
    }
    let mut decoded = Vec::with_capacity(path.len());
    let mut bytes = path.as_bytes().iter().copied();
    while let Some(byte) = bytes.next() {
        if byte == b'%' {
            let hi = bytes
                .next()
                .and_then(hex)
                .ok_or("Invalid file URL percent escape")?;
            let lo = bytes
                .next()
                .and_then(hex)
                .ok_or("Invalid file URL percent escape")?;
            decoded.push(hi * 16 + lo);
        } else {
            decoded.push(byte);
        }
    }
    let path = PathBuf::from(String::from_utf8(decoded).map_err(|_| "File URL is not UTF-8")?);
    check_path(&path)?;
    Ok(path)
}

fn hex(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

/// Existing roots may be temporarily offline. Only newly introduced identities
/// must currently be readable local directories. Empty directories are allowed;
/// image discovery and its user-facing warnings belong to collection validation.
pub fn prepare_update(
    candidate: Config,
    previous: &Config,
    config_path: &Path,
) -> Result<Config, String> {
    let candidate = candidate.validate_and_normalize(config_path)?;
    let previous = previous.validate_and_normalize(config_path)?;
    let existing: std::collections::BTreeSet<_> =
        previous.folders.iter().map(|p| root_identity(p)).collect();
    for root in &candidate.folders {
        if !existing.contains(&root_identity(root)) {
            validate_new_root(root)?;
        }
    }
    Ok(candidate)
}

fn validate_new_root(root: &Path) -> Result<(), String> {
    if !fs::metadata(root)
        .map_err(|e| format!("Inspect folder {}: {e}", root.display()))?
        .is_dir()
    {
        return Err(format!("Not a directory: {}", root.display()));
    }
    fs::read_dir(root).map_err(|e| format!("Read folder {}: {e}", root.display()))?;
    let name = std::ffi::CString::new(root.as_os_str().as_bytes()).map_err(|e| e.to_string())?;
    let mut info = std::mem::MaybeUninit::<libc::statfs>::uninit();
    // SAFETY: name is NUL terminated and info points to writable statfs storage.
    if unsafe { libc::statfs(name.as_ptr(), info.as_mut_ptr()) } != 0 {
        return Err(format!(
            "Inspect folder filesystem: {}",
            std::io::Error::last_os_error()
        ));
    }
    // SAFETY: a successful statfs initialized info.
    let kind = unsafe { info.assume_init() }.f_type as u64;
    if !local_filesystem(kind) {
        return Err(format!(
            "Folder must use local storage (network/GVFS/FUSE rejected): {}",
            root.display()
        ));
    }
    Ok(())
}

fn local_filesystem(kind: u64) -> bool {
    // Conservative: all FUSE, including local FUSE, is rejected because its
    // filesystem type cannot distinguish local storage from GVFS/remote mounts.
    !matches!(
        kind,
        0x6969
            | 0xff534d42
            | 0xfe534d42
            | 0x517b
            | 0x65735546
            | 0x564c
            | 0x73757245
            | 0x5346414f
            | 0x01021997
            | 0x00c36400
    )
}

fn open_config(path: &Path) -> Result<(File, fs::Metadata), String> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
        .map_err(|e| format!("Read configuration {}: {e}", path.display()))?;
    let metadata = file.metadata().map_err(|e| e.to_string())?;
    if !metadata.is_file() || metadata.len() > MAX_CONFIG_BYTES as u64 {
        return Err("Configuration must be a regular file of at most 1 MiB".into());
    }
    Ok((file, metadata))
}

pub fn read_bytes(path: &Path) -> Result<Vec<u8>, String> {
    let (file, _) = open_config(path)?;
    bounded_read(file)
}

fn bounded_read(file: File) -> Result<Vec<u8>, String> {
    let mut bytes = Vec::new();
    file.take(MAX_CONFIG_BYTES as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|e| e.to_string())?;
    if bytes.len() > MAX_CONFIG_BYTES {
        return Err("Configuration exceeds 1 MiB".into());
    }
    Ok(bytes)
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ConfigCommit {
    Durable,
    DurabilityUncertain(String),
}

/// Optimistic concurrency checks immediately precede writing and rename. As
/// with any pathname-based compare-and-rename, external writers must cooperate
/// to eliminate the final check/rename race; no ordinary rename is a CAS.
#[cfg(test)]
pub fn persist(
    config: &Config,
    path: &Path,
    expected_bytes: &[u8],
) -> Result<ConfigCommit, String> {
    persist_guarded(config, path, expected_bytes, || Ok(()))
}

/// The guard is called after temporary-file sync, before the final conflict
/// check and rename. Once it succeeds, callers must treat a stalled operation
/// as outcome-uncertain rather than promising that a timeout undid the write.
pub fn persist_guarded(
    config: &Config,
    path: &Path,
    expected_bytes: &[u8],
    commit_guard: impl FnOnce() -> std::io::Result<()>,
) -> Result<ConfigCommit, String> {
    persist_with_hooks(config, path, expected_bytes, commit_guard, |directory| {
        directory.sync_all()
    })
}

pub(crate) fn persist_with_hooks(
    config: &Config,
    path: &Path,
    expected_bytes: &[u8],
    before_replace: impl FnOnce() -> std::io::Result<()>,
    sync_directory: impl FnOnce(&File) -> std::io::Result<()>,
) -> Result<ConfigCommit, String> {
    let bytes = serialize_prepared(config)?;
    let (original, identity) = open_config(path)?;
    if bounded_read(original)? != expected_bytes {
        return Err("Configuration changed externally; reload before saving".into());
    }
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let directory = File::open(parent).map_err(|e| format!("Open configuration directory: {e}"))?;
    static NEXT_TEMP: AtomicU64 = AtomicU64::new(0);
    let (temp_path, mut temp) = (0..128)
        .find_map(|_| {
            let name = parent.join(format!(
                ".swiftwall-config-{}-{}.tmp",
                std::process::id(),
                NEXT_TEMP.fetch_add(1, Ordering::Relaxed)
            ));
            match OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .custom_flags(libc::O_NOFOLLOW)
                .open(&name)
            {
                Ok(file) => Some(Ok((name, file))),
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => None,
                Err(e) => Some(Err(e.to_string())),
            }
        })
        .ok_or("Could not allocate unique configuration temporary file")??;
    struct Cleanup(PathBuf);
    impl Drop for Cleanup {
        fn drop(&mut self) {
            let _ = fs::remove_file(&self.0);
        }
    }
    let cleanup = Cleanup(temp_path);
    temp.write_all(&bytes)
        .and_then(|_| temp.sync_all())
        .map_err(|e| format!("Write configuration temporary file: {e}"))?;
    before_replace().map_err(|e| format!("Prepare configuration replacement: {e}"))?;
    let (latest, latest_identity) = open_config(path)?;
    if identity.dev() != latest_identity.dev()
        || identity.ino() != latest_identity.ino()
        || bounded_read(latest)? != expected_bytes
    {
        return Err("Configuration changed externally; reload before saving".into());
    }
    fs::rename(&cleanup.0, path).map_err(|e| format!("Replace configuration: {e}"))?;
    match sync_directory(&directory) {
        Ok(()) => Ok(ConfigCommit::Durable),
        Err(e) => Ok(ConfigCommit::DurabilityUncertain(format!(
            "Configuration replaced, but directory sync failed: {e}"
        ))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{PermissionsExt, symlink};

    struct TestDir(PathBuf);
    impl TestDir {
        fn new() -> Self {
            static ID: AtomicU64 = AtomicU64::new(0);
            let path = std::env::temp_dir().join(format!(
                "swiftwall-config-test-{}-{}",
                std::process::id(),
                ID.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&path).unwrap();
            Self(path)
        }
        fn config_path(&self) -> PathBuf {
            self.0.join("external-config.toml")
        }
        fn config(&self) -> Config {
            Config {
                folders: vec![self.0.clone()],
                ..Config::default()
            }
        }
    }
    impl Drop for TestDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn strict_schema_limits_and_normalization() {
        let dir = TestDir::new();
        let path = dir.config_path();
        fs::write(
            &path,
            "interval_seconds = 1\nselection = 'shuffle'\nfolders = ['images']\n",
        )
        .unwrap();
        let config = Config::load(&path).unwrap();
        assert_eq!(config.folders, vec![dir.0.join("images")]);
        fs::write(&path, "folders = ['images']\nrevision = 1\n").unwrap();
        assert!(Config::load(&path).is_err());
        for interval in [0, 31_536_001] {
            let mut c = dir.config();
            c.interval_seconds = interval;
            assert!(c.validate_and_normalize(&path).is_err());
        }
        for selection in ["", "random", "Shuffle"] {
            let mut c = dir.config();
            c.selection = selection.into();
            assert!(c.validate_and_normalize(&path).is_err());
        }
        for selection in ["shuffle", "sequential"] {
            let mut c = dir.config();
            c.selection = selection.into();
            assert!(c.validate_and_normalize(&path).is_ok());
        }
        let mut c = dir.config();
        c.folders.clear();
        assert!(c.validate_and_normalize(&path).is_err());
        c.folders = vec![dir.0.clone(); 65];
        assert!(c.validate_and_normalize(&path).is_err());
        c.folders = vec![PathBuf::from("x".repeat(4097))];
        assert!(c.validate_and_normalize(&path).is_err());
        c.folders = vec![PathBuf::from("bad\0path")];
        assert!(c.validate_and_normalize(&path).is_err());
        c.folders = vec![PathBuf::from("a/../images")];
        assert!(c.validate_and_normalize(&path).is_err());
        let relative = Path::new("external-config.toml");
        c.folders = vec![PathBuf::from("images")];
        assert_eq!(
            c.validate_and_normalize(relative).unwrap().folders,
            vec![std::env::current_dir().unwrap().join("images")]
        );
    }

    #[test]
    fn file_urls_are_strict_and_literal_paths_are_not_urls() {
        assert_eq!(
            decode_file_url("file:///tmp/a%20b%23%25%C3%A9").unwrap(),
            PathBuf::from("/tmp/a b#%é")
        );
        assert_eq!(
            decode_file_url("file://localhost/tmp/雪").unwrap(),
            PathBuf::from("/tmp/雪")
        );
        for bad in [
            "file:/tmp/a",
            "file://remote/tmp/a",
            "file://localhost.evil/a",
            "file:///a?b",
            "file:///a#b",
            "file:///a%",
            "file:///a%2",
            "file:///a%GG",
            "file:///a%00",
            "file:///a%ff",
            "file://localhost",
        ] {
            assert!(decode_file_url(bad).is_err(), "{bad}");
        }
        let dir = TestDir::new();
        let mut c = dir.config();
        c.folders = vec![dir.0.join("a b#%雪")];
        assert_eq!(c.validate_and_normalize(&dir.config_path()).unwrap(), c);
        use std::os::unix::ffi::OsStringExt;
        c.folders = vec![PathBuf::from(std::ffi::OsString::from_vec(vec![
            b'/', 0xff,
        ]))];
        assert!(c.validate_and_normalize(&dir.config_path()).is_err());
    }

    #[test]
    fn file_url_length_limit_applies_to_decoded_path() {
        let dir = TestDir::new();
        let mut decoded = dir.0.join("missing");
        let mut encoded = format!("file://{}/missing", dir.0.display());
        // Each component is below NAME_MAX; the entire decoded path is below
        // PATH_MAX. Nothing is created for this lexical normalization test.
        for _ in 0..24 {
            decoded.push("雪".repeat(32));
            encoded.push('/');
            encoded.push_str(&"%E9%9B%AA".repeat(32));
        }
        assert!(encoded.len() > MAX_PATH_BYTES);
        assert!(encoded.len() < 64 * 1024);
        assert!(decoded.as_os_str().as_bytes().len() <= MAX_PATH_BYTES);
        let mut config = dir.config();
        config.folders = vec![PathBuf::from(&encoded)];
        assert_eq!(
            config
                .validate_and_normalize(&dir.config_path())
                .unwrap()
                .folders,
            vec![decoded]
        );
        config.folders = vec![PathBuf::from(format!("{encoded}/%00/.."))];
        assert!(config.validate_and_normalize(&dir.config_path()).is_err());
        config.folders = vec![PathBuf::from(format!(
            "file:///{}",
            "%E9%9B%AA/".repeat(1024)
        ))];
        assert!(config.validate_and_normalize(&dir.config_path()).is_err());
    }

    #[test]
    fn new_roots_are_local_directories_existing_missing_roots_survive() {
        let dir = TestDir::new();
        let path = dir.config_path();
        let previous = dir.config();
        let empty = dir.0.join("empty");
        fs::create_dir(&empty).unwrap();
        let mut candidate = previous.clone();
        candidate.folders.push(empty.clone());
        assert!(prepare_update(candidate.clone(), &previous, &path).is_ok());
        let alias = dir.0.join("alias");
        symlink(&empty, &alias).unwrap();
        candidate.folders.push(alias);
        assert_eq!(
            prepare_update(candidate, &previous, &path)
                .unwrap()
                .folders
                .len(),
            2
        );
        let missing = dir.0.join("offline");
        let mut old = previous.clone();
        old.folders.push(missing.clone());
        assert!(prepare_update(old.clone(), &old, &path).is_ok());
        assert!(prepare_update(old, &previous, &path).is_err());
        let file = dir.0.join("file");
        fs::write(&file, "not a directory").unwrap();
        let mut candidate = previous.clone();
        candidate.folders.push(file);
        assert!(prepare_update(candidate, &previous, &path).is_err());
        assert!(local_filesystem(0x01021994)); // tmpfs
        assert!(local_filesystem(0xef53)); // ext-family
        for kind in [0x6969, 0xff534d42, 0xfe534d42, 0x517b, 0x65735546] {
            assert!(!local_filesystem(kind));
        }
    }

    #[test]
    fn canonical_roots_preserve_symlink_parent_semantics() {
        let dir = TestDir::new();
        let target = dir.0.join("target");
        fs::create_dir_all(target.join("child")).unwrap();
        symlink(target.join("child"), dir.0.join("alias")).unwrap();
        let mut config = dir.config();
        config.folders = vec![dir.0.join("alias/.."), target.clone()];
        assert_eq!(
            config
                .validate_and_normalize(&dir.config_path())
                .unwrap()
                .folders,
            vec![fs::canonicalize(target).unwrap()]
        );
    }

    #[test]
    fn unresolved_roots_never_collapse_parents_or_follow_broken_links() {
        let dir = TestDir::new();
        let missing = dir.0.join("missing/雪 spaces # % '\"");
        assert_eq!(normalize_root(&missing).unwrap(), missing);
        for suffix in ["missing/..", "missing/../other"] {
            assert!(normalize_root(&dir.0.join(suffix)).is_err(), "{suffix}");
        }
        symlink(dir.0.join("absent"), dir.0.join("dangling")).unwrap();
        symlink("loop-b", dir.0.join("loop-a")).unwrap();
        symlink("loop-a", dir.0.join("loop-b")).unwrap();
        fs::write(dir.0.join("file"), "not a directory").unwrap();
        for suffix in [
            "dangling",
            "dangling/child",
            "dangling/..",
            "loop-a",
            "loop-a/child",
            "loop-a/..",
            "file/child",
            "file/..",
        ] {
            assert!(normalize_root(&dir.0.join(suffix)).is_err(), "{suffix}");
        }
        fs::create_dir(dir.0.join("real")).unwrap();
        symlink("real", dir.0.join("alias")).unwrap();
        // Even a valid symlink prefix cannot justify remapping a missing tail.
        assert!(normalize_root(&dir.0.join("alias/missing")).is_err());
    }

    #[test]
    fn unavailable_roots_keep_their_spelling_without_real_mounts() {
        let path = Path::new("/offline/雪 spaces # % '\"/images");
        for errno in [
            libc::ENOENT,
            libc::EACCES,
            libc::EPERM,
            libc::EIO,
            libc::ESTALE,
            libc::ENODEV,
            libc::ENXIO,
            libc::ETIMEDOUT,
            libc::ENOTCONN,
        ] {
            let normalize = |path: &Path| {
                normalize_root_with(
                    path,
                    |_| Err(std::io::Error::from_raw_os_error(errno)),
                    |prefix| {
                        if prefix == Path::new("/") {
                            Ok(RootComponent::Directory)
                        } else {
                            Err(std::io::Error::from_raw_os_error(errno))
                        }
                    },
                )
            };
            assert_eq!(normalize(path).unwrap(), path, "errno {errno}");
            assert!(normalize(Path::new("/offline/../other")).is_err());
        }
        for errno in [libc::ELOOP, libc::ENOTDIR, libc::EINVAL, libc::ENAMETOOLONG] {
            assert!(
                normalize_root_with(
                    path,
                    |_| Err(std::io::Error::from_raw_os_error(errno)),
                    |_| panic!("unsafe canonicalization error must reject immediately"),
                )
                .is_err()
            );
            assert!(
                normalize_root_with(
                    path,
                    |_| Err(std::io::Error::from_raw_os_error(libc::ENOENT)),
                    |_| Err(std::io::Error::from_raw_os_error(errno)),
                )
                .is_err()
            );
        }
        assert!(
            normalize_root_with(
                path,
                |_| Err(std::io::Error::from_raw_os_error(libc::EIO)),
                |_| Ok(RootComponent::Symlink),
            )
            .is_err()
        );
    }

    #[test]
    fn offline_configured_root_survives_update_persist_and_recovery() {
        let dir = TestDir::new();
        let root = dir.0.join("offline 雪 # % '\"");
        fs::create_dir(&root).unwrap();
        let previous = Config {
            folders: vec![root.clone()],
            ..Config::default()
        }
        .validate_and_normalize(&dir.config_path())
        .unwrap();
        fs::remove_dir(&root).unwrap();
        let bytes = toml::to_string(&previous).unwrap().into_bytes();
        fs::write(dir.config_path(), &bytes).unwrap();
        let candidate = Config {
            interval_seconds: 42,
            ..previous.clone()
        };
        let prepared = prepare_update(candidate, &previous, &dir.config_path()).unwrap();
        assert_eq!(prepared.folders, previous.folders);
        persist(&prepared, &dir.config_path(), &bytes).unwrap();
        assert_eq!(Config::load(&dir.config_path()).unwrap(), prepared);
        fs::create_dir(&root).unwrap();
        assert_eq!(Config::load(&dir.config_path()).unwrap(), prepared);
    }

    #[test]
    fn bounded_reads_reject_symlinks_and_oversized_files() {
        let dir = TestDir::new();
        let path = dir.config_path();
        fs::write(&path, vec![b' '; MAX_CONFIG_BYTES]).unwrap();
        assert_eq!(read_bytes(&path).unwrap().len(), MAX_CONFIG_BYTES);
        fs::write(&path, vec![b' '; MAX_CONFIG_BYTES + 1]).unwrap();
        assert!(read_bytes(&path).is_err());
        let alias = dir.0.join("link");
        symlink(&path, &alias).unwrap();
        assert!(read_bytes(&alias).is_err());
        assert!(read_bytes(&dir.0).is_err());
    }

    #[test]
    fn persistence_does_not_renormalize_prepared_roots() {
        let dir = TestDir::new();
        let path = dir.config_path();
        let previous = dir.config();
        let original = toml::to_string(&previous).unwrap().into_bytes();
        fs::write(&path, &original).unwrap();
        let root = dir.0.join("prepared-root");
        let other = dir.0.join("other-root");
        fs::create_dir(&root).unwrap();
        fs::create_dir(&other).unwrap();
        let candidate = Config {
            folders: vec![root.clone()],
            ..previous.clone()
        };
        let prepared = prepare_update(candidate, &previous, &path).unwrap();
        let prepared_bytes = toml::to_string(&prepared).unwrap().into_bytes();
        fs::rename(&root, dir.0.join("moved-root")).unwrap();
        symlink(&other, &root).unwrap();
        assert_eq!(
            persist(&prepared, &path, &original).unwrap(),
            ConfigCommit::Durable
        );
        assert_eq!(read_bytes(&path).unwrap(), prepared_bytes);
        assert_eq!(
            toml::from_str::<Config>(&fs::read_to_string(&path).unwrap()).unwrap(),
            prepared
        );
    }

    #[test]
    fn persistence_rejects_invalid_prepared_shape_without_changing_bytes() {
        let dir = TestDir::new();
        let path = dir.config_path();
        let original = b"folders = ['.']\n";
        fs::write(&path, original).unwrap();
        let mut invalid = vec![dir.config(); 8];
        invalid[0].folders = vec![PathBuf::from("relative")];
        invalid[1].folders = vec![PathBuf::from("file:///tmp/images")];
        invalid[2].folders = vec![PathBuf::from("/bad\0path")];
        invalid[3].folders = vec![PathBuf::from(format!("/{}", "x".repeat(MAX_PATH_BYTES)))];
        invalid[4].folders.clear();
        invalid[5].folders = vec![dir.0.clone(); MAX_ROOTS + 1];
        invalid[6].selection = "random".into();
        invalid[7].interval_seconds = 0;
        for config in invalid {
            assert!(persist(&config, &path, original).is_err());
            assert_eq!(read_bytes(&path).unwrap(), original);
        }
    }

    #[test]
    fn pre_rename_failures_and_late_external_changes_preserve_target() {
        let dir = TestDir::new();
        let path = dir.config_path();
        let original = b"folders = ['.']\n";
        fs::write(&path, original).unwrap();
        let config = dir.config();
        assert!(
            persist_with_hooks(
                &config,
                &path,
                original,
                || Err(std::io::Error::other("injected pre-rename failure")),
                |_| panic!("must not sync after failed replacement")
            )
            .is_err()
        );
        assert_eq!(fs::read(&path).unwrap(), original);
        let external = b"folders = ['external']\n";
        assert!(
            persist_with_hooks(
                &config,
                &path,
                original,
                || fs::write(&path, external),
                |_| panic!("must not commit conflict")
            )
            .is_err()
        );
        assert_eq!(fs::read(&path).unwrap(), external);
        fs::write(&path, original).unwrap();
        // Even an equal-byte inode replacement is a conflict.
        let replacement = dir.0.join("replacement");
        fs::write(&replacement, original).unwrap();
        assert!(
            persist_with_hooks(
                &config,
                &path,
                original,
                || fs::rename(&replacement, &path),
                |_| panic!("must not commit replaced inode")
            )
            .is_err()
        );
        assert_eq!(fs::read(&path).unwrap(), original);
        let saved = dir.0.join("saved");
        assert!(
            persist_with_hooks(
                &config,
                &path,
                original,
                || {
                    fs::rename(&path, &saved)?;
                    symlink(&saved, &path)
                },
                |_| panic!("must not follow symlink")
            )
            .is_err()
        );
        assert!(
            fs::symlink_metadata(&path)
                .unwrap()
                .file_type()
                .is_symlink()
        );
        assert_eq!(fs::read(&saved).unwrap(), original);
        assert!(fs::read_dir(&dir.0).unwrap().all(|entry| {
            !entry
                .unwrap()
                .file_name()
                .to_string_lossy()
                .ends_with(".tmp")
        }));
    }

    #[test]
    fn atomic_commit_conflict_permissions_and_post_rename_failure() {
        let dir = TestDir::new();
        let path = dir.config_path();
        let original = b"folders = ['.']\n";
        fs::write(&path, original).unwrap();
        let mut config = dir.config();
        config.selection = "sequential".into();
        assert!(persist(&config, &path, b"stale").is_err());
        assert_eq!(fs::read(&path).unwrap(), original);
        let mut invalid = config.clone();
        invalid.interval_seconds = 0;
        assert!(persist(&invalid, &path, original).is_err());
        assert_eq!(fs::read(&path).unwrap(), original);
        assert_eq!(
            persist(&config, &path, original).unwrap(),
            ConfigCommit::Durable
        );
        assert_eq!(Config::load(&path).unwrap(), config);
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        let expected = read_bytes(&path).unwrap();
        config.interval_seconds += 1;
        assert!(matches!(
            persist_with_hooks(
                &config,
                &path,
                &expected,
                || Ok(()),
                |_| Err(std::io::Error::other("injected sync failure"))
            )
            .unwrap(),
            ConfigCommit::DurabilityUncertain(_)
        ));
        assert_eq!(Config::load(&path).unwrap(), config);
        let alias = dir.0.join("link");
        symlink(&path, &alias).unwrap();
        let expected = read_bytes(&path).unwrap();
        assert!(persist(&config, &alias, &expected).is_err());
        assert_eq!(read_bytes(&path).unwrap(), expected);
        assert!(fs::read_dir(&dir.0).unwrap().all(|entry| {
            !entry
                .unwrap()
                .file_name()
                .to_string_lossy()
                .ends_with(".tmp")
        }));
        assert!(!dir.0.join("swiftwall").exists());
    }
}

/// Resolve only the configuration path; offline CLI validation needs no runtime
/// directory, desktop paths, or HOME when an explicit config/XDG path is set.
pub fn config_path_from_env() -> Result<PathBuf, String> {
    if let Some(path) = std::env::var_os("SWIFTWALL_CONFIG") {
        return Ok(PathBuf::from(path));
    }
    let base = if let Some(path) = std::env::var_os("XDG_CONFIG_HOME") {
        PathBuf::from(path)
    } else {
        PathBuf::from(std::env::var_os("HOME").ok_or("HOME is unset")?).join(".config")
    };
    Ok(base.join("swiftwall/config.toml"))
}

#[derive(Clone, Debug)]
pub struct Locations {
    pub config: PathBuf,
    pub state: PathBuf,
    pub runtime: PathBuf,
    pub background: PathBuf,
    pub program: PathBuf,
}
impl Locations {
    pub fn from_env() -> Result<Self, String> {
        let home = PathBuf::from(std::env::var_os("HOME").ok_or("HOME is unset")?);
        let env_path = |name: &str, default: PathBuf| {
            std::env::var_os(name).map(PathBuf::from).unwrap_or(default)
        };
        let runtime = std::env::var_os("XDG_RUNTIME_DIR")
            .map(PathBuf::from)
            .ok_or("XDG_RUNTIME_DIR is unset")?;
        Ok(Self {
            config: config_path_from_env()?,
            state: env_path(
                "SWIFTWALL_STATE_DIR",
                env_path("XDG_STATE_HOME", home.join(".local/state")).join("swiftwall"),
            )
            .join("state.json"),
            runtime: env_path("SWIFTWALL_RUNTIME_DIR", runtime.join("swiftwall")),
            // Omarchy currently hardcodes this location, independently of XDG_STATE_HOME.
            background: env_path(
                "SWIFTWALL_BACKGROUND_LINK",
                home.join(".local/state/omarchy/current/background"),
            ),
            program: env_path("SWIFTWALL_OMARCHY_COMMAND", PathBuf::from("omarchy")),
        })
    }
}
