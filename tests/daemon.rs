#![cfg(target_os = "linux")]

use serde_json::Value;
use std::fs;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Output, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::thread;
use std::time::{Duration, Instant};

const WAIT: Duration = Duration::from_secs(8);
const POLL: Duration = Duration::from_millis(25);

struct TempDir(PathBuf);

impl TempDir {
    fn new() -> Self {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        loop {
            let path = std::env::temp_dir().join(format!(
                "sw-it-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            match fs::create_dir(&path) {
                Ok(()) => return Self(fs::canonicalize(path).unwrap()),
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(e) => panic!("create isolated directory: {e}"),
            }
        }
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

// Every spawned process is reaped, including when an assertion unwinds.
struct Process(Option<Child>);

impl Process {
    fn wait(&mut self, timeout: Duration) -> Option<ExitStatus> {
        let end = Instant::now() + timeout;
        loop {
            if let Some(status) = self.0.as_mut().unwrap().try_wait().unwrap() {
                return Some(status);
            }
            if Instant::now() >= end {
                return None;
            }
            thread::sleep(POLL);
        }
    }

    fn output(mut self) -> Output {
        assert!(self.wait(WAIT).is_some(), "CLI process timed out");
        self.0.take().unwrap().wait_with_output().unwrap()
    }

    fn terminate(&mut self) -> Option<ExitStatus> {
        let child = self.0.as_mut()?;
        if let Ok(Some(status)) = child.try_wait() {
            self.0.take();
            return Some(status);
        }
        // SIGTERM exercises the daemon's normal signalfd shutdown and state save.
        unsafe { libc::kill(child.id() as libc::pid_t, libc::SIGTERM) };
        let status = self.wait(Duration::from_secs(12));
        if status.is_some() {
            self.0.take();
        }
        status
    }
}

impl Drop for Process {
    fn drop(&mut self) {
        if let Some(mut child) = self.0.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

struct Fixture {
    daemon: Option<Process>,
    dir: TempDir,
    root: PathBuf,
    background: PathBuf,
    initial: PathBuf,
    script: PathBuf,
}

impl Fixture {
    fn new(images: usize, root_exists: bool) -> Self {
        let dir = TempDir::new();
        for name in [
            "home",
            "state",
            "runtime",
            "xdg-runtime",
            "desktop",
            "collections",
        ] {
            fs::create_dir(dir.0.join(name)).unwrap();
        }
        let root = dir.0.join("collections/wallpapers");
        if root_exists {
            fs::create_dir(&root).unwrap();
        }
        let initial = dir.0.join("desktop/manual.png");
        png(&initial);
        let background = dir.0.join("desktop/background");
        symlink(&initial, &background).unwrap();
        let script = dir.0.join("fake-omarchy");
        let fixture = Self {
            daemon: None,
            dir,
            root,
            background,
            initial,
            script,
        };
        for index in 0..images {
            // Content detection must work for both supported filename extensions.
            let extension = if index % 2 == 0 { "png" } else { "jpg" };
            png(&fixture.root.join(format!("image-{index}.{extension}")));
        }
        fixture.config(300, "shuffle", std::slice::from_ref(&fixture.root));
        fixture.setter("ln -sfn -- \"$4\" \"$SWIFTWALL_BACKGROUND_LINK\"");
        fixture
    }

    fn config(&self, interval: u64, selection: &str, folders: &[PathBuf]) {
        fs::write(
            self.dir.0.join("config.toml"),
            format!(
                "interval_seconds = {interval}\nselection = {}\nfolders = {}\n",
                serde_json::to_string(selection).unwrap(),
                serde_json::to_string(folders).unwrap()
            ),
        )
        .unwrap();
    }

    fn command(&self, verb: &str) -> Command {
        let mut command = Command::new(env!("CARGO_BIN_EXE_swiftwall"));
        command
            .arg(verb)
            .env_clear()
            .env("PATH", "/usr/bin:/bin")
            .env("HOME", self.dir.0.join("home"))
            .env("XDG_CONFIG_HOME", self.dir.0.join("home/config"))
            .env("XDG_STATE_HOME", self.dir.0.join("home/state"))
            .env("XDG_CACHE_HOME", self.dir.0.join("home/cache"))
            .env("XDG_RUNTIME_DIR", self.dir.0.join("xdg-runtime"))
            .env("SWIFTWALL_CONFIG", self.dir.0.join("config.toml"))
            .env("SWIFTWALL_STATE_DIR", self.dir.0.join("state"))
            .env("SWIFTWALL_RUNTIME_DIR", self.dir.0.join("runtime"))
            .env("SWIFTWALL_BACKGROUND_LINK", &self.background)
            .env("SWIFTWALL_OMARCHY_COMMAND", &self.script)
            .stdin(Stdio::null());
        command
    }

    fn setter(&self, body: &str) {
        // The fake never invokes Omarchy or any desktop command and writes only
        // the explicitly inherited, absolute, isolated background symlink.
        fs::write(
            &self.script,
            format!(
                "#!/bin/sh\nset -eu\n[ \"$#\" -eq 4 ]\n[ \"$1\" = theme ]\n[ \"$2\" = bg ]\n[ \"$3\" = set ]\ncase \"$SWIFTWALL_BACKGROUND_LINK\" in /*) ;; *) exit 90 ;; esac\n{body}\n"
            ),
        )
        .unwrap();
        fs::set_permissions(&self.script, fs::Permissions::from_mode(0o700)).unwrap();
    }

    fn start(&mut self, eligible: usize) -> Value {
        assert!(self.daemon.is_none());
        let log = fs::File::create(self.dir.0.join("daemon.log")).unwrap();
        self.daemon = Some(Process(Some(
            self.command("daemon")
                .stdout(Stdio::null())
                .stderr(log)
                .spawn()
                .unwrap(),
        )));
        self.wait_status("startup scan", |s| {
            s["scanning"] == false && s["eligible_images"] == eligible
        })
    }

    fn stop(&mut self) {
        self.persisted();
        let mut daemon = self.daemon.take().unwrap();
        let status = daemon.terminate();
        assert!(
            status.is_some_and(|s| s.success()),
            "daemon did not shut down successfully: {status:?}; {}",
            self.log()
        );
        assert!(!self.dir.0.join("runtime/control.sock").exists());
    }

    fn log(&self) -> String {
        fs::read_to_string(self.dir.0.join("daemon.log")).unwrap_or_default()
    }

    fn response(&self, verb: &str) -> (bool, Value) {
        let output = Process(Some(
            self.command(verb)
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .spawn()
                .unwrap(),
        ))
        .output();
        let value = serde_json::from_slice(&output.stdout).unwrap_or_else(|e| {
            panic!(
                "{verb}: invalid JSON: {e}; stdout={}; stderr={}; daemon={}",
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr),
                self.log()
            )
        });
        (output.status.success(), value)
    }

    fn ok(&self, verb: &str) -> Value {
        let (success, response) = self.response(verb);
        assert!(success && response["ok"] == true, "{verb}: {response}");
        response
    }

    fn wait_status(&self, description: &str, predicate: impl Fn(&Value) -> bool) -> Value {
        let end = Instant::now() + WAIT;
        let mut last = Value::Null;
        loop {
            // The socket may not exist until daemon initialization completes.
            if self.dir.0.join("runtime/control.sock").exists() {
                let response = self.ok("status");
                last = response["status"].clone();
                if predicate(&last) {
                    return last;
                }
            }
            assert!(
                Instant::now() < end,
                "timed out waiting for {description}; last status={last}; daemon={}",
                self.log()
            );
            thread::sleep(POLL);
        }
    }

    fn apply(&self, verb: &str) -> Value {
        let response = self.ok(verb);
        assert!(response["message"].as_str().unwrap().contains("accepted"));
        self.wait_status("asynchronous apply and follow-up scan", |s| {
            s["applying"] == false && s["scanning"] == false
        })
    }

    fn manual(&self, path: &Path) {
        let staging = self.dir.0.join("desktop/manual-link.tmp");
        symlink(path, &staging).unwrap();
        fs::rename(staging, &self.background).unwrap();
    }

    fn persisted(&self) -> Value {
        self.wait_status("successful asynchronous persistence", |s| {
            s["persistence_pending"] == false && s["last_persistence_error"].is_null()
        })
    }

    fn stays(&self, duration: Duration, predicate: impl Fn(&Value) -> bool) {
        let end = Instant::now() + duration;
        loop {
            let status = self.ok("status")["status"].clone();
            assert!(
                predicate(&status),
                "unexpected status during quiet period: {status}"
            );
            if Instant::now() >= end {
                break;
            }
            thread::sleep(POLL);
        }
    }

    fn state(&self) -> Value {
        if self.daemon.is_some() {
            self.persisted();
        }
        // Offline reads follow stop(), which checks the status barrier before
        // SIGTERM and requires successful shutdown (including its final flush).
        serde_json::from_slice(&fs::read(self.dir.0.join("state/state.json")).unwrap()).unwrap()
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        if let Some(mut daemon) = self.daemon.take() {
            let _ = daemon.terminate();
            // Process::drop escalates to SIGKILL if graceful shutdown timed out.
            drop(daemon);
        }
    }
}

fn png(path: &Path) {
    image::RgbImage::from_pixel(2, 2, image::Rgb([17, 43, 91]))
        .save_with_format(path, image::ImageFormat::Png)
        .unwrap();
}

fn scheduled(status: &Value) -> f64 {
    assert_eq!(status["paused"], false, "{status}");
    assert!(status["protective_suspension"].is_null(), "{status}");
    let remaining = status["next_change_in_seconds"].as_f64().unwrap();
    let interval = status["interval_seconds"].as_f64().unwrap();
    assert!(
        remaining > interval - 10.0_f64.min(interval / 2.0) && remaining <= interval,
        "{status}"
    );
    remaining
}

fn applied(status: &Value) -> PathBuf {
    assert_eq!(status["applying"], false, "{status}");
    assert!(status["requested_wallpaper"].is_null(), "{status}");
    assert_eq!(
        status["last_application_result"]["success"], true,
        "{status}"
    );
    PathBuf::from(status["observed_wallpaper"].as_str().unwrap())
}

#[test]
fn startup_adopts_desktop_without_applying_and_manual_changes_reset_timer() {
    let mut f = Fixture::new(3, true);
    // Even a fake setter that would fail must not be run on startup.
    f.setter("exit 71");
    let initial = f.start(3);
    assert_eq!(initial["observed_wallpaper"], f.initial.to_str().unwrap());
    assert!(initial["last_application_result"].is_null());
    assert!(initial["requested_wallpaper"].is_null());
    scheduled(&initial);
    thread::sleep(Duration::from_millis(350));
    let before = scheduled(&f.ok("status")["status"]);
    let manual = f.dir.0.join("desktop/other.png");
    png(&manual);
    f.manual(&manual);
    let status = f.wait_status("external wallpaper observation", |s| {
        s["observed_wallpaper"] == manual.to_str().unwrap()
    });
    assert!(scheduled(&status) > before, "timer was not reset: {status}");
    assert!(status["last_application_result"].is_null());
    assert_eq!(fs::read_link(&f.background).unwrap(), manual);
    f.stop();
}

#[test]
fn manual_changes_preserve_user_pause_and_resume_gets_full_interval() {
    let mut f = Fixture::new(2, true);
    f.start(2);
    f.ok("pause");
    let manual = f.dir.0.join("desktop/paused-manual.png");
    png(&manual);
    f.manual(&manual);
    let status = f.wait_status("manual change while paused", |s| {
        s["observed_wallpaper"] == manual.to_str().unwrap()
    });
    assert_eq!(status["paused"], true);
    assert_eq!(status["state"], "paused");
    assert_eq!(status["no_schedule_reason"], "user-paused");
    assert!(status["next_change_in_seconds"].is_null());
    assert!(status["protective_suspension"].is_null());
    scheduled(&f.ok("resume")["status"]);
    assert_eq!(fs::read_link(&f.background).unwrap(), manual);
    f.stop();
}

#[test]
fn own_updates_do_not_suspend_and_history_navigation_does_not_consume_bag() {
    let mut f = Fixture::new(4, true);
    f.start(4);
    let first_status = f.apply("next");
    let first = applied(&first_status);
    scheduled(&first_status);
    let second_status = f.apply("next");
    let second = applied(&second_status);
    assert_ne!(first, second);
    scheduled(&second_status);
    let bag = f.state()["carousel"]["bag"].clone();
    let previous = f.apply("previous");
    assert_eq!(applied(&previous), first);
    scheduled(&previous);
    let forward = f.apply("next");
    assert_eq!(applied(&forward), second);
    scheduled(&forward);
    // Let delayed inotify/debounce events run after the worker completes.
    thread::sleep(Duration::from_millis(300));
    scheduled(&f.ok("status")["status"]);
    assert_eq!(f.state()["carousel"]["bag"], bag);
    f.stop();
}

#[test]
fn concurrent_commands_remain_responsive_and_serialize_applications() {
    let mut f = Fixture::new(3, true);
    f.setter("ln -sfn -- \"$4\" \"$SWIFTWALL_BACKGROUND_LINK\"\nsleep 2");
    f.start(3);
    let accepted = f.ok("next");
    assert_eq!(accepted["status"]["applying"], true);
    let target = accepted["status"]["requested_wallpaper"].clone();
    thread::scope(|scope| {
        let handles: Vec<_> = (0..16)
            .map(|index| {
                let f = &f;
                scope.spawn(move || {
                    let verb = if index % 2 == 0 { "status" } else { "next" };
                    (verb, f.response(verb))
                })
            })
            .collect();
        for handle in handles {
            let (verb, (success, response)) = handle.join().unwrap();
            if verb == "status" {
                assert!(success && response["ok"] == true, "{response}");
                assert_eq!(response["status"]["applying"], true);
                assert_eq!(response["status"]["requested_wallpaper"], target);
            } else {
                assert!(!success && response["ok"] == false, "{response}");
                assert!(response["error"].as_str().unwrap().contains("in progress"));
            }
        }
    });
    let status = f.wait_status("serialized application", |s| {
        s["applying"] == false && s["scanning"] == false
    });
    assert_eq!(applied(&status).to_str().unwrap(), target.as_str().unwrap());
    scheduled(&status);
    assert_eq!(
        f.state()["carousel"]["history"].as_array().unwrap().len(),
        1
    );
    f.stop();
}

#[test]
fn missing_collection_roots_reappear_and_rearm_inotify_without_rescan_command() {
    let mut f = Fixture::new(0, false);
    let empty = f.start(0);
    assert_eq!(empty["no_schedule_reason"], "no eligible images");
    fs::create_dir(&f.root).unwrap();
    png(&f.root.join("first.png"));
    f.wait_status("missing root creation via inotify", |s| {
        s["scanning"] == false && s["eligible_images"] == 1
    });
    // A subsequent file event must be watched inside the newly created root.
    png(&f.root.join("second.jpg"));
    f.wait_status("watch rearmed on created root", |s| {
        s["scanning"] == false && s["eligible_images"] == 2
    });
    fs::remove_dir_all(&f.root).unwrap();
    f.wait_status("root deletion", |s| {
        s["scanning"] == false && s["eligible_images"] == 0
    });
    fs::create_dir(&f.root).unwrap();
    let recovered = f.root.join("recovered.png");
    png(&recovered);
    f.wait_status("root recreation", |s| {
        s["scanning"] == false && s["eligible_images"] == 1
    });
    assert_eq!(applied(&f.apply("next")), recovered);
    f.stop();
}

#[test]
fn invalid_cached_image_becomes_eligible_when_replaced_with_valid_pixels() {
    let mut f = Fixture::new(0, true);
    let repaired = f.root.join("repaired.jpg");
    fs::write(&repaired, b"not an image").unwrap();
    let status = f.start(0);
    assert!(
        status["warnings"]
            .as_array()
            .unwrap()
            .iter()
            .any(|w| { w.as_str().is_some_and(|w| w.contains("repaired.jpg")) })
    );
    // Force another scan with unchanged metadata to exercise the negative cache.
    f.ok("rescan");
    f.wait_status("cached rejection", |s| {
        s["scanning"] == false && s["eligible_images"] == 0
    });
    png(&repaired);
    f.wait_status("inotify revalidation after repair", |s| {
        s["scanning"] == false && s["eligible_images"] == 1
    });
    assert_eq!(applied(&f.apply("next")), repaired);
    f.stop();
}

#[test]
fn paused_restart_preserves_history_and_bag_and_resume_restarts_deadline() {
    let mut f = Fixture::new(4, true);
    f.start(4);
    let first = applied(&f.apply("next"));
    let second = applied(&f.apply("next"));
    assert_ne!(first, second);
    f.ok("pause");
    let carousel = f.state()["carousel"].clone();
    f.stop();
    assert_eq!(f.state()["paused"], true);
    assert_eq!(f.state()["carousel"], carousel);
    // Startup must adopt a desktop that changed while the daemon was stopped,
    // never restore either the historical wallpaper or a persisted request.
    let manual = f.dir.0.join("desktop/offline.png");
    png(&manual);
    f.manual(&manual);
    let status = f.start(4);
    assert_eq!(status["paused"], true);
    assert!(status["next_change_in_seconds"].is_null());
    assert_eq!(status["observed_wallpaper"], manual.to_str().unwrap());
    assert_eq!(fs::read_link(&f.background).unwrap(), manual);
    assert_eq!(f.state()["carousel"], carousel);
    // Manual navigation is permitted while user-paused and retains that pause.
    let previous = f.apply("previous");
    assert_eq!(applied(&previous), second);
    assert_eq!(previous["paused"], true);
    assert!(previous["next_change_in_seconds"].is_null());
    assert_eq!(f.state()["carousel"]["bag"], carousel["bag"]);
    scheduled(&f.ok("resume")["status"]);
    f.stop();
    thread::sleep(Duration::from_millis(350));
    let restarted = f.start(4);
    scheduled(&restarted);
    assert_eq!(fs::read_link(&f.background).unwrap(), second);
    f.stop();
}

#[test]
fn subprocess_failures_reconcile_observation_without_committing_history() {
    let mut f = Fixture::new(3, true);
    f.start(3);
    let carousel = f.state()["carousel"].clone();
    for body in [
        "echo deliberate-failure >&2\nexit 17",
        "exit 0",
        "ln -sfn -- \"$4\" \"$SWIFTWALL_BACKGROUND_LINK\"\nexit 17",
    ] {
        f.setter(body);
        let status = f.apply("next");
        assert_eq!(
            status["last_application_result"]["success"], false,
            "{status}"
        );
        assert_eq!(status["state"], "error", "{status}");
        assert!(status["error"].as_str().is_some_and(|s| !s.is_empty()));
        assert!(status["protective_suspension"].is_null(), "{status}");
        assert_eq!(
            status["observed_wallpaper"],
            fs::read_link(&f.background).unwrap().to_str().unwrap()
        );
        assert_eq!(f.state()["carousel"], carousel);
        // Restore the baseline externally before testing the next failure mode.
        f.manual(&f.initial);
        f.wait_status("baseline restored", |s| {
            s["observed_wallpaper"] == f.initial.to_str().unwrap()
        });
    }
    fs::remove_file(&f.script).unwrap();
    let spawn_failure = f.apply("next");
    assert_eq!(spawn_failure["last_application_result"]["success"], false);
    assert!(
        spawn_failure["error"]
            .as_str()
            .unwrap()
            .contains("Cannot start")
    );
    assert_eq!(f.state()["carousel"], carousel);
    f.setter("ln -sfn -- \"$4\" \"$SWIFTWALL_BACKGROUND_LINK\"");
    let recovered = f.apply("next");
    applied(&recovered);
    scheduled(&recovered);
    assert!(recovered["error"].is_null());
    f.stop();
}

#[test]
fn external_change_during_sleeping_setter_protectively_suspends_until_resume() {
    let mut f = Fixture::new(3, true);
    // Observing the requested link while applying proves the fake has reached
    // its sleep, avoiding a race with the worker's preflight validation.
    f.setter("ln -sfn -- \"$4\" \"$SWIFTWALL_BACKGROUND_LINK\"\nsleep 2");
    f.start(3);
    let carousel = f.state()["carousel"].clone();
    let accepted = f.ok("next");
    let target = accepted["status"]["requested_wallpaper"].clone();
    f.wait_status("setter reached sleep", |s| {
        s["applying"] == true && s["observed_wallpaper"] == target
    });
    let external = f.dir.0.join("desktop/racing.png");
    png(&external);
    f.manual(&external);
    let suspended = f.wait_status("external conflict detected during apply", |s| {
        s["applying"] == true && s["protective_suspension"].is_string()
    });
    assert_eq!(suspended["observed_wallpaper"], external.to_str().unwrap());
    let status = f.wait_status("raced setter completion", |s| {
        s["applying"] == false && s["scanning"] == false
    });
    assert_eq!(status["state"], "protective-suspension");
    assert_eq!(status["paused"], false);
    assert!(status["next_change_in_seconds"].is_null());
    assert_eq!(status["last_application_result"]["success"], false);
    assert_eq!(fs::read_link(&f.background).unwrap(), external);
    assert_eq!(f.state()["carousel"], carousel);
    let (success, rejected) = f.response("next");
    assert!(!success && rejected["ok"] == false, "{rejected}");
    assert!(rejected["error"].as_str().unwrap().contains("suspended"));
    scheduled(&f.ok("resume")["status"]);
    assert_eq!(fs::read_link(&f.background).unwrap(), external);
    f.stop();
}

#[test]
fn automatic_deadline_is_reset_by_manual_change_then_applies_exactly_once() {
    let mut f = Fixture::new(3, true);
    f.config(3, "shuffle", std::slice::from_ref(&f.root));
    scheduled(&f.start(3));
    let before = f.wait_status("partially elapsed real deadline", |s| {
        s["next_change_in_seconds"]
            .as_f64()
            .is_some_and(|remaining| remaining < 1.8)
    });
    assert!(before["last_application_result"].is_null(), "{before}");
    let manual = f.dir.0.join("desktop/before-deadline.png");
    png(&manual);
    f.manual(&manual);
    let reset = f.wait_status("manual change resetting real deadline", |s| {
        s["observed_wallpaper"] == manual.to_str().unwrap()
    });
    assert!(scheduled(&reset) > 2.3, "{reset}");
    // Cross the original deadline, but remain safely before the reset deadline.
    // Checking throughout also catches transient early replacement/reversion.
    f.stays(Duration::from_secs(2), |s| {
        s["observed_wallpaper"] == manual.to_str().unwrap()
            && s["applying"] == false
            && s["last_application_result"].is_null()
            && s["protective_suspension"].is_null()
            && fs::read_link(&f.background).unwrap() == manual
    });
    let automatic = f.wait_status("automatic application after reset deadline", |s| {
        s["last_application_result"]["success"] == true
            && s["applying"] == false
            && s["scanning"] == false
    });
    let selected = applied(&automatic);
    assert!(selected.starts_with(&f.root));
    scheduled(&automatic);
    // Stop the next deadline so the persisted history can be inspected without
    // racing another automatic application on a heavily loaded test machine.
    f.ok("pause");
    let state = f.state();
    assert_eq!(state["carousel"]["history"], serde_json::json!([selected]));
    assert_eq!(state["carousel"]["bag"].as_array().unwrap().len(), 2);
    assert!(state["protective_suspension"].is_null());
    f.stop();
}

#[test]
fn explicit_next_and_previous_work_while_paused_without_resuming() {
    let mut f = Fixture::new(3, true);
    f.start(3);
    f.ok("pause");
    let first = f.apply("next");
    let first_path = applied(&first);
    let second = f.apply("next");
    let second_path = applied(&second);
    assert_ne!(first_path, second_path);
    let bag = f.state()["carousel"]["bag"].clone();
    let previous = f.apply("previous");
    assert_eq!(applied(&previous), first_path);
    let forward = f.apply("next");
    assert_eq!(applied(&forward), second_path);
    for status in [&first, &second, &previous, &forward] {
        assert_eq!(status["paused"], true, "{status}");
        assert_eq!(status["state"], "paused", "{status}");
        assert_eq!(status["no_schedule_reason"], "user-paused");
        assert!(status["next_change_in_seconds"].is_null(), "{status}");
        assert!(status["protective_suspension"].is_null(), "{status}");
    }
    assert_eq!(f.state()["carousel"]["bag"], bag);
    f.stop();
}

#[test]
fn reload_rejects_invalid_selection_and_never_applies_old_collection() {
    let mut f = Fixture::new(3, true);
    f.start(3);
    f.config(2, "not-a-selection", std::slice::from_ref(&f.root));
    let (success, invalid) = f.response("reload");
    assert!(!success && invalid["ok"] == false, "{invalid}");
    assert!(invalid["error"].as_str().unwrap().contains("selection"));
    let unchanged = f.ok("status")["status"].clone();
    assert_eq!(unchanged["interval_seconds"], 300);
    assert_eq!(unchanged["selection"], "shuffle");
    assert_eq!(unchanged["eligible_images"], 3);
    let original = applied(&f.apply("next"));
    assert!(original.starts_with(&f.root));

    let replacement_root = f.dir.0.join("collections/replacement");
    fs::create_dir(&replacement_root).unwrap();
    let replacement = replacement_root.join("new.png");
    png(&replacement);
    f.config(300, "shuffle", &[replacement_root]);
    let reloaded = f.ok("reload");
    assert_eq!(reloaded["status"]["scanning"], true);
    // The scan may finish before this separate CLI arrives. Either rejection
    // while unready or acceptance of the new collection is valid; an old target
    // is never valid, even though the old files remain present and decodable.
    let (success, immediate) = f.response("next");
    if success {
        assert_eq!(immediate["ok"], true);
        if !immediate["status"]["requested_wallpaper"].is_null() {
            assert_eq!(
                immediate["status"]["requested_wallpaper"],
                replacement.to_str().unwrap()
            );
        }
        let completed = f.wait_status("new collection application", |s| {
            s["applying"] == false && s["scanning"] == false
        });
        assert_eq!(applied(&completed), replacement);
    } else {
        assert_eq!(immediate["ok"], false);
        assert!(immediate["error"].as_str().unwrap().contains("scan"));
        f.wait_status("reloaded collection ready", |s| {
            s["scanning"] == false && s["eligible_images"] == 1
        });
        assert_eq!(fs::read_link(&f.background).unwrap(), original);
        assert_eq!(applied(&f.apply("next")), replacement);
    }
    assert_eq!(
        f.state()["carousel"]["paths"],
        serde_json::json!([replacement])
    );
    f.stop();
}

#[test]
fn missing_background_at_startup_suspends_without_running_setter() {
    let mut f = Fixture::new(2, true);
    f.config(2, "shuffle", std::slice::from_ref(&f.root));
    fs::remove_file(&f.background).unwrap();
    // If called, this normal fake would recreate the missing link, making an
    // accidental invocation observable without writing a separate marker file.
    let status = f.start(2);
    assert_eq!(status["state"], "protective-suspension");
    assert!(status["observed_wallpaper"].is_null());
    assert!(status["protective_suspension"].is_string());
    for verb in ["next", "previous"] {
        let (success, rejected) = f.response(verb);
        assert!(!success && rejected["ok"] == false, "{rejected}");
        assert!(rejected["error"].as_str().unwrap().contains("suspended"));
    }
    f.stays(Duration::from_millis(2300), |s| {
        s["state"] == "protective-suspension"
            && s["paused"] == false
            && s["next_change_in_seconds"].is_null()
            && s["last_application_result"].is_null()
            && s["requested_wallpaper"].is_null()
            && !f.background.symlink_metadata().is_ok()
    });
    f.manual(&f.initial);
    let restored = f.wait_status("missing background restored", |s| {
        s["observed_wallpaper"] == f.initial.to_str().unwrap()
    });
    assert_eq!(restored["state"], "protective-suspension");
    assert!(restored["next_change_in_seconds"].is_null());
    scheduled(&f.ok("resume")["status"]);
    f.stop();
}

#[test]
fn rapid_rescans_of_state_directory_converge_without_self_triggered_writes() {
    let mut f = Fixture::new(0, true);
    let state_dir = f.dir.0.join("state");
    png(&state_dir.join("wallpaper.png"));
    f.config(300, "shuffle", &[state_dir]);
    f.start(1);
    thread::scope(|scope| {
        let handles: Vec<_> = (0..24)
            .map(|_| {
                let f = &f;
                scope.spawn(move || f.ok("rescan"))
            })
            .collect();
        for handle in handles {
            handle.join().unwrap();
        }
    });
    f.wait_status("coalesced rescan and persistence convergence", |s| {
        s["scanning"] == false
            && s["eligible_images"] == 1
            && s["persistence_pending"] == false
            && s["last_persistence_error"].is_null()
    });
    // Allow a final persistence-triggered inotify debounce and scan to finish.
    // A feedback loop may look idle between scans; unchanged mtime catches it.
    thread::sleep(Duration::from_millis(450));
    let state = f.state();
    let path = f.dir.0.join("state/state.json");
    let modified = fs::metadata(&path).unwrap().modified().unwrap();
    f.stays(Duration::from_millis(800), |s| {
        s["scanning"] == false
            && s["applying"] == false
            && s["eligible_images"] == 1
            && s["persistence_pending"] == false
            && s["last_persistence_error"].is_null()
            && fs::metadata(&path).unwrap().modified().unwrap() == modified
    });
    assert_eq!(f.state(), state);
    f.stop();
}
