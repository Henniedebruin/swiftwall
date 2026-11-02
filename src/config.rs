use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, Deserialize, Serialize)]
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
        let text = std::fs::read_to_string(path).map_err(|e| {
            format!(
                "Read configuration {}: {e}; see config.example.toml",
                path.display()
            )
        })?;
        let mut config: Self =
            toml::from_str(&text).map_err(|e| format!("Invalid configuration: {e}"))?;
        if !(1..=31_536_000).contains(&config.interval_seconds) {
            return Err("interval_seconds must be between 1 and 31536000".into());
        }
        if config.selection != "shuffle" {
            return Err("v0.1 supports selection = \"shuffle\" only".into());
        }
        if config.folders.is_empty() {
            return Err("Configure at least one folder".into());
        }
        for folder in &mut config.folders {
            if let Ok(rest) = folder.strip_prefix("~") {
                *folder =
                    PathBuf::from(std::env::var_os("HOME").ok_or("HOME is unset")?).join(rest);
            } else if !folder.is_absolute() {
                *folder = path.parent().unwrap_or(Path::new(".")).join(&*folder);
            }
        }
        config.folders.sort();
        config.folders.dedup();
        Ok(config)
    }
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
            config: env_path(
                "SWIFTWALL_CONFIG",
                env_path("XDG_CONFIG_HOME", home.join(".config")).join("swiftwall/config.toml"),
            ),
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
