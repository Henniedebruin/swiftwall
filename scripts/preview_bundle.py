"""File-only preview staging with fresh QML URLs and pre-completion socket pinning.

Never installs/registers/enables a plugin, changes shell environment, or starts a
process. A separate approved guard must own registration and removal. Production
plugin installation uses a different fixed identity; never overwrite it for a
preview, since compiled QML may survive file replacement in the running engine.
"""

import base64
import json
import os
from pathlib import Path
import shutil
import uuid

import plugin_support


PRODUCTION_ID = plugin_support.PLUGIN_ID


def stage_preview(source, parent, runtime):
    """Return a fresh bundle whose component URLs have never been used here.

    The caller supplies an already existing private staging parent and runtime
    directory. No production daemon or shell environment is inferred. Repeat calls
    always produce distinct plugin IDs and directory URLs, not rewritten sources
    at a previous path. Merely rewriting literals at a cached URL is insufficient.
    """
    source = Path(source)
    parent = Path(parent)
    runtime = Path(runtime)
    for path in (parent, runtime):
        if (not path.is_absolute() or not path.is_dir() or path.is_symlink()
                or path.resolve(strict=True) != path):
            raise ValueError("Existing absolute unlinked staging/runtime directories required")
    if parent == runtime or parent.is_relative_to(runtime) or runtime.is_relative_to(parent):
        raise ValueError("Keep runtime and staged plugin directories separate")
    # Omarchy's actual watcher can discover files without an explicit registration.
    # Stage outside every live plugin tree, not merely outside the chosen home.
    if "/omarchy/plugins" in str(parent) or "/usr/share/omarchy" in str(parent):
        raise ValueError("Preview staging must be outside live plugin discovery")
    socket = runtime / "control.sock"
    if len(bytes(str(socket), "utf-8")) >= 108:
        raise ValueError("Private Unix socket path exceeds Linux sockaddr_un limit")
    if str(runtime) == str(Path.home() / ".local/state/swiftwall"):
        raise ValueError("Not a private preview runtime")
    actual_runtime = os.environ.get("XDG_RUNTIME_DIR")
    standard_runtime = Path("/run/user") / str(os.getuid()) / "swiftwall"
    if runtime == standard_runtime or (actual_runtime and runtime == Path(actual_runtime) / "swiftwall"):
        raise ValueError("Production SwiftWall runtime is forbidden for previews")
    generation = plugin_support.source_generation(source)
    identity = "local.swiftwall-preview-" + uuid.uuid4().hex
    bundle = parent / identity
    bundle.mkdir(mode=0o700)  # No clobber/reuse, even on an improbable UUID collision.
    try:
        for name, item in generation.items():
            destination = bundle / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            raw = base64.b64decode(item["bytes"])
            if name.endswith(".qml"):
                raw = raw.decode("utf-8").replace(PRODUCTION_ID, identity).encode("utf-8")
            destination.write_bytes(raw)
            destination.chmod(item["mode"])
        manifest = json.loads((bundle / "manifest.json").read_text())
        manifest["id"] = identity
        manifest["name"] = "SwiftWall isolated preview"
        (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        shared = bundle / "plugin/SharedService.qml"
        text = shared.read_text()
        start = text.index("    property string socketPath:")
        end = text.index("    property bool enabled:", start)
        shared.write_text(text[:start] + "    property string socketPath: "
                          + json.dumps(str(socket)) + "\n" + text[end:])
        service = bundle / "plugin/Service.qml"
        service.write_text("import QtQuick\n\nSharedService { socketPath: "
                           + json.dumps(str(socket)) + " }\n")
        # Both base and entrypoint are pinned before Component.onCompleted. The
        # unique directory also gives every implicit local type a fresh URL.
        if (PRODUCTION_ID in (bundle / "plugin/BarWidget.qml").read_text()
                or PRODUCTION_ID in (bundle / "plugin/Panel.qml").read_text()):
            raise ValueError("Preview module identity was not replaced")
        return {"id": identity, "bundle": bundle, "socket": socket}
    except Exception:
        shutil.rmtree(bundle)
        raise
