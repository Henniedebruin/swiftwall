"""One-shot, reconciled Omarchy activation. No import/constructor side effects.

Only explicitly called methods execute commands. Tests inject a private command
transport; production uses process groups, structured argv and per-command bounds.
A transport timeout is not proof that a shell-side mutation did not complete.
"""

from copy import deepcopy
from dataclasses import asdict, dataclass
import json
import os
import re
from pathlib import Path
import signal
import subprocess
import tempfile
import time

from evidence_support import private_evidence_directory, private_evidence_log


PLUGIN_ID = "io.github.henniedebruin.swiftwall"
IPC_TIMEOUT = "10s"
PROCESS_TIMEOUT = 12  # Includes Omarchy's 10s timeout and its 1s kill escalation.


@dataclass
class ActivationOutcome:
    state: str
    transport: str
    detail: str
    snapshot: dict | None = None

    def as_dict(self):
        return asdict(self)


class ActivationError(RuntimeError):
    def __init__(self, outcome):
        self.outcome = outcome
        super().__init__(f"activation-{outcome.state} ({outcome.transport}): {outcome.detail}")


def process(args, *, env, timeout, **kwargs):
    """Reserve the leader PID until group cleanup, then reap before reconciliation."""
    # Files cannot fill a pipe while we wait without reaping. Capture at most
    # 1 Mi characters per stream (bytes on timeout); temporary disk use is uncapped.
    capture_limit = 1024 * 1024
    with tempfile.TemporaryFile(mode="w+t") as stdout, tempfile.TemporaryFile(mode="w+t") as stderr:
        deadline = time.monotonic() + timeout
        child = subprocess.Popen(args, env=env, start_new_session=True,
                                 stdin=subprocess.DEVNULL, stdout=stdout,
                                 stderr=stderr, text=True)
        timed_out = False
        try:
            while os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                time.sleep(min(0.01, remaining))
        finally:
            # WNOWAIT keeps even an exited leader's PID reserved through killpg.
            # This stops local commands, not requests already executing remotely.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            finally:
                child.wait(timeout=2)
        stdout.seek(0)
        stderr.seek(0)
        if timed_out:
            # Like communicate(), TimeoutExpired carries bytes even in text mode.
            raise subprocess.TimeoutExpired(args, timeout,
                                            output=stdout.buffer.read(capture_limit),
                                            stderr=stderr.buffer.read(capture_limit))
        return subprocess.CompletedProcess(args, child.returncode,
                                           stdout.read(capture_limit), stderr.read(capture_limit))


def without_plugin(config, plugin_id=PLUGIN_ID):
    """Normalize only our own entries; never rewrite unrelated widgets/settings."""
    result = deepcopy(config)
    bar = result.get("bar", {})
    if not isinstance(bar, dict):
        raise ValueError("Invalid effective bar configuration")
    layout = bar.get("layout", {})
    if not isinstance(layout, dict):
        raise ValueError("Invalid effective bar layout")
    for section, entries in layout.items():
        if not isinstance(entries, list) or not all(isinstance(e, dict) and isinstance(e.get("id"), str) for e in entries):
            raise ValueError("Invalid effective bar entries")
        layout[section] = [e for e in entries if e["id"] != plugin_id]
    for key in ("plugins", "disabledPlugins"):
        if key in result:
            if not isinstance(result[key], list):
                raise ValueError("Invalid plugin configuration")
            result[key] = [e for e in result[key] if e != plugin_id
                           and not (isinstance(e, dict) and e.get("id") == plugin_id)]
            if not result[key]:
                result.pop(key)
    return result


class OmarchyActivation:
    def __init__(self, *, home, evidence, runner=process, env=None, plugin_id=PLUGIN_ID):
        if plugin_id != PLUGIN_ID and not re.fullmatch(r"local\.swiftwall-preview-[0-9a-f]{32}", plugin_id):
            raise ValueError("Only SwiftWall production or fresh preview identities are allowed")
        self.plugin_id = plugin_id
        self.home = Path(home)
        self.evidence = Path(evidence)
        self.runner = runner
        self.env = dict(os.environ if env is None else env)
        self.env["OMARCHY_SHELL_IPC_TIMEOUT"] = IPC_TIMEOUT
        self.baseline = None
        self.activation_started = False
        self.enable_attempted = False
        self.disable_attempted = False
        self.last_outcome = None

    def record(self, event, **fields):
        with private_evidence_directory(self.evidence) as directory:
            stream = private_evidence_log(directory, create=True, name="activation.jsonl")
            assert stream is not None  # create=True either returns a stream or raises.
            with stream:
                stream.write(json.dumps({"event": event, **fields}) + "\n")

    def invoke(self, args):
        try:
            result = self.runner(list(map(str, args)), env=dict(self.env),
                                 timeout=PROCESS_TIMEOUT, capture_output=True, text=True)
            diagnostic = result.stdout + result.stderr
            transport = ("ok" if result.returncode == 0 else
                         "timeout" if result.returncode in (124, 137) or "omarchy-shell is not responding" in diagnostic
                         else "error")
            self.record("command", args=list(map(str, args)), transport=transport,
                        returncode=result.returncode, stdout=result.stdout, stderr=result.stderr,
                        ipc_timeout=IPC_TIMEOUT, process_timeout=PROCESS_TIMEOUT)
            return transport, result
        except subprocess.TimeoutExpired as exc:
            self.record("command", args=list(map(str, args)), transport="timeout",
                        detail=str(exc), ipc_timeout=IPC_TIMEOUT, process_timeout=PROCESS_TIMEOUT)
            return "timeout", None
        except (OSError, UnicodeError) as exc:
            self.record("command", args=list(map(str, args)), transport="error", detail=str(exc))
            return "error", None

    def query(self, method):
        transport, result = self.invoke(["omarchy-shell", "shell", method])
        if transport != "ok":
            raise ValueError(f"Authoritative {method} unavailable ({transport})")
        return json.loads(result.stdout)

    def normalized(self, config):
        return without_plugin(config, self.plugin_id)

    def snapshot(self):
        # Read the runtime registry on both sides of effective config. These are
        # observations, not an atomic transaction or a cancellation fence.
        first = self.query("listPlugins")
        config = self.query("listShellConfig")
        second = self.query("listPlugins")
        if not isinstance(first, list) or not isinstance(second, list) or not isinstance(config, dict):
            raise ValueError("Malformed authoritative plugin/bar state")
        def registry(entries):
            result = {}
            for entry in entries:
                if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                        or type(entry.get("enabled")) is not bool or entry["id"] in result):
                    raise ValueError("Invalid/duplicate plugin registry entry")
                result[entry["id"]] = entry["enabled"]
            return result
        before, after = registry(first), registry(second)
        if before != after:
            raise ValueError("Registry changed during state observation")
        self.normalized(config)  # Validate layout before any mutation.
        if "layout" not in config.get("bar", {}):
            raise ValueError("Effective bar layout missing")
        path = self.home / ".config/omarchy/shell.json"
        persisted = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(persisted, dict):
            raise ValueError("Invalid persisted shell configuration")
        self.normalized(persisted)
        snapshot = {"registry": after, "config": config, "persisted": persisted}
        self.record("snapshot", **snapshot)
        return snapshot

    def baseline_is_inactive(self):
        return self.baseline["registry"].get(self.plugin_id) is not True and not any(
            entry["id"] == self.plugin_id for values in self.baseline["config"]["bar"]["layout"].values()
            for entry in values)

    def prepare(self):
        """Capture a first-install baseline before any plugin file publication."""
        try:
            if self.baseline is None:
                self.baseline = self.snapshot()
            if not self.baseline_is_inactive():
                return self.finish("ambiguous", "not-sent", "Pre-existing activation is not owned by this attempt")
        except (ValueError, OSError) as exc:
            return self.finish("ambiguous", "not-sent", str(exc))
        self.record("prepared", baseline=self.baseline)

    def classify(self, snapshot):
        unrelated = lambda value: {key: enabled for key, enabled in value.items() if key != self.plugin_id}
        if unrelated(snapshot["registry"]) != unrelated(self.baseline["registry"]):
            return "ambiguous", "Unrelated plugin registry changed; never overwrite it"
        if self.normalized(snapshot.get("persisted", {})) != self.normalized(self.baseline.get("persisted", {})):
            return "ambiguous", "Unrelated persisted shell configuration changed; never overwrite it"
        if self.normalized(snapshot["config"]) != self.normalized(self.baseline["config"]):
            return "ambiguous", "Unrelated shell/bar configuration changed; never overwrite it"
        entries = [(section, index) for section, values in snapshot["config"]["bar"]["layout"].items()
                   for index, entry in enumerate(values) if entry["id"] == self.plugin_id]
        enabled = snapshot["registry"].get(self.plugin_id)
        if enabled is True and entries == [("right", 0)]:
            return "success", "Registry and effective bar confirm one requested placement"
        if (enabled is False or self.plugin_id not in snapshot["registry"]) and not entries:
            return "inactive", "Registry and effective bar confirm inactive state at observation"
        return "partial", "Registry and bar disagree, placement is wrong/duplicated, or plugin is absent"

    def finish(self, state, transport, detail, snapshot=None):
        self.last_outcome = ActivationOutcome(state, transport, detail, snapshot)
        self.record("outcome", **self.last_outcome.as_dict())
        if state != "success":
            raise ActivationError(self.last_outcome)
        return self.last_outcome

    def activate(self):
        if self.activation_started:
            return self.finish("ambiguous", "not-sent", "Activation already attempted; refusing another request")
        self.activation_started = True
        self.prepare()
        transport, _ = self.invoke(["omarchy", "plugin", "validate",
                                   self.home / ".config/omarchy/plugins" / self.plugin_id])
        if transport != "ok":
            return self.finish("rejected", transport, "Installed package validation failed; no enable sent")
        # Discovery may trigger reload; do not conflate its reply with readiness.
        discovery, _ = self.invoke(["omarchy-shell", "shell", "rescanPlugins"])
        try:
            discovered = self.snapshot()
            if self.normalized(discovered["config"]) != self.normalized(self.baseline["config"]):
                raise ValueError("Unrelated shell state changed during discovery")
            if self.plugin_id not in discovered["registry"]:
                raise ValueError("Plugin not discovered; no enable sent")
            discovered_state, discovered_detail = self.classify(discovered)
            if discovered_state != "inactive":
                # Autodiscovery or another actor may already have placed it.
                # Never send enable over observed full/partial activation.
                return self.finish(discovered_state, discovery, discovered_detail + "; no enable sent", discovered)
        except (ValueError, OSError) as exc:
            return self.finish("ambiguous", discovery, str(exc))
        self.enable_attempted = True  # Armed before sending even if reply is lost.
        transport, result = self.invoke(["omarchy", "plugin", "enable", self.plugin_id,
                                         "--section", "right", "--index", "0"])
        try:
            snapshot = self.snapshot()
            state, detail = self.classify(snapshot)
        except (ValueError, OSError) as exc:
            return self.finish("ambiguous", transport, str(exc))
        if state == "inactive":
            # A lost reply can still be queued. Never infer explicit rejection or
            # resend enable just because an immediate observation is inactive.
            explicit = (result is not None and transport == "error"
                        and result.stderr.startswith("omarchy-plugin-enable:"))
            return self.finish("rejected" if explicit else "ambiguous", transport,
                               detail + "; no enable retry is permitted", snapshot)
        return self.finish(state, transport, detail, snapshot)

    def deactivate(self):
        """Reconcile and disable at most once; no file removal on unknown state."""
        if self.baseline is None:
            raise ActivationError(ActivationOutcome("ambiguous", "not-sent", "No baseline available for safe cleanup"))
        if not self.baseline_is_inactive():
            raise ActivationError(ActivationOutcome("ambiguous", "not-sent", "Refusing to disable pre-existing activation"))
        try:
            before = self.snapshot()
        except (ValueError, OSError) as exc:
            raise ActivationError(ActivationOutcome("ambiguous", "query-error", str(exc))) from exc
        state, detail = self.classify(before)
        if not self.enable_attempted and state == "inactive":
            self.record("deactivated", transport="not-needed")
            return ActivationOutcome("success", "not-needed", "No enable was sent; inactive state verified", before)
        if self.disable_attempted:
            raise ActivationError(ActivationOutcome("ambiguous", "not-sent", "Disable already attempted; manual reconciliation required"))
        self.disable_attempted = True
        transport, _ = self.invoke(["omarchy", "plugin", "disable", self.plugin_id])
        try:
            after = self.snapshot()
            state, detail = self.classify(after)
        except (ValueError, OSError) as exc:
            raise ActivationError(ActivationOutcome("ambiguous", transport, str(exc))) from exc
        if state != "inactive":
            raise ActivationError(ActivationOutcome("ambiguous", transport,
                                                    "Deactivation unverified: " + detail, after))
        self.record("deactivated", transport=transport, snapshot=after)
        return ActivationOutcome("success", transport, "Inactive registry and effective bar verified", after)
