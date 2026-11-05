"""Pure file-only preview tests; no QML engine, sockets, commands, or services."""

import json
import os
from pathlib import Path
import re
import stat
import tempfile
import unittest
from unittest import mock

import plugin_support
import preview_bundle


ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "manifest.json", "plugin/BarWidget.qml", "plugin/FolderPicker.qml",
    "plugin/Panel.qml", "plugin/Service.qml", "plugin/SettingsDraft.qml",
    "plugin/SharedService.qml", "plugin/pick_folder.py",
)


def snapshot(root):
    result = {}
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        result[str(path.relative_to(root))] = (
            stat.S_IMODE(mode),
            os.readlink(path) if stat.S_ISLNK(mode)
            else path.read_bytes() if stat.S_ISREG(mode) else None,
        )
    return result


class PreviewBundleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="preview-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / "source"
        (self.source / "plugin").mkdir(parents=True)
        for name in FILES:
            (self.source / name).write_bytes((ROOT / name).read_bytes())
        self.parent = self.base / "bundles"
        self.runtime = self.base / "runtime"
        self.home = self.base / "home"
        self.xdg = self.base / "xdg"
        for path in (self.parent, self.runtime, self.home, self.xdg):
            path.mkdir(mode=0o700)
        self.sentinel = str(self.base / "parent-runtime-must-not-be-used")
        self.enterContext(mock.patch.dict(os.environ, {
            "SWIFTWALL_RUNTIME_DIR": self.sentinel,
            "XDG_RUNTIME_DIR": str(self.xdg),
        }))
        self.enterContext(mock.patch.object(Path, "home", return_value=self.home))
        for name in ("subprocess.Popen", "os.system", "os.fork", "os.posix_spawn"):
            self.enterContext(mock.patch(
                name, side_effect=AssertionError("process creation forbidden")))

        original_open = Path.open

        def guarded_open(path, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                if path.is_relative_to(self.source) or path.is_relative_to(ROOT):
                    raise AssertionError(f"source write forbidden: {path}")
            return original_open(path, mode, *args, **kwargs)

        self.guarded_open = guarded_open

    def stage(self, **overrides):
        arguments = dict(source=self.source, parent=self.parent, runtime=self.runtime)
        arguments.update(overrides)
        before = snapshot(self.source)
        environment = dict(os.environ)
        try:
            with mock.patch.object(Path, "open", self.guarded_open):
                return preview_bundle.stage_preview(**arguments)
        finally:
            self.assertEqual(snapshot(self.source), before, "source was modified")
            self.assertEqual(dict(os.environ), environment, "parent environment changed")

    def assert_refused(self, error=ValueError, message=None, **overrides):
        before = snapshot(self.base)
        with self.assertRaisesRegex(error, message or ".+"):
            self.stage(**overrides)
        self.assertEqual(snapshot(self.base), before, "refusal left filesystem changes")

    def test_successive_calls_have_distinct_ids_directories_and_component_urls(self):
        first = self.stage()
        original = snapshot(first["bundle"])
        second = self.stage()
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(first["bundle"], second["bundle"])
        for result in (first, second):
            self.assertEqual(set(result), {"id", "bundle", "socket"})
            self.assertRegex(result["id"], r"^local\.swiftwall-preview-[0-9a-f]{32}$")
            self.assertNotEqual(result["id"], plugin_support.PLUGIN_ID)
            self.assertEqual(result["bundle"], self.parent / result["id"])
            self.assertEqual(result["socket"], self.runtime / "control.sock")
            manifest = json.loads((result["bundle"] / "manifest.json").read_text())
            self.assertEqual(manifest["id"], result["id"])
        for name in FILES:
            if name.endswith(".qml"):
                with self.subTest(component=name):
                    urls = [(item["bundle"] / name).as_uri() for item in (first, second)]
                    self.assertNotEqual(*urls)
        self.assertEqual(snapshot(first["bundle"]), original)
        self.assertEqual(set(self.parent.iterdir()), {first["bundle"], second["bundle"]})

    def test_exact_eight_file_payload_and_original_manifest_fields(self):
        (self.source / "not-payload.txt").write_text("must not be staged")
        result = self.stage()
        bundle = result["bundle"]
        self.assertEqual(tuple(plugin_support.FILES), FILES)
        self.assertEqual({str(p.relative_to(bundle)) for p in bundle.rglob("*")
                          if p.is_file()}, set(FILES))
        self.assertEqual({str(p.relative_to(bundle)) for p in bundle.rglob("*")
                          if p.is_dir()}, {"plugin"})
        expected = json.loads((self.source / "manifest.json").read_text())
        expected.update(id=result["id"], name="SwiftWall isolated preview")
        self.assertEqual(json.loads((bundle / "manifest.json").read_text()), expected)
        for name in FILES:
            self.assertEqual(stat.S_IMODE((bundle / name).stat().st_mode),
                             0o755 if name.endswith(".py") else 0o644)
        self.assertEqual(stat.S_IMODE(bundle.stat().st_mode), 0o700)

    def test_both_socket_overrides_are_literal_and_ignore_parent_environment(self):
        runtime = self.base / 'private-"quoted"-runtime'
        runtime.mkdir(mode=0o700)
        result = self.stage(runtime=runtime)
        literal = json.dumps(str(runtime / "control.sock"))
        shared = (result["bundle"] / "plugin/SharedService.qml").read_text()
        original = (self.source / "plugin/SharedService.qml").read_text()
        start = original.index("    property string socketPath:")
        end = original.index("    property bool enabled:", start)
        self.assertEqual(shared, original[:start] + "    property string socketPath: "
                         + literal + "\n" + original[end:])
        service = (result["bundle"] / "plugin/Service.qml").read_text()
        self.assertEqual(service, "import QtQuick\n\nSharedService { socketPath: "
                         + literal + " }\n")
        for text in (shared, service):
            self.assertNotIn(self.sentinel, text)
            self.assertNotIn("SWIFTWALL_RUNTIME_DIR", text)
        self.assertEqual(os.environ["SWIFTWALL_RUNTIME_DIR"], self.sentinel)
        self.assertEqual(list(runtime.iterdir()), [])

    def test_all_qml_identity_replacements_and_other_payload_bytes(self):
        result = self.stage()
        for name in FILES:
            if name in ("manifest.json", "plugin/SharedService.qml", "plugin/Service.qml"):
                continue
            with self.subTest(file=name):
                original = (self.source / name).read_bytes()
                expected = original.replace(plugin_support.PLUGIN_ID.encode(),
                                            result["id"].encode()) if name.endswith(".qml") else original
                self.assertEqual((result["bundle"] / name).read_bytes(), expected)
        for name in ("BarWidget.qml", "Panel.qml"):
            text = (result["bundle"] / "plugin" / name).read_text()
            self.assertNotIn(plugin_support.PLUGIN_ID, text)
            module_ids = re.findall(r'\bmoduleName\s*:\s*"([^"]+)"', text)
            self.assertTrue(module_ids, f"no module identity checked in {name}")
            self.assertEqual(set(module_ids), {result["id"]})

    def test_python_helper_is_byte_for_byte_unchanged(self):
        result = self.stage()
        name = "plugin/pick_folder.py"
        self.assertEqual((result["bundle"] / name).read_bytes(),
                         (self.source / name).read_bytes())

    def test_parent_and_runtime_must_be_existing_absolute_directories(self):
        regular = self.base / "regular"
        regular.write_text("not a directory")
        for field in ("parent", "runtime"):
            for path in (Path("relative-preview"), self.base / "missing", regular):
                with self.subTest(field=field, path=path):
                    self.assert_refused(message="Existing absolute unlinked", **{field: path})

    def test_parent_and_runtime_reject_direct_and_ancestor_symlinks(self):
        alias = self.base / "alias"
        alias.symlink_to(self.base, target_is_directory=True)
        dangling = self.base / "dangling"
        dangling.symlink_to(self.base / "missing", target_is_directory=True)
        for field, target in (("parent", self.parent), ("runtime", self.runtime)):
            direct = self.base / (field + "-link")
            direct.symlink_to(target, target_is_directory=True)
            for path in (direct, alias / target.name, dangling):
                with self.subTest(field=field, path=path):
                    self.assert_refused(message="Existing absolute unlinked", **{field: path})

    def test_runtime_and_staging_must_be_separate_siblings_not_nested(self):
        child = self.parent / "child"
        child.mkdir()
        for parent, runtime in ((self.parent, self.parent), (self.parent, child),
                                (child, self.parent)):
            with self.subTest(parent=parent, runtime=runtime):
                self.assert_refused(parent=parent, runtime=runtime, message="separate")

    def test_live_plugin_discovery_staging_destinations_are_refused(self):
        for relative in (".config/omarchy/plugins", "usr/share/omarchy/plugins"):
            path = self.base / relative
            path.mkdir(parents=True)
            with self.subTest(path=path):
                self.assert_refused(parent=path, message="outside live plugin discovery")

    def test_production_home_runtime_is_refused(self):
        runtime = self.home / ".local/state/swiftwall"
        runtime.mkdir(parents=True)
        self.assert_refused(runtime=runtime, message="Not a private preview runtime")

    def test_production_xdg_runtime_is_refused(self):
        runtime = self.xdg / "swiftwall"
        runtime.mkdir()
        self.assert_refused(runtime=runtime, message="Production SwiftWall runtime")

    def test_oversized_unix_socket_path_is_refused_before_staging(self):
        runtime = self.base / ("r" * 108)
        runtime.mkdir()
        self.assertGreaterEqual(len(os.fsencode(runtime / "control.sock")), 108)
        self.assert_refused(runtime=runtime, message="sockaddr_un limit")

    def test_source_file_symlink_is_rejected_by_plugin_support_before_bundle_creation(self):
        target = self.source / FILES[-1]
        external = self.base / "original-helper"
        target.rename(external)
        target.symlink_to(external)
        with mock.patch.object(preview_bundle.plugin_support, "source_generation",
                               wraps=plugin_support.source_generation) as generation:
            self.assert_refused(error=plugin_support.InstallError, message="Refusing symlink")
        generation.assert_called_once_with(self.source)
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_source_directory_symlinks_are_rejected_before_bundle_creation(self):
        alias = self.base / "source-alias"
        alias.symlink_to(self.source, target_is_directory=True)
        self.assert_refused(source=alias, error=plugin_support.InstallError,
                            message="Refusing symlink")
        plugin = self.source / "plugin"
        external = self.base / "original-plugin"
        plugin.rename(external)
        plugin.symlink_to(external, target_is_directory=True)
        self.assert_refused(error=plugin_support.InstallError, message="Refusing symlink")
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_partial_copy_failure_rolls_back_only_new_bundle(self):
        existing = self.stage()["bundle"]
        before = snapshot(existing)
        original_write = Path.write_bytes
        written = []

        def fail_second_write(path, data):
            written.append(path)
            if len(written) == 2:
                raise OSError("injected staging write failure")
            return original_write(path, data)

        with mock.patch.object(Path, "write_bytes", fail_second_write):
            self.assert_refused(error=OSError, message="injected staging write failure")
        self.assertEqual(len(written), 2)
        self.assertEqual(snapshot(existing), before)
        self.assertEqual(list(self.parent.iterdir()), [existing])

    def test_socket_rewrite_failure_rolls_back_bundle(self):
        original_write = Path.write_text

        def fail_socket_rewrite(path, text, *args, **kwargs):
            if path.name == "SharedService.qml":
                raise OSError("injected socket rewrite failure")
            return original_write(path, text, *args, **kwargs)

        with mock.patch.object(Path, "write_text", fail_socket_rewrite):
            self.assert_refused(error=OSError, message="injected socket rewrite failure")
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_uuid_collision_does_not_clobber_existing_bundle(self):
        identity = mock.Mock(hex="a" * 32)
        with mock.patch.object(preview_bundle.uuid, "uuid4", return_value=identity):
            existing = self.stage()["bundle"]
            self.assert_refused(error=FileExistsError)
        self.assertEqual(list(self.parent.iterdir()), [existing])


if __name__ == "__main__":
    unittest.main()
