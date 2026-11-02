//! Runs the wallpaper command without a shell and without interpreting the image.
//!
//! Limits and ownership:
//! - The caller owns reconciliation; this module only reports the command outcome.
//! - The image is canonicalized and checked as a regular file immediately before
//!   spawning. This does not prevent another process from replacing it between
//!   that check and the command opening it (the command accepts a path, not an fd).
//! - stdout and stderr share a socket. Only the first `DIAGNOSTIC_LIMIT` bytes are
//!   retained; excess output is drained in bounded batches so it cannot starve
//!   timeout checks. Stream attribution and exact cross-stream ordering are lost.
//! - Each invocation has a new process group. On every exit path after spawning,
//!   that group is killed, including when the direct child exits successfully.
//!   `waitid(WNOWAIT)` keeps the leader's PID reserved until group cleanup, avoiding
//!   signaling a recycled group ID. The caller must not independently reap this
//!   module's child (including through a process-wide SIGCHLD handler).
//! - This is cleanup, not a sandbox: descendants that change process group/session
//!   can escape. Inherited pipes held by such descendants never delay our return.
//! - The timeout bounds the active-command loop, not filesystem calls, spawn, or
//!   the final blocking reap after SIGKILL. An uninterruptible kernel wait can
//!   exceed it. Polling adds at most one small scheduling interval in normal use.
//! - Linux/Unix process and socket APIs are required. No image decoding or actual
//!   desktop integration is performed here or in the tests.

use serde::{Deserialize, Serialize};
use std::fs;
use std::io::{self, Read};
use std::os::fd::{AsRawFd, OwnedFd};
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::time::{Duration, Instant};

const DIAGNOSTIC_LIMIT: usize = 16 * 1024;
const DRAIN_BUDGET: usize = 64 * 1024;
const WAIT_INTERVAL: Duration = Duration::from_millis(10);

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ApplyResult {
    pub success: bool,
    pub message: String,
}

/// Invoke `program theme bg set <canonical-image>` with bounded diagnostics.
///
/// Success requires a zero exit status and successful process-group cleanup.
/// Timeout is always a failure, even if the child exits during timeout cleanup.
pub fn apply(program: &Path, image: &Path, timeout: Duration) -> ApplyResult {
    let image = match fs::canonicalize(image) {
        Ok(path) => path,
        Err(error) => return failure(format!("Cannot resolve image path: {error}")),
    };
    match fs::metadata(&image) {
        Ok(metadata) if metadata.is_file() => {}
        Ok(_) => return failure("Image path is not a regular file.".into()),
        Err(error) => return failure(format!("Cannot inspect image file: {error}")),
    }

    let (mut reader, writer) = match UnixStream::pair() {
        Ok(pair) => pair,
        Err(error) => return failure(format!("Cannot create diagnostic socket: {error}")),
    };
    if let Err(error) = reader.set_nonblocking(true) {
        return failure(format!("Cannot configure diagnostic socket: {error}"));
    }
    let stderr = match writer.try_clone() {
        Ok(writer) => writer,
        Err(error) => return failure(format!("Cannot duplicate diagnostic socket: {error}")),
    };

    let started = Instant::now();
    let mut command = Command::new(program);
    command
        .args(["theme", "bg", "set"])
        .arg(&image)
        .stdin(Stdio::null())
        .stdout(Stdio::from(OwnedFd::from(writer)))
        .stderr(Stdio::from(OwnedFd::from(stderr)))
        .process_group(0);
    let child = match command.spawn() {
        Ok(child) => child,
        Err(error) => return failure(format!("Cannot start wallpaper command: {error}")),
    };
    // Command retains its configured output descriptors after spawn.
    drop(command);
    let mut child = RunningChild(Some(child));
    let mut diagnostics = Diagnostics::default();
    let outcome = loop {
        if started.elapsed() >= timeout {
            break Err(format!("Wallpaper command timed out after {timeout:?}."));
        }
        match child.exited() {
            Ok(true) => break Ok(()),
            Ok(false) => {}
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => break Err(format!("Cannot observe wallpaper command: {error}")),
        }
        if let Err(error) = diagnostics.drain(&mut reader) {
            break Err(format!("Cannot read command diagnostics: {error}"));
        }
        let remaining = timeout.saturating_sub(started.elapsed());
        if let Err(error) = wait_for_output(&reader, diagnostics.eof, remaining.min(WAIT_INTERVAL))
        {
            break Err(format!("Cannot poll command diagnostics: {error}"));
        }
    };

    // Kill before reaping: even a successful child may leave descendants behind.
    let status = child.finish();
    // Never wait for EOF: a descendant may have escaped our process group while
    // retaining the socket. One bounded final drain captures queued diagnostics.
    let drain = diagnostics.drain(&mut reader);
    let (success, mut message) = match (outcome, status) {
        (Err(reason), status) => {
            let mut reason = reason;
            if let Err(error) = status {
                reason.push_str(&format!(" Cleanup failed: {error}"));
            }
            (false, reason)
        }
        (Ok(()), Err(error)) => (false, format!("Command cleanup failed: {error}")),
        (Ok(()), Ok(status)) if status.success() => (true, "Wallpaper command succeeded.".into()),
        (Ok(()), Ok(status)) => (false, format!("Wallpaper command failed: {status}.")),
    };
    let success = if let Err(error) = drain {
        message.push_str(&format!(" Cannot read final diagnostics: {error}"));
        false
    } else {
        success
    };
    diagnostics.append_to(&mut message);
    ApplyResult { success, message }
}

fn failure(message: String) -> ApplyResult {
    ApplyResult {
        success: false,
        message,
    }
}

/// Owns the unreaped group leader; no signal may be sent after `finish` reaps it.
struct RunningChild(Option<Child>);

impl RunningChild {
    fn exited(&self) -> io::Result<bool> {
        let child = self.0.as_ref().expect("child has not been reaped");
        // SAFETY: zero is a valid initial siginfo_t and waitid initializes it.
        let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
        // SAFETY: info is writable, and the PID belongs to our unreaped child.
        let result = unsafe {
            libc::waitid(
                libc::P_PID,
                child.id() as libc::id_t,
                &mut info,
                libc::WEXITED | libc::WNOHANG | libc::WNOWAIT,
            )
        };
        if result == -1 {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: waitid with WEXITED populates the SIGCHLD fields, or leaves
        // si_pid zero when WNOHANG finds no waitable child.
        Ok(unsafe { info.si_pid() } != 0)
    }

    fn finish(&mut self) -> io::Result<ExitStatus> {
        let mut child = self.0.take().expect("child has not been reaped");
        // SAFETY: process_group(0) made the child the group leader. Its PID has
        // not been reaped, so a negative PID cannot target a recycled group.
        let killed = unsafe { libc::kill(-(child.id() as libc::pid_t), libc::SIGKILL) };
        let group_error = if killed == -1 {
            let error = io::Error::last_os_error();
            (error.raw_os_error() != Some(libc::ESRCH)).then_some(error)
        } else {
            None
        };
        // Also target the direct child in case it changed its own process group.
        // Failure here is normal for a child which has already exited.
        let _ = child.kill();
        let status = child.wait()?;
        match group_error {
            Some(error) => Err(error),
            None => Ok(status),
        }
    }
}

impl Drop for RunningChild {
    fn drop(&mut self) {
        if self.0.is_some() {
            let _ = self.finish();
        }
    }
}

#[derive(Default)]
struct Diagnostics {
    bytes: Vec<u8>,
    truncated: bool,
    eof: bool,
}

impl Diagnostics {
    fn drain(&mut self, reader: &mut UnixStream) -> io::Result<()> {
        let mut buffer = [0_u8; 4096];
        // Bound attempts as well as bytes: repeated EINTR must not starve checks.
        for _ in 0..DRAIN_BUDGET / buffer.len() {
            match reader.read(&mut buffer) {
                Ok(0) => {
                    self.eof = true;
                    break;
                }
                Ok(count) => {
                    let keep = count.min(DIAGNOSTIC_LIMIT - self.bytes.len());
                    self.bytes.extend_from_slice(&buffer[..keep]);
                    self.truncated |= keep < count;
                }
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => break,
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) => return Err(error),
            }
        }
        Ok(())
    }

    fn append_to(&self, message: &mut String) {
        if !self.bytes.is_empty() {
            message.push_str("\nCommand output (stdout/stderr):\n");
            message.push_str(&String::from_utf8_lossy(&self.bytes));
        }
        if self.truncated {
            message.push_str("\n[diagnostics truncated]");
        }
    }
}

fn wait_for_output(reader: &UnixStream, eof: bool, duration: Duration) -> io::Result<()> {
    if eof {
        // POLLHUP is level-triggered; polling an EOF socket would spin.
        std::thread::sleep(duration);
        return Ok(());
    }
    let mut descriptor = libc::pollfd {
        fd: reader.as_raw_fd(),
        events: libc::POLLIN,
        revents: 0,
    };
    // Round up sub-millisecond waits; never poll indefinitely.
    let millis =
        duration.as_millis() + u128::from(!duration.subsec_nanos().is_multiple_of(1_000_000));
    // SAFETY: descriptor is a valid single pollfd for the lifetime of this call.
    let result = unsafe { libc::poll(&mut descriptor, 1, millis.min(i32::MAX as u128) as i32) };
    if result == -1 {
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(error);
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{PermissionsExt, symlink};
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT_TEMP: AtomicU64 = AtomicU64::new(0);

    struct Fixture(PathBuf);

    impl Fixture {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "swiftwall-backend-{}-{}-{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_nanos(),
                NEXT_TEMP.fetch_add(1, Ordering::Relaxed),
            ));
            fs::create_dir(&path).unwrap();
            Self(path)
        }

        fn image(&self) -> PathBuf {
            let path = self.0.join("image file");
            // Deliberately not a decodable image: decoding belongs elsewhere.
            fs::write(&path, b"not an image").unwrap();
            path
        }

        fn script(&self, body: &str) -> PathBuf {
            let path = self.0.join("fake command ; ' $ executable");
            fs::write(&path, format!("#!/bin/sh\n{body}\n")).unwrap();
            fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
            path
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn success_and_null_stdin() {
        let f = Fixture::new();
        let program = f.script(
            "if read -r line; then exit 12; fi\nprintf 'ok stdout\\n'\nprintf 'ok stderr\\n' >&2",
        );
        let result = apply(&program, &f.image(), Duration::from_secs(2));
        assert!(result.success, "{}", result.message);
        assert!(result.message.contains("ok stdout"));
        assert!(result.message.contains("ok stderr"));
    }

    #[test]
    fn nonzero_exit_preserves_diagnostics() {
        let f = Fixture::new();
        let program = f.script("printf 'failure details' >&2\nexit 23");
        let result = apply(&program, &f.image(), Duration::from_secs(2));
        assert!(!result.success);
        assert!(result.message.contains("23"), "{}", result.message);
        assert!(result.message.contains("failure details"));
    }

    #[test]
    fn passes_exact_arguments_and_canonical_path_without_shell_expansion() {
        let f = Fixture::new();
        let image = f.0.join("- ' \" ; $(touch SHOULD_NOT_EXIST) `false`\n.png");
        fs::write(&image, b"image").unwrap();
        let link = f.0.join("image symlink");
        symlink(&image, &link).unwrap();
        let program = f.script("[ \"$#\" -eq 4 ] || exit 21\n[ \"$1\" = theme ] || exit 22\n[ \"$2\" = bg ] || exit 23\n[ \"$3\" = set ] || exit 24\nprintf '%s' \"$4\"");
        let result = apply(&program, &link, Duration::from_secs(2));
        assert!(result.success, "{}", result.message);
        assert!(
            result
                .message
                .ends_with(fs::canonicalize(&image).unwrap().to_str().unwrap())
        );
    }

    #[test]
    fn non_utf8_image_path_is_passed_losslessly() {
        use std::os::unix::ffi::OsStrExt;
        let f = Fixture::new();
        let image = f.0.join(std::ffi::OsStr::from_bytes(b"image-\xff.png"));
        fs::write(&image, b"image").unwrap();
        // Shell compares the received bytes with a known octal-escaped filename.
        let program = f.script("expected=\"$(printf 'image-\\377.png')\"\n[ \"${4##*/}\" = \"$expected\" ] || exit 17\n[ -f \"$4\" ]");
        let result = apply(&program, &image, Duration::from_secs(2));
        assert!(result.success, "{}", result.message);
    }

    #[test]
    fn signal_exit_and_invalid_utf8_diagnostics_are_reported() {
        let f = Fixture::new();
        let program = f.script("printf '\\377diagnostic' >&2\nkill -TERM \"$$\"");
        let result = apply(&program, &f.image(), Duration::from_secs(2));
        assert!(!result.success);
        assert!(result.message.contains("signal"), "{}", result.message);
        assert!(result.message.contains("\u{fffd}diagnostic"));
    }

    #[test]
    fn rejects_non_regular_and_missing_images_before_spawning() {
        let f = Fixture::new();
        let missing_program = f.0.join("must not run");
        for image in [f.0.clone(), f.0.join("missing")] {
            let result = apply(&missing_program, &image, Duration::from_secs(1));
            assert!(!result.success);
            assert!(
                !result.message.contains("Cannot start"),
                "{}",
                result.message
            );
        }
        let fifo = f.0.join("fifo");
        use std::os::unix::ffi::OsStrExt;
        let name = std::ffi::CString::new(fifo.as_os_str().as_bytes()).unwrap();
        // SAFETY: name is a valid NUL-terminated pathname.
        assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
        let result = apply(&missing_program, &fifo, Duration::from_secs(1));
        assert!(!result.success);
        assert!(result.message.contains("not a regular file"));
    }

    #[test]
    fn reports_spawn_failure() {
        let f = Fixture::new();
        let result = apply(&f.0.join("missing"), &f.image(), Duration::from_secs(1));
        assert!(!result.success);
        assert!(result.message.contains("Cannot start"));
    }

    #[test]
    fn drains_flooded_stdout_and_stderr_with_bounded_retention() {
        let f = Fixture::new();
        let program = f.script("i=0\nwhile [ \"$i\" -lt 20000 ]; do\n printf 'stdout-012345678901234567890123456789\\n'\n printf 'stderr-012345678901234567890123456789\\n' >&2\n i=$((i + 1))\ndone");
        let result = apply(&program, &f.image(), Duration::from_secs(10));
        assert!(result.success, "{}", result.message);
        assert!(result.message.contains("stdout-"));
        assert!(result.message.contains("stderr-"));
        assert!(result.message.contains("[diagnostics truncated]"));
        assert!(result.message.len() < DIAGNOSTIC_LIMIT + 256);
    }

    #[test]
    fn continuous_output_cannot_starve_timeout() {
        let f = Fixture::new();
        let program =
            f.script("while :; do printf 'endless output\\n'; printf 'errors\\n' >&2; done");
        let start = Instant::now();
        let result = apply(&program, &f.image(), Duration::from_millis(150));
        assert!(!result.success);
        assert!(result.message.contains("timed out"));
        assert!(start.elapsed() < Duration::from_secs(5));
        assert!(result.message.len() < DIAGNOSTIC_LIMIT + 256);
    }

    #[test]
    fn closed_output_does_not_end_command_early() {
        let f = Fixture::new();
        let program = f.script("exec 1>&- 2>&-\nwhile :; do :; done");
        let result = apply(&program, &f.image(), Duration::from_millis(100));
        assert!(!result.success);
        assert!(result.message.contains("timed out"));
    }

    #[test]
    fn zero_timeout_is_failure() {
        let f = Fixture::new();
        let result = apply(&f.script("exit 0"), &f.image(), Duration::ZERO);
        assert!(!result.success);
        assert!(result.message.contains("timed out"));
    }

    // Linux /proc distinguishes killed-but-not-yet-reaped descendants from live
    // processes. We cannot reap grandchildren; their new parent owns that duty.
    #[cfg(target_os = "linux")]
    fn assert_dead(pid: i32) {
        let start = Instant::now();
        loop {
            match fs::read_to_string(format!("/proc/{pid}/stat")) {
                Err(error) if error.kind() == io::ErrorKind::NotFound => return,
                Ok(stat)
                    if stat.rsplit_once(") ").is_some_and(|(_, fields)| {
                        fields.starts_with('Z') || fields.starts_with('X')
                    }) =>
                {
                    return;
                }
                _ => {}
            }
            assert!(
                start.elapsed() < Duration::from_secs(2),
                "process {pid} remains alive"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn timeout_kills_descendants_and_reaps_direct_child() {
        let f = Fixture::new();
        let program = f.script(
            "printf 'leader=%s\\n' \"$$\"\nsleep 30 &\nprintf 'descendant=%s\\n' \"$!\"\nwait",
        );
        let result = apply(&program, &f.image(), Duration::from_millis(300));
        assert!(!result.success);
        assert!(result.message.contains("timed out"));
        for label in ["leader=", "descendant="] {
            let pid: i32 = result
                .message
                .lines()
                .find_map(|line| line.strip_prefix(label))
                .unwrap()
                .parse()
                .unwrap();
            assert_dead(pid);
            if label == "leader=" {
                // SAFETY: WNOHANG does not block; a null status pointer is valid.
                assert_eq!(
                    unsafe { libc::waitpid(pid, std::ptr::null_mut(), libc::WNOHANG) },
                    -1
                );
                assert_eq!(
                    io::Error::last_os_error().raw_os_error(),
                    Some(libc::ECHILD)
                );
            }
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn direct_child_exit_cleans_descendant_holding_output_open() {
        let f = Fixture::new();
        let program = f.script("sleep 30 &\nprintf 'descendant=%s\\n' \"$!\"\nexit 0");
        let start = Instant::now();
        let result = apply(&program, &f.image(), Duration::from_secs(5));
        assert!(result.success, "{}", result.message);
        assert!(start.elapsed() < Duration::from_secs(2));
        let pid: i32 = result
            .message
            .lines()
            .find_map(|line| line.strip_prefix("descendant="))
            .unwrap()
            .parse()
            .unwrap();
        assert_dead(pid);
    }
}
