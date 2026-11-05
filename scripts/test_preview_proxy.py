"""Private Unix-socket tests. No daemon, production sockets or subprocesses."""

import base64
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

import preview_proxy as proxy


class PreviewProxyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pp-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "front").mkdir(mode=0o700)
        (self.root / "back").mkdir(mode=0o700)
        self.front = self.root / "front/control.sock"
        self.backend = self.root / "back/control.sock"
        self.log = self.root / "record.ndjson"
        self.received = bytearray()
        self.accepted = 0
        self.stop = threading.Event()
        self.server = None
        self.server_clients = []
        self.server_thread = None
        self.instance = None
        original = socket.socket.connect

        def guarded_connect(sock, address):
            if address not in (str(self.front), str(self.backend)):
                raise AssertionError(f"Non-fixture connect forbidden: {address}")
            return original(sock, address)

        self.enterContext(mock.patch.object(socket.socket, "connect", guarded_connect))
        for name in ("subprocess.Popen", "subprocess.run", "os.system", "os.fork"):
            self.enterContext(mock.patch(name, side_effect=AssertionError("No processes")))
        self.addCleanup(self.cleanup_sockets)

    def cleanup_sockets(self):
        if self.instance:
            self.instance.close()
        self.stop.set()
        if self.server_thread:
            self.server_thread.join(3)
            self.assertFalse(self.server_thread.is_alive())
        for client in self.server_clients:
            client.close()
        if self.server:
            self.server.close()
        if self.backend.exists():
            self.backend.unlink()

    def start_backend(self, replies=None):
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.backend))
        self.server.listen(8)
        self.server.settimeout(0.05)

        def serve():
            while not self.stop.is_set():
                try:
                    client, _ = self.server.accept()
                except socket.timeout:
                    continue
                self.accepted += 1
                self.server_clients.append(client)
                client.settimeout(0.05)
                while not self.stop.is_set():
                    try:
                        data = client.recv(65536)
                        if not data:
                            break
                        self.received.extend(data)
                        client.sendall(data if replies is None else replies)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                client.close()

        self.server_thread = threading.Thread(target=serve, name="fixture-backend")
        self.server_thread.start()

    def start_proxy(self, **kwargs):
        args = dict(front=self.front, backend=self.backend, root=self.root,
                    allowed_pid=os.getpid(), backend_pid=os.getpid(), log=self.log)
        args.update(kwargs)
        self.instance = proxy.RecordingProxy(**args)
        return self.instance.start()

    def connect(self):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2)
        self.addCleanup(client.close)
        client.connect(str(self.front))
        return client

    def wait(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate():
            if time.monotonic() > deadline:
                self.fail("Timed out waiting for fixture")
            time.sleep(0.01)

    def records(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def receive(self, client, length):
        result = bytearray()
        while len(result) < length:
            chunk = client.recv(length - len(result))
            if not chunk:
                break
            result.extend(chunk)
        return bytes(result)

    def test_hello_subscribe_exact_bytes_and_per_connection_ids(self):
        self.start_backend()
        self.start_proxy()
        for op in ("hello", "subscribe", "status"):
            client = self.connect()
            wire = (' {"v":1, "id":"same", "op":"' + op + '"}\n').encode()
            client.sendall(wire[:7])
            client.sendall(wire[7:])
            self.assertEqual(self.receive(client, len(wire)), wire)
            client.close()
        self.wait(lambda: len(self.records()) >= 6)
        frames = self.records()
        self.assertEqual([r["op"] for r in frames if r["kind"] == "reply"],
                         ["hello", "subscribe", "status"])
        for record in frames:
            self.assertEqual(record["peer_pid"], os.getpid())
            self.assertIn(record["path"], (str(self.front), str(self.backend)))
            self.assertIsInstance(record["time"], float)
            self.assertTrue(base64.b64decode(record["raw_b64"]).endswith(b"\n"))

    def test_snapshot_and_reply_id_mapping(self):
        reply = b'{"id":"x","ok":true}\n{"event":"snapshot","seq":1}\n'
        self.start_backend(reply)
        self.start_proxy()
        client = self.connect()
        client.sendall(b'{"id":"x","op":"subscribe"}\n')
        self.assertEqual(self.receive(client, len(reply)), reply)
        records = self.records()
        self.assertEqual(records[1]["op"], "subscribe")
        self.assertEqual(records[2]["event"], "snapshot")
        self.assertNotIn("op", records[2])

    def test_wrong_front_pid_never_connects_backend(self):
        self.start_backend()
        self.start_proxy(allowed_pid=os.getpid() + 100000)
        client = self.connect()
        try:
            client.sendall(b'{"op":"hello"}\n')
            self.assertEqual(client.recv(100), b"")
        except ConnectionResetError:
            pass
        self.wait(lambda: bool(self.records()))
        self.assertEqual(self.accepted, 0)
        self.assertEqual(self.received, b"")
        self.assertEqual(self.records()[0]["reason"], "front credentials")

    def test_wrong_backend_pid_before_any_write(self):
        self.start_backend()
        self.start_proxy(backend_pid=os.getpid() + 100000)
        client = self.connect()
        client.sendall(b'{"op":"hello"}\n')
        try:
            self.assertEqual(client.recv(100), b"")
        except ConnectionResetError:
            pass
        self.wait(lambda: bool(self.records()))
        self.assertEqual(self.received, b"")
        self.assertEqual(self.records()[0]["reason"], "backend credentials")

    def test_wrong_front_uid(self):
        with mock.patch.object(proxy, "_credentials", return_value=(os.getpid(), os.getuid() + 1)):
            self.start_proxy()
            client = self.connect()
            self.assertEqual(client.recv(1), b"")
            self.wait(lambda: bool(self.records()))
            self.assertEqual(self.records()[0]["kind"], "reject")

    def test_wrong_backend_uid(self):
        self.start_backend()
        with mock.patch.object(proxy, "_credentials", side_effect=[
                (os.getpid(), os.getuid()), (os.getpid(), os.getuid() + 1)]):
            self.start_proxy()
            client = self.connect()
            client.sendall(b"hello\n")
            try:
                client.recv(1)
            except ConnectionResetError:
                pass
            self.wait(lambda: bool(self.records()))
            self.assertEqual(self.received, b"")
            self.assertEqual(self.records()[0]["reason"], "backend credentials")

    def test_paths_outside_relative_traversal_and_production_forbidden(self):
        production = Path(f"/run/user/{os.getuid()}/swiftwall/control.sock")
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root)}):
            for field in ("front", "backend", "log"):
                for path in (production, self.root.parent / "escape.sock", Path("relative"),
                             self.root / "front/../escape.sock"):
                    with self.subTest(field=field, path=path), self.assertRaises(ValueError):
                        self.start_proxy(**{field: path})

    def test_root_and_parents_symlink_forbidden(self):
        link = self.root / "link"
        link.symlink_to(self.root / "back", target_is_directory=True)
        for kwargs in ({"backend": link / "control.sock"}, {"root": link}):
            with self.assertRaises(ValueError):
                self.start_proxy(**kwargs)
        self.backend.symlink_to(self.front)
        with self.assertRaises(ValueError):
            self.start_proxy()

    def test_private_root_and_separate_directories_required(self):
        self.root.chmod(0o755)
        with self.assertRaises(ValueError):
            self.start_proxy()
        self.root.chmod(0o700)
        for backend in (self.front.parent / "other.sock", self.root / "other.sock"):
            with self.assertRaises(ValueError):
                self.start_proxy(backend=backend)

    def test_existing_front_socket_not_clobbered(self):
        occupied = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(occupied.close)
        occupied.bind(str(self.front))
        inode = self.front.stat().st_ino
        with self.assertRaises(FileExistsError):
            self.start_proxy()
        self.assertEqual(self.front.stat().st_ino, inode)
        self.assertFalse(self.log.exists())

    def test_missing_backend_no_default_fallback(self):
        self.start_proxy()
        client = self.connect()
        client.sendall(b"hello\n")
        try:
            client.recv(1)
        except ConnectionResetError:
            pass
        self.wait(lambda: bool(self.records()))
        self.assertEqual(self.records()[0]["kind"], "abort")

    def test_close_cleans_workers_listener_and_clients(self):
        self.start_backend()
        instance = self.start_proxy()
        client = self.connect()
        client.sendall(b"hello\n")
        self.assertEqual(self.receive(client, 6), b"hello\n")
        workers = list(instance._workers)
        instance.close()
        instance.close()
        self.assertFalse(self.front.exists())
        self.assertFalse(instance._accept_thread.is_alive())
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertFalse(instance._clients)
        self.assertFalse(instance._workers)
        self.assertEqual(client.recv(1), b"")

    def test_worker_limit(self):
        self.start_backend()
        instance = self.start_proxy()
        clients = [self.connect() for _ in range(8)]
        self.wait(lambda: len(instance._workers) == 8)
        ninth = self.connect()
        self.assertEqual(ninth.recv(1), b"")
        self.assertLessEqual(len(instance._workers), 8)
        self.wait(lambda: bool(self.records()))
        self.assertEqual(self.records()[0]["reason"], "worker limit")
        self.assertEqual(len(clients), 8)

    def test_max_payload_aborts_without_recording_or_forwarding_huge_frame(self):
        self.start_backend()
        self.start_proxy()
        client = self.connect()
        try:
            client.sendall(b"x" * (proxy.MAX_BUFFER + 1))
            self.assertEqual(client.recv(1), b"")
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.wait(lambda: bool(self.records()))
        self.assertEqual(self.received, b"")
        self.assertTrue(all("raw_b64" not in r for r in self.records()))
        self.assertLess(self.log.stat().st_size, 4096)

    def test_backend_payload_overflow_not_recorded_or_forwarded(self):
        self.start_backend(b"x" * (proxy.MAX_BUFFER + 1))
        self.start_proxy()
        client = self.connect()
        client.sendall(b"{}\n")
        self.assertEqual(client.recv(1), b"")
        self.wait(lambda: any(r["kind"] == "abort" for r in self.records()))
        self.assertFalse(any(r["kind"] == "reply" for r in self.records()))
        self.assertLess(self.log.stat().st_size, 4096)

    def test_insecure_or_symlink_log_refused(self):
        self.log.write_text("sentinel")
        self.log.chmod(0o644)
        with self.assertRaises(ValueError):
            self.start_proxy()
        self.assertEqual(self.log.read_text(), "sentinel")
        self.assertFalse(self.front.exists())
        self.log.unlink()
        self.log.symlink_to(self.root / "target")
        with self.assertRaises(ValueError):
            self.start_proxy()
        self.assertFalse((self.root / "target").exists())

    def test_global_record_budget(self):
        self.start_backend()
        with mock.patch.object(proxy, "MAX_RECORDS", 4):
            self.start_proxy()
            client = self.connect()
            client.sendall(b"{}\n" * 10)
            try:
                while client.recv(1024):
                    pass
            except ConnectionResetError:
                pass
            self.wait(lambda: not self.instance._workers)
            self.assertEqual(len(self.records()), 4)

    def test_idle_subscription_no_heartbeat_and_half_close_preserved(self):
        self.start_backend()
        self.start_proxy()
        client = self.connect()
        wire = b'{"op":"subscribe"}\n'
        client.sendall(wire)
        self.assertEqual(self.receive(client, len(wire)), wire)
        client.settimeout(0.45)
        with self.assertRaises(socket.timeout):
            client.recv(1)
        self.assertEqual(self.received, wire)
        client.settimeout(2)
        client.shutdown(socket.SHUT_WR)
        self.assertEqual(client.recv(1), b"")


if __name__ == "__main__":
    unittest.main()
