//! Linux-only readiness primitives. No polling thread or periodic wakeup.
use std::collections::HashMap;
use std::ffi::CString;
use std::fs::{self, File, OpenOptions};
use std::io;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::net::UnixListener;
use std::path::{Path, PathBuf};

pub struct Instance {
    pub listener: UnixListener,
    _lock: File,
    socket: PathBuf,
}
impl Instance {
    pub fn acquire(dir: &Path) -> Result<Self, String> {
        match fs::symlink_metadata(dir) {
            Ok(m) if !m.is_dir() || m.uid() != unsafe { libc::geteuid() } => {
                return Err("Runtime directory is not an owned directory".into());
            }
            Ok(_) => {}
            Err(e) if e.kind() == io::ErrorKind::NotFound => {
                fs::DirBuilder::new()
                    .recursive(true)
                    .create(dir)
                    .map_err(|e| e.to_string())?;
            }
            Err(e) => return Err(e.to_string()),
        }
        fs::set_permissions(dir, fs::Permissions::from_mode(0o700)).map_err(|e| e.to_string())?;
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(dir.join("daemon.lock"))
            .map_err(|e| e.to_string())?;
        lock.try_lock()
            .map_err(|_| "Another SwiftWall daemon holds the singleton lock".to_string())?;
        let socket = dir.join("control.sock");
        match fs::remove_file(&socket) {
            Ok(()) => {}
            Err(e) if e.kind() == io::ErrorKind::NotFound => {}
            Err(e) => return Err(e.to_string()),
        }
        let listener = UnixListener::bind(&socket).map_err(|e| e.to_string())?;
        listener.set_nonblocking(true).map_err(|e| e.to_string())?;
        Ok(Self {
            listener,
            _lock: lock,
            socket,
        })
    }
}
impl Drop for Instance {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.socket);
    }
}

pub struct Signals(pub OwnedFd);
impl Signals {
    pub fn new() -> Result<Self, String> {
        // Block before spawning workers: they inherit the mask. signalfd wakes poll.
        unsafe {
            let mut mask: libc::sigset_t = std::mem::zeroed();
            libc::sigemptyset(&mut mask);
            libc::sigaddset(&mut mask, libc::SIGTERM);
            libc::sigaddset(&mut mask, libc::SIGINT);
            let rc = libc::pthread_sigmask(libc::SIG_BLOCK, &mask, std::ptr::null_mut());
            if rc != 0 {
                return Err(io::Error::from_raw_os_error(rc).to_string());
            }
            let fd = libc::signalfd(-1, &mask, libc::SFD_CLOEXEC | libc::SFD_NONBLOCK);
            if fd < 0 {
                return Err(io::Error::last_os_error().to_string());
            }
            Ok(Self(OwnedFd::from_raw_fd(fd)))
        }
    }
}

pub struct Watcher {
    pub fd: OwnedFd,
    kinds: HashMap<i32, u8>,
}
pub const BACKGROUND: u8 = 1;
pub const COLLECTION: u8 = 2;
impl Watcher {
    pub fn new(background: &Path, roots: &[PathBuf]) -> Result<Self, String> {
        let fd = unsafe { libc::inotify_init1(libc::IN_NONBLOCK | libc::IN_CLOEXEC) };
        if fd < 0 {
            return Err(io::Error::last_os_error().to_string());
        }
        let mut result = Self {
            fd: unsafe { OwnedFd::from_raw_fd(fd) },
            kinds: HashMap::new(),
        };
        result.add_nearest(background.parent().unwrap_or(Path::new("/")), BACKGROUND)?;
        for root in roots {
            result.add_nearest(root, COLLECTION)?;
        }
        Ok(result)
    }
    fn add_nearest(&mut self, path: &Path, kind: u8) -> Result<(), String> {
        let mut path = path;
        loop {
            let name = CString::new(path.as_os_str().as_bytes()).map_err(|e| e.to_string())?;
            let mask = libc::IN_CREATE
                | libc::IN_DELETE
                | libc::IN_MOVED_FROM
                | libc::IN_MOVED_TO
                | libc::IN_CLOSE_WRITE
                | libc::IN_ATTRIB
                | libc::IN_DELETE_SELF
                | libc::IN_MOVE_SELF
                | libc::IN_ONLYDIR;
            let wd = unsafe { libc::inotify_add_watch(self.fd.as_raw_fd(), name.as_ptr(), mask) };
            if wd >= 0 {
                *self.kinds.entry(wd).or_default() |= kind;
                return Ok(());
            }
            let error = io::Error::last_os_error();
            // Permission changes on an inaccessible root can still be observed
            // through an accessible ancestor; scanning reports the root warning.
            if matches!(
                error.raw_os_error(),
                Some(libc::ENOENT | libc::ENOTDIR | libc::EACCES)
            ) && let Some(parent) = path.parent()
            {
                path = parent;
                continue;
            }
            return Err(format!("Watch {}: {error}", path.display()));
        }
    }
    pub fn drain(&self) -> Result<u8, String> {
        let mut all = 0;
        let mut buffer = [0u8; 65536];
        // Bounded work per loop; remaining events keep the fd ready.
        for _ in 0..16 {
            let n = unsafe {
                libc::read(
                    self.fd.as_raw_fd(),
                    buffer.as_mut_ptr().cast(),
                    buffer.len(),
                )
            };
            if n < 0 {
                let e = io::Error::last_os_error();
                if e.kind() == io::ErrorKind::WouldBlock {
                    break;
                }
                if e.kind() == io::ErrorKind::Interrupted {
                    continue;
                }
                return Err(e.to_string());
            }
            if n == 0 {
                break;
            }
            let mut offset = 0;
            while offset + std::mem::size_of::<libc::inotify_event>() <= n as usize {
                let event = unsafe {
                    std::ptr::read_unaligned(
                        buffer.as_ptr().add(offset).cast::<libc::inotify_event>(),
                    )
                };
                all |= if event.mask & libc::IN_Q_OVERFLOW != 0 {
                    BACKGROUND | COLLECTION
                } else {
                    *self
                        .kinds
                        .get(&event.wd)
                        .unwrap_or(&(BACKGROUND | COLLECTION))
                };
                offset += std::mem::size_of::<libc::inotify_event>() + event.len as usize;
            }
        }
        Ok(all)
    }
}

pub fn poll(fds: &mut [libc::pollfd], timeout: i32) -> Result<(), String> {
    let rc = unsafe { libc::poll(fds.as_mut_ptr(), fds.len() as libc::nfds_t, timeout) };
    if rc < 0 && io::Error::last_os_error().kind() != io::ErrorKind::Interrupted {
        Err(io::Error::last_os_error().to_string())
    } else {
        Ok(())
    }
}
pub fn interest(fd: RawFd, events: i16) -> libc::pollfd {
    libc::pollfd {
        fd,
        events,
        revents: 0,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn singleton_and_missing_root_notifications() {
        let dir = std::env::temp_dir().join(format!("swiftwall-runtime-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let first = Instance::acquire(&dir.join("runtime")).unwrap();
        assert!(Instance::acquire(&dir.join("runtime")).is_err());
        let root = dir.join("missing/child");
        let watcher = Watcher::new(&dir.join("background"), &[root]).unwrap();
        fs::create_dir(dir.join("missing")).unwrap();
        let mut fds = [interest(watcher.fd.as_raw_fd(), libc::POLLIN)];
        poll(&mut fds, 1000).unwrap();
        assert_ne!(watcher.drain().unwrap() & COLLECTION, 0);
        drop(first);
        let second = Instance::acquire(&dir.join("runtime")).unwrap();
        drop(second);
        fs::remove_dir_all(dir).unwrap();
    }
}
