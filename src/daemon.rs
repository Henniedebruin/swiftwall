use crate::backend::{self, ApplyResult};
use crate::carousel::Carousel;
use crate::collection::{self, Fingerprint, ScanResult};
use crate::config::{Config, Locations};
use crate::runtime::{self, BACKGROUND, COLLECTION, Instance, Signals, Watcher};
use crate::state::State;
use serde_json::{Value, json};
use std::collections::HashMap;
use std::fs;
use std::io::{self, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Receiver, Sender, SyncSender};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const DEBOUNCE: Duration = Duration::from_millis(200);
const COMMAND_TIMEOUT: Duration = Duration::from_secs(10);

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
        let failed = result.is_err();
        match result {
            Ok(()) => self.persistence_error = None,
            Err(e) => self.persistence_failed(e),
        }
        if let Some(snapshot) = dirty {
            // Preserve the latest state, including a newly imposed suspension,
            // without automatically retrying forever on an unwritable filesystem.
            let snapshot = if failed { self.state.clone() } else { snapshot };
            if let Err(e) = self.persister.as_mut().unwrap().submit(snapshot) {
                self.persistence_failed(e);
            }
        }
    }

    fn flush_persistence(&mut self, completions: &Receiver<Completion>) -> Result<(), String> {
        while self.persister.as_ref().is_some_and(Persister::pending) {
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

struct Peer {
    stream: UnixStream,
    input: Vec<u8>,
    output: Vec<u8>,
    sent: usize,
    expires: Instant,
}
impl Peer {
    fn new(stream: UnixStream) -> io::Result<Self> {
        stream.set_nonblocking(true)?;
        Ok(Self {
            stream,
            input: vec![],
            output: vec![],
            sent: 0,
            expires: Instant::now() + Duration::from_secs(2),
        })
    }
}

pub fn run(locations: Locations) -> Result<(), String> {
    let config = Config::load(&locations.config)?;
    let instance = Instance::acquire(&locations.runtime)?;
    let signals = Signals::new()?;
    let state = State::load(&locations.state)?;
    let initial = observe(&locations.background)?;
    let mut engine = Engine::new(config, state, initial, Instant::now());
    let mut watcher = Watcher::new(&locations.background, &engine.config.folders)?;
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
    let mut scan_at: Option<Instant> = None;
    let mut background_at: Option<Instant> = None;
    let outcome = (|| -> Result<(), String> {
        loop {
            if !engine.scanning && engine.scan_dirty {
                engine.watch_events |=
                    rearm_watcher(&mut watcher, &locations.background, &engine.config.folders)?;
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
            let now = Instant::now();
            let rotation =
                if engine.flight.is_none() && engine.collection_ready && !engine.paths.is_empty() {
                    engine.deadline
                } else {
                    None
                };
            let next = [
                rotation,
                scan_at,
                background_at,
                peers.iter().map(|p| p.expires).min(),
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
                    if p.output.is_empty() {
                        libc::POLLIN
                    } else {
                        libc::POLLOUT
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
                }
            }
            // Process existing peers before accepting new ones: fds corresponds to the old vector.
            for (index, peer) in peers.iter_mut().enumerate() {
                let ready = fds[index + 4].revents;
                if peer.output.is_empty() && ready & (libc::POLLIN | libc::POLLHUP) != 0 {
                    let mut buffer = [0u8; 512];
                    match peer.stream.read(&mut buffer) {
                        Ok(0) => peer.expires = Instant::now(),
                        Ok(n) => {
                            peer.input.extend_from_slice(&buffer[..n]);
                            if let Some(end) = peer.input.iter().position(|b| *b == b'\n') {
                                let response = match std::str::from_utf8(&peer.input[..end]) {
                                    Ok(command) => command_response(
                                        command,
                                        &mut engine,
                                        &locations,
                                        &complete_tx,
                                        &wake_write,
                                        &mut watcher,
                                    ),
                                    Err(_) => json!({"ok":false,"error":"Command must be UTF-8"}),
                                };
                                peer.output =
                                    serde_json::to_vec(&response).map_err(|e| e.to_string())?;
                                peer.output.push(b'\n');
                            } else if peer.input.len() > 4096 {
                                peer.expires = Instant::now();
                            }
                        }
                        Err(e) if e.kind() == io::ErrorKind::WouldBlock => {}
                        Err(_) => peer.expires = Instant::now(),
                    }
                }
                if !peer.output.is_empty() {
                    match peer.stream.write(&peer.output[peer.sent..]) {
                        Ok(n) => {
                            peer.sent += n;
                            if peer.sent == peer.output.len() {
                                peer.expires = Instant::now();
                            }
                        }
                        Err(e) if e.kind() == io::ErrorKind::WouldBlock => {}
                        Err(_) => peer.expires = Instant::now(),
                    }
                }
            }
            peers.retain(|p| p.expires > Instant::now());
            if fds[0].revents != 0 {
                for _ in 0..64 {
                    match instance.listener.accept() {
                        Ok((stream, _)) if peers.len() < 64 => {
                            if let Ok(peer) = Peer::new(stream) {
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
            if engine.flight.is_none()
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
    // Both signal and error exits retain the singleton lock until backend cleanup
    // and the same transaction finalization used by normal completion have finished.
    finish_apply(&mut engine, &locations);
    engine.save(&locations);
    let persistence = engine.flush_persistence(&complete_rx);
    drop(instance);
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
    watcher: &mut Watcher,
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
        "reload" => {
            if engine.flight.is_some() {
                return Err("Cannot reload during a wallpaper application".into());
            }
            let config = Config::load(&locations.config)?;
            engine.watch_events |= rearm_watcher(watcher, &locations.background, &config.folders)?;
            engine.config = config;
            engine.collection_ready = false;
            engine.reset(Instant::now());
            request_scan(engine);
            Ok("configuration reloaded")
        }
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

pub fn client(locations: &Locations, command: &str) -> Result<(), String> {
    if ![
        "next", "previous", "pause", "resume", "status", "rescan", "reload",
    ]
    .contains(&command)
    {
        return Err("Unknown command; use --help".into());
    }
    let mut stream = UnixStream::connect(locations.runtime.join("control.sock"))
        .map_err(|e| format!("Daemon unavailable: {e}"))?;
    stream
        .set_read_timeout(Some(Duration::from_secs(3)))
        .map_err(|e| e.to_string())?;
    stream
        .set_write_timeout(Some(Duration::from_secs(3)))
        .map_err(|e| e.to_string())?;
    writeln!(stream, "{command}").map_err(|e| e.to_string())?;
    let mut bytes = Vec::new();
    stream
        .take(1024 * 1024)
        .read_to_end(&mut bytes)
        .map_err(|e| e.to_string())?;
    let response: Value = serde_json::from_slice(&bytes).map_err(|e| e.to_string())?;
    println!(
        "{}",
        serde_json::to_string_pretty(&response).map_err(|e| e.to_string())?
    );
    if response["ok"] == true {
        Ok(())
    } else {
        Err(response["error"]
            .as_str()
            .unwrap_or("Request completed in protective suspension")
            .into())
    }
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
