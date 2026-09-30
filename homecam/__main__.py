"""Command line entry point: ``python -m homecam``."""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import sys

from aiohttp import web

from .app import create_app
from .config import ConfigError, Settings
from .database import Database


def _init_admin(db_path: Path, reset: bool) -> int:
    db = Database(db_path)
    try:
        db.open()
        exists = db.has_admin()
        if exists and not reset:
            print("Administrator already exists; use --reset to replace it.", file=sys.stderr)
            return 2
        if not exists and reset:
            print("No administrator exists; omit --reset for first setup.", file=sys.stderr)
            return 2
        password = getpass.getpass("New administrator password: ")
        confirmation = getpass.getpass("Repeat administrator password: ")
        if password != confirmation:
            print("Passwords do not match.", file=sys.stderr)
            return 2
        if len(password) < 12 or len(password) > 1024:
            print("Password must contain 12 to 1024 characters.", file=sys.stderr)
            return 2
        db.set_admin_password(password, reset=reset)
        print("Administrator password initialized; previous admin sessions were revoked." if reset else "Administrator password initialized.")
        return 0
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m homecam")
    subparsers = parser.add_subparsers(dest="command")
    init_parser = subparsers.add_parser("init-admin", help="initialize or reset the administrator password")
    init_parser.add_argument("--reset", action="store_true", help="replace the existing password and revoke all sessions")
    args = parser.parse_args()

    if args.command == "init-admin":
        db_path = Path(os.environ.get("HOMECAM_DB", "/var/lib/homecam-lite/homecam.db"))
        return _init_admin(db_path, args.reset)

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"HomeCam Lite configuration error: {exc}", file=sys.stderr)
        return 2
    application = create_app(settings)
    web.run_app(application, host=settings.host, port=settings.port, access_log=None, shutdown_timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
