"""Validate the local plugin without installing, enabling or executing it.

Use Qt 6 explicitly: some Omarchy systems also have a Qt 5 qmllint on PATH.
Omarchy's runtime-only qs import alias is staged in a temporary import directory.
"""

import base64
import collections
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import plugin_support

ROOT = Path(__file__).resolve().parents[1]
SHELL = Path("/usr/share/omarchy/shell")


def stage_package(source, destination):
    """Validate only the same eight-file payload that the installer publishes."""
    generation = plugin_support.source_generation(source)
    destination.mkdir()
    for name, item in generation.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(item["bytes"]))
        path.chmod(item["mode"])
    return destination


def main():
    lint = Path("/usr/lib/qt6/bin/qmllint")
    if not lint.is_file() or not shutil.which("omarchy"):
        raise RuntimeError("Installed Omarchy and Qt 6 qmllint are required")
    with tempfile.TemporaryDirectory(prefix="swiftwall-qmllint-") as temporary:
        package = stage_package(ROOT, Path(temporary) / "package")
        subprocess.run(["omarchy", "plugin", "validate", str(package)], check=True, timeout=30)
        imports = Path(temporary) / "qs"
        imports.mkdir()
        for module in ("Commons", "Ui"):
            shutil.copytree(SHELL / module, imports / module)
        files = sorted((ROOT / "plugin").glob("*.qml"))
        result = subprocess.run([
            str(lint), "--ignore-settings", "-I", temporary,
            "--missing-property", "info", "--signal-handler-parameters", "info",
            "--max-warnings", "0", "--json", "-", *map(str, files),
        ], capture_output=True, text=True, timeout=30, check=False)
        report = json.loads(result.stdout)
        counts = collections.Counter()
        failures = []
        for entry in report["files"]:
            for diagnostic in entry.get("warnings", []):
                counts[(diagnostic["type"], diagnostic["id"])] += 1
                if diagnostic["type"] in ("warning", "error", "critical", "fatal"):
                    failures.append({"file": entry["filename"], **diagnostic})
        if failures or result.returncode:
            print(json.dumps(failures, indent=2), file=sys.stderr)
            raise RuntimeError(f"Qt 6 qmllint failed ({result.returncode}): {result.stderr}")
        print(f"PASS: installed Omarchy manifest validator and Qt 6 qmllint ({len(files)} QML files)")
        for (level, category), count in sorted(counts.items()):
            print(f"{level}: {category}: {count}")
        print("INFO: dynamic host/Loader/theme properties and the missing socket-enum lint metadata remain informational;")
        print("      run the separate offscreen runtime harness to verify actual socket/error handling.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, plugin_support.InstallError, subprocess.SubprocessError) as error:
        raise SystemExit(f"plugin validation: {error}") from error
