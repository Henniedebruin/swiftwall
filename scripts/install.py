
"""Install files only; never change live service settings or run systemctl.

Before update or rollback, manually disable/stop an active service. Review
packaging/INSTALL.md for manual activation and file-only rollback limitations.
"""

import argparse
import sys
from pathlib import Path

import install_support as support


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home(),
                        help="installation home (default: actual home); supports isolated testing")
    parser.add_argument("--binary", type=Path, help="built Swiftwall executable (required unless --rollback)")
    parser.add_argument("--dry-run", action="store_true", help="validate only; write nothing and run no commands")
    parser.add_argument("--rollback", action="store_true",
                        help="restore the previous owned version once; never run systemctl")
    parser.add_argument("--enable-autostart", action="store_true",
                        help="unsupported: activation must be performed manually after review")
    args = parser.parse_args(argv)
    if args.enable_autostart:
        parser.error("--enable-autostart is not supported: scripts never change live service settings. "
                     "Review packaging/INSTALL.md for manual systemctl activation after installation.")
    if args.rollback and args.binary:
        parser.error("--rollback cannot be combined with --binary")
    if not args.rollback and args.binary is None:
        parser.error("--binary is required unless --rollback is specified")
    home = support.home_path(args.home)
    try:
        if args.rollback:
            support.rollback(home, args.dry_run)
        else:
            support.install(home, args.binary, args.dry_run)
    except (support.InstallError, OSError) as exc:
        print(f"swiftwall: {exc}", file=sys.stderr)
        return 1
    print("Validation complete; no changes made." if args.dry_run else
          "Previous files restored; running processes and service settings unchanged." if args.rollback else
          "Files installed. Service was not reloaded, enabled, or started.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
