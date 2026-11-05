"""Place SwiftWall plugin files only; never enable plugins or alter bar settings."""

import argparse
import sys
from pathlib import Path

# A dry-run must not create Python import caches alongside the source either.
sys.dont_write_bytecode = True
import plugin_support as support


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    for operation in ("install", "verify", "uninstall", "rollback"):
        command = commands.add_parser(operation)
        command.add_argument("--home", type=Path, default=Path.home(),
                             help="existing home directory (temporary homes supported)")
        if operation != "verify":
            command.add_argument("--dry-run", action="store_true",
                                 help="validate without any filesystem changes")
        if operation == "install":
            command.add_argument("--source", type=Path,
                                 default=Path(__file__).absolute().parent.parent,
                                 help="repository root; only the eight allowlisted files are read")
    args = parser.parse_args(argv)
    if args.operation in ("uninstall", "rollback"):
        print("WARNING: Disable the SwiftWall widget/plugin manually before "
              f"{args.operation}. No live IPC will be called; bar settings are preserved.",
              file=sys.stderr)
    try:
        support.run(args.operation, args.home, getattr(args, "source", None),
                    getattr(args, "dry_run", False))
    except (support.InstallError, OSError) as exc:
        print(f"swiftwall plugin: {exc}", file=sys.stderr)
        return 1
    if args.operation == "verify":
        print("Verified owned plugin bytes, hashes, modes, and exact file set; no changes made.")
    elif args.dry_run:
        print("Dry-run validation complete; no filesystem changes made.")
    else:
        print(f"Plugin file {args.operation} complete. No enablement, registry commands, "
              "bar settings, or engine changes. Placement may trigger registry autodiscovery.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
