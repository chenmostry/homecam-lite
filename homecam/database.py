"""SQLite storage. The database contains only credential digests and metadata."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from typing import Iterator

from .security import hash_password, token_digest


SCHEMA = """
CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    salt BLOB NOT NULL,
    password_hash BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pairings (
    code_hash TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_expiry_idx ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS pairings_expiry_idx ON pairings(expires_at);
"""


def utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.conn: sqlite3.Connection | None = None

    def open(self) -> None:
        if self.conn is not None:
            return
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.path.is_symlink():
                raise RuntimeError("database path must not be a symlink")
            old_umask = os.umask(0o077)
            try:
                self.conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
            finally:
                os.umask(old_umask)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                self.conn.close()
                self.conn = None
                raise
        else:
            self.conn = sqlite3.connect(":memory:", timeout=5.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _conn(self) -> sqlite3.Connection:
        if self.conn is None:
            raise RuntimeError("database is not open")
        return self.conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def has_admin(self) -> bool:
        return self._conn().execute("SELECT 1 FROM admins WHERE id=1").fetchone() is not None

    def set_admin_password(self, password: str, *, reset: bool = False) -> None:
        salt, digest = hash_password(password)
        with self.transaction() as conn:
            if not reset and conn.execute("SELECT 1 FROM admins WHERE id=1").fetchone():
                raise RuntimeError("administrator already exists; use init-admin --reset")
            conn.execute("INSERT INTO admins(id,salt,password_hash) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET salt=excluded.salt,password_hash=excluded.password_hash", (salt, digest))
            if reset:
                conn.execute("DELETE FROM sessions")

    def admin_password_row(self) -> sqlite3.Row | None:
        return self._conn().execute("SELECT salt,password_hash FROM admins WHERE id=1").fetchone()

    def create_session(self, raw_token: str, now: float, ttl: int) -> None:
        self._conn().execute(
            "INSERT INTO sessions(token_hash,created_at,expires_at) VALUES(?,?,?)",
            (token_digest(raw_token), now, now + ttl),
        )

    def session_expiry(self, raw_token: str, now: float) -> float | None:
        digest = token_digest(raw_token)
        row = self._conn().execute("SELECT expires_at FROM sessions WHERE token_hash=?", (digest,)).fetchone()
        if row is None:
            return None
        if float(row["expires_at"]) <= now:
            self._conn().execute("DELETE FROM sessions WHERE token_hash=?", (digest,))
            return None
        return float(row["expires_at"])

    def session_hash_is_valid(self, digest: str, now: float) -> bool:
        return self._conn().execute(
            "SELECT 1 FROM sessions WHERE token_hash=? AND expires_at>?",
            (digest, now),
        ).fetchone() is not None

    def delete_session(self, raw_token: str) -> str:
        digest = token_digest(raw_token)
        self._conn().execute("DELETE FROM sessions WHERE token_hash=?", (digest,))
        return digest

    def add_pairing(self, code: str, name: str, expires_at: float) -> None:
        from .security import token_digest
        self._conn().execute(
            "INSERT INTO pairings(code_hash,name,expires_at) VALUES(?,?,?)",
            (token_digest(code), name, expires_at),
        )

    def redeem_pairing(self, code: str, device_id: str, device_token: str, now: float) -> str | None:
        from .security import token_digest
        code_hash = token_digest(code)
        with self.transaction() as conn:
            row = conn.execute("SELECT name,expires_at FROM pairings WHERE code_hash=?", (code_hash,)).fetchone()
            if row is None or float(row["expires_at"]) <= now:
                conn.execute("DELETE FROM pairings WHERE code_hash=?", (code_hash,))
                return None
            conn.execute("DELETE FROM pairings WHERE code_hash=?", (code_hash,))
            conn.execute(
                "INSERT INTO devices(id,name,token_hash,created_at) VALUES(?,?,?,?)",
                (device_id, row["name"], token_digest(device_token), utc_iso(now)),
            )
            return str(row["name"])

    def device_for_token(self, raw_token: str) -> sqlite3.Row | None:
        from .security import token_digest
        return self._conn().execute(
            "SELECT id,name,created_at,token_hash FROM devices WHERE token_hash=?",
            (token_digest(raw_token),),
        ).fetchone()

    def device_by_id(self, device_id: str) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT id,name,created_at,token_hash FROM devices WHERE id=?",
            (device_id,),
        ).fetchone()

    def list_devices(self) -> list[sqlite3.Row]:
        return list(self._conn().execute("SELECT id,name,created_at FROM devices ORDER BY created_at,id"))

    def has_device(self, device_id: str) -> bool:
        return self._conn().execute("SELECT 1 FROM devices WHERE id=?", (device_id,)).fetchone() is not None

    def delete_device(self, device_id: str) -> bool:
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM devices WHERE id=?", (device_id,))
            return cur.rowcount > 0

    def cleanup(self, now: float) -> list[str]:
        conn = self._conn()
        expired = [str(row[0]) for row in conn.execute("SELECT token_hash FROM sessions WHERE expires_at<=?", (now,))]
        conn.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
        conn.execute("DELETE FROM pairings WHERE expires_at<=?", (now,))
        return expired
