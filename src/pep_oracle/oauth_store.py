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
from typing import Protocol

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
    def get_client(self, client_id: str) -> ClientRecord | None: ...
    def put_auth_code(
        self, code: str, *, client_id: str, code_challenge: str, redirect_uri: str, ttl_seconds: int
    ) -> None: ...
    def pop_auth_code(self, code: str) -> AuthCodeRecord | None: ...
    def put_refresh(
        self, token: str, *, client_id: str, family_id: str, ttl_seconds: int
    ) -> None: ...
    def get_refresh(self, token: str) -> RefreshRecord | None: ...
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
        self._shared: sqlite3.Connection | None = None
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

    def get_client(self, client_id: str) -> ClientRecord | None:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM clients WHERE client_id = ?", (client_id,)).fetchone()
        finally:
            self._release(conn)
        if row is None:
            return None
        return ClientRecord(
            row["client_id"],
            row["client_name"] or "",
            json.loads(row["redirect_uris"]),
            row["created_at"],
        )

    # --- auth codes ---
    def put_auth_code(
        self, code: str, *, client_id: str, code_challenge: str, redirect_uri: str, ttl_seconds: int
    ) -> None:
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO auth_codes (code, client_id, code_challenge, redirect_uri, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (code, client_id, code_challenge, redirect_uri, time.time() + ttl_seconds),
            )
        finally:
            self._release(conn)

    def pop_auth_code(self, code: str) -> AuthCodeRecord | None:
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
        return AuthCodeRecord(
            row["client_id"], row["code_challenge"], row["redirect_uri"], row["expires_at"]
        )

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

    def get_refresh(self, token: str) -> RefreshRecord | None:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM refresh_tokens WHERE token = ?", (token,)).fetchone()
        finally:
            self._release(conn)
        if row is None:
            return None
        return RefreshRecord(
            row["token"],
            row["client_id"],
            row["issued_at"],
            row["expires_at"],
            bool(row["revoked"]),
            row["family_id"],
        )

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
            conn.execute(
                "UPDATE refresh_tokens SET revoked = 1 WHERE family_id = ? AND family_id != ''",
                (family_id,),
            )
        finally:
            self._release(conn)


class DynamoDbStore:
    """DynamoDB OAuthStore (single table + family GSI).

    Item shapes (pk = type#id):
      client#<id>   : client_name, redirect_uris(list), created_at
      code#<code>   : client_id, code_challenge, redirect_uri, expires_at, ttl
      refresh#<tok> : client_id, issued_at, expires_at, revoked(0/1), family_id, ttl
    `family_id` is a GSI so revoke_family can find a family's tokens. `ttl` drives
    DynamoDB native expiry (cleanup only -- reads still check expires_at, since TTL
    deletion lags up to ~48h). Rotation safety comes from a conditional update on
    `revoked`. In prod the table is created by CDK (Phase 2c); `ensure_table` is for
    local/moto."""

    GSI = "family-index"

    def __init__(self, table_name: str, region: str) -> None:
        import boto3

        self._ddb = boto3.resource("dynamodb", region_name=region)
        self._table = self._ddb.Table(table_name)
        self._table_name = table_name

    def ensure_table(self) -> None:
        """Create the table + GSI if absent (local/moto; prod uses CDK)."""
        client = self._ddb.meta.client
        existing = client.list_tables().get("TableNames", [])
        if self._table_name in existing:
            return
        client.create_table(
            TableName=self._table_name,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "family_id", "AttributeType": "S"},
            ],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": self.GSI,
                    "KeySchema": [{"AttributeName": "family_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "KEYS_ONLY"},
                }
            ],
        )

    # --- clients ---
    def put_client(self, client_id: str, client_name: str, redirect_uris: list[str]) -> int:
        now = int(time.time())
        self._table.put_item(
            Item={
                "pk": f"client#{client_id}",
                "client_id": client_id,
                "client_name": client_name or "",
                "redirect_uris": redirect_uris,
                "created_at": now,
            }
        )
        return now

    def get_client(self, client_id: str) -> ClientRecord | None:
        item = self._table.get_item(Key={"pk": f"client#{client_id}"}).get("Item")
        if item is None:
            return None
        return ClientRecord(
            item["client_id"],
            item.get("client_name", ""),
            list(item["redirect_uris"]),
            int(item["created_at"]),
        )

    # --- auth codes ---
    def put_auth_code(
        self, code: str, *, client_id: str, code_challenge: str, redirect_uri: str, ttl_seconds: int
    ) -> None:
        expires_at = time.time() + ttl_seconds
        self._table.put_item(
            Item={
                "pk": f"code#{code}",
                "client_id": client_id,
                "code_challenge": code_challenge,
                "redirect_uri": redirect_uri,
                "expires_at": str(expires_at),
                "ttl": int(expires_at) + 5,
            }
        )

    def pop_auth_code(self, code: str) -> AuthCodeRecord | None:
        from botocore.exceptions import ClientError

        try:
            item = self._table.delete_item(  # atomic single-use
                Key={"pk": f"code#{code}"},
                ConditionExpression="attribute_exists(pk)",
                ReturnValues="ALL_OLD",
            ).get("Attributes")
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise
        if item is None:
            return None
        expires_at = float(item["expires_at"])
        if expires_at <= time.time():
            return None
        return AuthCodeRecord(
            item["client_id"], item["code_challenge"], item["redirect_uri"], expires_at
        )

    # --- refresh tokens ---
    def put_refresh(self, token: str, *, client_id: str, family_id: str, ttl_seconds: int) -> None:
        now = int(time.time())
        item: dict = {
            "pk": f"refresh#{token}",
            "client_id": client_id,
            "issued_at": now,
            "expires_at": now + ttl_seconds,
            "revoked": 0,
            "ttl": now + ttl_seconds + 5,
        }
        # DynamoDB rejects empty strings as GSI key values; omit family_id when
        # empty so the item is simply excluded from the family-index GSI.
        if family_id:
            item["family_id"] = family_id
        self._table.put_item(Item=item)

    def get_refresh(self, token: str) -> RefreshRecord | None:
        item = self._table.get_item(Key={"pk": f"refresh#{token}"}).get("Item")
        if item is None:
            return None
        return RefreshRecord(
            token,
            item["client_id"],
            int(item["issued_at"]),
            int(item["expires_at"]),
            bool(int(item["revoked"])),
            item.get("family_id", ""),
        )

    def revoke_refresh(self, token: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._table.update_item(
                Key={"pk": f"refresh#{token}"},
                UpdateExpression="SET revoked = :one",
                ConditionExpression="attribute_exists(pk) AND revoked = :zero",
                ExpressionAttributeValues={":one": 1, ":zero": 0},
            )
            return True  # this call flipped active->revoked
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False  # missing or already revoked
            raise

    def revoke_family(self, family_id: str) -> None:
        if not family_id:
            return
        from boto3.dynamodb.conditions import Key

        resp = self._table.query(
            IndexName=self.GSI, KeyConditionExpression=Key("family_id").eq(family_id)
        )
        for row in resp.get("Items", []):
            self._table.update_item(
                Key={"pk": row["pk"]},
                UpdateExpression="SET revoked = :one",
                ExpressionAttributeValues={":one": 1},
            )
