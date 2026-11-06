use crate::backend::{self, ApplyResult};
use crate::carousel::Carousel;
use crate::collection::{self, Fingerprint, ScanResult};
use crate::config::{self, Config, Locations};
use crate::protocol::{self, Request};
use crate::runtime::{self, BACKGROUND, COLLECTION, Instance, Signals, Watcher};
use crate::state::State;
use serde_json::{Value, json};
use std::collections::HashMap;
use std::fs;
use std::io::{self, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::mpsc::{self, Receiver, Sender, SyncSender};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const DEBOUNCE: Duration = Duration::from_millis(200);
const COMMAND_TIMEOUT: Duration = Duration::from_secs(10);
const SETTINGS_TIMEOUT: Duration = Duration::from_secs(10);
const SETTINGS_SHUTDOWN: Duration = Duration::from_secs(5);

pub fn observe(path: &Path) -> Result<Option<PathBuf>, String> {
    match fs::read_link(path) {
        Ok(target) => Ok(Some(if target.is_absolute() {
            target
        } else {
            path.parent().unwrap_or(Path::new("/")).join(target)
        })),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(format!("Cannot read background symlink: {e}")),
    }
}

struct Engine {
    config: Config,
    state: State,
    paths: Vec<PathBuf>,
    deadline: Option<Instant>,
    error: Option<String>,
    warnings: Vec<String>,
    scanning: bool,
    scan_dirty: bool,
    collection_ready: bool,
    watch_events: u8,
    generation: u64,
    flight: Option<Flight>,
    persister: Option<Persister>,
    persistence_error: Option<String>,
    config_pending: bool,
    config_durability: Option<String>,
    config_worker_error: Option<String>,
    watcher_prepared_for_scan: bool,
}
struct Flight {
    target: PathBuf,
    before: Option<PathBuf>,
    proposal: Carousel,
    external_conflict: bool,
    worker: JoinHandle<ApplyResult>,
}
impl Engine {
    fn new(config: Config, mut state: State, observed: Option<PathBuf>, now: Instant) -> Self {
        // Startup always adopts the desktop; a stale persisted request never restores it.
        state.observed = observed;
        state.requested = None;
        let mut this = Self {
            config,
            state,
            paths: vec![],
            deadline: None,
            error: None,
            warnings: vec![],
            scanning: false,
            scan_dirty: false,
            collection_ready: false,
            watch_events: 0,
            generation: 0,
            flight: None,
            persister: None,
            persistence_error: None,
            config_pending: false,
            config_durability: None,
            config_worker_error: None,
            watcher_prepared_for_scan: false,
        };
        if this.state.observed.is_none() {
            this.suspend(
                "Background link is unavailable; resume after the background state is restored"
                    .into(),
            );
        } else {
            this.reset(now);
        }
        this
    }
    fn reset(&mut self, now: Instant) {
        self.deadline = if self.state.paused || self.state.protective_suspension.is_some() {
            None
        } else {
            Some(now + Duration::from_secs(self.config.interval_seconds))
        };
    }
    fn suspend(&mut self, reason: String) {
        self.state.protective_suspension = Some(reason);
        self.deadline = None;
    }
    /// Returns true only for a newly observed external state (not our own request).
    fn reconcile(&mut self, observed: Option<PathBuf>, now: Instant) -> bool {
        if self.state.observed == observed {
            return false;
        }
        if let Some(flight) = &mut self.flight {
            if observed.as_ref() == Some(&flight.target) {
                self.state.observed = observed;
                return false;
            }
            // A vanished link may be the setter's brief replacement gap. The
            // caller debounces it before passing None into reconciliation.
            if observed != flight.before {
                flight.external_conflict = true;
            }
            self.state.observed = observed;
            self.suspend("External wallpaper change overlapped an application; resume explicitly after checking the desktop".into());
            return true;
        }
        self.state.observed = observed;
        if self.state.observed.is_none() {
            self.suspend(
                "Background link is unavailable; resume after the background state is restored"
                    .into(),
            );
        } else {
            // A manual change never clears a user pause or protective suspension.
            self.reset(now);
        }
        true
    }
    fn status(&self, now: Instant) -> Value {
        let reason = if self.state.paused {
            Some("user-paused".to_string())
        } else if let Some(reason) = &self.state.protective_suspension {
            Some(reason.clone())
        } else if self.flight.is_some() {
            Some("application in progress".into())
        } else if self.config_pending {
            Some("configuration transaction in progress".into())
        } else if !self.collection_ready || self.paths.is_empty() {
            Some(
                if self.scanning || self.scan_dirty {
                    "collection scan in progress"
                } else {
                    "no eligible images"
                }
                .into(),
            )
        } else {
            None
        };
        json!({
            "state": if self.state.protective_suspension.is_some() {"protective-suspension"} else if self.state.paused {"paused"} else if self.error.is_some() {"error"} else {"running"},
            "observed_wallpaper":self.state.observed.as_ref().map(|p|p.to_string_lossy()),
            "requested_wallpaper":self.state.requested.as_ref().map(|p|p.to_string_lossy()),
            "interval_seconds":self.config.interval_seconds,"selection":self.config.selection,
            "eligible_images":self.paths.len(),"scanning":self.scanning || self.scan_dirty,"applying":self.flight.is_some(),
            "next_change_in_seconds":if reason.is_none() {self.deadline.map(|d|d.saturating_duration_since(now).as_secs_f64())}else{None},
            "no_schedule_reason":reason,"last_application_result":self.state.last_result,
            "visually_verified":"unknown","error":self.error,"warnings":self.warnings,
            "paused":self.state.paused,"protective_suspension":self.state.protective_suspension,
            "persistence_pending":self.persister.as_ref().is_some_and(Persister::pending),
            "last_persistence_error":self.persistence_error,
            "config_pending":self.config_pending,"config_durability":self.config_durability,
            "config_worker_error":self.config_worker_error,
        })
    }
    fn save(&mut self, locations: &Locations) {
        let result = match &mut self.persister {
            Some(persister) => persister.submit(self.state.clone()),
            None => self.state.save(&locations.state),
        };
        if let Err(e) = result {
            self.persistence_failed(e);
        }
    }

    fn persistence_failed(&mut self, error: String) {
        self.suspend(format!("Cannot persist carousel state: {error}"));
        eprintln!("swiftwall: {error}");
        self.persistence_error = Some(error);
    }

    fn persisted(&mut self, result: Result<(), String>) {
        let Some(persister) = &mut self.persister else {
            return;
        };
        persister.active = false;
        let dirty = persister.dirty.take();
        let recovering = std::mem::take(&mut persister.recovering);
        let failed = result.is_err();
        match result {
            Ok(()) => self.persistence_error = None,
            Err(e) => self.persistence_failed(e),
        }
        if dirty.is_some() || (failed && !recovering) {
            // Preserve the latest state, including a newly imposed suspension,
            // without automatically retrying forever on an unwritable filesystem.
            let snapshot = if failed {
                self.state.clone()
            } else {
                dirty.unwrap()
            };
            if let Err(e) = self.persister.as_mut().unwrap().submit(snapshot) {
                self.persistence_failed(e);
            } else {
                // One best-effort write of the newly imposed suspension, even
                // without a dirty successor. Failure of that write cannot itself
                // generate another retry. Independent saves may still coalesce.
                self.persister.as_mut().unwrap().recovering = failed;
            }
        }
    }

    fn flush_persistence(&mut self, completions: &Receiver<Completion>) -> Result<(), String> {
        self.flush_persistence_with_timeout(completions, SETTINGS_SHUTDOWN)
    }

    fn flush_persistence_with_timeout(
        &mut self,
        completions: &Receiver<Completion>,
        timeout: Duration,
    ) -> Result<(), String> {
        let deadline = Instant::now() + timeout;
        while self.persister.as_ref().is_some_and(Persister::pending) {
            if Instant::now() >= deadline {
                let error = "State flush timed out; pending write outcome uncertain".to_string();
                self.persistence_failed(error.clone());
                return Err(error);
            }
            match completions.recv_timeout(Duration::from_millis(100)) {
                Ok(Completion::Persisted(result)) => self.persisted(result),
                Ok(_) => {}
                Err(mpsc::RecvTimeoutError::Timeout)
                    if !self.persister.as_ref().unwrap().worker.is_finished() => {}
                Err(_) => {
                    self.persistence_failed(
                        "Persistence worker stopped before flushing state".into(),
                    );
                    break;
                }
            }
        }
        if let Some(persister) = self.persister.take()
            && let Err(e) = persister.close()
        {
            self.persistence_failed(e);
        }
        match &self.persistence_error {
            Some(error) => Err(error.clone()),
            None => Ok(()),
        }
    }
}

struct Persister {
    tx: SyncSender<State>,
    worker: JoinHandle<()>,
    active: bool,
    dirty: Option<State>,
    recovering: bool,
}
impl Persister {
    fn new(path: PathBuf, tx: Sender<Completion>, wake: UnixStream) -> Result<Self, String> {
        Self::spawn(move |state| state.save(&path), tx, wake)
    }

    fn spawn(
        mut save: impl FnMut(&State) -> Result<(), String> + Send + 'static,
        tx: Sender<Completion>,
        mut wake: UnixStream,
    ) -> Result<Self, String> {
        let (requests, rx) = mpsc::sync_channel::<State>(1);
        let worker = thread::Builder::new()
            .name("swiftwall-persist".into())
            .spawn(move || {
                while let Ok(state) = rx.recv() {
                    let result = save(&state);
                    notify(&tx, &mut wake, Completion::Persisted(result));
                }
            })
            .map_err(|e| e.to_string())?;
        Ok(Self {
            tx: requests,
            worker,
            active: false,
            dirty: None,
            recovering: false,
        })
    }

    fn pending(&self) -> bool {
        self.active || self.dirty.is_some()
    }

    fn submit(&mut self, snapshot: State) -> Result<(), String> {
        if self.active {
            self.dirty = Some(snapshot);
        } else {
            self.tx
                .try_send(snapshot)
                .map_err(|e| format!("Queue state persistence: {e}"))?;
            self.active = true;
        }
        Ok(())
    }

    fn close(self) -> Result<(), String> {
        drop(self.tx);
        self.worker
            .join()
            .map_err(|_| "Persistence worker panicked".into())
    }
}

enum Completion {
    Scan(u64, ScanResult),
    Applied,
    Persisted(Result<(), String>),
    Configured(u64, Result<PreparedConfig, String>),
    Validated(u64, Value),
}
fn notify(tx: &Sender<Completion>, wake: &mut UnixStream, event: Completion) {
    if tx.send(event).is_ok() {
        let _ = wake.write(&[1]);
    }
}
fn scanner(rx: Receiver<(u64, Vec<PathBuf>)>, tx: Sender<Completion>, mut wake: UnixStream) {
    let mut cache: HashMap<PathBuf, (Fingerprint, Result<(), String>)> = HashMap::new();
    while let Ok((generation, roots)) = rx.recv() {
        let mut result = collection::scan(&roots);
        // Membership is canonical, while configured roots may be symlink aliases.
        // Retain invalid candidates too: rescanning unchanged files must not retry
        // their headers, even when a root's spelling differs from its target.
        let discovered: std::collections::HashSet<_> = result.paths.iter().cloned().collect();
        cache.retain(|path, _| discovered.contains(path));
        result.paths.retain(|path| {
            let fingerprint = match collection::fingerprint(path) {
                Ok(f) => f,
                Err(e) => {
                    result.warnings.push(e);
                    return false;
                }
            };
            let valid = match cache.get(path) {
                Some((old, valid)) if old == &fingerprint => valid.clone(),
                _ => {
                    let valid = collection::validate(path).map(|_| ());
                    cache.insert(path.clone(), (fingerprint, valid.clone()));
                    valid
                }
            };
            if let Err(e) = valid {
                result.warnings.push(e);
                false
            } else {
                true
            }
        });

        notify(&tx, &mut wake, Completion::Scan(generation, result));
    }
}
fn request_scan(engine: &mut Engine) {
    engine.generation += 1;
    engine.scan_dirty = true;
}

fn rearm_watcher(
    watcher: &mut Watcher,
    background: &Path,
    roots: &[PathBuf],
) -> Result<u8, String> {
    // Overlap the watches, then preserve old readiness before dropping the old fd.
    // A scan started after this handoff covers changes preceding the new watches.
    let next = Watcher::new(background, roots)?;
    let pending = watcher.drain()?;
    *watcher = next;
    Ok(pending)
}

fn finish_apply(engine: &mut Engine, locations: &Locations) {
    let Some(flight) = engine.flight.take() else {
        return;
    };
    let mut result = flight.worker.join().unwrap_or_else(|_| ApplyResult {
        success: false,
        message: "Wallpaper application worker panicked".into(),
    });
    match observe(&locations.background) {
        Ok(observed) => {
            let matched = observed.as_ref() == Some(&flight.target);
            let external = flight.external_conflict || (!matched && observed != flight.before);
            engine.state.observed = observed;
            if external {
                engine.suspend("External wallpaper change raced with SwiftWall; check the desktop and resume explicitly".into());
                result.success = false;
                result
                    .message
                    .push_str(" External-change race detected; automatic rotation suspended.");
            } else if !matched {
                result.success = false;
                result
                    .message
                    .push_str(" Requested path was not observed in Omarchy state.");
            }
            if result.success && matched && !external {
                let mut proposal = flight.proposal;
                proposal.commit(flight.target);
                proposal.reconcile(&engine.paths);
                engine.state.carousel = proposal;
                engine.error = None;
            } else {
                engine.error = Some(result.message.clone());
            }
            engine.reset(Instant::now());
        }
        Err(e) => {
            result.success = false;
            result.message.push_str(&format!(" {e}"));
            engine.error = Some(result.message.clone());
            engine.suspend(e);
        }
    }
    engine.state.last_result = Some(result);
    engine.state.requested = None;
    engine.save(locations);
}

fn apply(
    engine: &mut Engine,
    locations: &Locations,
    previous: bool,
    automatic: bool,
    tx: &Sender<Completion>,
    wake: &UnixStream,
) -> Result<(), String> {
    if engine.config_pending {
        return Err("A configuration transaction is in progress".into());
    }
    if engine.flight.is_some() {
        return Err("A wallpaper application is already in progress".into());
    }
    if engine.state.protective_suspension.is_some() {
        return Err("Protectively suspended; inspect status and resume explicitly".into());
    }
    if !engine.collection_ready {
        return Err("Wait for the initial or reloaded collection scan".into());
    }
    let current = observe(&locations.background)?;
    if engine.reconcile(current, Instant::now()) && automatic {
        engine.save(locations);
        return Ok(());
    }
    if engine.state.protective_suspension.is_some() {
        engine.save(locations);
        return Err("Background state unavailable".into());
    }
    let mut proposal = engine.state.carousel.clone();
    let target = if previous {
        proposal.previous(engine.state.observed.as_deref())
    } else if engine.config.selection == "sequential" {
        proposal.next_sequential(engine.state.observed.as_deref())
    } else {
        proposal.next(engine.state.observed.as_deref())
    }
    .ok_or("No eligible image or previous history")?;
    if Some(&target) == engine.state.observed.as_ref() {
        engine.reset(Instant::now());
        return Ok(());
    }
    let before = engine.state.observed.clone();
    let program = locations.program.clone();
    let link = locations.background.clone();
    let requested = target.clone();
    let baseline = before.clone();
    let tx = tx.clone();
    let mut wake = wake.try_clone().map_err(|e| e.to_string())?;
    let worker=thread::Builder::new().name("swiftwall-apply".into()).spawn(move|| {
        let result=match collection::validate(&requested) {
            Err(e)=>ApplyResult{success:false,message:e},
            Ok(_)=>match observe(&link) {
                Ok(current) if current==baseline=>backend::apply(&program,&requested,COMMAND_TIMEOUT),
                _=>ApplyResult{success:false,message:"Background changed or became unreadable during apply preflight; command not executed".into()},
            }
        };
        notify(&tx,&mut wake,Completion::Applied);
        result
    }).map_err(|e|e.to_string())?;
    engine.state.requested = Some(target.clone());
    engine.flight = Some(Flight {
        target,
        before,
        proposal,
        external_conflict: false,
        worker,
    });
    engine.deadline = None;
    Ok(())
}

const PEER_PROGRESS: Duration = Duration::from_secs(2);
struct Peer {
    id: u64,
    stream: UnixStream,
    input: Vec<u8>,
    output: Vec<u8>,
    latest: Option<Vec<u8>>,
    sent: usize,
    expires: Option<Instant>,
    dead: bool,
    subscriber: bool,
    snapshot_seq: Option<u64>,
    waiting: bool,
    request: Option<Request>,
}
impl Peer {
    fn new(stream: UnixStream, id: u64) -> io::Result<Self> {
        stream.set_nonblocking(true)?;
        Ok(Self {
            id,
            stream,
            input: vec![],
            output: vec![],
            latest: None,
            sent: 0,
            expires: Some(Instant::now() + PEER_PROGRESS),
            dead: false,
            subscriber: false,
            snapshot_seq: None,
            waiting: false,
            request: None,
        })
    }
    fn queue(&mut self, value: &Value) {
        let bytes = protocol::encode(value);
        if self.output.is_empty() || self.sent == 0 {
            self.output = bytes;
            self.sent = 0;
            // Replacing an unsent snapshot must not extend a slow reader's bound.
            if self.expires.is_none() {
                self.expires = Some(Instant::now() + PEER_PROGRESS);
            }
        } else {
            // Never replace a partially transmitted NDJSON frame.
            self.latest = Some(bytes);
        }
    }
    fn reply(&mut self, value: Value) {
        self.waiting = false;
        let value = match &self.request {
            Some(request) => protocol::metadata(value, request.id.as_deref()),
            None => value,
        };
        self.queue(&value);
    }
}

struct PreparedConfig {
    config: Config,
    watcher: Watcher,
    bytes: Vec<u8>,
    committed: bool,
    durability: Option<String>,
}
const PREPARING: u8 = 0;
const COMMITTING: u8 = 1;
const CANCELLED: u8 = 2;

#[derive(Clone)]
struct CommitGate(Arc<AtomicU8>);
impl CommitGate {
    fn new() -> Self {
        Self(Arc::new(AtomicU8::new(PREPARING)))
    }
    fn begin_commit(&self) -> io::Result<()> {
        self.0
            .compare_exchange(PREPARING, COMMITTING, Ordering::SeqCst, Ordering::SeqCst)
            .map(|_| ())
            .map_err(|_| io::Error::other("Configuration cancelled before replacement"))
    }
    fn cancel_before_commit(&self) -> bool {
        self.0
            .compare_exchange(PREPARING, CANCELLED, Ordering::SeqCst, Ordering::SeqCst)
            .is_ok()
            || self.0.load(Ordering::SeqCst) == CANCELLED
    }
    fn cancelled(&self) -> bool {
        self.0.load(Ordering::SeqCst) == CANCELLED
    }
}

struct ConfigJob {
    worker: JoinHandle<()>,
    peer_id: u64,
    gate: CommitGate,
    deadline: Option<Instant>,
    reported: bool,
}
struct ValidationJob {
    deadline: Option<Instant>,
    reported: bool,
}

struct Control {
    instance: String,
    revision: u64,
    bytes: Vec<u8>,
    seq: u64,
    fingerprint: Option<(Value, Option<Instant>)>,
    transaction: Option<ConfigJob>,
    validations: HashMap<u64, ValidationJob>,
}
impl Control {
    fn new(bytes: Vec<u8>) -> Self {
        Self {
            instance: format!("{:032x}", rand::random::<u128>()),
            revision: 0,
            bytes,
            seq: 0,
            fingerprint: None,
            transaction: None,
            validations: HashMap::new(),
        }
    }
    fn next_work_deadline(&self) -> Option<Instant> {
        self.transaction
            .as_ref()
            .and_then(|job| job.deadline)
            .into_iter()
            .chain(self.validations.values().filter_map(|job| job.deadline))
            .min()
    }
    fn revision(&self) -> String {
        format!("{}:{}", self.instance, self.revision)
    }
    fn settings(&self, engine: &Engine) -> Value {
        json!({"ok":true,"settings":engine.config,"revision":self.revision(),
            "config_durability":engine.config_durability})
    }
    fn snapshot(&self, engine: &Engine) -> Value {
        protocol::metadata(
            json!({"event":"snapshot","instance":self.instance,"seq":self.seq,
            "status":engine.status(Instant::now()),"settings":engine.config,
            "revision":self.revision()}),
            None,
        )
    }
    fn publish(&mut self, engine: &Engine, peers: &mut [Peer]) {
        let mut value = self.snapshot(engine);
        value["status"]
            .as_object_mut()
            .unwrap()
            .remove("next_change_in_seconds");
        value.as_object_mut().unwrap().remove("seq");
        let fingerprint = (value, engine.deadline);
        if self.fingerprint.as_ref() != Some(&fingerprint) {
            self.fingerprint = Some(fingerprint);
            self.seq += 1;
        }
        let snapshot = self.snapshot(engine);
        for peer in peers
            .iter_mut()
            .filter(|p| p.subscriber && !p.dead && p.snapshot_seq != Some(self.seq))
        {
            peer.queue(&snapshot);
            peer.snapshot_seq = Some(self.seq);
        }
    }
}

fn same_settings(a: &Config, b: &Config) -> bool {
    a.interval_seconds == b.interval_seconds && a.selection == b.selection && a.folders == b.folders
}

fn prepare_config(
    candidate: Option<Config>,
    previous: Config,
    locations: Locations,
    expected: Vec<u8>,
) -> Result<PreparedConfig, String> {
    let (config, bytes) = if let Some(candidate) = candidate {
        let config = config::prepare_update(candidate, &previous, &locations.config)?;
        if config::read_bytes(&locations.config)? != expected {
            return Err("Configuration changed on disk; reload before updating settings".into());
        }
        (config, expected)
    } else {
        let bytes = config::read_bytes(&locations.config)?;
        let text = std::str::from_utf8(&bytes).map_err(|e| e.to_string())?;
        let config: Config =
            toml::from_str(text).map_err(|e| format!("Invalid configuration: {e}"))?;
        (
            config::prepare_update(config, &previous, &locations.config)?,
            bytes,
        )
    };
    let watcher = Watcher::new(&locations.background, &config.folders)?;
    Ok(PreparedConfig {
        config,
        watcher,
        bytes,
        committed: false,
        durability: None,
    })
}

fn start_config(
    candidate: Option<Config>,
    peer_id: u64,
    engine: &mut Engine,
    control: &mut Control,
    locations: &Locations,
    tx: &Sender<Completion>,
    wake: &UnixStream,
) -> Result<(), String> {
    if control.transaction.is_some() || engine.flight.is_some() {
        return Err(
            "Wait for the current configuration transaction or wallpaper application".into(),
        );
    }
    let previous = engine.config.clone();
    let locations = locations.clone();
    let expected = control.bytes.clone();
    let write = candidate.is_some();
    let gate = CommitGate::new();
    let worker_gate = gate.clone();
    let tx = tx.clone();
    let mut wake = wake.try_clone().map_err(|e| e.to_string())?;
    let worker = thread::Builder::new()
        .name("swiftwall-config".into())
        .spawn(move || {
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                let mut prepared = prepare_config(
                    candidate,
                    previous.clone(),
                    locations.clone(),
                    expected.clone(),
                )?;
                if write && !same_settings(&previous, &prepared.config) {
                    let committed_bytes = toml::to_string(&prepared.config)
                        .map_err(|e| e.to_string())?
                        .into_bytes();
                    prepared.durability = match config::persist_guarded(
                        &prepared.config,
                        &locations.config,
                        &expected,
                        || worker_gate.begin_commit(),
                    )? {
                        config::ConfigCommit::Durable => None,
                        config::ConfigCommit::DurabilityUncertain(warning) => Some(warning),
                    };
                    // Keep the exact committed serialization, never an unrelated external edit.
                    prepared.bytes = committed_bytes;
                    prepared.committed = true;
                }
                if worker_gate.cancelled() {
                    return Err("Configuration cancelled before replacement".into());
                }
                Ok(prepared)
            }))
            .unwrap_or_else(|_| Err("Configuration worker panicked".into()));
            notify(&tx, &mut wake, Completion::Configured(peer_id, result));
        })
        .map_err(|e| e.to_string())?;
    control.transaction = Some(ConfigJob {
        worker,
        peer_id,
        gate,
        deadline: Some(Instant::now() + SETTINGS_TIMEOUT),
        reported: false,
    });
    engine.config_pending = true;
    engine.config_worker_error = None;
    Ok(())
}

fn finish_config(
    result: Result<PreparedConfig, String>,
    engine: &mut Engine,
    control: &mut Control,
    watcher: &mut Watcher,
) -> Value {
    if let Some(job) = control.transaction.take() {
        let _ = job.worker.join();
        if job.gate.cancelled() {
            engine.config_pending = false;
            return protocol::error(
                "worker_cancelled",
                "Configuration cancelled before replacement; active settings unchanged",
            );
        }
    }
    engine.config_pending = false;
    engine.config_worker_error = None;
    match result {
        Err(error) => protocol::error(
            if error.contains("changed on disk") || error.contains("changed externally") {
                "config_conflict"
            } else {
                "config_update_failed"
            },
            error,
        ),
        Ok(prepared) => {
            let changed = !same_settings(&engine.config, &prepared.config);
            let roots_changed = engine.config.folders != prepared.config.folders;
            // The replacement was armed before persistence. Preserve queued old events.
            match watcher.drain() {
                Ok(events) => engine.watch_events |= events,
                Err(error) => {
                    engine.error = Some(error);
                    engine.watch_events |= BACKGROUND | COLLECTION;
                }
            }
            *watcher = prepared.watcher;
            // The settings worker already performed the potentially blocking
            // watch construction. Do not repeat it on the reactor for this scan.
            engine.watcher_prepared_for_scan = true;
            engine.config = prepared.config;
            // A no-op does not establish durability for a previous uncertain commit.
            if prepared.committed || prepared.bytes != control.bytes {
                engine.config_durability = prepared.durability;
                if let Some(warning) = &engine.config_durability {
                    eprintln!("swiftwall: configuration durability uncertain: {warning}");
                }
            }
            control.bytes = prepared.bytes;
            if changed {
                control.revision += 1;
                engine.reset(Instant::now());
            }
            if roots_changed {
                engine.collection_ready = false;
                request_scan(engine);
            }
            let mut response = control.settings(engine);
            response["message"] = json!("configuration reloaded or updated");
            response["status"] = engine.status(Instant::now());
            response
        }
    }
}

fn validate_folders(candidate: Config, previous: &Config, locations: &Locations) -> Value {
    // A picker preview has the same local/readable-new-root policy as Save.
    // Existing unavailable roots remain warnings, not silent removals.
    let candidate = match config::prepare_update(candidate, previous, &locations.config) {
        Ok(config) => config,
        Err(error) => return protocol::error("validation_error", error),
    };
    let folders: Vec<_> = candidate
        .folders
        .iter()
        .map(|path| {
            let available = fs::read_dir(path).is_ok();
            let mut scan = collection::scan(std::slice::from_ref(path));
            let candidates = scan.paths.len();
            let eligible = scan
                .paths
                .iter()
                .filter(|path| match collection::validate(path) {
                    Ok(_) => true,
                    Err(error) => {
                        if scan.warnings.len() < 32 {
                            scan.warnings.push(error);
                        }
                        false
                    }
                })
                .count();
            scan.warnings.truncate(32);
            json!({"path":path,"available":available,"eligible_images":eligible,
            "candidate_images":candidates,"warnings":scan.warnings})
        })
        .collect();
    json!({"ok":true,"folders":folders})
}

fn settings_config(value: Value) -> Result<Config, String> {
    let settings: protocol::Settings = serde_json::from_value(value).map_err(|e| e.to_string())?;
    Ok(Config {
        interval_seconds: settings.interval_seconds,
        selection: settings.selection,
        folders: settings.folders.into_iter().map(PathBuf::from).collect(),
    })
}

fn complete_config_job(
    id: u64,
    result: Result<PreparedConfig, String>,
    engine: &mut Engine,
    control: &mut Control,
    watcher: &mut Watcher,
    peers: &mut [Peer],
) {
    let Some(job) = &control.transaction else {
        return;
    };
    if job.peer_id != id {
        return;
    }
    let reported = job.reported;
    let response = finish_config(result, engine, control, watcher);
    if !reported && let Some(peer) = peers.iter_mut().find(|peer| peer.id == id) {
        peer.reply(response);
    }
}

fn complete_validation_job(id: u64, result: Value, control: &mut Control, peers: &mut [Peer]) {
    let reported = control
        .validations
        .remove(&id)
        .is_none_or(|job| job.reported);
    if !reported && let Some(peer) = peers.iter_mut().find(|peer| peer.id == id) {
        peer.reply(result);
    }
}

fn expire_settings_work(
    engine: &mut Engine,
    control: &mut Control,
    peers: &mut [Peer],
    now: Instant,
) {
    if let Some(job) = &mut control.transaction
        && job.deadline.is_some_and(|deadline| deadline <= now)
    {
        job.deadline = None;
        job.reported = true;
        let cancelled = job.gate.cancel_before_commit();
        if cancelled {
            // Cancellation only withdraws permission to replace the config. The
            // blocked syscall/thread still occupies its slot until it returns.
            engine.config_pending = false;
        }
        let message = if cancelled {
            "Settings preparation timed out; replacement cancelled and active settings unchanged. Worker slot remains occupied until it finishes or the daemon exits."
        } else {
            "Settings commit timed out; replacement may already have occurred. Rotation remains gated until the worker finishes; do not assume rollback."
        };
        engine.config_worker_error = Some(message.into());
        if let Some(peer) = peers.iter_mut().find(|peer| peer.id == job.peer_id) {
            let mut reply = protocol::error("worker_timeout", message);
            reply["outcome_uncertain"] = json!(!cancelled);
            peer.reply(reply);
        }
    }
    for (id, job) in &mut control.validations {
        if job.deadline.is_some_and(|deadline| deadline <= now) {
            job.deadline = None;
            job.reported = true;
            if let Some(peer) = peers.iter_mut().find(|peer| peer.id == *id) {
                peer.reply(protocol::error("worker_timeout", "Folder validation timed out; no settings were changed. The worker slot remains occupied until it finishes or the daemon exits."));
            }
        }
    }
}

fn settle_settings_shutdown(
    engine: &mut Engine,
    control: &mut Control,
    watcher: &mut Watcher,
    completions: &Receiver<Completion>,
    timeout: Duration,
) -> Result<(), String> {
    if let Some(job) = &control.transaction
        && job.gate.cancel_before_commit()
    {
        // A preparing worker may still be inside a kernel filesystem call, but
        // can never replace configuration after losing this atomic permission.
        let job = control.transaction.take().unwrap();
        if job.worker.is_finished() {
            let _ = job.worker.join();
        }
        engine.config_pending = false;
        return Ok(());
    }
    let deadline = Instant::now() + timeout;
    while control.transaction.is_some() {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err("Shutdown timed out awaiting configuration commit; outcome uncertain. Singleton retained until process exit; inspect saved configuration before restart.".into());
        }
        match completions.recv_timeout(remaining) {
            Ok(Completion::Configured(_, result)) => {
                finish_config(result, engine, control, watcher);
            }
            Ok(Completion::Persisted(result)) => engine.persisted(result),
            Ok(_) => {}
            Err(_) => {
                return Err(
                    "Configuration commit did not finalize before shutdown; outcome uncertain"
                        .into(),
                );
            }
        }
    }
    Ok(())
}

struct DispatchContext<'a> {
    locations: &'a Locations,
    tx: &'a Sender<Completion>,
    wake: &'a UnixStream,
    watcher: &'a mut Watcher,
}

fn dispatch(
    frame: &[u8],
    peer: &mut Peer,
    subscribers: usize,
    engine: &mut Engine,
    control: &mut Control,
    context: DispatchContext<'_>,
) {
    let DispatchContext {
        locations,
        tx,
        wake,
        watcher,
    } = context;
    let json_request = frame
        .iter()
        .find(|b| !b.is_ascii_whitespace())
        .is_some_and(|b| matches!(*b, b'{' | b'['));
    let op = if json_request {
        match protocol::parse(frame) {
            Ok(request) => {
                let op = request.op.clone();
                peer.request = Some(request);
                op
            }
            Err(error) => {
                let id = serde_json::from_slice::<Value>(frame)
                    .ok()
                    .and_then(|value| value.get("id").and_then(Value::as_str).map(str::to_owned));
                peer.queue(&protocol::metadata(error, id.as_deref()));
                return;
            }
        }
    } else {
        match std::str::from_utf8(frame) {
            Ok(command) => command.trim().to_owned(),
            Err(_) => {
                peer.reply(protocol::error("invalid_request", "Command must be UTF-8"));
                return;
            }
        }
    };
    let result = match op.as_str() {
        "hello" | "getsettings" if json_request => control.settings(engine),
        "subscribe" if json_request => {
            if subscribers >= protocol::MAX_SUBSCRIBERS {
                protocol::error("subscriber_limit", "At most 8 subscribers are allowed")
            } else {
                peer.subscriber = true;
                peer.expires = None;
                // Publication at the next reactor turn provides a consistent initial seq.
                return;
            }
        }
        "updatesettings" if !json_request => {
            protocol::error("invalid_request", "updatesettings requires JSON")
        }
        "updatesettings" | "reload" => {
            let candidate = if op == "updatesettings" {
                let request = peer.request.as_ref().unwrap();
                if request.if_revision.as_deref() != Some(control.revision().as_str()) {
                    peer.reply(protocol::error(
                        "revision_conflict",
                        "Settings revision is stale; getsettings and retry",
                    ));
                    return;
                }
                match request
                    .settings
                    .clone()
                    .ok_or("settings is required".to_string())
                    .and_then(settings_config)
                {
                    Ok(config) => Some(config),
                    Err(error) => {
                        peer.reply(protocol::error("validation_error", error));
                        return;
                    }
                }
            } else {
                None
            };
            match start_config(candidate, peer.id, engine, control, locations, tx, wake) {
                Ok(()) => {
                    peer.waiting = true;
                    peer.expires = None;
                    return;
                }
                Err(error) => protocol::error("busy", error),
            }
        }
        "validatefolders" if json_request => {
            if control.validations.len() >= 2 {
                protocol::error("busy", "Folder validation workers are busy")
            } else {
                let request = peer.request.as_ref().unwrap();
                let mut candidate = engine.config.clone();
                if let Some(folders) = &request.folders {
                    candidate.folders = folders.iter().map(PathBuf::from).collect();
                } else if let Some(settings) = &request.settings {
                    candidate = match settings_config(settings.clone()) {
                        Ok(config) => config,
                        Err(error) => {
                            peer.reply(protocol::error("validation_error", error));
                            return;
                        }
                    };
                } else {
                    peer.reply(protocol::error("validation_error", "folders is required"));
                    return;
                }
                let locations = locations.clone();
                let previous = engine.config.clone();
                let tx = tx.clone();
                let id = peer.id;
                match wake.try_clone().and_then(|mut wake| {
                    thread::Builder::new()
                        .name("swiftwall-validate".into())
                        .spawn(move || {
                            let result = std::panic::catch_unwind(|| {
                                validate_folders(candidate, &previous, &locations)
                            })
                            .unwrap_or_else(|_| {
                                protocol::error("validation_error", "Validation worker panicked")
                            });
                            notify(&tx, &mut wake, Completion::Validated(id, result));
                        })
                }) {
                    Ok(_) => {
                        control.validations.insert(
                            id,
                            ValidationJob {
                                deadline: Some(Instant::now() + SETTINGS_TIMEOUT),
                                reported: false,
                            },
                        );
                        peer.waiting = true;
                        peer.expires = None;
                        return;
                    }
                    Err(error) => protocol::error("worker_failed", error),
                }
            }
        }
        _ => command_response(&op, engine, locations, tx, wake, watcher),
    };
    peer.reply(result);
}

pub fn run(locations: Locations) -> Result<(), String> {
    let config_bytes = config::read_bytes(&locations.config)?;
    let text = std::str::from_utf8(&config_bytes).map_err(|e| e.to_string())?;
    let config: Config = toml::from_str(text).map_err(|e| format!("Invalid configuration: {e}"))?;
    let config = config.validate_and_normalize(&locations.config)?;
    let mut control = Control::new(config_bytes);
    let instance = Instance::acquire(&locations.runtime)?;
    let signals = Signals::new()?;
    let state = State::load(&locations.state)?;
    let initial = observe(&locations.background)?;
    let mut engine = Engine::new(config, state, initial, Instant::now());
    let mut watcher = Watcher::new(&locations.background, &engine.config.folders)?;
    engine.watcher_prepared_for_scan = true;
    let (mut wake_read, wake_write) = UnixStream::pair().map_err(|e| e.to_string())?;
    wake_read.set_nonblocking(true).map_err(|e| e.to_string())?;
    wake_write
        .set_nonblocking(true)
        .map_err(|e| e.to_string())?;
    let (complete_tx, complete_rx) = mpsc::channel();
    let (scan_tx, scan_rx) = mpsc::channel();
    let worker_tx = complete_tx.clone();
    let worker_wake = wake_write.try_clone().map_err(|e| e.to_string())?;
    thread::Builder::new()
        .name("swiftwall-scan".into())
        .spawn(move || scanner(scan_rx, worker_tx, worker_wake))
        .map_err(|e| e.to_string())?;
    engine.persister = Some(Persister::new(
        locations.state.clone(),
        complete_tx.clone(),
        wake_write.try_clone().map_err(|e| e.to_string())?,
    )?);
    request_scan(&mut engine);
    engine.save(&locations);
    let mut peers: Vec<Peer> = vec![];
    let mut next_peer_id = 0u64;
    let mut scan_at: Option<Instant> = None;
    let mut background_at: Option<Instant> = None;
    let outcome = (|| -> Result<(), String> {
        loop {
            if !engine.scanning && engine.scan_dirty {
                if engine.watcher_prepared_for_scan {
                    engine.watcher_prepared_for_scan = false;
                } else {
                    engine.watch_events |=
                        rearm_watcher(&mut watcher, &locations.background, &engine.config.folders)?;
                }
                scan_tx
                    .send((engine.generation, engine.config.folders.clone()))
                    .map_err(|e| e.to_string())?;
                engine.scanning = true;
                engine.scan_dirty = false;
            }
            if engine.watch_events & COLLECTION != 0 {
                scan_at = Some(Instant::now() + DEBOUNCE);
            }
            if engine.watch_events & BACKGROUND != 0 {
                background_at = Some(Instant::now() + DEBOUNCE);
            }
            engine.watch_events = 0;
            control.publish(&engine, &mut peers);
            let now = Instant::now();
            let rotation = if !engine.config_pending
                && engine.flight.is_none()
                && engine.collection_ready
                && !engine.paths.is_empty()
            {
                engine.deadline
            } else {
                None
            };
            let next = [
                rotation,
                scan_at,
                background_at,
                peers.iter().filter_map(|p| p.expires).min(),
                control.next_work_deadline(),
            ]
            .into_iter()
            .flatten()
            .min();
            let timeout = next
                .map(|d| {
                    let remaining = d.saturating_duration_since(now);
                    // Round up so a fractional millisecond never becomes a busy poll.
                    let millis = remaining.as_millis()
                        + u128::from(!remaining.subsec_nanos().is_multiple_of(1_000_000));
                    millis.min(i32::MAX as u128) as i32
                })
                .unwrap_or(-1);
            let mut fds = vec![
                runtime::interest(instance.listener.as_raw_fd(), libc::POLLIN),
                runtime::interest(signals.0.as_raw_fd(), libc::POLLIN),
                runtime::interest(watcher.fd.as_raw_fd(), libc::POLLIN),
                runtime::interest(wake_read.as_raw_fd(), libc::POLLIN),
            ];
            fds.extend(peers.iter().map(|p| {
                runtime::interest(
                    p.stream.as_raw_fd(),
                    if !p.output.is_empty() {
                        libc::POLLOUT
                    } else {
                        libc::POLLIN
                    },
                )
            }));
            runtime::poll(&mut fds, timeout)?;
            if fds[1].revents != 0 {
                break;
            }
            if fds[2].revents != 0 {
                let kinds = watcher.drain()?;
                if kinds & COLLECTION != 0 {
                    scan_at = Some(Instant::now() + DEBOUNCE);
                }
                if kinds & BACKGROUND != 0 {
                    // Read an existing link immediately; debounce only transient absence.
                    match observe(&locations.background) {
                        Ok(Some(path)) => {
                            if engine.reconcile(Some(path), Instant::now()) {
                                engine.save(&locations);
                            }
                        }
                        Ok(None) => {}
                        Err(e) => {
                            engine.suspend(e);
                            engine.save(&locations);
                        }
                    }
                    // Ancestor watches must move down when a missing directory reappears.
                    background_at = Some(Instant::now() + DEBOUNCE);
                }
            }
            if fds[3].revents != 0 {
                let mut bytes = [0u8; 1024];
                while wake_read.read(&mut bytes).is_ok_and(|n| n > 0) {}
            }
            while let Ok(event) = complete_rx.try_recv() {
                match event {
                    Completion::Scan(generation, result) => {
                        engine.scanning = false;
                        if generation == engine.generation {
                            let before = serde_json::to_vec(&engine.state.carousel).ok();
                            engine.paths = result.paths;
                            engine.state.carousel.reconcile(&engine.paths);
                            engine.warnings = result.warnings.into_iter().take(32).collect();
                            engine.collection_ready = true;
                            // An unchanged scan must not write into a watched state directory.
                            if before.is_none()
                                || before != serde_json::to_vec(&engine.state.carousel).ok()
                            {
                                engine.save(&locations);
                            }
                        }
                    }
                    Completion::Applied => {
                        finish_apply(&mut engine, &locations);
                        // Revalidation detects deleted/corrupt replacements and updates eligibility.
                        request_scan(&mut engine);
                    }
                    Completion::Persisted(result) => engine.persisted(result),
                    Completion::Configured(id, result) => {
                        complete_config_job(
                            id,
                            result,
                            &mut engine,
                            &mut control,
                            &mut watcher,
                            &mut peers,
                        );
                    }
                    Completion::Validated(id, result) => {
                        complete_validation_job(id, result, &mut control, &mut peers);
                    }
                }
            }
            // Process existing peers before accepting new ones: fds corresponds to the old vector.
            let mut subscribers = peers.iter().filter(|p| p.subscriber && !p.dead).count();
            for (index, peer) in peers.iter_mut().enumerate() {
                let ready = fds[index + 4].revents;
                if ready & (libc::POLLERR | libc::POLLNVAL) != 0 {
                    peer.dead = true;
                }
                if peer.output.is_empty() && ready & (libc::POLLIN | libc::POLLHUP) != 0 {
                    let mut buffer = [0u8; 8192];
                    match peer.stream.read(&mut buffer) {
                        Ok(0) => peer.dead = true,
                        Ok(n) if peer.subscriber || peer.waiting => {
                            // Subscribers are receive-only; commands use transient connections.
                            let _ = n;
                            peer.dead = true;
                        }
                        Ok(n) => {
                            let newline = buffer[..n].iter().position(|b| *b == b'\n');
                            let payload = newline.unwrap_or(n);
                            // Enforce the frame bound before extending the buffer or parsing JSON.
                            if peer.input.len() + payload >= protocol::MAX_FRAME {
                                peer.queue(&protocol::metadata(
                                    protocol::error("frame_too_large", "Request exceeds 64 KiB"),
                                    None,
                                ));
                            } else {
                                peer.input.extend_from_slice(&buffer[..payload]);
                                if newline.is_some() {
                                    let frame = std::mem::take(&mut peer.input);
                                    dispatch(
                                        &frame,
                                        peer,
                                        subscribers,
                                        &mut engine,
                                        &mut control,
                                        DispatchContext {
                                            locations: &locations,
                                            tx: &complete_tx,
                                            wake: &wake_write,
                                            watcher: &mut watcher,
                                        },
                                    );
                                    if peer.subscriber {
                                        subscribers += 1;
                                    }
                                }
                            }
                        }
                        Err(e) if e.kind() == io::ErrorKind::WouldBlock => {}
                        Err(_) => peer.dead = true,
                    }
                }
                if !peer.dead && !peer.output.is_empty() {
                    match peer.stream.write(&peer.output[peer.sent..]) {
                        Ok(0) => peer.dead = true,
                        Ok(n) => {
                            peer.sent += n;
                            peer.expires = Some(Instant::now() + PEER_PROGRESS);
                            if peer.sent == peer.output.len() {
                                if peer.subscriber {
                                    peer.output = peer.latest.take().unwrap_or_default();
                                    peer.sent = 0;
                                    if peer.output.is_empty() {
                                        peer.expires = None;
                                    }
                                } else {
                                    peer.dead = true;
                                }
                            }
                        }
                        Err(e) if e.kind() == io::ErrorKind::WouldBlock => {}
                        Err(_) => peer.dead = true,
                    }
                }
            }
            peers.retain(|p| !p.dead && p.expires.is_none_or(|d| d > Instant::now()));
            if fds[0].revents != 0 {
                for _ in 0..64 {
                    match instance.listener.accept() {
                        Ok((stream, _)) if peers.len() < 64 => {
                            next_peer_id = next_peer_id
                                .checked_add(1)
                                .ok_or("Peer ID space exhausted")?;
                            if let Ok(peer) = Peer::new(stream, next_peer_id) {
                                peers.push(peer);
                            }
                        }
                        Ok(_) => {}
                        Err(e) if e.kind() == io::ErrorKind::WouldBlock => break,
                        Err(e) => return Err(e.to_string()),
                    }
                }
            }
            let now = Instant::now();
            expire_settings_work(&mut engine, &mut control, &mut peers, now);
            if background_at.is_some_and(|d| d <= now) {
                background_at = None;
                match observe(&locations.background) {
                    Ok(path) => {
                        if engine.reconcile(path, now) {
                            engine.save(&locations);
                        }
                    }
                    Err(e) => {
                        engine.suspend(e);
                        engine.save(&locations);
                    }
                }
                engine.watch_events |=
                    rearm_watcher(&mut watcher, &locations.background, &engine.config.folders)?;
            }
            if scan_at.is_some_and(|d| d <= now) {
                scan_at = None;
                request_scan(&mut engine);
            }
            if !engine.config_pending
                && engine.flight.is_none()
                && engine.collection_ready
                && !engine.paths.is_empty()
                && engine.deadline.is_some_and(|d| d <= now)
                && let Err(e) = apply(
                    &mut engine,
                    &locations,
                    false,
                    true,
                    &complete_tx,
                    &wake_write,
                )
            {
                engine.error = Some(e);
                engine.reset(Instant::now());
            }
        }
        Ok(())
    })();
    if let Err(error) = settle_settings_shutdown(
        &mut engine,
        &mut control,
        &mut watcher,
        &complete_rx,
        SETTINGS_SHUTDOWN,
    ) {
        // Do not release the singleton while an outcome-uncertain writer can
        // still run. main exits nonzero; the OS releases this descriptor only
        // at process teardown. A deadline is not syscall cancellation.
        std::mem::forget(instance);
        return Err(error);
    }
    finish_apply(&mut engine, &locations);
    engine.save(&locations);
    let persistence = engine.flush_persistence(&complete_rx);
    if engine.persister.as_ref().is_some_and(Persister::pending) {
        std::mem::forget(instance);
    } else {
        drop(instance);
    }
    match (outcome, persistence) {
        (Err(error), Err(persistence)) => {
            Err(format!("{error}; state flush failed: {persistence}"))
        }
        (Err(error), Ok(())) => Err(error),
        (Ok(()), Err(error)) => Err(format!("State flush failed: {error}")),
        (Ok(()), Ok(())) => Ok(()),
    }
}

fn command_response(
    command: &str,
    engine: &mut Engine,
    locations: &Locations,
    complete_tx: &Sender<Completion>,
    wake: &UnixStream,
    _watcher: &mut Watcher,
) -> Value {
    if command == "status" {
        return json!({"ok":true,"status":engine.status(Instant::now())});
    }
    let result: Result<&str, String> = (|| match command {
        "pause" => {
            engine.state.paused = true;
            engine.deadline = None;
            Ok("paused; an in-flight application is not undone")
        }
        "resume" => {
            if engine.flight.is_some() {
                return Err("Wait for the in-flight application before resuming".into());
            }
            let observed =
                observe(&locations.background)?.ok_or("Background link is unavailable")?;
            engine.state.observed = Some(observed);
            engine.state.paused = false;
            engine.state.protective_suspension = None;
            engine.error = None;
            engine.reset(Instant::now());
            Ok("resumed with a full interval")
        }
        "next" | "previous" => {
            apply(
                engine,
                locations,
                command == "previous",
                false,
                complete_tx,
                wake,
            )?;
            Ok("accepted; inspect status for completion")
        }
        "rescan" => {
            request_scan(engine);
            Ok("scan requested")
        }
        "reload" => Err("Reload must be dispatched through the configuration worker".into()),
        _ => Err("Unknown command".into()),
    })();
    match result {
        Ok(message) => {
            engine.save(locations);
            json!({"ok":engine.state.protective_suspension.is_none(),"message":message,"status":engine.status(Instant::now())})
        }
        Err(error) => json!({"ok":false,"error":error}),
    }
}

/// One-shot JSON CLI: print exactly one NDJSON response, including failures.
/// Subscribe returns its initial snapshot; persistent subscription is a socket API.
pub fn json_client(locations: &Locations, request: &str) -> Result<(), String> {
    if request.contains('\n') || request.contains('\r') {
        return Err("Pass a single JSON frame without literal newlines".into());
    }
    if request.len() >= protocol::MAX_FRAME {
        return Err("Request exceeds 64 KiB".into());
    }
    let response = exchange(locations, request)?;
    println!(
        "{}",
        serde_json::to_string(&response).map_err(|e| e.to_string())?
    );
    response_result(&response)
}

fn response_result(response: &Value) -> Result<(), String> {
    if response["ok"] == true || response["event"] == "snapshot" {
        Ok(())
    } else {
        Err(response["error"]
            .as_str()
            .unwrap_or("Request completed in protective suspension")
            .into())
    }
}

fn exchange(locations: &Locations, command: &str) -> Result<Value, String> {
    use std::io::BufRead;
    let mut stream = UnixStream::connect(locations.runtime.join("control.sock"))
        .map_err(|e| format!("Daemon unavailable: {e}"))?;
    stream
        .set_read_timeout(Some(Duration::from_secs(30)))
        .map_err(|e| e.to_string())?;
    stream
        .set_write_timeout(Some(PEER_PROGRESS))
        .map_err(|e| e.to_string())?;
    writeln!(stream, "{command}").map_err(|e| e.to_string())?;
    let mut bytes = Vec::new();
    io::BufReader::new(stream.take(protocol::MAX_RESPONSE as u64 + 1))
        .read_until(b'\n', &mut bytes)
        .map_err(|e| e.to_string())?;
    if bytes.len() > protocol::MAX_RESPONSE || bytes.last() != Some(&b'\n') {
        return Err("Daemon response exceeds 1 MiB or is incomplete".into());
    }
    serde_json::from_slice(&bytes).map_err(|e| e.to_string())
}

pub fn client(locations: &Locations, command: &str) -> Result<(), String> {
    if ![
        "next", "previous", "pause", "resume", "status", "rescan", "reload",
    ]
    .contains(&command)
    {
        return Err("Unknown command; use --help".into());
    }
    let response = exchange(locations, command)?;
    println!(
        "{}",
        serde_json::to_string_pretty(&response).map_err(|e| e.to_string())?
    );
    response_result(&response)
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Fixture(Locations);
    impl Fixture {
        fn new() -> Self {
            use std::sync::atomic::{AtomicU64, Ordering};
            static NEXT: AtomicU64 = AtomicU64::new(0);
            let dir = std::env::temp_dir().join(format!(
                "swiftwall-daemon-unit-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&dir).unwrap();
            Self(Locations {
                config: dir.join("config.toml"),
                state: dir.join("state.json"),
                runtime: dir.join("runtime"),
                background: dir.join("background"),
                program: dir.join("must-not-run"),
            })
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            fs::remove_dir_all(self.0.state.parent().unwrap()).unwrap();
        }
    }

    fn engine(now: Instant) -> Engine {
        Engine::new(
            Config::default(),
            State {
                version: 1,
                ..State::default()
            },
            Some("/old.png".into()),
            now,
        )
    }
    #[test]
    fn snapshots_ignore_countdown_but_track_exact_deadline_and_initial_delivery() {
        let mut engine = engine(Instant::now());
        engine.collection_ready = true;
        engine.paths.push("/image.png".into());
        let mut control = Control::new(vec![]);
        control.publish(&engine, &mut []);
        let seq = control.seq;
        let revision = control.revision();
        thread::sleep(Duration::from_millis(5));
        control.publish(&engine, &mut []);
        assert_eq!(control.seq, seq);
        engine.deadline = engine.deadline.map(|d| d + Duration::from_nanos(1));
        control.publish(&engine, &mut []);
        assert_eq!(control.seq, seq + 1);
        assert_eq!(control.revision(), revision);
        let (stream, _reader) = UnixStream::pair().unwrap();
        let mut peer = Peer::new(stream, 1).unwrap();
        peer.subscriber = true;
        peer.expires = None;
        control.publish(&engine, std::slice::from_mut(&mut peer));
        let initial: Value = serde_json::from_slice(&peer.output).unwrap();
        assert_eq!(initial["event"], "snapshot");
        assert_eq!(initial["seq"], control.seq);
        let output = peer.output.clone();
        control.publish(&engine, std::slice::from_mut(&mut peer));
        assert_eq!(peer.output, output);
        assert!(peer.latest.is_none());
    }

    #[test]
    fn subscriber_output_coalesces_without_corrupting_partial_frames_or_extending_timeout() {
        let (stream, _reader) = UnixStream::pair().unwrap();
        let mut peer = Peer::new(stream, 1).unwrap();
        peer.queue(&json!({"seq":1}));
        let first = peer.output.clone();
        let expires = peer.expires;
        peer.sent = 1;
        for seq in 2..100 {
            peer.queue(&json!({"seq":seq}));
        }
        assert_eq!(peer.output, first);
        assert_eq!(peer.expires, expires);
        let latest: Value = serde_json::from_slice(peer.latest.as_ref().unwrap()).unwrap();
        assert_eq!(latest["seq"], 99);
    }

    #[test]
    fn preparation_timeout_revokes_replacement_and_keeps_the_worker_slot() {
        let fixture = Fixture::new();
        let locations = &fixture.0;
        let mut engine = engine(Instant::now());
        engine.config.folders = vec![locations.config.parent().unwrap().to_path_buf()];
        let original = toml::to_string(&engine.config).unwrap().into_bytes();
        fs::write(&locations.config, &original).unwrap();
        std::os::unix::fs::symlink("/old.png", &locations.background).unwrap();
        let mut control = Control::new(original.clone());
        let mut watcher = Watcher::new(&locations.background, &engine.config.folders).unwrap();
        let (tx, rx) = mpsc::channel();
        let (_read, wake) = UnixStream::pair().unwrap();
        let gate = CommitGate::new();
        let worker_gate = gate.clone();
        let (entered_tx, entered_rx) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel();
        let mut candidate = engine.config.clone();
        candidate.interval_seconds += 1;
        let proposed = candidate.clone();
        let expected = original.clone();
        let path = locations.config.clone();
        let worker = thread::spawn(move || {
            let result = config::persist_guarded(&proposed, &path, &expected, || {
                entered_tx.send(()).unwrap();
                release_rx.recv_timeout(Duration::from_secs(3)).unwrap();
                worker_gate.begin_commit()
            });
            tx.send(result).unwrap();
        });
        entered_rx.recv_timeout(Duration::from_secs(3)).unwrap();
        control.transaction = Some(ConfigJob {
            worker,
            peer_id: 7,
            gate,
            deadline: Some(Instant::now()),
            reported: false,
        });
        engine.config_pending = true;
        let (stream, _reader) = UnixStream::pair().unwrap();
        let mut peer = Peer::new(stream, 7).unwrap();
        expire_settings_work(
            &mut engine,
            &mut control,
            std::slice::from_mut(&mut peer),
            Instant::now(),
        );
        let reply: Value = serde_json::from_slice(&peer.output).unwrap();
        assert_eq!(reply["code"], "worker_timeout");
        assert_eq!(reply["outcome_uncertain"], false);
        assert!(!engine.config_pending);
        assert!(control.transaction.is_some());
        assert!(control.next_work_deadline().is_none());
        let (complete_tx, _complete_rx) = mpsc::channel();
        assert!(
            start_config(
                Some(candidate),
                8,
                &mut engine,
                &mut control,
                locations,
                &complete_tx,
                &wake
            )
            .is_err()
        );
        for command in ["status", "pause", "resume"] {
            let response = command_response(
                command,
                &mut engine,
                locations,
                &complete_tx,
                &wake,
                &mut watcher,
            );
            assert_eq!(response["ok"], true, "{response}");
        }
        assert_eq!(fs::read(&locations.config).unwrap(), original);
        release_tx.send(()).unwrap();
        assert!(rx.recv_timeout(Duration::from_secs(3)).unwrap().is_err());
        let timeout_frame = peer.output.clone();
        peer.sent = 1; // A late completion must not corrupt a partial timeout reply.
        complete_config_job(
            7,
            Err("late cancellation".into()),
            &mut engine,
            &mut control,
            &mut watcher,
            std::slice::from_mut(&mut peer),
        );
        assert_eq!(peer.output, timeout_frame);
        assert!(peer.latest.is_none());
        assert_eq!(fs::read(&locations.config).unwrap(), original);
        assert_eq!(control.revision, 0);
        assert!(control.transaction.is_none());
    }

    #[test]
    fn committing_timeout_is_uncertain_and_late_commit_is_reconciled() {
        let fixture = Fixture::new();
        let locations = &fixture.0;
        let mut engine = engine(Instant::now());
        engine.config.folders = vec![locations.config.parent().unwrap().to_path_buf()];
        let original = toml::to_string(&engine.config).unwrap().into_bytes();
        fs::write(&locations.config, &original).unwrap();
        let mut control = Control::new(original.clone());
        let mut watcher = Watcher::new(&locations.background, &engine.config.folders).unwrap();
        let gate = CommitGate::new();
        let worker_gate = gate.clone();
        let (tx, rx) = mpsc::channel();
        let (entered_tx, entered_rx) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel();
        let previous = engine.config.clone();
        let mut candidate = previous.clone();
        candidate.interval_seconds += 1;
        let worker_locations = locations.clone();
        let expected = original.clone();
        let worker = thread::spawn(move || {
            let mut prepared = prepare_config(
                Some(candidate),
                previous,
                worker_locations.clone(),
                expected.clone(),
            )
            .unwrap();
            let outcome = config::persist_guarded(
                &prepared.config,
                &worker_locations.config,
                &expected,
                || {
                    worker_gate.begin_commit()?;
                    entered_tx.send(()).unwrap();
                    release_rx.recv_timeout(Duration::from_secs(3)).unwrap();
                    Ok(())
                },
            )
            .unwrap();
            assert_eq!(outcome, config::ConfigCommit::Durable);
            prepared.bytes = toml::to_string(&prepared.config).unwrap().into_bytes();
            prepared.committed = true;
            tx.send(Completion::Configured(7, Ok(prepared))).unwrap();
        });
        entered_rx.recv_timeout(Duration::from_secs(3)).unwrap();
        control.transaction = Some(ConfigJob {
            worker,
            peer_id: 7,
            gate,
            deadline: Some(Instant::now()),
            reported: false,
        });
        engine.config_pending = true;
        let (stream, _reader) = UnixStream::pair().unwrap();
        let mut peer = Peer::new(stream, 7).unwrap();
        expire_settings_work(
            &mut engine,
            &mut control,
            std::slice::from_mut(&mut peer),
            Instant::now(),
        );
        let reply: Value = serde_json::from_slice(&peer.output).unwrap();
        assert_eq!(reply["outcome_uncertain"], true);
        assert!(engine.config_pending);
        assert_eq!(fs::read(&locations.config).unwrap(), original);
        let started = Instant::now();
        assert!(
            settle_settings_shutdown(
                &mut engine,
                &mut control,
                &mut watcher,
                &rx,
                Duration::from_millis(5)
            )
            .unwrap_err()
            .contains("uncertain")
        );
        assert!(started.elapsed() < Duration::from_millis(500));
        assert!(control.transaction.is_some());
        release_tx.send(()).unwrap();
        let Completion::Configured(_, result) = rx.recv_timeout(Duration::from_secs(3)).unwrap()
        else {
            panic!("wrong completion")
        };
        let response = finish_config(result, &mut engine, &mut control, &mut watcher);
        assert_eq!(response["ok"], true);
        assert!(!engine.config_pending);
        assert_eq!(engine.config.interval_seconds, 301);
        assert_eq!(control.bytes, fs::read(&locations.config).unwrap());
    }

    #[test]
    fn post_rename_stall_finalizes_without_a_second_reply_even_if_sync_fails() {
        for fail_sync in [false, true] {
            let fixture = Fixture::new();
            let locations = &fixture.0;
            let mut engine = engine(Instant::now());
            engine.config.folders = vec![locations.config.parent().unwrap().to_path_buf()];
            let original = toml::to_string(&engine.config).unwrap().into_bytes();
            fs::write(&locations.config, &original).unwrap();
            let mut control = Control::new(original.clone());
            let mut watcher = Watcher::new(&locations.background, &engine.config.folders).unwrap();
            let gate = CommitGate::new();
            let worker_gate = gate.clone();
            let (tx, rx) = mpsc::channel();
            let (entered_tx, entered_rx) = mpsc::channel();
            let (release_tx, release_rx) = mpsc::channel();
            let previous = engine.config.clone();
            let mut candidate = previous.clone();
            candidate.interval_seconds += 1;
            let owned_locations = locations.clone();
            let worker = thread::spawn(move || {
                let mut prepared = prepare_config(
                    Some(candidate),
                    previous,
                    owned_locations.clone(),
                    original.clone(),
                )
                .unwrap();
                let result = config::persist_with_hooks(
                    &prepared.config,
                    &owned_locations.config,
                    &original,
                    || worker_gate.begin_commit(),
                    |directory| {
                        entered_tx.send(()).unwrap();
                        release_rx.recv_timeout(Duration::from_secs(3)).unwrap();
                        if fail_sync {
                            Err(io::Error::other("simulated directory sync failure"))
                        } else {
                            directory.sync_all()
                        }
                    },
                )
                .unwrap();
                prepared.durability = match result {
                    config::ConfigCommit::Durable => None,
                    config::ConfigCommit::DurabilityUncertain(message) => Some(message),
                };
                prepared.committed = true;
                prepared.bytes = toml::to_string(&prepared.config).unwrap().into_bytes();
                tx.send(Completion::Configured(7, Ok(prepared))).unwrap();
            });
            entered_rx.recv_timeout(Duration::from_secs(3)).unwrap();
            assert_eq!(
                Config::load(&locations.config).unwrap().interval_seconds,
                301
            );
            assert_eq!(engine.config.interval_seconds, 300);
            control.transaction = Some(ConfigJob {
                worker,
                peer_id: 7,
                gate,
                deadline: Some(Instant::now()),
                reported: false,
            });
            engine.config_pending = true;
            let (stream, _reader) = UnixStream::pair().unwrap();
            let mut peer = Peer::new(stream, 7).unwrap();
            expire_settings_work(
                &mut engine,
                &mut control,
                std::slice::from_mut(&mut peer),
                Instant::now(),
            );
            let timeout_frame = peer.output.clone();
            peer.sent = 1;
            assert!(engine.config_pending);
            assert!(
                settle_settings_shutdown(
                    &mut engine,
                    &mut control,
                    &mut watcher,
                    &rx,
                    Duration::from_millis(5)
                )
                .is_err()
            );
            release_tx.send(()).unwrap();
            let Completion::Configured(id, result) =
                rx.recv_timeout(Duration::from_secs(3)).unwrap()
            else {
                panic!("wrong completion")
            };
            complete_config_job(
                id,
                result,
                &mut engine,
                &mut control,
                &mut watcher,
                std::slice::from_mut(&mut peer),
            );
            assert_eq!(peer.output, timeout_frame);
            assert!(peer.latest.is_none());
            assert_eq!(engine.config.interval_seconds, 301);
            assert_eq!(engine.config_durability.is_some(), fail_sync);
            assert_eq!(control.revision, 1);
            assert_eq!(control.bytes, fs::read(&locations.config).unwrap());
            assert!(control.transaction.is_none() && !engine.config_pending);
        }
    }

    #[test]
    fn shutdown_does_not_join_a_preparing_settings_worker() {
        let fixture = Fixture::new();
        let locations = &fixture.0;
        let mut engine = engine(Instant::now());
        engine.config.folders = vec![locations.config.parent().unwrap().to_path_buf()];
        let mut control = Control::new(vec![]);
        let mut watcher = Watcher::new(&locations.background, &engine.config.folders).unwrap();
        let gate = CommitGate::new();
        let worker_gate = gate.clone();
        let (release_tx, release_rx) = mpsc::channel();
        let (finished_tx, finished_rx) = mpsc::channel();
        let worker = thread::spawn(move || {
            release_rx.recv_timeout(Duration::from_secs(3)).unwrap();
            finished_tx
                .send(worker_gate.begin_commit().is_err())
                .unwrap();
        });
        control.transaction = Some(ConfigJob {
            worker,
            peer_id: 7,
            gate,
            deadline: Some(Instant::now() + SETTINGS_TIMEOUT),
            reported: false,
        });
        engine.config_pending = true;
        let (_tx, rx) = mpsc::channel();
        let started = Instant::now();
        settle_settings_shutdown(
            &mut engine,
            &mut control,
            &mut watcher,
            &rx,
            Duration::from_millis(5),
        )
        .unwrap();
        assert!(started.elapsed() < Duration::from_millis(500));
        assert!(!engine.config_pending && control.transaction.is_none());
        release_tx.send(()).unwrap();
        assert!(finished_rx.recv_timeout(Duration::from_secs(3)).unwrap());
    }

    #[test]
    fn validation_timeout_does_not_free_slots_for_unbounded_replacements() {
        let fixture = Fixture::new();
        let locations = &fixture.0;
        let mut engine = engine(Instant::now());
        engine.config.folders = vec![locations.config.parent().unwrap().to_path_buf()];
        let mut watcher = Watcher::new(&locations.background, &engine.config.folders).unwrap();
        let mut control = Control::new(vec![]);
        let mut peers = Vec::new();
        let mut readers = Vec::new();
        for id in [1, 2] {
            control.validations.insert(
                id,
                ValidationJob {
                    deadline: Some(Instant::now()),
                    reported: false,
                },
            );
            let (stream, reader) = UnixStream::pair().unwrap();
            peers.push(Peer::new(stream, id).unwrap());
            readers.push(reader);
        }
        expire_settings_work(&mut engine, &mut control, &mut peers, Instant::now());
        assert_eq!(control.validations.len(), 2);
        assert!(control.next_work_deadline().is_none());
        for peer in &peers {
            let response: Value = serde_json::from_slice(&peer.output).unwrap();
            assert_eq!(response["code"], "worker_timeout");
        }
        let (stream, _reader) = UnixStream::pair().unwrap();
        let mut peer = Peer::new(stream, 3).unwrap();
        let (tx, _rx) = mpsc::channel();
        let (_read, wake) = UnixStream::pair().unwrap();
        let request = serde_json::to_vec(
            &json!({"v":1,"op":"validatefolders","folders":engine.config.folders}),
        )
        .unwrap();
        dispatch(
            &request,
            &mut peer,
            0,
            &mut engine,
            &mut control,
            DispatchContext {
                locations,
                tx: &tx,
                wake: &wake,
                watcher: &mut watcher,
            },
        );
        let response: Value = serde_json::from_slice(&peer.output).unwrap();
        assert_eq!(response["code"], "busy");
        assert_eq!(control.validations.len(), 2);
        let output = peers[0].output.clone();
        expire_settings_work(&mut engine, &mut control, &mut peers, Instant::now());
        assert_eq!(peers[0].output, output);
        peers[0].sent = 1;
        complete_validation_job(1, json!({"ok":true}), &mut control, &mut peers);
        assert_eq!(peers[0].output, output);
        assert!(peers[0].latest.is_none());
        assert_eq!(control.validations.len(), 1);
    }

    #[test]
    fn state_shutdown_wait_is_bounded_without_claiming_to_cancel_the_write() {
        let fixture = Fixture::new();
        let mut engine = engine(Instant::now());
        let (tx, rx) = mpsc::channel();
        let (_read, wake) = UnixStream::pair().unwrap();
        let (entered_tx, entered_rx) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel();
        engine.persister = Some(
            Persister::spawn(
                move |_| {
                    entered_tx.send(()).unwrap();
                    release_rx.recv_timeout(Duration::from_secs(3)).unwrap();
                    Ok(())
                },
                tx,
                wake,
            )
            .unwrap(),
        );
        engine.save(&fixture.0);
        entered_rx.recv_timeout(Duration::from_secs(3)).unwrap();
        let started = Instant::now();
        let error = engine
            .flush_persistence_with_timeout(&rx, Duration::from_millis(5))
            .unwrap_err();
        assert!(error.contains("uncertain"));
        assert!(started.elapsed() < Duration::from_millis(500));
        assert!(engine.persister.as_ref().unwrap().pending());
        release_tx.send(()).unwrap();
        let Completion::Persisted(result) = rx.recv_timeout(Duration::from_secs(3)).unwrap() else {
            panic!("wrong completion")
        };
        engine.persisted(result);
        engine.flush_persistence(&rx).unwrap();
    }

    #[test]
    fn config_transactions_commit_consecutively_and_noop_preserves_deadline() {
        let fixture = Fixture::new();
        let locations = &fixture.0;
        let mut engine = engine(Instant::now());
        engine.config.folders = vec![locations.config.parent().unwrap().to_path_buf()];
        let bytes = toml::to_string(&engine.config).unwrap().into_bytes();
        fs::write(&locations.config, &bytes).unwrap();
        let mut control = Control::new(bytes);
        let mut watcher = Watcher::new(&locations.background, &engine.config.folders).unwrap();
        let (tx, rx) = mpsc::channel();
        let (_read, wake) = UnixStream::pair().unwrap();
        for interval in [400, 500, 500] {
            let before_deadline = engine.deadline;
            let before_revision = control.revision;
            let unchanged = engine.config.interval_seconds == interval;
            let mut candidate = engine.config.clone();
            candidate.interval_seconds = interval;
            start_config(
                Some(candidate.clone()),
                123,
                &mut engine,
                &mut control,
                locations,
                &tx,
                &wake,
            )
            .unwrap();
            assert!(engine.config_pending);
            assert!(
                start_config(
                    Some(candidate),
                    124,
                    &mut engine,
                    &mut control,
                    locations,
                    &tx,
                    &wake
                )
                .is_err()
            );
            let Completion::Configured(id, result) =
                rx.recv_timeout(Duration::from_secs(3)).unwrap()
            else {
                panic!("wrong completion")
            };
            assert_eq!(id, 123);
            let response = finish_config(result, &mut engine, &mut control, &mut watcher);
            assert_eq!(response["ok"], true, "{response}");
            assert!(!engine.config_pending);
            assert_eq!(engine.config.interval_seconds, interval);
            assert_eq!(control.bytes, fs::read(&locations.config).unwrap());
            if unchanged {
                assert_eq!(engine.deadline, before_deadline);
                assert_eq!(control.revision, before_revision);
            } else {
                assert_ne!(engine.deadline, before_deadline);
                assert_eq!(control.revision, before_revision + 1);
            }
        }
        let deadline = engine.deadline;
        let revision = control.revision;
        fs::write(&locations.config, b"# external edit\n").unwrap();
        let candidate = engine.config.clone();
        start_config(
            Some(candidate),
            999,
            &mut engine,
            &mut control,
            locations,
            &tx,
            &wake,
        )
        .unwrap();
        let Completion::Configured(_, result) = rx.recv_timeout(Duration::from_secs(3)).unwrap()
        else {
            panic!("wrong completion")
        };
        let response = finish_config(result, &mut engine, &mut control, &mut watcher);
        assert_eq!(response["code"], "config_conflict");
        assert_eq!(engine.deadline, deadline);
        assert_eq!(control.revision, revision);
    }

    #[test]
    fn committed_uncertainty_installs_config_and_preserves_pause_and_protection() {
        let fixture = Fixture::new();
        let mut engine = engine(Instant::now());
        engine.state.paused = true;
        engine.suspend("protective reason".into());
        let mut control = Control::new(vec![]);
        let mut watcher = Watcher::new(&fixture.0.background, &[]).unwrap();
        let mut candidate = engine.config.clone();
        candidate.interval_seconds = 900;
        let prepared = PreparedConfig {
            config: candidate,
            bytes: vec![1],
            watcher: Watcher::new(&fixture.0.background, &[]).unwrap(),
            committed: true,
            durability: Some("directory sync failed".into()),
        };
        let response = finish_config(Ok(prepared), &mut engine, &mut control, &mut watcher);
        assert_eq!(response["ok"], true);
        assert_eq!(response["config_durability"], "directory sync failed");
        assert_eq!(engine.config.interval_seconds, 900);
        assert!(engine.state.paused);
        assert_eq!(
            engine.state.protective_suspension.as_deref(),
            Some("protective reason")
        );
        assert!(engine.deadline.is_none());
        assert!(engine.persistence_error.is_none());
        let noop = PreparedConfig {
            config: engine.config.clone(),
            bytes: control.bytes.clone(),
            watcher: Watcher::new(&fixture.0.background, &[]).unwrap(),
            committed: false,
            durability: None,
        };
        let revision = control.revision;
        let response = finish_config(Ok(noop), &mut engine, &mut control, &mut watcher);
        assert_eq!(response["config_durability"], "directory sync failed");
        assert_eq!(control.revision, revision);
    }

    #[test]
    fn external_change_resets_timer_and_preserves_user_pause() {
        let now = Instant::now();
        let mut engine = engine(now);
        let change = now + Duration::from_secs(290);
        assert!(engine.reconcile(Some("/manual.png".into()), change));
        assert_eq!(engine.deadline, Some(change + Duration::from_secs(300)));
        assert!(!engine.state.paused);
        engine.state.paused = true;
        engine.deadline = None;
        engine.reconcile(Some("/other.png".into()), change + Duration::from_secs(1));
        assert!(engine.state.paused);
        assert!(engine.deadline.is_none());
    }
    #[test]
    fn startup_never_restores_persisted_path_and_no_catchup() {
        let now = Instant::now();
        let state = State {
            version: 1,
            observed: Some("/stale.png".into()),
            requested: Some("/stale.png".into()),
            ..State::default()
        };
        let mut engine = Engine::new(Config::default(), state, Some("/desktop.png".into()), now);
        assert_eq!(engine.state.observed, Some("/desktop.png".into()));
        assert!(engine.state.requested.is_none());
        let wake = now + Duration::from_secs(86_400);
        engine.reset(wake);
        assert_eq!(engine.deadline, Some(wake + Duration::from_secs(300)));
    }
    #[test]
    fn startup_missing_background_requires_explicit_resume_and_preserves_pause() {
        for paused in [false, true] {
            let now = Instant::now();
            let state = State {
                version: 1,
                paused,
                observed: Some("/stale.png".into()),
                requested: Some("/stale.png".into()),
                ..State::default()
            };
            let mut engine = Engine::new(Config::default(), state, None, now);
            assert_eq!(engine.state.paused, paused);
            assert!(engine.state.observed.is_none());
            assert!(engine.state.requested.is_none());
            assert!(engine.state.protective_suspension.is_some());
            assert!(engine.deadline.is_none());
            assert!(!engine.reconcile(None, now));
            assert!(engine.reconcile(Some("/restored.png".into()), now));
            assert_eq!(engine.state.paused, paused);
            assert!(engine.state.protective_suspension.is_some());
            assert!(engine.deadline.is_none());
        }
    }

    #[test]
    fn unready_collection_rejects_selection_before_observing_or_spawning() {
        let fixture = Fixture::new();
        let mut engine = engine(Instant::now());
        engine.state.carousel.reconcile(&["/stale.png".into()]);
        let (tx, _) = mpsc::channel();
        let (wake, _) = UnixStream::pair().unwrap();
        for automatic in [false, true] {
            let error = apply(&mut engine, &fixture.0, false, automatic, &tx, &wake).unwrap_err();
            assert!(error.contains("collection scan"));
            assert!(engine.flight.is_none());
            assert_eq!(engine.state.observed, Some("/old.png".into()));
        }
    }

    #[test]
    fn finalization_joins_and_persists_successful_proposal_once_while_paused() {
        let fixture = Fixture::new();
        let mut engine = engine(Instant::now());
        engine.paths = vec!["/a.png".into(), "/b.png".into()];
        engine.state.carousel.reconcile(&engine.paths);
        engine.collection_ready = true;
        engine.state.paused = true;
        let mut proposal = engine.state.carousel.clone();
        let target = proposal.next(engine.state.observed.as_deref()).unwrap();
        std::os::unix::fs::symlink(&target, &fixture.0.background).unwrap();
        let mut expected = proposal.clone();
        expected.commit(target.clone());
        expected.reconcile(&engine.paths);
        engine.state.requested = Some(target.clone());
        engine.flight = Some(Flight {
            target: target.clone(),
            before: engine.state.observed.clone(),
            proposal,
            external_conflict: false,
            worker: thread::spawn(|| ApplyResult {
                success: true,
                message: "fake success".into(),
            }),
        });
        finish_apply(&mut engine, &fixture.0);
        finish_apply(&mut engine, &fixture.0);
        let saved = State::load(&fixture.0.state).unwrap();
        assert_eq!(saved.observed, Some(target));
        assert!(saved.requested.is_none());
        assert!(saved.last_result.unwrap().success);
        assert!(saved.paused);
        assert!(engine.flight.is_none());
        assert!(engine.deadline.is_none());
        assert_eq!(
            serde_json::to_value(saved.carousel).unwrap(),
            serde_json::to_value(expected).unwrap()
        );
    }

    #[test]
    fn watcher_handoff_preserves_pending_collection_events() {
        let fixture = Fixture::new();
        let root = fixture.0.state.parent().unwrap().join("collection");
        fs::create_dir(&root).unwrap();
        let mut watcher = Watcher::new(&fixture.0.background, std::slice::from_ref(&root)).unwrap();
        fs::write(root.join("changed.png"), b"fake image").unwrap();
        let pending = rearm_watcher(
            &mut watcher,
            &fixture.0.background,
            std::slice::from_ref(&root),
        )
        .unwrap();
        assert_ne!(pending & COLLECTION, 0);
        fs::write(root.join("another.png"), b"fake image").unwrap();
        assert_ne!(watcher.drain().unwrap() & COLLECTION, 0);
    }

    #[test]
    fn persistence_coalesces_slow_writes_and_flushes_the_latest_snapshot() {
        let fixture = Fixture::new();
        let mut engine = engine(Instant::now());
        let (tx, rx) = mpsc::channel();
        let (wake, _read) = UnixStream::pair().unwrap();
        let (started_tx, started_rx) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel();
        let (written_tx, written_rx) = mpsc::channel();
        let path = fixture.0.state.clone();
        let mut first = true;
        engine.persister = Some(
            Persister::spawn(
                move |state| {
                    if first {
                        first = false;
                        started_tx.send(()).unwrap();
                        release_rx.recv().unwrap();
                    }
                    state.save(&path)?;
                    written_tx.send(state.observed.clone()).unwrap();
                    Ok(())
                },
                tx,
                wake,
            )
            .unwrap(),
        );
        engine.save(&fixture.0);
        started_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        for index in 0..1000 {
            engine.state.observed = Some(format!("/latest-{index}.png").into());
            engine.save(&fixture.0);
        }
        // Status and enqueue remain usable while the worker is blocked in I/O.
        assert_eq!(engine.status(Instant::now())["persistence_pending"], true);
        assert!(rx.try_recv().is_err());
        assert!(engine.persister.as_ref().unwrap().active);
        assert_eq!(
            engine
                .persister
                .as_ref()
                .unwrap()
                .dirty
                .as_ref()
                .unwrap()
                .observed,
            engine.state.observed
        );
        release_tx.send(()).unwrap();
        engine.flush_persistence(&rx).unwrap();
        let written: Vec<_> = written_rx.try_iter().collect();
        assert_eq!(
            written,
            vec![Some("/old.png".into()), Some("/latest-999.png".into())]
        );
        assert_eq!(
            State::load(&fixture.0.state).unwrap().observed,
            engine.state.observed
        );
        assert_eq!(engine.status(Instant::now())["persistence_pending"], false);
        assert!(engine.persistence_error.is_none());
    }

    #[test]
    fn persistence_flush_failure_suspends_and_retains_the_previous_file() {
        let fixture = Fixture::new();
        let mut engine = engine(Instant::now());
        engine.state.save(&fixture.0.state).unwrap();
        let previous = fs::read(&fixture.0.state).unwrap();
        let (tx, rx) = mpsc::channel();
        let (wake, _read) = UnixStream::pair().unwrap();
        engine.persister =
            Some(Persister::spawn(|_| Err("simulated disk failure".into()), tx, wake).unwrap());
        engine.state.paused = true;
        engine.save(&fixture.0);
        let error = engine.flush_persistence(&rx).unwrap_err();
        assert!(error.contains("simulated disk failure"));
        assert!(engine.state.protective_suspension.is_some());
        assert!(engine.deadline.is_none());
        assert_eq!(
            engine.status(Instant::now())["last_persistence_error"],
            error
        );
        assert_eq!(fs::read(&fixture.0.state).unwrap(), previous);
    }

    #[test]
    fn failed_write_without_dirty_successor_persists_suspension_once() {
        let fixture = Fixture::new();
        let mut engine = engine(Instant::now());
        let (tx, rx) = mpsc::channel();
        let (wake, _read) = UnixStream::pair().unwrap();
        let (written_tx, written_rx) = mpsc::channel();
        let path = fixture.0.state.clone();
        let mut attempts = 0;
        engine.persister = Some(
            Persister::spawn(
                move |state| {
                    attempts += 1;
                    written_tx
                        .send(state.protective_suspension.clone())
                        .unwrap();
                    if attempts == 1 {
                        Err("transient disk failure".into())
                    } else {
                        state.save(&path)
                    }
                },
                tx,
                wake,
            )
            .unwrap(),
        );
        engine.save(&fixture.0);
        let Completion::Persisted(result) = rx.recv_timeout(Duration::from_secs(2)).unwrap() else {
            panic!()
        };
        assert!(engine.persister.as_ref().unwrap().dirty.is_none());
        engine.persisted(result);
        assert!(engine.persister.as_ref().unwrap().pending());
        assert!(engine.deadline.is_none());
        assert!(engine.persistence_error.is_some());
        engine.flush_persistence(&rx).unwrap();
        let written: Vec<_> = written_rx.try_iter().collect();
        assert_eq!(written.len(), 2);
        assert!(written[0].is_none());
        assert!(
            written[1]
                .as_ref()
                .unwrap()
                .contains("transient disk failure")
        );
        let saved = State::load(&fixture.0.state).unwrap();
        assert_eq!(
            saved.protective_suspension,
            engine.state.protective_suspension
        );
        let restarted = Engine::new(
            engine.config.clone(),
            saved,
            Some("/old.png".into()),
            Instant::now(),
        );
        assert!(restarted.deadline.is_none());
        assert!(engine.persistence_error.is_none());
    }

    #[test]
    fn repeated_failure_without_dirty_successor_stops_after_one_followup() {
        let fixture = Fixture::new();
        let mut engine = engine(Instant::now());
        let (tx, rx) = mpsc::channel();
        let (wake, _read) = UnixStream::pair().unwrap();
        let attempts = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let written = attempts.clone();
        engine.persister = Some(
            Persister::spawn(
                move |_| {
                    written.fetch_add(1, Ordering::SeqCst);
                    Err("persistent disk failure".into())
                },
                tx,
                wake,
            )
            .unwrap(),
        );
        engine.save(&fixture.0);
        assert!(engine.flush_persistence(&rx).is_err());
        assert_eq!(attempts.load(Ordering::SeqCst), 2);
        assert!(rx.try_recv().is_err());
        assert!(engine.state.protective_suspension.is_some());
        assert!(engine.deadline.is_none());
    }

    #[test]
    fn persistence_recovery_preserves_intervening_dirty_and_later_coalesced_saves() {
        let fixture = Fixture::new();
        let mut engine = engine(Instant::now());
        let (tx, rx) = mpsc::channel();
        let (wake, _read) = UnixStream::pair().unwrap();
        let (written_tx, written_rx) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel();
        let path = fixture.0.state.clone();
        let mut attempts = 0;
        engine.persister = Some(
            Persister::spawn(
                move |state| {
                    attempts += 1;
                    written_tx.send(state.clone()).unwrap();
                    release_rx.recv_timeout(Duration::from_secs(3)).unwrap();
                    if attempts == 1 {
                        Err("transient disk failure".into())
                    } else {
                        state.save(&path)
                    }
                },
                tx,
                wake,
            )
            .unwrap(),
        );
        engine.save(&fixture.0);
        let initial = written_rx.recv_timeout(Duration::from_secs(3)).unwrap();
        assert!(initial.protective_suspension.is_none());
        // Queue a successor before processing the first failed completion.
        engine.state.paused = true;
        engine.state.observed = Some("/dirty-before-failure.png".into());
        engine.save(&fixture.0);
        release_tx.send(()).unwrap();
        let Completion::Persisted(result) = rx.recv_timeout(Duration::from_secs(3)).unwrap() else {
            panic!("wrong completion")
        };
        assert!(result.is_err());
        engine.persisted(result);
        let recovery = written_rx.recv_timeout(Duration::from_secs(3)).unwrap();
        assert!(recovery.paused);
        assert_eq!(recovery.observed, engine.state.observed);
        let suspension = recovery.protective_suspension.clone();
        assert!(
            suspension
                .as_ref()
                .unwrap()
                .contains("transient disk failure")
        );
        assert!(engine.persistence_error.is_some());
        assert!(engine.persister.as_ref().unwrap().recovering);
        assert!(engine.deadline.is_none());

        // Independent saves during recovery must coalesce to the newest state.
        for image in ["/later-discarded.png", "/later-latest.png"] {
            engine.state.observed = Some(image.into());
            engine.save(&fixture.0);
        }
        release_tx.send(()).unwrap();
        let Completion::Persisted(result) = rx.recv_timeout(Duration::from_secs(3)).unwrap() else {
            panic!("wrong completion")
        };
        assert!(result.is_ok());
        engine.persisted(result);
        assert!(engine.persistence_error.is_none());
        assert!(engine.persister.as_ref().unwrap().pending());
        assert!(!engine.persister.as_ref().unwrap().recovering);
        assert_eq!(engine.state.protective_suspension, suspension);
        assert_eq!(
            State::load(&fixture.0.state).unwrap().observed,
            recovery.observed
        );
        let latest = written_rx.recv_timeout(Duration::from_secs(3)).unwrap();
        assert_eq!(latest.observed, Some("/later-latest.png".into()));
        assert!(latest.paused);
        assert_eq!(latest.protective_suspension, suspension);
        release_tx.send(()).unwrap();
        engine.flush_persistence(&rx).unwrap();
        assert!(written_rx.try_recv().is_err()); // exactly three writes
        assert_eq!(engine.status(Instant::now())["persistence_pending"], false);
        assert!(engine.persistence_error.is_none());
        let saved = State::load(&fixture.0.state).unwrap();
        assert_eq!(saved.observed, latest.observed);
        assert!(saved.paused);
        assert_eq!(saved.protective_suspension, suspension);
        let restarted = Engine::new(
            engine.config.clone(),
            saved,
            latest.observed,
            Instant::now(),
        );
        assert!(restarted.deadline.is_none());
        assert_eq!(restarted.state.protective_suspension, suspension);
    }

    #[test]
    fn persistence_failure_updates_dirty_snapshot_without_unbounded_retries() {
        let fixture = Fixture::new();
        let mut engine = engine(Instant::now());
        let (tx, rx) = mpsc::channel();
        let (wake, _read) = UnixStream::pair().unwrap();
        engine.persister =
            Some(Persister::spawn(|_| Err("persistent disk failure".into()), tx, wake).unwrap());
        engine.save(&fixture.0);
        engine.state.paused = true;
        engine.save(&fixture.0);
        assert!(engine.flush_persistence(&rx).is_err());
        assert!(engine.state.paused);
        assert!(engine.state.protective_suspension.is_some());
        assert!(!fixture.0.state.exists());
    }

    #[test]
    fn repeated_scan_requests_coalesce_without_invalidating_ready_collection() {
        let mut engine = engine(Instant::now());
        engine.collection_ready = true;
        engine.scanning = true;
        for _ in 0..100 {
            request_scan(&mut engine);
        }
        assert!(engine.scanning);
        assert!(engine.scan_dirty);
        assert!(engine.collection_ready);
        assert_eq!(engine.generation, 100);
    }

    #[test]
    fn missing_background_is_protective_not_a_user_pause() {
        let mut engine = engine(Instant::now());
        engine.reconcile(None, Instant::now());
        assert!(!engine.state.paused);
        assert!(engine.deadline.is_none());
        assert!(engine.state.protective_suspension.is_some());
    }
}
