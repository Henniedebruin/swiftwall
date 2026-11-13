"""Activation evidence safety: private fixtures only, no live shell or manager."""
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

from deployment import Deployment, ENGINE_ENVIRONMENT
from plugin_activation import OmarchyActivation


class ActivationEvidenceTests(unittest.TestCase):
    def test_activation_rejects_hardlink_and_fifo_logs_without_writing(self):
        for kind in ("hardlink", "fifo"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                evidence = root / "evidence"
                evidence.mkdir()
                target = root / "target"
                target.write_bytes(b"untouched")
                log = evidence / "activation.jsonl"
                if kind == "hardlink":
                    os.link(target, log)
                else:
                    os.mkfifo(log)
                subject = OmarchyActivation(home=root, evidence=evidence)
                with self.assertRaises((OSError, ValueError)):
                    subject.record("snapshot", config={"secret": "dummy-canary"})
                self.assertEqual(target.read_bytes(), b"untouched")

    def test_activation_rejects_symlinked_evidence_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir()
            evidence = root / "evidence"
            evidence.symlink_to(target, target_is_directory=True)
            subject = OmarchyActivation(home=root, evidence=evidence)
            with self.assertRaises((OSError, ValueError)):
                subject.record("snapshot", config={"secret": "dummy-canary"})
            self.assertEqual(list(target.iterdir()), [])

    def test_activation_logs_are_private_and_existing_entries_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "evidence"
            subject = OmarchyActivation(home=root, evidence=evidence)
            previous_umask = os.umask(0)
            try:
                subject.record("first")
                log = evidence / "activation.jsonl"
                self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
                log.chmod(0o666)
                evidence.chmod(0o777)
                subject.record("second")
            finally:
                os.umask(previous_umask)
            self.assertEqual(stat.S_IMODE(evidence.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
            self.assertEqual([json.loads(line)["event"] for line in log.read_text().splitlines()],
                             ["first", "second"])

    def test_coordinator_tightening_does_not_trust_preplanted_activation_symlink(self):
        with tempfile.TemporaryDirectory(prefix="swiftwall-evidence-") as temporary:
            root = Path(temporary)
            home = root / "home"
            config_path = home / ".config/omarchy/shell.json"
            config_path.parent.mkdir(parents=True)
            canary = "dummy-controller-api-secret-only-for-regression"
            config = {"bar": {"layout": {"right": []}},
                      "controller": {"apiSecret": canary}}
            config_path.write_text(json.dumps(config))
            config_path.chmod(0o600)
            evidence = root / "evidence"
            evidence.mkdir(mode=0o777)
            evidence.chmod(0o777)
            target = root / "attacker-readable-target"
            target.write_bytes(b"untouched\n")
            target.chmod(0o666)
            (evidence / "activation.jsonl").symlink_to(target)
            artifact = root / "unexecuted-artifact"
            artifact.write_bytes(b"fixture only")
            calls = []

            def runner(args, **kwargs):
                calls.append(args)
                value = config if args[-1] == "listShellConfig" else []
                return subprocess.CompletedProcess(args, 0, json.dumps(value), "")

            controller = OmarchyActivation(home=home, evidence=evidence, runner=runner)
            env = {key: str(root / key) for key in ENGINE_ENVIRONMENT}
            env["HOME"] = str(home)
            Deployment(target_version="0.2.0-dev", home=home, unit="private-fixture.service", env=env,
                       v01_hash="unused", v02=artifact, v02_hash="unused",
                       plugin_source=root, backup_root=root / "backup",
                       evidence=evidence, activation_controller=controller)
            self.assertEqual(stat.S_IMODE(evidence.stat().st_mode), 0o700)
            try:
                with self.assertRaises((OSError, ValueError)):
                    controller.snapshot()
            finally:
                # Owner/mode tightening of the containing directory alone is insufficient.
                self.assertEqual(target.read_bytes(), b"untouched\n")
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o666)
                self.assertTrue((evidence / "activation.jsonl").is_symlink())


if __name__ == "__main__":
    unittest.main()
