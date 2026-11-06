#![cfg(target_os = "linux")]

use serde_json::{Value, json};
use std::fs;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Output, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::thread;
use std::time::{Duration, Instant};

const WAIT: Duration = Duration::from_secs(3);
const TICK: Duration = Duration::from_millis(10);
const FRAME: usize = 64 * 1024;
const RESPONSE: usize = 1024 * 1024;

struct TempDir(PathBuf);
impl TempDir {
    fn new() -> Self {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        loop {
            let path = std::env::temp_dir().join(format!(
                "sw-v02-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            match fs::create_dir(&path) {
                Ok(()) => return Self(fs::canonicalize(path).unwrap()),
                Err(e) if e.kind() == io::ErrorKind::AlreadyExists => continue,
                Err(e) => panic!("create isolated fixture: {e}"),
            }
        }
    }
}
impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

// All children are bounded and reaped, including assertion-unwind paths.
struct Process(Option<Child>);
impl Process {
    fn wait(&mut self) -> Option<ExitStatus> {
        let end = Instant::now() + WAIT;
        loop {
            if let Some(status) = self.0.as_mut().unwrap().try_wait().unwrap() {
                return Some(status);
            }
            if Instant::now() >= end {
                return None;
            }
            thread::sleep(TICK);
        }
    }
    fn terminate(&mut self) -> Option<ExitStatus> {
        let child = self.0.as_mut()?;
        if let Some(status) = child.try_wait().unwrap() {
            return Some(status);
        }
        // The owned child has not been reaped, so its PID cannot have been reused.
        unsafe {
            libc::kill(child.id() as libc::pid_t, libc::SIGTERM);
        }
        self.wait()
    }
    fn output(mut self) -> Output {
        assert!(self.wait().is_some(), "CLI timed out");
        self.0.take().unwrap().wait_with_output().unwrap()
    }
}
impl Drop for Process {
    fn drop(&mut self) {
        let _ = self.terminate();
        if let Some(mut child) = self.0.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

struct Fixture {
    // Drop processes before removing their isolated filesystem.
    daemon: Option<Process>,
    dir: TempDir,
    root: PathBuf,
    background: PathBuf,
}
impl Fixture {
    fn new() -> Self {
        let dir = TempDir::new();
        for name in [
            "home",
            "config",
            "state",
            "runtime",
            "xdg-runtime",
            "desktop",
            "walls",
        ] {
            fs::create_dir(dir.0.join(name)).unwrap();
        }
        let root = dir.0.join("walls");
        for name in ["a.png", "b.png", "c.png"] {
            png(&root.join(name));
        }
        let initial = dir.0.join("desktop/manual.png");
        png(&initial);
        let background = dir.0.join("desktop/background");
        symlink(&initial, &background).unwrap();
        let fixture = Self {
            daemon: None,
            dir,
            root,
            background,
        };
        fixture.write_config(&fixture.settings(300, "shuffle", vec![fixture.root.clone()]));
        let script = fixture.path("fake-omarchy");
        // No service manager, desktop bus, real Omarchy, or real wallpaper command.
        // Both link and target are checked against the fixture root before mutation.
        fs::write(
            &script,
            r##"#!/bin/sh
set -eu
[ "$#" -eq 4 ]
[ "$1" = theme ] && [ "$2" = bg ] && [ "$3" = set ]
case "$SWIFTWALL_BACKGROUND_LINK" in "$TEST_ROOT"/desktop/background) ;; *) exit 90 ;; esac
case "$4" in "$TEST_ROOT"/*) ;; *) exit 91 ;; esac
printf '%s\n' "$4" >> "$TEST_ROOT/applied"
ln -sfn -- "$4" "$SWIFTWALL_BACKGROUND_LINK"
"##,
        )
        .unwrap();
        fs::set_permissions(script, fs::Permissions::from_mode(0o700)).unwrap();
        fixture
    }
    fn path(&self, name: &str) -> PathBuf {
        self.dir.0.join(name)
    }
    fn settings(&self, interval: u64, mode: &str, roots: Vec<PathBuf>) -> Value {
        json!({"interval_seconds":interval,"selection":mode,"folders":roots})
    }
    fn write_config(&self, settings: &Value) {
        fs::write(
            self.path("config/config.toml"),
            format!(
                "interval_seconds = {}\nselection = {}\nfolders = {}\n",
                settings["interval_seconds"], settings["selection"], settings["folders"]
            ),
        )
        .unwrap();
    }
    fn bytes(&self) -> Vec<u8> {
        fs::read(self.path("config/config.toml")).unwrap()
    }
    fn command(&self, args: &[&str]) -> Command {
        let mut command = Command::new(env!("CARGO_BIN_EXE_swiftwall"));
        command
            .args(args)
            .env_clear()
            .env("PATH", "/usr/bin:/bin")
            .env("HOME", self.path("home"))
            .env("XDG_CONFIG_HOME", self.path("home/config"))
            .env("XDG_STATE_HOME", self.path("home/state"))
            .env("XDG_CACHE_HOME", self.path("home/cache"))
            .env("XDG_DATA_HOME", self.path("home/data"))
            .env("XDG_RUNTIME_DIR", self.path("xdg-runtime"))
            .env("SWIFTWALL_RUNTIME_DIR", self.path("runtime"))
            .env("SWIFTWALL_CONFIG", self.path("config/config.toml"))
            .env("SWIFTWALL_STATE_DIR", self.path("state"))
            .env("SWIFTWALL_BACKGROUND_LINK", &self.background)
            .env("SWIFTWALL_OMARCHY_COMMAND", self.path("fake-omarchy"))
            .env("TEST_ROOT", &self.dir.0)
            .stdin(Stdio::null());
        command
    }
    fn cli(&self, args: &[&str]) -> Output {
        Process(Some(
            self.command(args)
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .spawn()
                .unwrap(),
        ))
        .output()
    }
    fn start(&mut self) {
        self.start_with_images(3);
    }
    fn start_with_images(&mut self, eligible: usize) {
        assert!(self.daemon.is_none());
        self.daemon = Some(Process(Some(
            self.command(&["daemon"])
                .stdout(Stdio::null())
                .stderr(fs::File::create(self.path("daemon.log")).unwrap())
                .spawn()
                .unwrap(),
        )));
        let end = Instant::now() + WAIT;
        while !self.path("runtime/control.sock").exists() {
            assert!(
                Instant::now() < end,
                "startup timeout: {}",
                fs::read_to_string(self.path("daemon.log")).unwrap()
            );
            thread::sleep(TICK);
        }
        self.wait_status(|s| s["scanning"] == false && s["eligible_images"] == eligible);
    }
    fn stop(&mut self) {
        let mut process = self.daemon.take().unwrap();
        assert!(
            process.terminate().is_some_and(|s| s.success()),
            "shutdown failed: {}",
            fs::read_to_string(self.path("daemon.log")).unwrap()
        );
        assert!(!self.path("runtime/control.sock").exists());
    }
    fn socket(&self) -> UnixStream {
        let stream = UnixStream::connect(self.path("runtime/control.sock")).unwrap();
        stream.set_read_timeout(Some(WAIT)).unwrap();
        stream.set_write_timeout(Some(WAIT)).unwrap();
        stream
    }
    fn request(&self, request: Value) -> Value {
        let mut socket = self.socket();
        writeln!(socket, "{request}").unwrap();
        receive(&mut BufReader::new(socket))
    }
    fn op(&self, op: &str) -> Value {
        self.request(json!({"v":1,"id":"test","op":op}))
    }
    fn ok(&self, op: &str) -> Value {
        let v = self.op(op);
        assert_ok(&v);
        v
    }
    fn get(&self) -> Value {
        self.ok("getsettings")
    }
    fn update(&self, revision: &Value, settings: Value) -> Value {
        self.request(json!({"v":1,"id":"write","op":"updatesettings",
            "if_revision":revision,"settings":settings}))
    }
    fn wait_status(&self, predicate: impl Fn(&Value) -> bool) -> Value {
        let end = Instant::now() + WAIT;
        loop {
            let s = self.ok("status")["status"].clone();
            if predicate(&s) {
                return s;
            }
            assert!(Instant::now() < end, "status timeout: {s}");
            thread::sleep(TICK);
        }
    }
    fn stable(&self) -> Value {
        self.wait_status(|s| {
            s["scanning"] == false
                && s["applying"] == false
                && s["persistence_pending"] == false
                && s["config_pending"] == false
        })
    }
    fn apply(&self, op: &str, expected: &Path) {
        self.ok(op);
        self.wait_status(|s| {
            s["applying"] == false
                && s["scanning"] == false
                && s["observed_wallpaper"] == json!(expected)
        });
    }
    fn state(&self) -> Value {
        self.stable();
        serde_json::from_slice(&fs::read(self.path("state/state.json")).unwrap()).unwrap()
    }
    fn subscribe(&self) -> BufReader<UnixStream> {
        let mut socket = self.socket();
        writeln!(socket, "{}", json!({"v":1,"op":"subscribe"})).unwrap();
        BufReader::new(socket)
    }
    fn manual(&self, target: &Path) {
        let staging = self.path("desktop/staging");
        symlink(target, &staging).unwrap();
        fs::rename(staging, &self.background).unwrap();
    }
}

fn png(path: &Path) {
    image::RgbImage::from_pixel(1, 1, image::Rgb([19, 47, 83]))
        .save_with_format(path, image::ImageFormat::Png)
        .unwrap();
}
fn assert_ok(value: &Value) {
    assert_eq!(value["ok"], true, "{value}");
}
fn receive(reader: &mut BufReader<UnixStream>) -> Value {
    let mut bytes = Vec::new();
    reader
        .take(RESPONSE as u64 + 1)
        .read_until(b'\n', &mut bytes)
        .unwrap();
    assert!(
        bytes.len() <= RESPONSE && bytes.last() == Some(&b'\n'),
        "incomplete/oversize response"
    );
    serde_json::from_slice(&bytes).unwrap()
}
fn quiet(reader: &BufReader<UnixStream>, millis: i32) {
    assert!(reader.buffer().is_empty(), "unexpected buffered event");
    let mut fd = libc::pollfd {
        fd: reader.get_ref().as_raw_fd(),
        events: libc::POLLIN,
        revents: 0,
    };
    let result = unsafe { libc::poll(&mut fd, 1, millis) };
    assert_eq!(
        result, 0,
        "unexpected heartbeat/event/disconnect: {}",
        fd.revents
    );
}
fn settle(reader: &mut BufReader<UnixStream>) -> Value {
    let end = Instant::now() + WAIT;
    let mut last = receive(reader);
    loop {
        let mut fd = libc::pollfd {
            fd: reader.get_ref().as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        };
        if reader.buffer().is_empty() && unsafe { libc::poll(&mut fd, 1, 100) } == 0 {
            return last;
        }
        assert!(Instant::now() < end, "subscription did not settle: {last}");
        let next = receive(reader);
        assert_eq!(next["event"], "snapshot");
        assert_eq!(next["instance"], last["instance"]);
        assert!(next["seq"].as_u64().unwrap() > last["seq"].as_u64().unwrap());
        last = next;
    }
}

#[test]
fn handshake_cli_and_noop_preserve_revision_disk_and_timer() {
    let mut f = Fixture::new();
    f.start();
    f.stable();
    let hello = f.ok("hello");
    assert_eq!(hello["v"], 1);
    assert_eq!(hello["protocol_version"], 1);
    assert_eq!(hello["id"], "test");
    assert!(
        hello["daemon_version"]
            .as_str()
            .is_some_and(|s| !s.is_empty())
    );
    for capability in ["settings", "subscribe", "sequential"] {
        assert!(
            hello["capabilities"]
                .as_array()
                .unwrap()
                .contains(&json!(capability))
        );
    }
    let before = f.stable()["next_change_in_seconds"].as_f64().unwrap();
    let bytes = f.bytes();
    thread::sleep(Duration::from_millis(160));
    for op in ["hello", "getsettings", "reload"] {
        assert_eq!(f.ok(op)["revision"], hello["revision"]);
    }
    let noop = f.update(&hello["revision"], hello["settings"].clone());
    assert_ok(&noop);
    assert_eq!(noop["revision"], hello["revision"]);
    assert_eq!(f.bytes(), bytes);
    let after = f.stable()["next_change_in_seconds"].as_f64().unwrap();
    assert!(
        after < before - 0.10,
        "no-op reset timer: {before} -> {after}"
    );
    let output = f.cli(&["settings"]);
    assert!(output.status.success());
    assert_eq!(
        serde_json::from_slice::<Value>(&output.stdout).unwrap()["settings"],
        hello["settings"]
    );
    let output = f.cli(&["request", r#"{"v":1,"id":"cli","op":"hello"}"#]);
    assert!(output.status.success());
    assert_eq!(output.stdout.iter().filter(|b| **b == b'\n').count(), 1);
    assert_eq!(
        serde_json::from_slice::<Value>(&output.stdout).unwrap()["id"],
        "cli"
    );
    assert!(!f.path("applied").exists());
}

#[test]
fn stale_revision_and_external_edits_are_transactional() {
    let mut f = Fixture::new();
    f.start();
    let initial = f.get();
    let mut changed = initial["settings"].clone();
    changed["interval_seconds"] = json!(123);
    let committed = f.update(&initial["revision"], changed.clone());
    assert_ok(&committed);
    assert_ne!(initial["revision"], committed["revision"]);
    let bytes = f.bytes();
    assert_eq!(
        f.update(&initial["revision"], initial["settings"].clone())["code"],
        "revision_conflict"
    );
    assert_eq!(f.bytes(), bytes);
    let mut external = changed.clone();
    external["interval_seconds"] = json!(124);
    f.write_config(&external);
    let edited = f.bytes();
    changed["selection"] = json!("sequential");
    assert_eq!(
        f.update(&committed["revision"], changed)["code"],
        "config_conflict"
    );
    assert_eq!(f.bytes(), edited);
    assert_eq!(f.get()["settings"], committed["settings"]);
    let reloaded = f.ok("reload");
    assert_eq!(reloaded["settings"], external);
    assert_ne!(reloaded["revision"], committed["revision"]);
    fs::write(f.path("config/config.toml"), "selection = [broken").unwrap();
    let broken = f.bytes();
    assert_eq!(f.op("reload")["ok"], false);
    assert_eq!(f.bytes(), broken);
    assert_eq!(f.get()["revision"], reloaded["revision"]);
}

#[test]
fn invalid_settings_never_change_runtime_revision_or_disk_bytes() {
    let mut f = Fixture::new();
    f.start();
    let current = f.get();
    let bytes = f.bytes();
    let base = current["settings"].clone();
    let mut cases = Vec::new();
    for (key, value) in [
        ("interval_seconds", json!(0)),
        ("interval_seconds", json!(31_536_001)),
        ("interval_seconds", json!(-1)),
        ("interval_seconds", json!(1.5)),
        ("selection", json!("random")),
        ("folders", json!([])),
        ("folders", json!([""])),
        ("folders", json!(["bad\u{0000}path"])),
        ("folders", json!(["x".repeat(4097)])),
        ("folders", json!(vec![f.root.clone(); 65])),
        ("folders", json!([f.path("missing-new-root")])),
        ("folders", json!([f.root.join("a.png")])),
        ("folders", json!(["file://remote.invalid/walls"])),
        ("folders", json!(["file:///tmp/wall#fragment"])),
        ("folders", json!(["file:///tmp/%zz"])),
        ("unexpected", json!(true)),
    ] {
        let mut settings = base.clone();
        settings[key] = value;
        cases.push(settings);
    }
    let mut missing = base.clone();
    missing.as_object_mut().unwrap().remove("selection");
    cases.push(missing);
    for settings in cases {
        let failure = f.update(&current["revision"], settings.clone());
        assert_eq!(failure["ok"], false, "accepted {settings}: {failure}");
        assert!(
            matches!(
                failure["code"].as_str(),
                Some("validation_error" | "config_update_failed")
            ),
            "{failure}"
        );
        assert_eq!(f.bytes(), bytes, "changed disk for {settings}");
        let after = f.get();
        assert_eq!(after["revision"], current["revision"]);
        assert_eq!(after["settings"], base);
    }
    for root in [f.path("missing-new-root"), f.root.join("a.png")] {
        let invalid = f.request(json!({"v":1,"op":"validatefolders","folders":[root]}));
        assert_eq!(
            invalid["ok"], false,
            "invalid preview root accepted: {invalid}"
        );
        assert_eq!(invalid["code"], "validation_error");
        assert_eq!(f.bytes(), bytes);
        assert_eq!(f.get()["revision"], current["revision"]);
    }
    assert!(!f.path("applied").exists());
}

fn file_url(path: &Path, localhost: bool) -> String {
    let mut url = if localhost {
        "file://localhost"
    } else {
        "file://"
    }
    .to_owned();
    for byte in path.to_str().unwrap().bytes() {
        if byte.is_ascii_alphanumeric() || b"/-_.~".contains(&byte) {
            url.push(byte as char);
        } else {
            url.push_str(&format!("%{byte:02X}"));
        }
    }
    url
}

#[test]
fn folder_urls_normalize_deduplicate_validate_and_allow_existing_missing_roots() {
    let mut f = Fixture::new();
    f.start();
    let special = f.path("spaces 雪 #% walls");
    fs::create_dir(&special).unwrap();
    png(&special.join("one.png"));
    let alias = f.path("alias");
    symlink(&special, &alias).unwrap();
    let initial = f.get();
    let bytes = f.bytes();
    let folders = json!([
        file_url(&special, false),
        file_url(&special, true),
        alias,
        special
    ]);
    let validation = f.request(json!({"v":1,"op":"validatefolders","folders":folders}));
    assert_ok(&validation);
    assert_eq!(validation["folders"].as_array().unwrap().len(), 1);
    assert_eq!(validation["folders"][0]["available"], true);
    assert_eq!(validation["folders"][0]["eligible_images"], 1);
    assert_eq!(f.bytes(), bytes);
    assert_eq!(f.get()["revision"], initial["revision"]);
    let updated = f.update(
        &initial["revision"],
        json!({"interval_seconds":200,
        "selection":"sequential","folders":folders}),
    );
    assert_ok(&updated);
    assert_eq!(updated["settings"]["folders"], json!([special]));
    f.wait_status(|s| s["scanning"] == false && s["eligible_images"] == 1);
    fs::remove_file(special.join("one.png")).unwrap();
    fs::remove_dir(&special).unwrap();
    let mut settings = updated["settings"].clone();
    settings["interval_seconds"] = json!(201);
    let retained = f.update(&updated["revision"], settings);
    assert_ok(&retained);
    f.wait_status(|s| s["scanning"] == false && s["eligible_images"] == 0);
    assert_eq!(retained["settings"]["folders"], json!([special]));
    let validation = f.request(json!({"v":1,"op":"validatefolders","folders":[special]}));
    assert_ok(&validation);
    assert_eq!(validation["folders"][0]["available"], false);
    let bytes = f.bytes();
    f.stop();
    f.start_with_images(0);
    assert_eq!(f.get()["settings"], retained["settings"]);
    assert_eq!(f.bytes(), bytes);
}

#[test]
fn sequential_history_mode_updates_pause_manual_changes_and_restart_are_durable() {
    let mut f = Fixture::new();
    f.start();
    f.ok("pause");
    let initial = f.get();
    let updated = f.update(
        &initial["revision"],
        f.settings(175, "sequential", vec![f.root.clone()]),
    );
    assert_ok(&updated);
    assert_eq!(updated["status"]["paused"], true);
    for name in ["a.png", "b.png", "c.png", "a.png"] {
        f.apply("next", &f.root.join(name));
    }
    f.apply("previous", &f.root.join("c.png"));
    let history = f.state()["carousel"].clone();
    let current = f.get();
    let switched = f.update(
        &current["revision"],
        f.settings(176, "shuffle", vec![f.root.clone()]),
    );
    assert_ok(&switched);
    assert_eq!(
        f.state()["carousel"],
        history,
        "mode update consumed bag/history"
    );
    // Forward successful history takes precedence in either mode.
    f.apply("next", &f.root.join("a.png"));
    let manual = f.path("desktop/manual.png");
    f.manual(&manual);
    f.wait_status(|s| s["observed_wallpaper"] == json!(manual) && s["paused"] == true);
    let manual_history = f.state()["carousel"].clone();
    let current = f.get();
    let changed = f.update(
        &current["revision"],
        f.settings(176, "sequential", vec![f.root.clone()]),
    );
    assert_ok(&changed);
    assert_eq!(changed["status"]["paused"], true);
    assert_eq!(changed["status"]["observed_wallpaper"], json!(manual));
    assert_eq!(f.state()["carousel"], manual_history);
    let before = f.state();
    let durable = f.get();
    let bytes = f.bytes();
    f.stop();
    f.start();
    assert_eq!(f.bytes(), bytes);
    let restarted = f.get();
    assert_eq!(restarted["settings"], durable["settings"]);
    assert_ne!(
        restarted["revision"], durable["revision"],
        "revision must be instance-scoped"
    );
    assert_eq!(f.state()["carousel"], before["carousel"]);
    let status = f.stable();
    assert_eq!(status["paused"], true);
    assert_eq!(status["observed_wallpaper"], json!(manual));
    assert!(status["next_change_in_seconds"].is_null());
    let resumed = f.ok("resume");
    assert!(
        resumed["status"]["next_change_in_seconds"]
            .as_f64()
            .unwrap()
            > 174.0
    );
}

#[test]
fn subscription_survives_idle_without_heartbeats_and_reports_monotonic_snapshots() {
    let mut f = Fixture::new();
    f.start();
    f.stable();
    let mut a = f.subscribe();
    let first = settle(&mut a);
    assert_eq!(first["event"], "snapshot");
    assert_eq!(first["v"], 1);
    assert_eq!(first["settings"], f.get()["settings"]);
    assert!(first["instance"].as_str().is_some_and(|s| !s.is_empty()));
    // Exceeds the ordinary peer's two-second progress deadline. Countdown is not an event.
    quiet(&a, 2150);
    let mut b = f.subscribe();
    let second = settle(&mut b);
    assert_eq!(first["seq"], second["seq"]);
    assert_eq!(first["instance"], second["instance"]);
    f.ok("pause");
    f.stable();
    let paused_a = settle(&mut a);
    let paused_b = settle(&mut b);
    assert_eq!(paused_a["seq"], paused_b["seq"]);
    assert!(paused_a["seq"].as_u64().unwrap() > first["seq"].as_u64().unwrap());
    assert_eq!(paused_a["status"]["paused"], true);
    let current = f.get();
    let changed = f.update(
        &current["revision"],
        f.settings(111, "sequential", vec![f.root.clone()]),
    );
    assert_ok(&changed);
    f.stable();
    let event = settle(&mut a);
    assert_eq!(event["revision"], changed["revision"]);
    assert_eq!(event["settings"], changed["settings"]);
    assert!(event["seq"].as_u64().unwrap() > paused_a["seq"].as_u64().unwrap());
    assert_eq!(event["status"]["paused"], true);
    quiet(&a, 150);
}

#[test]
fn slow_subscribers_are_bounded_and_concurrent_duplicate_clients_remain_responsive() {
    let mut f = Fixture::new();
    f.start();
    f.stable();
    let mut subscribers = Vec::new();
    for _ in 0..8 {
        let mut reader = f.subscribe();
        assert_eq!(receive(&mut reader)["event"], "snapshot");
        subscribers.push(reader);
    }
    let mut ninth = f.subscribe();
    assert_eq!(receive(&mut ninth)["code"], "subscriber_limit");
    let socket_path = f.path("runtime/control.sock");
    thread::scope(|scope| {
        for _ in 0..8 {
            let path = &socket_path;
            scope.spawn(move || {
                for _ in 0..8 {
                    let mut socket = UnixStream::connect(path).unwrap();
                    socket.set_read_timeout(Some(WAIT)).unwrap();
                    socket.set_write_timeout(Some(WAIT)).unwrap();
                    writeln!(socket, "{}", json!({"v":1,"id":"duplicate","op":"pause"})).unwrap();
                    let response = receive(&mut BufReader::new(socket));
                    assert_ok(&response);
                    assert_eq!(response["id"], "duplicate");
                }
            });
        }
    });
    // Leave all eight readers idle during changing snapshots; commands must not block on them.
    for _ in 0..40 {
        f.ok("resume");
        f.ok("pause");
    }
    f.stable();
    for reader in &mut subscribers {
        let last = settle(reader);
        assert_eq!(last["status"]["paused"], true);
        assert_eq!(last["event"], "snapshot");
    }
    let duplicate = f.cli(&["daemon"]);
    assert!(!duplicate.status.success());
    assert!(!duplicate.stderr.is_empty());
    assert_ok(&f.op("status"));
    assert!(!f.path("applied").exists());
}

#[test]
fn concurrent_settings_writers_cannot_commit_the_same_revision_twice() {
    let mut f = Fixture::new();
    f.start();
    let current = f.get();
    let barrier = std::sync::Barrier::new(8);
    let responses = thread::scope(|scope| {
        let mut workers = Vec::new();
        for index in 0..8 {
            let fixture = &f;
            let revision = &current["revision"];
            let barrier = &barrier;
            workers.push(scope.spawn(move || {
                barrier.wait();
                fixture.update(
                    revision,
                    fixture.settings(120 + index, "sequential", vec![fixture.root.clone()]),
                )
            }));
        }
        workers
            .into_iter()
            .map(|w| w.join().unwrap())
            .collect::<Vec<_>>()
    });
    let winners: Vec<_> = responses.iter().filter(|v| v["ok"] == true).collect();
    assert_eq!(winners.len(), 1, "{responses:?}");
    for failure in responses.iter().filter(|v| v["ok"] != true) {
        assert!(
            matches!(failure["code"].as_str(), Some("busy" | "revision_conflict")),
            "{failure}"
        );
    }
    assert_eq!(f.get()["settings"], winners[0]["settings"]);
    assert_eq!(f.get()["revision"], winners[0]["revision"]);
    let disk: toml::Value = toml::from_str(std::str::from_utf8(&f.bytes()).unwrap()).unwrap();
    assert_eq!(
        disk["interval_seconds"].as_integer().unwrap() as u64,
        winners[0]["settings"]["interval_seconds"].as_u64().unwrap()
    );
    assert!(!f.path("applied").exists());
}

#[test]
fn strict_envelopes_fragmented_frames_and_exact_64k_wire_limit() {
    let mut f = Fixture::new();
    f.start();
    for (request, code) in [
        (
            json!({"v":2,"id":"bad","op":"hello"}),
            "unsupported_version",
        ),
        (
            json!({"v":1,"id":"bad","op":"hello","unknown":1}),
            "invalid_request",
        ),
        (json!({"v":1,"id":42,"op":"hello"}), "invalid_request"),
        (json!({"v":1,"op":"not-an-operation"}), "command_failed"),
    ] {
        let response = f.request(request.clone());
        assert_eq!(response["code"], code);
        assert_eq!(response["ok"], false);
        assert_eq!(response["v"], 1);
        if request["id"] == "bad" {
            assert_eq!(response["id"], "bad");
        }
    }
    let mut socket = f.socket();
    socket.write_all(b"{\"v\":1,\"id\":\"fragment\",").unwrap();
    thread::sleep(Duration::from_millis(15));
    socket.write_all(b"\"op\":\"hello\"}\n").unwrap();
    assert_eq!(receive(&mut BufReader::new(socket))["id"], "fragment");
    // Bound includes the newline: 65535 payload bytes + newline is valid.
    let mut frame = br#"{"v":1,"op":"hello"}"#.to_vec();
    frame.resize(FRAME - 1, b' ');
    frame.push(b'\n');
    let mut socket = f.socket();
    for fragment in frame.chunks(997) {
        socket.write_all(fragment).unwrap();
    }
    assert_ok(&receive(&mut BufReader::new(socket)));
    // The 65536th payload byte must fail even before a newline arrives.
    let mut oversized = br#"{"v":1,"op":"hello"}"#.to_vec();
    oversized.resize(FRAME, b' ');
    let mut socket = f.socket();
    socket.write_all(&oversized).unwrap();
    assert_eq!(
        receive(&mut BufReader::new(socket))["code"],
        "frame_too_large"
    );
    let mut socket = f.socket();
    socket.write_all(b"{broken}\n").unwrap();
    assert_eq!(
        receive(&mut BufReader::new(socket))["code"],
        "invalid_request"
    );
    assert_ok(&f.op("hello"));
}

#[test]
fn disconnected_settings_request_still_commits_and_survives_restart() {
    let mut f = Fixture::new();
    f.start();
    let current = f.get();
    let settings = f.settings(119, "sequential", vec![f.root.clone()]);
    let mut socket = f.socket();
    writeln!(
        socket,
        "{}",
        json!({"v":1,"op":"updatesettings",
        "if_revision":current["revision"],"settings":settings})
    )
    .unwrap();
    // Closing just the read half deterministically leaves a complete request for dispatch.
    socket.shutdown(std::net::Shutdown::Read).unwrap();
    drop(socket);
    let end = Instant::now() + WAIT;
    loop {
        let after = f.get();
        if after["settings"] == settings {
            break;
        }
        assert!(Instant::now() < end, "disconnected update lost: {after}");
        thread::sleep(TICK);
    }
    let bytes = f.bytes();
    let disk: toml::Value = toml::from_str(std::str::from_utf8(&bytes).unwrap()).unwrap();
    assert_eq!(disk["interval_seconds"].as_integer(), Some(119));
    f.stop();
    f.start();
    assert_eq!(f.get()["settings"], settings);
    assert_eq!(f.bytes(), bytes);
    assert!(!f.path("applied").exists());
}

#[test]
fn missing_daemon_cli_fails_clearly_without_starting_anything() {
    let f = Fixture::new();
    let bytes = f.bytes();
    for args in [
        vec!["settings"],
        vec!["status"],
        vec!["request", r#"{"v":1,"op":"hello"}"#],
    ] {
        let output = f.cli(&args);
        assert!(!output.status.success());
        assert!(
            String::from_utf8_lossy(&output.stderr).contains("Daemon unavailable"),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert!(output.stdout.is_empty());
        assert!(!f.path("runtime/control.sock").exists());
        assert!(fs::read_dir(f.path("state")).unwrap().next().is_none());
        assert!(!f.path("applied").exists());
        assert_eq!(f.bytes(), bytes);
    }
}
