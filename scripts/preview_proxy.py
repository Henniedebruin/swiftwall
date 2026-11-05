"""Opt-in recorder for a user-approved fresh preview namespace; never starts a daemon.

API: RecordingProxy(front: Path, backend: Path, root: Path, allowed_pid: int,
                    backend_pid: int, log: Path).start() / .close().
All directories must already exist. The root must be caller-owned mode 0700.
Paths are explicit, absolute, symlink-free and below root; socket directories
must be disjoint. No environment variables or production defaults are consulted.
NDJSON records contain exact newline-terminated bytes as raw_b64, peer identity,
path, time, and parsed ID/op/event where available. Recording stops connections
at the global record budget. This is cooperative same-UID isolation, not protection
against a hostile owner racing directory renames. No CLI or subprocesses.
"""

import base64
import json
import os
from pathlib import Path
import selectors
import socket
import stat
import struct
import threading
import time


MAX_WORKERS = 8
MAX_RECORDS = 4096
MAX_BUFFER = 1024 * 1024


def _checked_path(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Explicit absolute paths without traversal are required")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f"Symlinks are forbidden: {part}")
    return path


def _credentials(sock):
    return struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[:2]


class RecordingProxy:
    def __init__(self, front: Path, backend: Path, root: Path, allowed_pid: int,
                 backend_pid: int, log: Path):
        self.front, self.backend, self.root, self.log = map(
            _checked_path, (front, backend, root, log))
        self.allowed_pid, self.backend_pid = allowed_pid, backend_pid
        if any(type(pid) is not int or pid <= 0 for pid in (allowed_pid, backend_pid)):
            raise ValueError("Explicit positive peer PIDs required")
        self._validate()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(MAX_WORKERS)
        self._workers = set()
        self._clients = set()
        self._listener = self._accept_thread = self._file = None
        self._identity = None
        self._records = 0
        self._started = False

    def _validate(self):
        for path in (self.root, self.front, self.backend, self.log):
            _checked_path(path)
        info = self.root.stat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700 or self.root != self.root.resolve()):
            raise ValueError("Existing caller-owned 0700 resolved root required")
        production = Path(f"/run/user/{os.getuid()}/swiftwall/control.sock")
        for path in (self.front, self.backend, self.log):
            if (not path.is_relative_to(self.root) or path == self.root
                    or path == production or path.resolve() == production):
                raise ValueError("Only private preview paths below root are allowed")
            if not path.parent.is_dir():
                raise ValueError("Existing socket/log parent required")
        a, b = self.front.parent, self.backend.parent
        if a.is_relative_to(b) or b.is_relative_to(a):
            raise ValueError("Front and backend require separate disjoint directories")
        if self.log in (self.front, self.backend):
            raise ValueError("Log must be separate from sockets")

    def start(self):
        """Bind without replacing anything; return self. Instances are single-use."""
        if self._started:
            raise RuntimeError("Proxy is single-use")
        self._started = True
        try:
            self._validate()
            if os.path.lexists(self.front):
                raise FileExistsError(self.front)
            fd = os.open(self.log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
                         | os.O_NONBLOCK, 0o600)
            self._file = os.fdopen(fd, "a", encoding="utf-8")
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise ValueError("Log must be a private caller-owned regular file")
            self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._listener.settimeout(2)
            self._listener.bind(str(self.front))
            info = self.front.lstat()
            self._identity = (info.st_dev, info.st_ino)
            os.chmod(self.front, 0o600)
            self._listener.listen(MAX_WORKERS)
            self._listener.setblocking(False)
            self._accept_thread = threading.Thread(target=self._accept, name="preview-accept", daemon=True)
            self._accept_thread.start()
            return self
        except BaseException:
            self.close()
            raise

    def _record(self, kind, pid, path, **fields):
        with self._log_lock:
            if self._records >= MAX_RECORDS:
                return False
            self._file.write(json.dumps(dict(kind=kind, peer_pid=pid, path=str(path),
                                            time=time.time(), **fields), ensure_ascii=True) + "\n")
            self._file.flush()
            self._records += 1
            return True

    def _accept(self):
        with selectors.DefaultSelector() as selector:
            selector.register(self._listener, selectors.EVENT_READ)
            while not self._stop.is_set():
                if not selector.select(0.2):
                    continue
                try:
                    client, _ = self._listener.accept()
                except OSError:
                    continue
                pid = None
                try:
                    client.settimeout(2)
                    pid, uid = _credentials(client)
                    if (pid, uid) != (self.allowed_pid, os.getuid()):
                        self._record("reject", pid, self.front, reason="front credentials")
                        client.close()
                        continue
                    if not self._slots.acquire(blocking=False):
                        self._record("reject", pid, self.front, reason="worker limit")
                        client.close()
                        continue
                    worker = threading.Thread(target=self._worker, args=(client, pid),
                                              name="preview-worker", daemon=True)
                    with self._lock:
                        self._clients.add(client)
                        self._workers.add(worker)
                    worker.start()
                except OSError:
                    client.close()

    def _frame(self, raw, direction, pid, pending):
        fields = {"raw_b64": base64.b64encode(raw).decode("ascii")}
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            value = None
        if isinstance(value, dict):
            identity = value.get("id")
            if isinstance(identity, str):
                fields["id"] = identity
            op = value.get("op") if direction == "request" else None
            if direction == "request" and isinstance(op, str):
                fields["op"] = op
                if isinstance(identity, str):
                    if len(pending) >= MAX_RECORDS:
                        raise ValueError("pending ID limit")
                    pending[identity] = op
            if direction == "reply":
                if value.get("event") == "snapshot":
                    fields["event"] = "snapshot"
                if isinstance(identity, str) and identity in pending:
                    fields["op"] = pending.pop(identity)
        if not self._record(direction, pid,
                            self.front if direction == "request" else self.backend, **fields):
            raise ValueError("record limit")

    def _worker(self, client, pid):
        backend = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            with self._lock:
                self._clients.add(backend)
            backend.settimeout(2)
            self._validate()
            backend.connect(str(self.backend))
            peer_pid, uid = _credentials(backend)
            if (peer_pid, uid) != (self.backend_pid, os.getuid()):
                self._record("reject", peer_pid, self.backend, reason="backend credentials")
                return
            self._relay(client, backend, pid, peer_pid)
        except (OSError, ValueError) as exc:
            self._record("abort", pid, self.front, reason=str(exc))
        finally:
            with self._lock:
                self._clients.discard(client)
                self._clients.discard(backend)
            client.close()
            backend.close()
            with self._lock:
                self._workers.discard(threading.current_thread())
            self._slots.release()

    def _relay(self, client, backend, pid, backend_pid):
        sockets = (client, backend)
        # Each direction's partial frame plus unsent bytes shares one budget.
        partial = [bytearray(), bytearray()]
        output = [bytearray(), bytearray()]
        eof = [False, False]
        shut = [False, False]
        pending = {}
        for sock in sockets:
            sock.setblocking(False)
        with selectors.DefaultSelector() as selector:
            while not self._stop.is_set():
                for i, sock in enumerate(sockets):
                    try:
                        selector.unregister(sock)
                    except KeyError:
                        pass
                    mask = (0 if eof[i] else selectors.EVENT_READ)
                    if output[1 - i]:
                        mask |= selectors.EVENT_WRITE
                    if mask:
                        selector.register(sock, mask, i)
                if all(eof) and not any(output):
                    return
                for key, mask in selector.select(0.2):
                    i = key.data
                    sock = key.fileobj
                    if mask & selectors.EVENT_WRITE:
                        try:
                            sent = sock.send(output[1 - i])
                            del output[1 - i][:sent]
                        except BlockingIOError:
                            pass
                    if mask & selectors.EVENT_READ:
                        capacity = MAX_BUFFER - len(partial[i]) - len(output[i])
                        if capacity <= 0:
                            raise ValueError("direction buffer limit")
                        try:
                            chunk = sock.recv(min(65536, capacity))
                        except BlockingIOError:
                            continue
                        if not chunk:
                            eof[i] = True
                            # Preserve an unterminated tail, but never record it as a frame.
                            output[i].extend(partial[i])
                            partial[i].clear()
                        else:
                            partial[i].extend(chunk)
                            while True:
                                end = partial[i].find(b"\n")
                                if end < 0:
                                    break
                                raw = bytes(partial[i][:end + 1])
                                del partial[i][:end + 1]
                                self._frame(raw, "request" if i == 0 else "reply",
                                            pid if i == 0 else backend_pid, pending)
                                output[i].extend(raw)
                            if len(partial[i]) >= MAX_BUFFER:
                                raise ValueError("frame buffer limit")
                for i in range(2):
                    if eof[i] and not output[i] and not shut[i]:
                        sockets[1 - i].shutdown(socket.SHUT_WR)
                        shut[i] = True

    def close(self):
        """Stop accepting, interrupt clients and join all threads; safe to repeat."""
        self._stop.set()
        # Stop the producer before taking the final worker snapshot.
        if self._accept_thread:
            self._accept_thread.join(3)
        if self._listener:
            self._listener.close()
        with self._lock:
            clients, workers = list(self._clients), list(self._workers)
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for worker in workers:
            worker.join(3)
        if self._identity is not None:
            try:
                info = self.front.lstat()
                if (info.st_dev, info.st_ino) == self._identity:
                    self.front.unlink()
            except FileNotFoundError:
                pass
        if self._file:
            self._file.close()
