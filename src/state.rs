use crate::{backend::ApplyResult, carousel::Carousel};
use serde::{Deserialize, Serialize};
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

const MAX_STATE_BYTES: usize = 8 * 1024 * 1024;
static NEXT_TEMP: AtomicU64 = AtomicU64::new(0);

#[derive(Default, Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct State {
    pub version: u32,
    pub paused: bool,
    pub protective_suspension: Option<String>,
    #[serde(with = "crate::path_serde::option")]
    pub observed: Option<PathBuf>,
    #[serde(with = "crate::path_serde::option")]
    pub requested: Option<PathBuf>,
    pub last_result: Option<ApplyResult>,
    pub carousel: Carousel,
}
impl State {
    pub fn load(path: &Path) -> Result<Self, String> {
        let file = match fs::File::open(path) {
            Ok(file) => file,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                return Ok(Self {
                    version: 1,
                    ..Self::default()
                });
            }
            Err(e) => return Err(format!("Read state: {e}")),
        };
        let mut bytes = Vec::new();
        file.take(MAX_STATE_BYTES as u64 + 1)
            .read_to_end(&mut bytes)
            .map_err(|e| e.to_string())?;
        if bytes.len() > MAX_STATE_BYTES {
            return Err("State exceeds 8 MiB".into());
        }
        let state: Self = serde_json::from_slice(&bytes)
            .map_err(|e| format!("Invalid state (preserved): {e}"))?;
        if state.version != 1 {
            return Err(format!("Unsupported state version {}", state.version));
        }
        Ok(state)
    }
    pub fn save(&self, path: &Path) -> Result<(), String> {
        let bytes = serde_json::to_vec_pretty(self).map_err(|e| format!("Serialize state: {e}"))?;
        if bytes.len() > MAX_STATE_BYTES {
            return Err("State exceeds 8 MiB".into());
        }
        let parent = path.parent().ok_or("State needs a parent directory")?;
        match fs::File::open(path) {
            Ok(file) => {
                let mut existing = Vec::new();
                (&file)
                    .take(MAX_STATE_BYTES as u64 + 1)
                    .read_to_end(&mut existing)
                    .map_err(|e| format!("Read existing state: {e}"))?;
                if existing == bytes {
                    // A preceding rename may have succeeded before directory fsync
                    // failed. Equality alone must not turn that failure into durability.
                    file.sync_all()
                        .map_err(|e| format!("Sync existing state: {e}"))?;
                    return fs::File::open(parent)
                        .and_then(|dir| dir.sync_all())
                        .map_err(|e| format!("Sync state directory: {e}"));
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(format!("Read existing state: {e}")),
        }
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        let (temp, mut file) = loop {
            let temp = parent.join(format!(
                ".state-{}-{}.tmp",
                std::process::id(),
                NEXT_TEMP.fetch_add(1, Ordering::Relaxed)
            ));
            match OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .custom_flags(libc::O_NOFOLLOW)
                .open(&temp)
            {
                Ok(file) => break (temp, file),
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(e) => return Err(format!("Create state temporary file: {e}")),
            }
        };
        let result = (|| {
            file.set_permissions(fs::Permissions::from_mode(0o600))?;
            file.write_all(&bytes)?;
            file.sync_all()?;
            fs::rename(&temp, path)?;
            fs::File::open(parent)?.sync_all()
        })();
        if result.is_err() {
            let _ = fs::remove_file(&temp);
        }
        result.map_err(|e| format!("Atomic state write: {e}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn non_utf8_observed_and_requested_round_trip() {
        use std::ffi::OsString;
        use std::os::unix::ffi::OsStringExt;

        let observed = b"/observed-\xff.png".to_vec();
        let requested = b"/requested-\xfe.png".to_vec();
        let state = State {
            version: 1,
            observed: Some(OsString::from_vec(observed.clone()).into()),
            requested: Some(OsString::from_vec(requested.clone()).into()),
            ..State::default()
        };
        let json = serde_json::to_value(&state).unwrap();
        assert_eq!(
            json["observed"],
            serde_json::json!({ "unix_bytes": observed })
        );
        assert_eq!(
            json["requested"],
            serde_json::json!({ "unix_bytes": requested })
        );
        let loaded: State = serde_json::from_value(json).unwrap();
        assert_eq!(loaded.observed, state.observed);
        assert_eq!(loaded.requested, state.requested);
    }

    #[test]
    fn plain_json_paths_nulls_and_missing_fields_remain_compatible() {
        let mut json = serde_json::to_value(State::default()).unwrap();
        assert!(json["observed"].is_null());
        assert!(json["requested"].is_null());
        json["observed"] = serde_json::json!("/café.png");
        json["requested"] = serde_json::json!("/plain.png");
        let loaded: State = serde_json::from_value(json.clone()).unwrap();
        assert_eq!(loaded.observed, Some("/café.png".into()));
        assert_eq!(loaded.requested, Some("/plain.png".into()));
        assert_eq!(serde_json::to_value(loaded).unwrap(), json);
        let empty: State = serde_json::from_str("{}").unwrap();
        assert!(empty.observed.is_none());
        assert!(empty.requested.is_none());
        let nulls: State = serde_json::from_str(r#"{"observed":null,"requested":null}"#).unwrap();
        assert!(nulls.observed.is_none());
        assert!(nulls.requested.is_none());
    }

    #[test]
    fn invalid_path_representations_are_rejected() {
        use std::ffi::OsString;
        use std::os::unix::ffi::OsStringExt;

        for invalid in [
            serde_json::json!("/nul\0.png"),
            serde_json::json!({ "unix_bytes": [47, 0, 255] }),
            serde_json::json!({ "unix_bytes": [256] }),
            serde_json::json!({ "unix_bytes": [-1] }),
            serde_json::json!({ "unix_bytes": "not bytes" }),
            serde_json::json!({}),
        ] {
            for field in ["observed", "requested"] {
                let mut json = serde_json::json!({});
                json[field] = invalid.clone();
                assert!(serde_json::from_value::<State>(json).is_err());
            }
            for field in ["paths", "bag", "history"] {
                let mut json = serde_json::json!({});
                json[field] = serde_json::json!([invalid]);
                assert!(serde_json::from_value::<Carousel>(json).is_err());
            }
        }
        let path = PathBuf::from(OsString::from_vec(b"/nul\0.png".to_vec()));
        let state = State {
            observed: Some(path.clone()),
            ..State::default()
        };
        assert!(serde_json::to_value(state).is_err());
        let mut carousel = Carousel::default();
        carousel.reconcile(&[path]);
        assert!(serde_json::to_value(carousel).is_err());
    }

    struct TempDir(PathBuf);
    impl TempDir {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "swiftwall-state-extra-{}-{}",
                std::process::id(),
                NEXT_TEMP.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&path).unwrap();
            Self(path)
        }
    }
    impl Drop for TempDir {
        fn drop(&mut self) {
            fs::remove_dir_all(&self.0).unwrap();
        }
    }

    #[test]
    fn unchanged_save_preserves_inode_and_creates_no_temporary_file() {
        use std::os::unix::fs::MetadataExt;
        let dir = TempDir::new();
        let path = dir.0.join("state.json");
        let state = State {
            version: 1,
            ..State::default()
        };
        state.save(&path).unwrap();
        let before = fs::metadata(&path).unwrap();
        state.save(&path).unwrap();
        let after = fs::metadata(&path).unwrap();
        assert_eq!(before.ino(), after.ino());
        assert_eq!(before.mtime_nsec(), after.mtime_nsec());
        assert_eq!(fs::read_dir(&dir.0).unwrap().count(), 1);
    }

    #[test]
    fn oversized_save_preserves_previous_loadable_state() {
        let dir = TempDir::new();
        let path = dir.0.join("state.json");
        let mut state = State {
            version: 1,
            paused: true,
            ..State::default()
        };
        state.save(&path).unwrap();
        let previous = fs::read(&path).unwrap();
        state.last_result = Some(ApplyResult {
            success: false,
            message: "x".repeat(MAX_STATE_BYTES),
        });
        assert!(state.save(&path).unwrap_err().contains("8 MiB"));
        assert_eq!(fs::read(&path).unwrap(), previous);
        assert!(State::load(&path).unwrap().paused);
        assert_eq!(fs::read_dir(&dir.0).unwrap().count(), 1);
    }

    #[test]
    fn existing_old_temporary_symlink_is_not_followed_or_removed() {
        let dir = TempDir::new();
        let victim = dir.0.join("victim");
        fs::write(&victim, b"preserved").unwrap();
        let stale = dir.0.join(format!(".state-{}.tmp", std::process::id()));
        std::os::unix::fs::symlink(&victim, &stale).unwrap();
        let state = State {
            version: 1,
            ..State::default()
        };
        state.save(&dir.0.join("state.json")).unwrap();
        assert_eq!(fs::read(&victim).unwrap(), b"preserved");
        assert!(stale.is_symlink());
    }

    #[test]
    fn recovery_preserves_pause_and_rejects_corruption() {
        let dir = std::env::temp_dir().join(format!("swiftwall-state-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("state.json");
        let state = State {
            version: 1,
            paused: true,
            observed: Some("/manual.png".into()),
            ..State::default()
        };
        state.save(&path).unwrap();
        let loaded = State::load(&path).unwrap();
        assert!(loaded.paused);
        assert_eq!(loaded.observed, state.observed);
        fs::write(&path, b"broken").unwrap();
        assert!(State::load(&path).is_err());
        assert_eq!(fs::read(&path).unwrap(), b"broken");
        fs::remove_dir_all(dir).unwrap();
    }
}
