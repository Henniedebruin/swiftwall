
"""Remove only unchanged installer-owned files; preserve all user config/state.

Never invokes systemctl or changes wallpapers. If the service is active/enabled,
manually disable --now before uninstalling (see packaging/INSTALL.md). File
recovery cannot restore running processes or service settings.
"""

import argparse
import sys
from pathlib import Path

import install_support as support


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--dry-run", action="store_true", help="validate only; write nothing")
    args = parser.parse_args(argv)
    try:
        support.uninstall(support.home_path(args.home), args.dry_run)
    except (support.InstallError, OSError) as exc:
        print(f"swiftwall: {exc}", file=sys.stderr)
        return 1
    print("Validation complete; no changes made." if args.dry_run else
          "Owned files removed; config/state preserved. Service was not changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
