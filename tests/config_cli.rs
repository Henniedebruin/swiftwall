#![cfg(target_os = "linux")]

use serde_json::Value;
use std::fs;
use std::os::unix::fs::symlink;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicU64, Ordering};

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        loop {
            let path = std::env::temp_dir().join(format!(
                "swiftwall-config-cli-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            match fs::create_dir(&path) {
                Ok(()) => return Self(fs::canonicalize(path).unwrap()),
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(error) => panic!("create fixture: {error}"),
            }
        }
    }

    fn config(&self, folders: &[PathBuf]) -> PathBuf {
        let path = self.0.join("config 雪 # % '\".toml");
        fs::write(
            &path,
            toml::to_string(&serde_json::json!({ "folders": folders })).unwrap(),
        )
        .unwrap();
        path
    }

    fn command(&self) -> Command {
        let mut command = Command::new(env!("CARGO_BIN_EXE_swiftwall"));
        // No inherited runtime, HOME, desktop commands, or real user config.
        command.env_clear().current_dir(&self.0).arg("check-config");
        command
    }

    fn check(&self, folders: &[PathBuf]) -> Output {
        self.command()
            .env("SWIFTWALL_CONFIG", self.config(folders))
            .output()
            .unwrap()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn folders(output: Output) -> Vec<PathBuf> {
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let json: Value = serde_json::from_slice(&output.stdout).unwrap();
    serde_json::from_value(json["folders"].clone()).unwrap()
}

#[test]
fn explicit_config_is_offline_and_preserves_missing_literal_paths() {
    let fixture = Fixture::new();
    let missing = fixture.0.join("missing 雪 spaces # % '\"/images");
    assert_eq!(
        folders(fixture.check(std::slice::from_ref(&missing))),
        vec![missing]
    );
    assert_eq!(
        folders(fixture.check(&[PathBuf::from("relative missing 雪 # % '\"")])),
        vec![fixture.0.join("relative missing 雪 # % '\"")]
    );
}

#[test]
fn offline_config_resolver_supports_xdg_and_home_defaults() {
    let fixture = Fixture::new();
    let missing = fixture.0.join("missing");
    let config = fixture.config(std::slice::from_ref(&missing));
    let xdg = fixture.0.join("xdg");
    fs::create_dir_all(xdg.join("swiftwall")).unwrap();
    fs::copy(&config, xdg.join("swiftwall/config.toml")).unwrap();
    assert_eq!(
        folders(
            fixture
                .command()
                .env("XDG_CONFIG_HOME", &xdg)
                .output()
                .unwrap()
        ),
        vec![missing.clone()]
    );
    let home = fixture.0.join("home");
    fs::create_dir_all(home.join(".config/swiftwall")).unwrap();
    fs::copy(config, home.join(".config/swiftwall/config.toml")).unwrap();
    assert_eq!(
        folders(fixture.command().env("HOME", home).output().unwrap()),
        vec![missing]
    );
}

#[test]
fn successful_symlink_parent_resolution_uses_filesystem_not_lexical_parent() {
    let fixture = Fixture::new();
    let target = fixture.0.join("target");
    fs::create_dir_all(target.join("child")).unwrap();
    symlink(target.join("child"), fixture.0.join("alias")).unwrap();
    assert_eq!(
        folders(fixture.check(&[fixture.0.join("alias/.."), target.clone()])),
        vec![target]
    );
}

#[test]
fn unresolved_parent_dangling_links_and_loops_are_errors_not_remapped_roots() {
    let fixture = Fixture::new();
    symlink("absent", fixture.0.join("dangling")).unwrap();
    symlink("loop-b", fixture.0.join("loop-a")).unwrap();
    symlink("loop-a", fixture.0.join("loop-b")).unwrap();
    for relative in [
        "missing/..",
        "missing/../other",
        "dangling",
        "dangling/child",
        "dangling/..",
        "loop-a",
        "loop-a/..",
    ] {
        let output = fixture.check(&[fixture.0.join(Path::new(relative))]);
        assert!(!output.status.success(), "accepted {relative}");
        assert!(output.stdout.is_empty());
        assert!(
            String::from_utf8_lossy(&output.stderr).contains("Cannot safely resolve folder"),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
}
