"""Pluggable OAuth state store: clients, single-use auth codes, refresh tokens.

Two backends behind one `OAuthStore` interface -- `SqliteStore` (local default,
unchanged behavior) and `DynamoDbStore` (cloud, conditional-write rotation, TTL).
Refresh rotation is concurrency-safe: `revoke_refresh` is a conditional write that
returns True only for the caller that actually flipped active->revoked, so
concurrent refreshes resolve to exactly one rotation.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading
import time
from typing import Optional, Protocol

from pep_oracle import config

REFRESH_TTL_SECONDS = 30 * 24 * 3600


@dataclasses.dataclass
class ClientRecord:
    client_id: str
    client_name: str
    redirect_uris: list[str]
    created_at: int


@dataclasses.dataclass
class AuthCodeRecord:
    client_id: str
    code_challenge: str
    redirect_uri: str
    expires_at: float


@dataclasses.dataclass
class RefreshRecord:
    token: str
    client_id: str
    issued_at: int
    expires_at: int
    revoked: bool
    family_id: str


class OAuthStore(Protocol):
    def put_client(self, client_id: str, client_name: str, redirect_uris: list[str]) -> int: ...
    def get_client(self, client_id: str) -> Optional[ClientRecord]: ...
    def put_auth_code(self, code: str, *, client_id: str, code_challenge: str,
                      redirect_uri: str, ttl_seconds: int) -> None: ...
    def pop_auth_code(self, code: str) -> Optional[AuthCodeRecord]: ...
    def put_refresh(self, token: str, *, client_id: str, family_id: str, ttl_seconds: int) -> None: ...
    def get_refresh(self, token: str) -> Optional[RefreshRecord]: ...
    def revoke_refresh(self, token: str) -> bool: ...
    def revoke_family(self, family_id: str) -> None: ...


def get_store() -> OAuthStore:
    """Build the configured store. sqlite -> file under DATA_DIR; dynamodb -> table."""
    if config.OAUTH_STORE == "dynamodb":
        return DynamoDbStore(config.OAUTH_DDB_TABLE, region=config.OAUTH_DDB_REGION)
    return SqliteStore(str(config.DATA_DIR / "oauth.db"))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
  client_id     TEXT PRIMARY KEY,
  client_name   TEXT,
  redirect_uris TEXT NOT NULL,
  created_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS refresh_tokens (
  token      TEXT PRIMARY KEY,
  client_id  TEXT NOT NULL,
  issued_at  INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  revoked    INTEGER NOT NULL DEFAULT 0,
  family_id  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS auth_codes (
  code          TEXT PRIMARY KEY,
  client_id     TEXT NOT NULL,
  code_challenge TEXT NOT NULL,
  redirect_uri  TEXT NOT NULL,
  expires_at    REAL NOT NULL
);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


class SqliteStore:
    """SQLite OAuthStore. For ``:memory:`` keep one shared connection serialized by
    a lock; for file paths open a fresh connection per call (cheap, thread-safe)."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._shared: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        if db_path == ":memory:":
            self._shared = _connect(db_path)
            self._shared.executescript(_SCHEMA)
        else:
            conn = _connect(db_path)
            try:
                conn.executescript(_SCHEMA)
            finally:
                conn.close()

    def _conn(self) -> sqlite3.Connection:
        if self._shared is not None:
            self._lock.acquire()
            return self._shared
        return _connect(self.db_path)

    def _release(self, conn: sqlite3.Connection) -> None:
        if conn is self._shared:
            self._lock.release()
        else:
            conn.close()

    # --- clients ---
    def put_client(self, client_id: str, client_name: str, redirect_uris: list[str]) -> int:
        now = int(time.time())
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO clients (client_id, client_name, redirect_uris, created_at) "
                "VALUES (?, ?, ?, ?)",
                (client_id, client_name or "", json.dumps(redirect_uris), now),
            )
        finally:
            self._release(conn)
        return now

    def get_client(self, client_id: str) -> Optional[ClientRecord]:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM clients WHERE client_id = ?", (client_id,)).fetchone()
        finally:
            self._release(conn)
        if row is None:
            return None
        return ClientRecord(row["client_id"], row["client_name"] or "",
                            json.loads(row["redirect_uris"]), row["created_at"])

    # --- auth codes ---
    def put_auth_code(self, code: str, *, client_id: str, code_challenge: str,
                      redirect_uri: str, ttl_seconds: int) -> None:
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO auth_codes (code, client_id, code_challenge, redirect_uri, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (code, client_id, code_challenge, redirect_uri, time.time() + ttl_seconds),
            )
        finally:
            self._release(conn)

    def pop_auth_code(self, code: str) -> Optional[AuthCodeRecord]:
        conn = self._conn()
        try:
            row = conn.execute(
                "DELETE FROM auth_codes WHERE code = ? "
                "RETURNING client_id, code_challenge, redirect_uri, expires_at",
                (code,),
            ).fetchone()
        finally:
            self._release(conn)
        if row is None or row["expires_at"] <= time.time():
            return None
        return AuthCodeRecord(row["client_id"], row["code_challenge"],
                             row["redirect_uri"], row["expires_at"])

    # --- refresh tokens ---
    def put_refresh(self, token: str, *, client_id: str, family_id: str, ttl_seconds: int) -> None:
        now = int(time.time())
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO refresh_tokens (token, client_id, issued_at, expires_at, revoked, family_id) "
                "VALUES (?, ?, ?, ?, 0, ?)",
                (token, client_id, now, now + ttl_seconds, family_id),
            )
        finally:
            self._release(conn)

    def get_refresh(self, token: str) -> Optional[RefreshRecord]:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM refresh_tokens WHERE token = ?", (token,)).fetchone()
        finally:
            self._release(conn)
        if row is None:
            return None
        return RefreshRecord(row["token"], row["client_id"], row["issued_at"],
                            row["expires_at"], bool(row["revoked"]), row["family_id"])

    def revoke_refresh(self, token: str) -> bool:
        conn = self._conn()
        try:
            cur = conn.execute(
                "UPDATE refresh_tokens SET revoked = 1 WHERE token = ? AND revoked = 0", (token,)
            )
            return cur.rowcount == 1  # True only if THIS call flipped active->revoked
        finally:
            self._release(conn)

    def revoke_family(self, family_id: str) -> None:
        conn = self._conn()
        try:
            conn.execute("UPDATE refresh_tokens SET revoked = 1 WHERE family_id = ?", (family_id,))
        finally:
            self._release(conn)


class DynamoDbStore:  # replaced in Task 3
    def __init__(self, *a, **k):
        raise NotImplementedError("DynamoDbStore lands in Task 3")
