#!/usr/bin/python3
"""On-demand XDG directory picker; invoke with /usr/bin/python3, never GTK.

Exactly one ASCII JSON line on stdout: {"v":1,"result":"selected","url":...},
{"v":1,"result":"cancelled"}, or {"v":1,"result":"error","message":...}.
Exit status is 0 for selected/cancelled, 1 for error. --timeout bounds the whole
operation (default 180s); connection/OpenFile are additionally bounded to 5s.
Cancellation/error may take up to 0.5s extra for best-effort Request.Close.
--watch-parent PID optionally uses Linux pidfd to cancel when the parent exits.
Local readability is only a preliminary check: the daemon must enforce its
network-mount policy and revalidate the directory when consuming the result.
"""

import argparse
import json
import math
import os
import re
import signal
import sys
import uuid
from pathlib import Path
from urllib.parse import unquote_to_bytes, urlsplit

PORTAL = "org.freedesktop.portal.Desktop"
DESKTOP = "/org/freedesktop/portal/desktop"
REQUEST = "org.freedesktop.portal.Request"


def local_folder(path):
    """Validate a UTF-8 absolute directory without resolving symlinks/mounts."""
    path.encode("utf-8", "strict")
    if not os.path.isabs(path) or "\0" in path:
        raise ValueError("Folder must be an absolute local path without NUL")
    if not os.path.isdir(path) or not os.access(path, os.R_OK | os.X_OK):
        raise ValueError("Folder must be an existing readable, searchable directory")
    return path


def validate_uri(uri):
    """Return a canonical local file URI, rejecting lossy URI/UTF-8 parsing."""
    if not isinstance(uri, str) or not uri.isascii():
        raise ValueError("Expected an ASCII, percent-encoded file URI")
    # urlsplit silently strips some control characters; reject them first.
    if re.search(r"[^A-Za-z0-9\-._~:/?#[\]@!$&'()*+,;=%]", uri):
        raise ValueError("Invalid character in file URI")
    if re.search(r"%(?![0-9A-Fa-f]{2})", uri):
        raise ValueError("Invalid percent escape in file URI")
    parts = urlsplit(uri)
    if (parts.scheme != "file" or parts.netloc not in ("", "localhost")
            or not uri.startswith(("file:///", "file://localhost/"))
            or "?" in uri or "#" in uri):
        raise ValueError("Expected an absolute local file URI without query or fragment")
    path = unquote_to_bytes(parts.path).decode("utf-8", "strict")
    # POSIX // paths can have implementation-defined remote semantics.
    if path.startswith("//"):
        raise ValueError("Double-slash paths are not supported")
    return Path(local_folder(path)).as_uri()


def _load_glib_unix():
    from gi.repository import GLibUnix
    return GLibUnix


def unix_apis(gi_module, GLib, loader=_load_glib_unix):
    """Select current GLibUnix APIs, with legacy GLib fallbacks by capability."""
    try:
        gi_module.require_version("GLibUnix", "2.0")
        glib_unix = loader()
    except (ImportError, ValueError):
        glib_unix = None

    signal_add = getattr(glib_unix, "signal_add", None)
    if not callable(signal_add):
        signal_add = getattr(GLib, "unix_signal_add", None)
    if not callable(signal_add):
        raise ValueError("GLib Unix signal API is unavailable")

    fd_add = getattr(glib_unix, "fd_add_full", None)
    if not callable(fd_add):
        fd_add = getattr(GLib, "unix_fd_add_full", None)
    return signal_add, fd_add if callable(fd_add) else None


class Picker:
    def __init__(self, Gio, GLib, timeout, initial_folder=None, parent=None, gi_module=None):
        self.Gio, self.GLib = Gio, GLib
        if gi_module is None:
            import gi as gi_module
        self.signal_add, self.fd_add = unix_apis(gi_module, GLib)
        self.timeout, self.initial_folder, self.parent = timeout, initial_folder, parent
        self.loop = GLib.MainLoop()
        self.connection = None
        self.cancel = Gio.Cancellable()
        self.sources = []
        self.subscriptions = []
        self.closed_handler = None
        self.parent_fd = None
        self.path = None
        self.reply_received = False
        self.pending_response = None
        self.result = None
        self.initial_timer = None

    def timer(self, milliseconds, callback):
        source = self.GLib.timeout_add(milliseconds, callback)
        self.sources.append(source)
        return source

    def remove_source(self, source_id):
        if source_id:
            source = self.GLib.MainContext.default().find_source_by_id(source_id)
            if source:
                source.destroy()

    def fail(self, message):
        self.finish({"v": 1, "result": "error", "message": message})

    def cancelled(self):
        self.finish({"v": 1, "result": "cancelled"})
        return self.GLib.SOURCE_CONTINUE

    def finish(self, result):
        if self.result is not None:
            return
        self.result = result
        self.cancel.cancel()
        if result["result"] != "selected" and self.path and self.connection and not self.connection.is_closed():
            # Do not block shutdown on a broken/disappeared portal.
            self.timer(500, self.stop)
            self.connection.call(PORTAL, self.path, REQUEST, "Close", None,
                                 self.GLib.VariantType.new("()"),
                                 self.Gio.DBusCallFlags.NO_AUTO_START, 450, None,
                                 self.close_done, None)
        else:
            self.loop.quit()

    def stop(self):
        self.loop.quit()
        return self.GLib.SOURCE_REMOVE

    def close_done(self, connection, result, _data):
        try:
            connection.call_finish(result)
        except self.GLib.Error:
            pass  # Close is best effort, including after a completed Response.
        self.loop.quit()

    def initial_expired(self):
        self.fail("Portal did not complete OpenFile within 5 seconds")
        return self.GLib.SOURCE_REMOVE

    def expired(self):
        self.fail("Folder picker timed out")
        return self.GLib.SOURCE_REMOVE

    def bus_closed(self, _connection, _vanished, _error):
        self.fail("Session bus closed")

    def owner_changed(self, _connection, _sender, _path, _interface, _signal, params):
        _name, old, new = params.unpack()
        if old and old != new:
            self.fail("Portal service disappeared or was replaced")

    def bus_ready(self, _source, result, _data):
        try:
            connection = self.Gio.bus_get_finish(result)
            if self.result is not None:
                return
            self.connection = connection
            connection.set_exit_on_close(False)
            self.closed_handler = connection.connect("closed", self.bus_closed)
            sender = connection.get_unique_name()[1:].replace(".", "_")
            token = "swiftwall_" + uuid.uuid4().hex
            self.path = DESKTOP + "/request/" + sender + "/" + token
            self.subscriptions.append(connection.signal_subscribe(
                "org.freedesktop.DBus", "org.freedesktop.DBus", "NameOwnerChanged",
                "/org/freedesktop/DBus", PORTAL, self.Gio.DBusSignalFlags.NONE,
                self.owner_changed))
            self.subscriptions.append(connection.signal_subscribe(
                PORTAL, REQUEST, "Response", self.path, None,
                self.Gio.DBusSignalFlags.NONE, self.response))
            options = {"handle_token": self.GLib.Variant("s", token),
                       "directory": self.GLib.Variant("b", True),
                       "multiple": self.GLib.Variant("b", False)}
            if self.initial_folder is not None:
                options["current_folder"] = self.GLib.Variant(
                    "ay", self.initial_folder.encode("utf-8") + b"\0")
            connection.call(PORTAL, DESKTOP, "org.freedesktop.portal.FileChooser",
                            "OpenFile", self.GLib.Variant("(ssa{sv})", (
                                "", "Add wallpaper folder", options)),
                            self.GLib.VariantType.new("(o)"),
                            self.Gio.DBusCallFlags.NONE, 5000, self.cancel,
                            self.open_done, None)
        except (self.GLib.Error, ValueError) as error:
            self.fail("Cannot open portal: " + str(error))

    def open_done(self, connection, result, _data):
        try:
            reply = connection.call_finish(result)
            if self.result is not None:
                return
            handle, = reply.unpack()
            if handle != self.path:
                self.fail("Portal returned an unexpected request handle")
                return
            self.reply_received = True
            self.remove_source(self.initial_timer)
            if self.pending_response is not None:
                self.consume_response(self.pending_response)
        except (self.GLib.Error, ValueError, TypeError) as error:
            self.fail("Portal OpenFile failed: " + str(error))

    def response(self, _connection, _sender, _path, _interface, _signal, params):
        if self.result is not None or self.pending_response is not None:
            return
        self.pending_response = params
        if self.reply_received:
            self.consume_response(params)

    def consume_response(self, params):
        try:
            if params.get_type_string() != "(ua{sv})":
                raise ValueError("Invalid Response signature")
            code = params.get_child_value(0).unpack()
            results = params.get_child_value(1)
            if code == 1:
                self.cancelled()
            elif code != 0:
                self.fail(f"Portal reported a failed selection (code {code})")
            else:
                uris = results.lookup_value("uris", self.GLib.VariantType.new("as"))
                if uris is None or uris.n_children() != 1:
                    raise ValueError("Expected exactly one selected URI")
                url = validate_uri(uris.unpack()[0])
                self.finish({"v": 1, "result": "selected", "url": url})
        except (ValueError, TypeError, UnicodeError, OSError) as error:
            self.fail("Invalid portal response: " + str(error))

    def run(self):
        try:
            for sig in (signal.SIGTERM, signal.SIGINT):
                self.sources.append(self.signal_add(
                    self.GLib.PRIORITY_DEFAULT, sig, self.cancelled))
            if self.parent is not None:
                if self.parent != os.getppid():
                    return {"v": 1, "result": "cancelled"}
                if not hasattr(os, "pidfd_open"):
                    raise ValueError("Parent watching requires Linux pidfd support")
                if self.fd_add is None:
                    raise ValueError("GLib Unix file-descriptor API is unavailable")
                self.parent_fd = os.pidfd_open(self.parent)
                self.sources.append(self.fd_add(
                    self.GLib.PRIORITY_DEFAULT, self.parent_fd,
                    self.GLib.IOCondition.IN | self.GLib.IOCondition.HUP | self.GLib.IOCondition.ERR,
                    lambda *_args: self.cancelled()))
            self.initial_timer = self.timer(5000, self.initial_expired)
            self.timer(max(1, math.ceil(self.timeout * 1000)), self.expired)
            self.Gio.bus_get(self.Gio.BusType.SESSION, self.cancel, self.bus_ready, None)
            self.loop.run()
            assert self.result is not None
            return self.result
        finally:
            self.cancel.cancel()
            for source in self.sources:
                self.remove_source(source)
            if self.connection:
                for subscription in self.subscriptions:
                    self.connection.signal_unsubscribe(subscription)
                if self.closed_handler:
                    self.connection.disconnect(self.closed_handler)
            if self.parent_fd is not None:
                os.close(self.parent_fd)


class Arguments(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main(argv=None):
    try:
        parser = Arguments(description=__doc__, add_help=False)
        parser.add_argument("--timeout", type=float, default=180.0)
        parser.add_argument("--initial-folder")
        parser.add_argument("--watch-parent", type=int)
        args = parser.parse_args(argv)
        if not math.isfinite(args.timeout) or not 0 < args.timeout <= 2147483:
            raise ValueError("--timeout must be finite, positive and at most 2147483 seconds")
        if args.watch_parent is not None and args.watch_parent <= 0:
            raise ValueError("--watch-parent must be a positive PID")
        if args.initial_folder is not None:
            local_folder(args.initial_folder)
            if args.initial_folder.startswith("//"):
                raise ValueError("Double-slash paths are not supported")
        # Lazy imports keep pure validation tests portable and never import Gtk.
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
        result = Picker(Gio, GLib, args.timeout, args.initial_folder, args.watch_parent,
                        gi_module=gi).run()
    except (ImportError, ValueError, OSError, UnicodeError) as error:
        result = {"v": 1, "result": "error", "message": str(error)}
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")), flush=True)
    return 1 if result["result"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
