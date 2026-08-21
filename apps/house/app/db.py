"""Users, sessions, and permissions.

SQLite with three small tables and no ORM. The schema is not going to grow enough to
earn one, and a single file on a PVC is easy to back up and easy to reason about.

Passwords use hashlib.scrypt from the standard library rather than a hashing library:
scrypt is memory-hard, it is right there, and it removes a dependency from something
that guards the household's lights.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass

# scrypt parameters. n=2**15 keeps a single hash near 100 ms on this hardware, which is
# slow enough to make guessing expensive and fast enough that logging in feels instant.
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
# These parameters need 128*n*r bytes = 32 MiB, which is exactly OpenSSL's default
# memory cap — so without a higher maxmem every hash raises "memory limit exceeded".
SCRYPT_MAXMEM = 64 * 1024 * 1024
SESSION_TTL = 30 * 24 * 3600  # a month; this is a light switch, not a bank

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY,
    username             TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name         TEXT NOT NULL,
    salt                 BLOB NOT NULL,
    password_hash        BLOB NOT NULL,
    is_admin             INTEGER NOT NULL DEFAULT 0,
    must_change_password INTEGER NOT NULL DEFAULT 1,
    created_at           INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS permissions (
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    room_key TEXT NOT NULL,
    PRIMARY KEY (user_id, room_key)
);
"""


@dataclass(frozen=True)
class User:
    id: int
    username: str
    display_name: str
    is_admin: bool
    must_change_password: bool


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        username=row["username"],
        display_name=row["display_name"],
        is_admin=bool(row["is_admin"]),
        must_change_password=bool(row["must_change_password"]),
    )


class Store:
    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # check_same_thread=False because uvicorn serves requests from a threadpool.
        # Every write goes through a single connection guarded by SQLite's own locking,
        # and this app's write volume is a few rows a day.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---------------------------------------------------------------- passwords

    @staticmethod
    def hash_password(password: str, salt: bytes) -> bytes:
        return hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
            maxmem=SCRYPT_MAXMEM, dklen=64,
        )

    def set_password(self, user_id: int, password: str, must_change: bool = False) -> None:
        salt = secrets.token_bytes(16)
        self.db.execute(
            "UPDATE users SET salt=?, password_hash=?, must_change_password=? WHERE id=?",
            (salt, self.hash_password(password, salt), int(must_change), user_id),
        )
        self.db.commit()

    def verify(self, username: str, password: str) -> User | None:
        row = self.db.execute(
            "SELECT * FROM users WHERE username=?", (username,)
        ).fetchone()
        if row is None:
            # Hash anyway. Returning early on an unknown username makes login timing a
            # username oracle, and this is cheap insurance against that.
            self.hash_password(password, b"\x00" * 16)
            return None
        expected = row["password_hash"]
        got = self.hash_password(password, row["salt"])
        if not hmac.compare_digest(expected, got):
            return None
        return _row_to_user(row)

    # ---------------------------------------------------------------- users

    def create_user(
        self, username: str, display_name: str, password: str,
        is_admin: bool = False, must_change: bool = True,
    ) -> int:
        salt = secrets.token_bytes(16)
        cur = self.db.execute(
            "INSERT INTO users (username, display_name, salt, password_hash, is_admin,"
            " must_change_password, created_at) VALUES (?,?,?,?,?,?,?)",
            (username, display_name, salt, self.hash_password(password, salt),
             int(is_admin), int(must_change), int(time.time())),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def get_user(self, user_id: int) -> User | None:
        row = self.db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return _row_to_user(row) if row else None

    def find_user(self, username: str) -> User | None:
        row = self.db.execute(
            "SELECT * FROM users WHERE username=?", (username,)
        ).fetchone()
        return _row_to_user(row) if row else None

    def all_users(self) -> list[User]:
        return [
            _row_to_user(r)
            for r in self.db.execute("SELECT * FROM users ORDER BY username")
        ]

    def delete_user(self, user_id: int) -> None:
        self.db.execute("DELETE FROM users WHERE id=?", (user_id,))
        self.db.commit()

    # ---------------------------------------------------------------- permissions

    def grant(self, user_id: int, room_key: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO permissions (user_id, room_key) VALUES (?,?)",
            (user_id, room_key),
        )
        self.db.commit()

    def revoke(self, user_id: int, room_key: str) -> None:
        self.db.execute(
            "DELETE FROM permissions WHERE user_id=? AND room_key=?", (user_id, room_key)
        )
        self.db.commit()

    def rooms_for(self, user_id: int) -> set[str]:
        return {
            r["room_key"]
            for r in self.db.execute(
                "SELECT room_key FROM permissions WHERE user_id=?", (user_id,)
            )
        }

    # ---------------------------------------------------------------- sessions

    def create_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        self.db.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
            (token, user_id, now, now + SESSION_TTL),
        )
        self.db.commit()
        return token

    def session_user(self, token: str | None) -> User | None:
        if not token:
            return None
        row = self.db.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.token=? AND s.expires_at > ?",
            (token, int(time.time())),
        ).fetchone()
        return _row_to_user(row) if row else None

    def destroy_session(self, token: str | None) -> None:
        if token:
            self.db.execute("DELETE FROM sessions WHERE token=?", (token,))
            self.db.commit()

    def destroy_user_sessions(self, user_id: int) -> None:
        """Used after a password change, so an old cookie cannot outlive the password."""
        self.db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        self.db.commit()

    def purge_expired_sessions(self) -> None:
        self.db.execute("DELETE FROM sessions WHERE expires_at <= ?", (int(time.time()),))
        self.db.commit()
