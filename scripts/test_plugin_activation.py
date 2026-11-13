"""Activation reconciliation tests; never contact a live shell or service manager.

Unit transports consume exact argv sequences. Process tests execute only private
Python fakes with an isolated HOME/PATH and JSON-backed authoritative state.
"""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import plugin_activation as activation


PLUGIN = activation.PLUGIN_ID
NEWS = "io.test.NewsTicker"
ENABLE = ["omarchy", "plugin", "enable", PLUGIN, "--section", "right", "--index", "0"]
DISABLE = ["omarchy", "plugin", "disable", PLUGIN]
RESCAN = ["omarchy-shell", "shell", "rescanPlugins"]


def shell_state(enabled=False):
    config = {
        "bar": {"layout": {
            "left": [{"id": NEWS, "settings": {"feed": "original", "limit": 4}}],
            "center": [{"id": "clock", "format": "hh:mm"}],
            "right": [{"id": "tray", "spacing": 7}],
        }, "height": 32},
        "plugins": [NEWS], "theme": {"name": "private-test"},
    }
    if enabled:
        config["bar"]["layout"]["right"].insert(0, {"id": PLUGIN})
        config["plugins"].append(PLUGIN)
    return {"registry": {PLUGIN: enabled, NEWS: True}, "config": config, "persisted": {}}


def write_persisted(home, config):
    path = Path(home) / ".config/omarchy/shell.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config))
    return path


class ScriptedRunner:
    """No fallback: every invocation must consume one exact expected command."""

    def __init__(self, test):
        self.test = test
        self.steps = []
        self.calls = []

    def expect(self, args, *, value=None, stdout="", stderr="", code=0, error=None, effect=None):
        if value is not None:
            stdout = json.dumps(value)
        self.steps.append((list(args), stdout, stderr, code, error, effect))

    def snapshot(self, state=None):
        state = shell_state() if state is None else state
        registry = [{"id": key, "enabled": value} for key, value in state["registry"].items()]
        self.expect(["omarchy-shell", "shell", "listPlugins"], value=registry)
        self.expect(["omarchy-shell", "shell", "listShellConfig"], value=state["config"])
        persisted = deepcopy(state["persisted"])
        self.expect(["omarchy-shell", "shell", "listPlugins"], value=registry,
                    effect=lambda: write_persisted(self.test.root, persisted))

    def __call__(self, args, *, env, timeout, capture_output, text):
        self.test.assertEqual(env["OMARCHY_SHELL_IPC_TIMEOUT"], "10s")
        self.test.assertEqual(timeout, 12)
        self.test.assertTrue(capture_output)
        self.test.assertTrue(text)
        self.test.assertTrue(self.steps, f"Unexpected command: {args}")
        expected, stdout, stderr, code, error, effect = self.steps.pop(0)
        self.test.assertEqual(args, expected)
        self.calls.append(list(args))
        if effect is not None:
            effect()
        if error is not None:
            raise error
        return subprocess.CompletedProcess(args, code, stdout, stderr)

    def assert_done(self):
        self.test.assertEqual(self.steps, [], "Expected commands were not consumed")


class ActivationTests(unittest.TestCase):
    def setUp(self):
        guard = mock.patch.object(subprocess, "Popen",
                                  side_effect=AssertionError("Live subprocess forbidden"))
        popen = guard.start()
        self.addCleanup(guard.stop)
        self.addCleanup(popen.assert_not_called)
        temporary = tempfile.TemporaryDirectory(prefix="swiftwall-activation-unit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.original_env = dict(os.environ)
        self.addCleanup(lambda: self.assertEqual(dict(os.environ), self.original_env))
        self.env = {"HOME": str(self.root), "PATH": "/nonexistent", "OMARCHY_SHELL_IPC_TIMEOUT": "2s"}
        self.runner = ScriptedRunner(self)
        self.addCleanup(self.runner.assert_done)
        self.new_subject()

    def new_subject(self):
        self.subject = activation.OmarchyActivation(
            home=self.root, evidence=self.root / "evidence", runner=self.runner, env=self.env)

    def prepare(self, *, discovery=None, discovered=None, baseline=None):
        self.attempt_start = len(self.runner.calls)
        self.runner.snapshot(baseline)
        self.runner.expect(["omarchy", "plugin", "validate",
                            str(self.root / ".config/omarchy/plugins" / PLUGIN)])
        self.runner.expect(RESCAN, **(discovery or {}))
        self.runner.snapshot(discovered)

    def attempt(self, *, after=None, command=None):
        self.prepare()
        self.runner.expect(ENABLE, **(command or {}))
        self.runner.snapshot(shell_state(True) if after is None else after)

    def failure(self, state, operation=None, transport=None):
        with self.assertRaises(activation.ActivationError) as caught:
            (operation or self.subject.activate)()
        outcome = caught.exception.outcome
        self.assertEqual(outcome.state, state)
        if transport is not None:
            self.assertEqual(outcome.transport, transport)
        return outcome

    def assert_activation_blocked(self):
        calls = deepcopy(self.runner.calls)
        enable_attempted = self.subject.enable_attempted
        self.assertTrue(self.subject.activation_started)
        outcome = self.failure("ambiguous", transport="not-sent")
        self.assertIn("Activation already attempted", outcome.detail)
        self.assertEqual(self.runner.calls, calls)
        self.assertEqual(self.subject.enable_attempted, enable_attempted)

    def assert_no_retry(self):
        self.assert_activation_blocked()
        self.assertTrue(self.subject.enable_attempted)
        self.assertEqual(self.runner.calls[self.attempt_start:].count(ENABLE), 1)

    def events(self):
        return [json.loads(line) for line in
                (self.root / "evidence/activation.jsonl").read_text().splitlines()]

    def test_malformed_bar_shapes_fail_closed(self):
        for value in (None, [], "invalid", True):
            with self.subTest(bar=value), self.assertRaises(ValueError):
                activation.without_plugin({"bar": value})

    def test_constructor_has_no_side_effects_and_copies_environment(self):
        self.assertFalse(self.runner.calls)
        self.assertFalse((self.root / "evidence").exists())
        self.assertEqual(self.env["OMARCHY_SHELL_IPC_TIMEOUT"], "2s")
        self.assertIsNot(self.subject.env, self.env)
        self.env["HOME"] = "changed-after-construction"
        self.assertEqual(self.subject.env["HOME"], str(self.root))

    def test_default_environment_is_copied_not_mutated(self):
        subject = activation.OmarchyActivation(home=self.root, evidence=self.root / "unused",
                                             runner=self.runner)
        self.assertEqual(subject.env["OMARCHY_SHELL_IPC_TIMEOUT"], "10s")
        self.assertEqual(dict(os.environ), self.original_env)

    def test_success_uses_authoritative_queries_and_one_enable(self):
        self.attempt()
        outcome = self.subject.activate()
        self.assertEqual(outcome.state, "success")
        self.assertEqual(outcome.transport, "ok")
        self.assertEqual(outcome.snapshot, shell_state(True))
        self.assertEqual(self.subject.baseline, shell_state())
        self.assertEqual(self.env["OMARCHY_SHELL_IPC_TIMEOUT"], "2s")
        self.assert_no_retry()
        commands = [event for event in self.events() if event["event"] == "command"]
        self.assertTrue(all(event["ipc_timeout"] == "10s" and event["process_timeout"] == 12
                            for event in commands))

    def test_committed_command_error_is_success_without_retry(self):
        self.attempt(command={"code": 1, "stderr": "reply delivery failed"})
        outcome = self.subject.activate()
        self.assertEqual((outcome.state, outcome.transport), ("success", "error"))
        self.assertEqual(outcome.snapshot, shell_state(True))
        self.assert_no_retry()

    def test_completed_before_subprocess_timeout_is_success_with_full_state(self):
        self.attempt(command={"error": subprocess.TimeoutExpired(ENABLE, 12)})
        outcome = self.subject.activate()
        self.assertEqual((outcome.state, outcome.transport), ("success", "timeout"))
        self.assertEqual(outcome.snapshot, shell_state(True))
        self.assertEqual(outcome.as_dict()["snapshot"], shell_state(True))
        self.assert_no_retry()

    def test_committed_timeout_exit_codes_are_success(self):
        for code in (124, 137):
            with self.subTest(code=code):
                self.new_subject()
                self.attempt(command={"code": code})
                outcome = self.subject.activate()
                self.assertEqual((outcome.state, outcome.transport), ("success", "timeout"))
                self.assertEqual(outcome.snapshot, shell_state(True))
                self.assert_no_retry()
        self.assertEqual(self.runner.calls.count(ENABLE), 2)  # One per independent attempt.

    def test_nonresponding_diagnostic_is_timeout_not_rejection(self):
        self.attempt(after=shell_state(), command={
            "code": 1, "stderr": "omarchy-shell is not responding"})
        self.failure("ambiguous", transport="timeout")
        self.assert_no_retry()

    def test_inactive_after_timeout_is_ambiguous_not_retried(self):
        self.attempt(after=shell_state(), command={"error": subprocess.TimeoutExpired(ENABLE, 12)})
        outcome = self.failure("ambiguous", transport="timeout")
        self.assertEqual(outcome.snapshot, shell_state())
        self.assert_no_retry()

    def test_explicit_stderr_rejection_with_inactive_state(self):
        self.attempt(after=shell_state(), command={
            "code": 1, "stderr": "omarchy-plugin-enable: invalid placement"})
        self.failure("rejected", transport="error")
        self.assert_no_retry()

    def test_generic_error_with_inactive_state_is_not_explicit_rejection(self):
        self.attempt(after=shell_state(), command={"code": 1, "stderr": "invalid placement"})
        self.failure("ambiguous", transport="error")
        self.assert_no_retry()

    def test_oserror_after_enable_is_ambiguous_without_retry(self):
        self.attempt(after=shell_state(), command={"error": OSError("private transport failed")})
        self.failure("ambiguous", transport="error")
        self.assert_no_retry()

    def test_registry_bar_disagreement_and_duplicate_placements_are_partial(self):
        cases = []
        enabled_without_bar = shell_state()
        enabled_without_bar["registry"][PLUGIN] = True
        cases.append(enabled_without_bar)
        disabled_with_bar = shell_state(True)
        disabled_with_bar["registry"][PLUGIN] = False
        cases.append(disabled_with_bar)
        duplicate = shell_state(True)
        duplicate["config"]["bar"]["layout"]["left"].append({"id": PLUGIN})
        cases.append(duplicate)
        wrong_position = shell_state(True)
        wrong_position["config"]["bar"]["layout"]["right"].reverse()
        cases.append(wrong_position)
        missing_registry = shell_state(True)
        del missing_registry["registry"][PLUGIN]
        cases.append(missing_registry)
        for state in cases:
            with self.subTest(state=state):
                self.new_subject()
                self.attempt(after=state)
                outcome = self.failure("partial")
                self.assertEqual(outcome.snapshot, state)
                calls = deepcopy(self.runner.calls)
                self.failure("ambiguous", transport="not-sent")
                self.assertEqual(self.runner.calls, calls)
        self.assertEqual(self.runner.calls.count(ENABLE), len(cases))

    def test_unrelated_newsticker_or_bar_change_is_ambiguous_and_never_overwritten(self):
        for section, key, value in (("left", "settings", {"feed": "concurrent-change"}),
                                    ("right", "spacing", 99)):
            with self.subTest(section=section):
                self.new_subject()
                changed = shell_state(True)
                entry = changed["config"]["bar"]["layout"][section][-1]
                entry[key] = value
                original = deepcopy(changed)
                self.attempt(after=changed)
                outcome = self.failure("ambiguous")
                self.assertEqual(outcome.snapshot, original)
                self.assertEqual(changed, original)
                self.assertEqual(self.subject.baseline, shell_state())
        self.assertFalse(any("disable" in args or "setShellConfig" in args
                             for args in self.runner.calls))

    def test_registry_only_newsticker_disable_add_remove_drift_is_ambiguous(self):
        for phase in ("discovery", "enable"):
            for change in ("disable", "add", "remove"):
                with self.subTest(phase=phase, change=change):
                    self.new_subject()
                    baseline = shell_state()
                    if change == "add":
                        del baseline["registry"][NEWS]
                    changed = shell_state(phase == "enable")
                    if change == "disable":
                        changed["registry"][NEWS] = False
                    elif change == "remove":
                        del changed["registry"][NEWS]
                    self.assertEqual(activation.without_plugin(changed["config"]),
                                     baseline["config"])
                    if phase == "discovery":
                        self.prepare(baseline=baseline, discovered=changed)
                    else:
                        self.prepare(baseline=baseline, discovered=baseline)
                        self.runner.expect(ENABLE)
                        self.runner.snapshot(changed)
                    outcome = self.failure("ambiguous", transport="ok")
                    self.assertIn("Unrelated plugin registry changed", outcome.detail)
                    self.assertEqual(outcome.snapshot, changed)
                    self.assertEqual(self.subject.baseline, baseline)
                    self.assertEqual(self.subject.enable_attempted, phase == "enable")
                    self.assertEqual(self.runner.calls[self.attempt_start:].count(ENABLE),
                                     int(phase == "enable"))
                    self.assert_activation_blocked()
        self.assertNotIn(DISABLE, self.runner.calls)

    def test_persisted_only_unrelated_drift_is_ambiguous_despite_unchanged_effective_config(self):
        for phase in ("discovery", "enable"):
            with self.subTest(phase=phase):
                self.new_subject()
                baseline = shell_state()
                baseline["persisted"] = deepcopy(baseline["config"])
                changed = shell_state(phase == "enable")
                changed["persisted"] = deepcopy(baseline["persisted"])
                changed["persisted"]["bar"]["layout"]["left"][0]["settings"]["feed"] = "disk-only"
                if phase == "discovery":
                    self.prepare(baseline=baseline, discovered=changed)
                else:
                    self.prepare(baseline=baseline, discovered=baseline)
                    self.runner.expect(ENABLE)
                    self.runner.snapshot(changed)
                outcome = self.failure("ambiguous", transport="ok")
                self.assertIn("Unrelated persisted shell configuration changed", outcome.detail)
                self.assertEqual(outcome.snapshot, changed)
                self.assertEqual(activation.without_plugin(outcome.snapshot["config"]),
                                 baseline["config"])
                self.assertEqual(self.subject.baseline, baseline)
                path = self.root / ".config/omarchy/shell.json"
                self.assertEqual(json.loads(path.read_text()), changed["persisted"])
                self.assertEqual(self.runner.calls[self.attempt_start:].count(ENABLE),
                                 int(phase == "enable"))
                self.assert_activation_blocked()
        self.assertNotIn(DISABLE, self.runner.calls)

    def test_prepare_captures_prepublication_baseline_and_normalizes_own_disk_changes(self):
        baseline = shell_state()
        baseline["persisted"] = deepcopy(baseline["config"])
        self.runner.snapshot(baseline)
        self.subject.prepare()
        self.assertEqual(self.subject.baseline, baseline)
        self.assertFalse(self.subject.activation_started)
        self.assertFalse(self.subject.enable_attempted)
        self.assertFalse(self.subject.disable_attempted)
        calls = deepcopy(self.runner.calls)
        self.subject.prepare()
        self.assertEqual(self.runner.calls, calls)
        # Simulate publication changing only our own persisted configuration.
        persisted = shell_state(True)["config"]
        path = write_persisted(self.root, persisted)
        discovered = shell_state()
        discovered["persisted"] = deepcopy(persisted)
        after = shell_state(True)
        after["persisted"] = deepcopy(persisted)
        self.attempt_start = len(self.runner.calls)
        self.runner.expect(["omarchy", "plugin", "validate",
                            str(self.root / ".config/omarchy/plugins" / PLUGIN)])
        self.runner.expect(RESCAN)
        self.runner.snapshot(discovered)
        self.runner.expect(ENABLE)
        self.runner.snapshot(after)
        outcome = self.subject.activate()
        self.assertEqual((outcome.state, outcome.transport), ("success", "ok"))
        self.assertEqual(outcome.snapshot, after)
        self.assertEqual(self.subject.baseline, baseline)
        self.assertEqual(json.loads(path.read_text()), persisted)
        self.assert_no_retry()

    def test_prepare_refuses_preexisting_activation_before_publication(self):
        self.runner.snapshot(shell_state(True))
        outcome = self.failure("ambiguous", self.subject.prepare, "not-sent")
        self.assertIn("not owned", outcome.detail)
        self.assertFalse(self.subject.activation_started)
        self.assertFalse(self.subject.enable_attempted)
        self.assertNotIn(ENABLE, self.runner.calls)
        self.assertNotIn(DISABLE, self.runner.calls)

    def test_absent_target_and_no_entries_is_inactive_for_cleanup(self):
        self.subject.baseline = shell_state()
        absent = shell_state()
        del absent["registry"][PLUGIN]
        self.assertEqual(self.subject.classify(absent)[0], "inactive")
        self.runner.snapshot(absent)
        outcome = self.subject.deactivate()
        self.assertEqual((outcome.state, outcome.transport), ("success", "not-needed"))
        self.assertEqual(outcome.snapshot, absent)
        self.assertNotIn(DISABLE, self.runner.calls)

    def test_absent_target_after_enable_timeout_remains_ambiguous_without_retry(self):
        absent = shell_state()
        del absent["registry"][PLUGIN]
        self.attempt(after=absent, command={"error": subprocess.TimeoutExpired(ENABLE, 12)})
        outcome = self.failure("ambiguous", transport="timeout")
        self.assertEqual(outcome.snapshot, absent)
        self.assertIn("inactive", outcome.detail)
        self.assert_no_retry()

    def test_validation_failure_never_enables(self):
        self.runner.snapshot()
        self.runner.expect(["omarchy", "plugin", "validate",
                            str(self.root / ".config/omarchy/plugins" / PLUGIN)], code=1)
        self.failure("rejected", transport="error")
        self.assertFalse(self.subject.enable_attempted)
        self.assertNotIn(ENABLE, self.runner.calls)

    def test_already_active_after_discovery_succeeds_without_enable(self):
        discovered = shell_state(True)
        self.prepare(discovered=discovered)
        outcome = self.subject.activate()
        self.assertEqual((outcome.state, outcome.transport), ("success", "ok"))
        self.assertEqual(outcome.snapshot, discovered)
        self.assertEqual(self.subject.baseline, shell_state())
        self.assertFalse(self.subject.enable_attempted)
        self.assertNotIn(ENABLE, self.runner.calls)
        self.assert_activation_blocked()

    def test_partial_after_discovery_aborts_without_enable(self):
        registry_only = shell_state()
        registry_only["registry"][PLUGIN] = True
        bar_only = shell_state(True)
        bar_only["registry"][PLUGIN] = False
        duplicate = shell_state(True)
        duplicate["config"]["bar"]["layout"]["right"].append({"id": PLUGIN})
        for discovered in (registry_only, bar_only, duplicate):
            with self.subTest(discovered=discovered):
                self.new_subject()
                self.prepare(discovered=discovered)
                outcome = self.failure("partial", transport="ok")
                self.assertEqual(outcome.snapshot, discovered)
                self.assertFalse(self.subject.enable_attempted)
                self.assertNotIn(ENABLE, self.runner.calls)
                self.assert_activation_blocked()

    def test_unrelated_change_after_discovery_aborts_without_enable(self):
        discovered = shell_state(True)
        discovered["config"]["bar"]["layout"]["left"][0]["settings"]["feed"] = "changed"
        original = deepcopy(discovered)
        self.prepare(discovered=discovered)
        self.failure("ambiguous", transport="ok")
        self.assertEqual(discovered, original)
        self.assertEqual(self.subject.baseline, shell_state())
        self.assertFalse(self.subject.enable_attempted)
        self.assertNotIn(ENABLE, self.runner.calls)
        self.assert_activation_blocked()

    def test_second_activate_after_package_failure_sends_no_commands(self):
        self.runner.snapshot()
        self.runner.expect(["omarchy", "plugin", "validate",
                            str(self.root / ".config/omarchy/plugins" / PLUGIN)], code=1)
        self.failure("rejected", transport="error")
        self.assertFalse(self.subject.enable_attempted)
        self.assertNotIn(ENABLE, self.runner.calls)
        self.assert_activation_blocked()

    def test_second_activate_after_query_failure_sends_no_commands(self):
        for response in ({"code": 124}, {"stdout": "not JSON"}):
            with self.subTest(response=response):
                self.new_subject()
                self.runner.expect(["omarchy-shell", "shell", "listPlugins"], **response)
                self.failure("ambiguous", transport="not-sent")
                self.assertIsNone(self.subject.baseline)
                self.assertFalse(self.subject.enable_attempted)
                self.assertNotIn(ENABLE, self.runner.calls)
                self.assert_activation_blocked()

    def test_rescan_lost_reply_still_uses_authoritative_discovery(self):
        self.prepare(discovery={"error": subprocess.TimeoutExpired(RESCAN, 12)})
        self.runner.expect(ENABLE)
        self.runner.snapshot(shell_state(True))
        self.assertEqual(self.subject.activate().state, "success")
        self.assert_no_retry()

    def test_unavailable_initial_query_never_enables(self):
        self.runner.expect(["omarchy-shell", "shell", "listPlugins"], code=124)
        self.failure("ambiguous", transport="not-sent")
        self.assertNotIn(ENABLE, self.runner.calls)

    def test_malformed_initial_query_never_enables(self):
        self.runner.expect(["omarchy-shell", "shell", "listPlugins"], stdout="not JSON")
        self.failure("ambiguous", transport="not-sent")
        self.assertNotIn(ENABLE, self.runner.calls)

    def test_duplicate_registry_is_malformed_not_partial(self):
        entry = {"id": PLUGIN, "enabled": False}
        self.runner.expect(["omarchy-shell", "shell", "listPlugins"], value=[entry, entry])
        self.runner.expect(["omarchy-shell", "shell", "listShellConfig"], value=shell_state()["config"])
        self.runner.expect(["omarchy-shell", "shell", "listPlugins"], value=[entry, entry])
        self.failure("ambiguous", transport="not-sent")
        self.assertNotIn(ENABLE, self.runner.calls)

    def test_registry_change_during_observation_is_ambiguous(self):
        self.runner.expect(["omarchy-shell", "shell", "listPlugins"],
                           value=[{"id": PLUGIN, "enabled": False}])
        self.runner.expect(["omarchy-shell", "shell", "listShellConfig"], value=shell_state()["config"])
        self.runner.expect(["omarchy-shell", "shell", "listPlugins"],
                           value=[{"id": PLUGIN, "enabled": True}])
        self.failure("ambiguous", transport="not-sent")

    def test_missing_bar_layout_is_ambiguous(self):
        state = shell_state()
        del state["config"]["bar"]["layout"]
        self.runner.snapshot(state)
        self.failure("ambiguous", transport="not-sent")

    def test_malformed_or_unavailable_post_enable_query_is_ambiguous(self):
        for response in ({"stdout": "{"}, {"code": 124}):
            with self.subTest(response=response):
                self.new_subject()
                self.prepare()
                self.runner.expect(ENABLE)
                self.runner.expect(["omarchy-shell", "shell", "listPlugins"], **response)
                self.failure("ambiguous", transport="ok")
                calls = deepcopy(self.runner.calls)
                self.failure("ambiguous", transport="not-sent")
                self.assertEqual(self.runner.calls, calls)

    def test_preexisting_committed_state_never_sends_enable(self):
        self.runner.snapshot(shell_state(True))
        self.failure("ambiguous", transport="not-sent")
        self.assertNotIn(ENABLE, self.runner.calls)

    def test_refused_preexisting_activation_then_deactivate_never_disables(self):
        active = shell_state(True)
        registry_only = shell_state()
        registry_only["registry"][PLUGIN] = True
        placed_only = shell_state(True)
        placed_only["registry"][PLUGIN] = False
        for baseline in (active, registry_only, placed_only):
            with self.subTest(baseline=baseline):
                self.new_subject()
                self.runner.snapshot(baseline)
                outcome = self.failure("ambiguous", transport="not-sent")
                self.assertIn("not owned", outcome.detail)
                self.assertEqual(self.subject.baseline, baseline)
                self.assertFalse(self.subject.enable_attempted)
                calls = deepcopy(self.runner.calls)
                cleanup = self.failure("ambiguous", self.subject.deactivate, "not-sent")
                self.assertIn("Refusing to disable pre-existing", cleanup.detail)
                self.assertEqual(self.runner.calls, calls)
                self.assertFalse(self.subject.disable_attempted)
                self.assertNotIn(DISABLE, self.runner.calls)
                self.assert_activation_blocked()

    def test_deactivation_queries_before_and_after_one_disable(self):
        self.attempt()
        self.subject.activate()
        self.runner.snapshot(shell_state(True))
        self.runner.expect(DISABLE)
        self.runner.snapshot()
        outcome = self.subject.deactivate()
        self.assertEqual((outcome.state, outcome.transport), ("success", "ok"))
        self.assertEqual(outcome.snapshot, shell_state())
        self.assertEqual(activation.without_plugin(shell_state(True)["config"]),
                         outcome.snapshot["config"])
        self.runner.snapshot()
        self.failure("ambiguous", self.subject.deactivate, "not-sent")
        self.assertEqual(self.runner.calls.count(DISABLE), 1)

    def test_deactivation_recovers_timeout_after_committed_disable(self):
        self.subject.baseline = shell_state()
        self.subject.enable_attempted = True
        self.runner.snapshot(shell_state(True))
        self.runner.expect(DISABLE, error=subprocess.TimeoutExpired(DISABLE, 12))
        self.runner.snapshot()
        outcome = self.subject.deactivate()
        self.assertEqual((outcome.state, outcome.transport), ("success", "timeout"))
        self.assertEqual(outcome.snapshot, shell_state())
        self.assertEqual(self.runner.calls.count(DISABLE), 1)

    def test_deactivation_unavailable_before_disable_leaves_cleanup_pending(self):
        self.subject.baseline = shell_state()
        self.subject.enable_attempted = True
        self.runner.expect(["omarchy-shell", "shell", "listPlugins"], code=124)
        self.failure("ambiguous", self.subject.deactivate, "query-error")
        self.assertFalse(self.subject.disable_attempted)
        self.assertNotIn(DISABLE, self.runner.calls)
        self.assertFalse(any(event["event"] == "deactivated" for event in self.events()))

    def test_deactivation_unavailable_after_disable_leaves_cleanup_pending_no_resend(self):
        self.subject.baseline = shell_state()
        self.subject.enable_attempted = True
        self.runner.snapshot(shell_state(True))
        self.runner.expect(DISABLE)
        self.runner.expect(["omarchy-shell", "shell", "listPlugins"], code=124)
        self.failure("ambiguous", self.subject.deactivate, "ok")
        self.assertTrue(self.subject.disable_attempted)
        self.assertFalse(any(event["event"] == "deactivated" for event in self.events()))
        self.runner.snapshot()
        self.failure("ambiguous", self.subject.deactivate, "not-sent")
        self.assertEqual(self.runner.calls.count(DISABLE), 1)

    def test_deactivation_unrelated_change_is_not_overwritten_or_declared_complete(self):
        self.subject.baseline = shell_state()
        self.subject.enable_attempted = True
        self.runner.snapshot(shell_state(True))
        self.runner.expect(DISABLE)
        changed = shell_state()
        changed["config"]["bar"]["layout"]["left"][0]["settings"]["feed"] = "new"
        self.runner.snapshot(changed)
        outcome = self.failure("ambiguous", self.subject.deactivate)
        self.assertEqual(outcome.snapshot, changed)
        self.assertEqual(self.runner.calls.count(DISABLE), 1)
        self.assertFalse(any(event["event"] == "deactivated" for event in self.events()))

    def test_deactivation_without_baseline_is_pending_without_commands(self):
        self.failure("ambiguous", self.subject.deactivate, "not-sent")
        self.assertEqual(self.runner.calls, [])

    def test_no_enable_sent_and_verified_inactive_needs_no_disable(self):
        self.subject.baseline = shell_state()
        self.runner.snapshot()
        outcome = self.subject.deactivate()
        self.assertEqual((outcome.state, outcome.transport), ("success", "not-needed"))
        self.assertNotIn(DISABLE, self.runner.calls)


# Both executable names are real private files, not mocked subprocess results.
# PATH contains only their directory; the explicit interpreter needs no lookup.
FAKE_EXECUTABLE = r'''#!/usr/bin/python3
import json
import os
from pathlib import Path
import subprocess
import sys
import time

name = Path(sys.argv[0]).name
args = sys.argv[1:]
state_path = Path(os.environ["FAKE_STATE"])
with Path(os.environ["FAKE_LOG"]).open("a") as stream:
    stream.write(json.dumps({"name": name, "args": args,
        "ipc_timeout": os.environ.get("OMARCHY_SHELL_IPC_TIMEOUT"),
        "home": os.environ.get("HOME"), "path": os.environ.get("PATH")}) + "\n")
assert os.environ["OMARCHY_SHELL_IPC_TIMEOUT"] == "10s"
state = json.loads(state_path.read_text())
plugin = state["plugin"]
if name == "omarchy":
    assert args[:1] == ["plugin"], args
    action = args[1]
    if action == "validate":
        assert args[2] == str(Path(os.environ["HOME"]) / ".config/omarchy/plugins" / plugin)
        sys.exit(0)
    assert action in ("enable", "disable"), args
    expected = ["plugin", action, plugin]
    if action == "enable":
        expected += ["--section", "right", "--index", "0"]
    assert args == expected, args
    # Nested executable lookup is deliberately confined to the private PATH too.
    reply = subprocess.run(["omarchy-shell", "shell", action, plugin],
                           capture_output=True, text=True, timeout=10)
    sys.stdout.write(reply.stdout)
    sys.stderr.write(reply.stderr)
    sys.exit(124 if action == "enable" and state["exit124"] else reply.returncode)
assert name == "omarchy-shell", name
assert args[0] == "shell", args
method = args[1]
if method == "listPlugins":
    print(json.dumps([{"id": key, "enabled": value}
                      for key, value in state["registry"].items()]))
elif method == "listShellConfig":
    print(json.dumps(state["config"]))
elif method == "rescanPlugins":
    print("rescanned")
elif method in ("enable", "disable"):
    assert args[2] == plugin
    state["registry"][plugin] = method == "enable"
    for entries in state["config"]["bar"]["layout"].values():
        entries[:] = [entry for entry in entries if entry["id"] != plugin]
    if method == "enable":
        state["config"]["bar"]["layout"]["right"].insert(0, {"id": plugin})
        state["config"]["plugins"].append(plugin)
    else:
        state["config"]["plugins"] = [key for key in state["config"]["plugins"] if key != plugin]
    state_path.write_text(json.dumps(state))
    if method == "enable":
        time.sleep(state["delay"])
    print("committed")
else:
    raise AssertionError(args)
'''


class ProcessCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="swiftwall-process-cleanup-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.command = self.root / "private-command"
        self.command.write_text('''#!/usr/bin/python3
import os
import sys
import time

size = int(sys.argv[2])
sys.stdout.write("o" * size + "\\\\n")
sys.stderr.write("e" * size + "\\\\n")
sys.stdout.flush()
sys.stderr.flush()
# Keep inherited output descriptors open after the leader's normal exit.
if os.fork() == 0:
    time.sleep(30)
    os._exit(0)
if sys.argv[1] == "timeout":
    time.sleep(30)
os._exit(7)
''')
        self.command.chmod(0o700)
        self.env = {"HOME": str(self.root), "PATH": str(self.root)}

    def run_checked(self, mode, size=256 * 1024):
        events = []
        children = []
        real_popen = subprocess.Popen
        real_killpg = os.killpg
        real_waitid = os.waitid

        def observe(idtype, pid, options):
            self.assertEqual(idtype, os.P_PID)
            self.assertEqual(options, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            result = real_waitid(idtype, pid, options)
            events.append(("observe", result))
            return result

        def private_popen(args, **kwargs):
            self.assertEqual(args[0], str(self.command))
            self.assertEqual(kwargs["env"], self.env)
            self.assertTrue(kwargs["start_new_session"])
            self.assertFalse(kwargs.get("shell", False))
            child = real_popen(args, **kwargs)
            children.append(child)
            original_wait, original_poll = child.wait, child.poll

            def wait(*args, **kwargs):
                self.assertIn("killpg", [event[0] for event in events])
                events.append(("wait", None))
                return original_wait(*args, **kwargs)

            def poll(*args, **kwargs):
                self.assertIn("killpg", [event[0] for event in events])
                return original_poll(*args, **kwargs)

            child.wait = wait
            child.poll = poll
            child.communicate = mock.Mock(side_effect=AssertionError("communicate may reap"))
            return child

        def killpg(pid, sig):
            self.assertEqual(pid, children[0].pid)
            self.assertEqual(sig, activation.signal.SIGKILL)
            self.assertIsNone(children[0].returncode)
            # Inspect the actual child, not just mocked method ordering: it must
            # still be waitable (or running), never already reaped at cleanup.
            status = real_waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if mode == "normal":
                self.assertIsNotNone(status)
                self.assertEqual(status.si_status, 7)
            else:
                self.assertIsNone(status)
            events.append(("killpg", status))
            real_killpg(pid, sig)

        args = [str(self.command), mode, str(size)]
        started = time.monotonic()
        with mock.patch.object(subprocess, "Popen", side_effect=private_popen), \
                mock.patch.object(os, "killpg", side_effect=killpg), \
                mock.patch.object(os, "waitid", side_effect=observe):
            if mode == "timeout":
                with self.assertRaises(subprocess.TimeoutExpired) as caught:
                    activation.process(args, env=self.env, timeout=0.5)
                result = caught.exception
                self.assertEqual(result.cmd, args)
                self.assertEqual(result.timeout, 0.5)
                self.assertEqual(result.output, b"o" * size + b"\\n")
                self.assertEqual(result.stderr, b"e" * size + b"\\n")
            else:
                result = activation.process(args, env=self.env, timeout=3)
                self.assertIsInstance(result, subprocess.CompletedProcess)
                self.assertEqual(result.args, args)
                self.assertEqual(result.returncode, 7)
                limit = 1024 * 1024
                self.assertEqual(result.stdout, ("o" * size + "\\n")[:limit])
                self.assertEqual(result.stderr, ("e" * size + "\\n")[:limit])
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual([event[0] for event in events].count("wait"), 1)
        self.assertEqual([event[0] for event in events].count("killpg"), 1)
        self.assertTrue(any(event[0] == "observe" for event in events))
        children[0].communicate.assert_not_called()
        with self.assertRaises(ChildProcessError):
            real_waitid(os.P_PID, children[0].pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)

    def test_normal_exit_kills_group_before_reaping_leader(self):
        self.run_checked("normal")

    def test_timeout_kills_group_before_reaping_leader(self):
        self.run_checked("timeout")

    def test_file_capture_is_limited_without_pipe_deadlock(self):
        self.run_checked("normal", size=2 * 1024 * 1024)


class PrivateProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="swiftwall-activation-process-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.state_path = self.root / "state.json"
        self.log_path = self.root / "commands.jsonl"
        for name in ("omarchy", "omarchy-shell"):
            executable = self.bin / name
            executable.write_text(FAKE_EXECUTABLE)
            executable.chmod(0o700)
        self.original_env = dict(os.environ)
        self.addCleanup(lambda: self.assertEqual(dict(os.environ), self.original_env))
        self.env = {"HOME": str(self.home), "PATH": str(self.bin),
                    "FAKE_STATE": str(self.state_path), "FAKE_LOG": str(self.log_path),
                    "OMARCHY_SHELL_IPC_TIMEOUT": "2s"}
        # Guard the real default runner, not replace it with a simulated result.
        # Any accidental attempt to use systemctl, a shell, or /usr/bin Omarchy
        # fails before a process can start. Children inherit the private PATH.
        real_popen = subprocess.Popen

        def private_popen(args, **kwargs):
            self.assertIsInstance(args, list)
            self.assertIn(args[0], ("omarchy", "omarchy-shell"))
            self.assertEqual(kwargs["env"]["PATH"], str(self.bin))
            self.assertEqual(kwargs["env"]["HOME"], str(self.home))
            self.assertEqual(kwargs["env"]["OMARCHY_SHELL_IPC_TIMEOUT"], "10s")
            self.assertTrue((self.bin / args[0]).is_file())
            self.assertFalse(kwargs.get("shell", False))
            return real_popen(args, **kwargs)

        guard = mock.patch.object(subprocess, "Popen", side_effect=private_popen)
        guard.start()
        self.addCleanup(guard.stop)
        self.subject = activation.OmarchyActivation(
            home=self.home, evidence=self.root / "evidence", env=self.env)
        self.assertIs(self.subject.runner, activation.process)

    def seed(self, *, delay=0, exit124=False):
        state = shell_state()
        state.update(plugin=PLUGIN, delay=delay, exit124=exit124)
        self.state_path.write_text(json.dumps(state))

    def log(self):
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def assert_private_environment(self, *, enable_count=1):
        calls = self.log()
        self.assertTrue(calls)
        for call in calls:
            self.assertEqual(call["ipc_timeout"], "10s")
            self.assertEqual(call["home"], str(self.home))
            self.assertEqual(call["path"], str(self.bin))
        self.assertEqual(sum(call["name"] == "omarchy" and call["args"] == ENABLE[1:]
                             for call in calls), enable_count)
        self.assertEqual(sum(call["name"] == "omarchy-shell" and
                             call["args"] == ["shell", "enable", PLUGIN] for call in calls), enable_count)
        self.assertEqual(self.env["OMARCHY_SHELL_IPC_TIMEOUT"], "2s")
        before = self.log_path.read_text()
        with self.assertRaises(activation.ActivationError):
            self.subject.activate()
        self.assertEqual(self.log_path.read_text(), before)

    def test_actual_default_runner_env_reaches_outer_and_nested_fake(self):
        self.seed()
        outcome = self.subject.activate()
        self.assertEqual((outcome.state, outcome.transport), ("success", "ok"))
        self.assertEqual(outcome.snapshot, shell_state(True))
        self.assert_private_environment()

    def test_actual_delayed_reply_over_two_seconds_is_not_old_ipc_timeout(self):
        self.seed(delay=2.2)
        started = time.monotonic()
        outcome = self.subject.activate()
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 2.2)
        self.assertEqual((outcome.state, outcome.transport), ("success", "ok"))
        self.assertEqual(outcome.snapshot, shell_state(True))
        self.assert_private_environment()

    def test_actual_exit124_after_commit_recovered_by_actual_queries(self):
        self.seed(exit124=True)
        outcome = self.subject.activate()
        self.assertEqual((outcome.state, outcome.transport), ("success", "timeout"))
        self.assertEqual(outcome.snapshot, shell_state(True))
        calls = self.log()
        nested = next(index for index, call in enumerate(calls)
                      if call["name"] == "omarchy-shell" and call["args"][1] == "enable")
        self.assertEqual([call["args"] for call in calls[nested + 1:]], [
            ["shell", "listPlugins"], ["shell", "listShellConfig"], ["shell", "listPlugins"]])
        self.assert_private_environment()

    def test_actual_persisted_disk_drift_is_independent_of_effective_fake_state(self):
        self.seed()
        original_fake_state = self.state_path.read_text()
        persisted = {"plugins": [NEWS], "theme": {"name": "disk-original"}}
        path = write_persisted(self.home, persisted)
        self.subject.prepare()
        baseline = shell_state()
        baseline["persisted"] = deepcopy(persisted)
        self.assertEqual(self.subject.baseline, baseline)
        persisted["theme"]["name"] = "disk-concurrent-change"
        write_persisted(self.home, persisted)
        with self.assertRaises(activation.ActivationError) as caught:
            self.subject.activate()
        outcome = caught.exception.outcome
        self.assertEqual((outcome.state, outcome.transport), ("ambiguous", "ok"))
        self.assertIn("persisted shell configuration changed", outcome.detail)
        self.assertEqual(outcome.snapshot["config"], shell_state()["config"])
        self.assertEqual(outcome.snapshot["persisted"], persisted)
        self.assertEqual(self.subject.baseline, baseline)
        self.assertEqual(self.state_path.read_text(), original_fake_state)
        self.assertEqual(json.loads(path.read_text()), persisted)
        self.assertFalse(self.subject.enable_attempted)
        self.assertFalse(any(call["name"] == "omarchy" and call["args"][1] == "disable"
                             for call in self.log()))
        self.assert_private_environment(enable_count=0)

    def test_actual_deactivation_preserves_unrelated_entries(self):
        self.seed()
        self.subject.activate()
        before = len(self.log())
        outcome = self.subject.deactivate()
        self.assertEqual((outcome.state, outcome.transport), ("success", "ok"))
        self.assertEqual(outcome.snapshot, shell_state())
        state = json.loads(self.state_path.read_text())
        self.assertEqual(state["config"], shell_state()["config"])
        self.assertEqual([call["args"] for call in self.log()[before:]], [
            ["shell", "listPlugins"], ["shell", "listShellConfig"], ["shell", "listPlugins"],
            DISABLE[1:], ["shell", "disable", PLUGIN],
            ["shell", "listPlugins"], ["shell", "listShellConfig"], ["shell", "listPlugins"]])
        self.assert_private_environment()


if __name__ == "__main__":
    unittest.main()
