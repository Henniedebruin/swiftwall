"""Pure preview activation tests: injected transport, temporary HOME, no processes."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import plugin_activation as activation


PRODUCTION = "io.github.henniedebruin.swiftwall"
PREVIEW_A = "local.swiftwall-preview-0123456789abcdef0123456789abcdef"
PREVIEW_B = "local.swiftwall-preview-fedcba9876543210fedcba9876543210"
NEWS = "io.test.NewsTicker"


def config_for(others):
    return {
        "bar": {"height": 32, "layout": {
            "left": [{"id": NEWS, "settings": {"feed": "original"}}],
            "center": [{"id": "clock", "format": "hh:mm"}],
            "right": [{"id": item, "settings": {"keep": item}} for item in others]
                     + [{"id": "tray", "spacing": 7}],
        }},
        "plugins": [NEWS, *others, {"id": others[0], "settings": {"keep": True}}],
        "disabledPlugins": [{"id": "unrelated-disabled", "settings": {"keep": True}}],
        "theme": {"name": "isolated-test"},
    }


class FakeShell:
    """Authoritative runtime state and real temporary JSON; never falls back to IPC."""

    def __init__(self, test, home, target, others):
        self.test = test
        self.home = home
        self.target = target
        self.registry = {target: False, NEWS: True, **dict.fromkeys(others, True)}
        self.config = config_for(others)
        self.persisted = deepcopy(self.config)
        self.persisted["disabledPlugins"].extend([target, {"id": target}])
        self.path = home / ".config/omarchy/shell.json"
        self.path.parent.mkdir(parents=True)
        self.write()
        self.calls = []
        self.commit_enable = True
        self.enable_reply = "ok"
        self.disable_reply = "ok"
        self.read_error_after_enable = False
        self.read_error = False
        self.drift_after_enable = None

    def write(self):
        self.path.write_text(json.dumps(self.persisted))

    def state(self):
        return deepcopy({"registry": self.registry, "config": self.config,
                         "persisted": json.loads(self.path.read_text())})

    def mutate(self, enabled):
        self.registry[self.target] = enabled
        for config in (self.config, self.persisted):
            for entries in config["bar"]["layout"].values():
                entries[:] = [entry for entry in entries if entry["id"] != self.target]
            for key in ("plugins", "disabledPlugins"):
                config[key] = [entry for entry in config[key]
                               if entry != self.target and not
                               (isinstance(entry, dict) and entry.get("id") == self.target)]
            if enabled:
                config["bar"]["layout"]["right"].insert(0, {"id": self.target})
                config["plugins"].append(self.target)
        self.write()

    def __call__(self, args, *, env, timeout, capture_output, text):
        self.test.assertEqual(env, {"HOME": str(self.home), "PATH": "/nonexistent",
                                   "OMARCHY_SHELL_IPC_TIMEOUT": "10s"})
        self.test.assertEqual(timeout, 12)
        self.test.assertIs(capture_output, True)
        self.test.assertIs(text, True)
        self.calls.append(list(args))
        value = None
        reply = "ok"
        if args == ["omarchy-shell", "shell", "listPlugins"]:
            if self.read_error:
                raise OSError("fake authoritative registry unavailable")
            value = [{"id": key, "enabled": enabled}
                     for key, enabled in self.registry.items()]
        elif args == ["omarchy-shell", "shell", "listShellConfig"]:
            value = self.config
        elif args == ["omarchy-shell", "shell", "rescanPlugins"]:
            pass
        elif args == ["omarchy", "plugin", "validate",
                      str(self.home / ".config/omarchy/plugins" / self.target)]:
            pass
        elif args == self.enable_argv:
            if self.commit_enable:
                self.mutate(True)
            if self.drift_after_enable is not None:
                self.drift_after_enable()
            self.read_error = self.read_error_after_enable
            reply = self.enable_reply
        elif args == self.disable_argv:
            self.mutate(False)
            reply = self.disable_reply
        else:
            self.test.fail(f"Unexpected command (no real-process fallback): {args!r}")
        if reply == "timeout-exception":
            raise subprocess.TimeoutExpired(args, timeout)
        return subprocess.CompletedProcess(args, 124 if reply == "timeout-exit" else 0,
                                           "" if value is None else json.dumps(value), "")

    @property
    def enable_argv(self):
        return ["omarchy", "plugin", "enable", self.target,
                "--section", "right", "--index", "0"]

    @property
    def disable_argv(self):
        return ["omarchy", "plugin", "disable", self.target]


class PreviewActivationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="swiftwall-preview-activation-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.addCleanup(self.assert_global_unchanged)

    def assert_global_unchanged(self):
        self.assertEqual(activation.PLUGIN_ID, PRODUCTION)

    def controller(self, target=PREVIEW_A, *, name="home", default=False):
        others = [item for item in (PRODUCTION, PREVIEW_A, PREVIEW_B) if item != target]
        home = self.root / name
        runner = FakeShell(self, home, target, others)
        env = {"HOME": str(home), "PATH": "/nonexistent",
               "OMARCHY_SHELL_IPC_TIMEOUT": "2s"}
        kwargs = {} if default else {"plugin_id": target}
        subject = activation.OmarchyActivation(home=home, evidence=home / "evidence",
                                               runner=runner, env=env, **kwargs)
        self.assertEqual(env["OMARCHY_SHELL_IPC_TIMEOUT"], "2s")
        self.assertEqual(runner.calls, [])
        self.assertFalse((home / "evidence").exists())
        return subject, runner

    def failure(self, operation, state="ambiguous"):
        with self.assertRaises(activation.ActivationError) as caught:
            operation()
        self.assertEqual(caught.exception.outcome.state, state)
        return caught.exception.outcome

    def assert_no_second_enable(self, subject, runner):
        before = deepcopy(runner.calls)
        self.failure(subject.activate)
        self.assertEqual(runner.calls, before)
        self.assertEqual(runner.calls.count(runner.enable_argv), 1)

    def assert_full_preview(self, outcome, runner):
        self.assertEqual(outcome.state, "success")
        self.assertEqual(outcome.snapshot, runner.state())
        self.assertIs(outcome.snapshot["registry"][runner.target], True)
        placements = [(section, index) for section, entries in
                      outcome.snapshot["config"]["bar"]["layout"].items()
                      for index, entry in enumerate(entries) if entry["id"] == runner.target]
        self.assertEqual(placements, [("right", 0)])
        self.assertEqual(outcome.snapshot["persisted"]["bar"]["layout"]["right"][0],
                         {"id": runner.target})

    def test_default_production_identity_and_commands_unchanged(self):
        subject, runner = self.controller(PRODUCTION, default=True)
        self.assertEqual(subject.plugin_id, PRODUCTION)
        self.assert_full_preview(subject.activate(), runner)
        self.assertEqual(subject.deactivate().state, "success")
        self.assertEqual(runner.calls.count(runner.enable_argv), 1)
        self.assertEqual(runner.calls.count(runner.disable_argv), 1)
        self.assert_global_unchanged()

    def test_accepts_only_production_or_exact_lowercase_hex_preview_ids(self):
        valid = (PRODUCTION, PREVIEW_A, PREVIEW_B,
                 "local.swiftwall-preview-" + "0" * 32,
                 "local.swiftwall-preview-" + "f" * 32)
        invalid = ("", NEWS, "io.github.henniedebruin.NewsTicker", "local.NewsTicker",
                   "local.swiftwall-preview-" + "a" * 31,
                   "local.swiftwall-preview-" + "a" * 33,
                   "local.swiftwall-preview-" + "A" * 32,
                   "local.swiftwall-preview-" + "g" * 32,
                   PREVIEW_A + "\n", PREVIEW_A + "/child", " " + PREVIEW_A,
                   PREVIEW_A.replace("local.", "localX"),
                   "io.github.henniedebruin.swiftwall-preview-" + "a" * 32)
        def forbidden_runner(*args, **kwargs):
            self.fail("Constructor must never invoke a command")
        for plugin_id in valid + invalid:
            with self.subTest(plugin_id=plugin_id):
                kwargs = dict(home=self.root, evidence=self.root / "unused",
                              runner=forbidden_runner, env={}, plugin_id=plugin_id)
                if plugin_id in valid:
                    self.assertEqual(activation.OmarchyActivation(**kwargs).plugin_id, plugin_id)
                else:
                    with self.assertRaises(ValueError):
                        activation.OmarchyActivation(**kwargs)
        self.assertFalse((self.root / "unused").exists())

    def test_two_controllers_normalize_only_their_own_identity(self):
        first, _ = self.controller(PREVIEW_A, name="first")
        second, _ = self.controller(PREVIEW_B, name="second")
        original = config_for([PRODUCTION, PREVIEW_A, PREVIEW_B])
        original["bar"]["layout"]["left"].append({"id": PREVIEW_A})
        original["bar"]["layout"]["center"].append({"id": PREVIEW_B})
        original["plugins"].extend([{"id": PREVIEW_A}, {"id": PREVIEW_B}])
        original["disabledPlugins"].extend([PREVIEW_A, {"id": PREVIEW_A},
                                             PREVIEW_B, {"id": PREVIEW_B}])
        untouched = deepcopy(original)
        for subject in (first, second, first):
            with self.subTest(target=subject.plugin_id):
                expected = config_for([PRODUCTION, PREVIEW_B if subject is first else PREVIEW_A])
                other = PREVIEW_B if subject is first else PREVIEW_A
                section = "center" if subject is first else "left"
                expected["bar"]["layout"][section].append({"id": other})
                expected["plugins"].append({"id": other})
                expected["disabledPlugins"].extend([other, {"id": other}])
                self.assertEqual(subject.normalized(original), expected)
                self.assertEqual(original, untouched)
        self.assert_global_unchanged()
        self.assertEqual(activation.without_plugin(original),
                         activation.OmarchyActivation(home=self.root, evidence=self.root / "unused",
                                                     runner=lambda *a, **k: self.fail("No commands"),
                                                     env={}).normalized(original))

    def test_two_independent_controllers_activate_and_cleanup_only_own_id(self):
        first, first_runner = self.controller(PREVIEW_A, name="first")
        second, second_runner = self.controller(PREVIEW_B, name="second")
        baselines = [first_runner.state(), second_runner.state()]
        for subject, runner, baseline in zip((first, second), (first_runner, second_runner), baselines):
            with self.subTest(target=subject.plugin_id):
                self.assertIs(baseline["registry"][subject.plugin_id], False)
                self.assert_full_preview(subject.activate(), runner)
                self.assertEqual(subject.baseline, baseline)
                self.assertEqual(subject.normalized(runner.config), subject.normalized(baseline["config"]))
                self.assertEqual(subject.normalized(runner.persisted), subject.normalized(baseline["persisted"]))
                self.assertEqual({key: value for key, value in runner.registry.items()
                                  if key != subject.plugin_id},
                                 {key: value for key, value in baseline["registry"].items()
                                  if key != subject.plugin_id})
                self.assertEqual(subject.deactivate().state, "success")
                self.assertEqual(runner.registry, baseline["registry"])
                self.assertEqual(runner.config, baseline["config"])
                self.assertEqual(subject.normalized(runner.persisted), subject.normalized(baseline["persisted"]))
                self.assertEqual(runner.calls.count(runner.disable_argv), 1)
                self.assert_no_second_enable(subject, runner)
        self.assert_global_unchanged()

    def test_completed_enable_timeout_reconciles_success_without_second_enable(self):
        for reply in ("timeout-exception", "timeout-exit"):
            with self.subTest(reply=reply):
                subject, runner = self.controller(name=reply)
                runner.enable_reply = reply
                outcome = subject.activate()
                self.assertEqual(outcome.transport, "timeout")
                self.assert_full_preview(outcome, runner)
                self.assert_no_second_enable(subject, runner)

    def test_inactive_after_enable_timeout_is_ambiguous_without_second_enable(self):
        subject, runner = self.controller()
        runner.commit_enable = False
        runner.enable_reply = "timeout-exception"
        outcome = self.failure(subject.activate)
        self.assertEqual(outcome.transport, "timeout")
        self.assertIs(outcome.snapshot["registry"][PREVIEW_A], False)
        self.assert_no_second_enable(subject, runner)

    def test_post_enable_read_error_is_ambiguous_without_second_enable(self):
        subject, runner = self.controller()
        runner.read_error_after_enable = True
        self.failure(subject.activate)
        self.assert_no_second_enable(subject, runner)
        self.failure(subject.deactivate)
        self.assertNotIn(runner.disable_argv, runner.calls)

    def test_initial_read_error_never_enables_or_retries(self):
        subject, runner = self.controller()
        runner.read_error = True
        self.failure(subject.activate)
        before = deepcopy(runner.calls)
        self.failure(subject.activate)
        self.assertEqual(runner.calls, before)
        self.assertNotIn(runner.enable_argv, runner.calls)

    def test_production_other_preview_and_news_drift_is_not_normalized_away(self):
        for identity in (PRODUCTION, PREVIEW_B, NEWS):
            for kind in ("registry", "config", "persisted"):
                with self.subTest(identity=identity, kind=kind):
                    subject, runner = self.controller(name=f"{identity}-{kind}")
                    def drift():
                        if kind == "registry":
                            runner.registry[identity] = False
                        else:
                            config = getattr(runner, kind)
                            config["bar"]["layout"]["left"].append(
                                {"id": identity, "settings": {"changed": True}})
                            runner.write()
                    runner.drift_after_enable = drift
                    self.failure(subject.activate)
                    changed = runner.state()
                    self.assert_no_second_enable(subject, runner)
                    self.assertEqual(runner.state(), changed)

    def test_completed_disable_timeout_is_scoped_and_never_resent(self):
        subject, runner = self.controller()
        self.assert_full_preview(subject.activate(), runner)
        runner.disable_reply = "timeout-exception"
        outcome = subject.deactivate()
        self.assertEqual((outcome.state, outcome.transport), ("success", "timeout"))
        self.assertIs(runner.registry[PREVIEW_A], False)
        for identity in (PRODUCTION, PREVIEW_B, NEWS):
            self.assertIs(runner.registry[identity], True)
        after = runner.state()
        self.failure(subject.deactivate)
        self.assertEqual(runner.calls.count(runner.disable_argv), 1)
        self.assertEqual(runner.state(), after)


if __name__ == "__main__":
    unittest.main()
