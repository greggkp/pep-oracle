# AWS Migration Phase 2b — OAuth state → DynamoDB Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the OAuth provider's state (registered clients, refresh tokens, and auth codes) behind a pluggable store interface with a SQLite backend (local default, unchanged behavior) and a DynamoDB backend (cloud), making refresh-token rotation concurrency-safe via conditional writes and the auth layer stateless so any Lambda container can serve any request.

**Architecture:** A new `oauth_store.py` defines an `OAuthStore` Protocol + record dataclasses and two implementations — `SqliteStore` (the existing SQLite logic, plus an `auth_codes` table replacing the in-memory `_auth_codes` dict) and `DynamoDbStore` (single table + a `family_id` GSI, `revoked`-conditional rotation, atomic single-use auth-code pop, native TTL for cleanup). `oauth.py` is refactored to depend only on the `OAuthStore` interface; `register_oauth_routes` takes a store object. The token-refresh path uses a **conditional revoke** that returns won/lost, so concurrent refreshes resolve to exactly one rotation (winner issues, loser gets a clean 400 — no spurious family revocation). Backend selected by `PEP_ORACLE_OAUTH_STORE`.

**Tech Stack:** boto3 (DynamoDB), moto (in-process DynamoDB mock for tests — no Docker), sqlite3, FastAPI, pytest. Region `ap-southeast-2`.

**Scope boundary:** This phase migrates only the **OAuth storage layer**. JWT signing stays HS256 with the existing `signing_key` string (the signing-backend seam + SSM + Cognito gate are **Phase 2b2**). No CDK / real DynamoDB table provisioning (that's **2c** — this phase's `DynamoDbStore.ensure_table()` is for local/moto only; prod table comes from CDK). The OAuth protocol behavior (DCR, PKCE, discovery, rotation, family-revoke-on-reuse) is preserved exactly — the existing `test_oauth.py` suite must stay green (with the two internal-poking tests updated to the new structure).

---

## File Structure

**Created:**
- `src/pep_oracle/oauth_store.py` — `OAuthStore` Protocol, record dataclasses, `SqliteStore`, `DynamoDbStore`, `get_store()` factory.
- `tests/test_oauth_store.py` — store-contract tests run against BOTH backends (parametrized), incl. the conditional-rotation race.

**Modified:**
- `pyproject.toml` — add `moto[dynamodb]` to the `dev` group; `boto3` already present.
- `src/pep_oracle/config.py` — `OAUTH_STORE`, `OAUTH_DDB_TABLE`, `OAUTH_DDB_REGION`.
- `src/pep_oracle/oauth.py` — depend on `OAuthStore` (remove the SQLite `_Store`, the free `_persist_*`/`_lookup_*`/`_revoke_*` functions, and the in-memory `_auth_codes`); `register_oauth_routes(app, signing_key, public_url, store)`; conditional-revoke rotation.
- `src/pep_oracle/server.py` — `mount_mcp_if_configured` builds the store from config via `oauth_store.get_store()` and passes it to `register_oauth_routes`.
- `tests/test_oauth.py` — construct a `SqliteStore` for the `:memory:` fixture; replace the two `oauth._auth_codes`-poking tests with time-based / store-based equivalents.
- `CLAUDE.md` — OAuth store seam note.

The store lives in its own module (not inside `oauth.py`) because backend implementations + their boto3/sqlite details are a distinct responsibility from the HTTP route logic, and they're tested independently via the contract suite.

---

## Task 1: Store interface, records, config, moto dep

**Files:**
- Create: `src/pep_oracle/oauth_store.py`
- Modify: `pyproject.toml`, `src/pep_oracle/config.py`
- Test: `tests/test_oauth_store.py`

- [ ] **Step 1: Add `moto` to the dev group in `pyproject.toml`**

```toml
[dependency-groups]
dev = [
    "pytest-asyncio>=1.3.0",
    "boto3>=1.34",
    "pyarrow>=15",
    "mangum>=0.17",
    "moto[dynamodb]>=5",
]
```

- [ ] **Step 2: Add OAuth-store config knobs in `src/pep_oracle/config.py`** — after the serving-source block, add:

```python
# --- OAuth store backend (Phase 2b) ---
# "sqlite" (local default, file/:memory:) or "dynamodb" (cloud). The serving
# Lambda sets "dynamodb"; the OptiPlex keeps "sqlite".
OAUTH_STORE = os.getenv("PEP_ORACLE_OAUTH_STORE", "sqlite")
OAUTH_DDB_TABLE = os.getenv("PEP_ORACLE_OAUTH_DDB_TABLE", "pep-oracle-oauth")
OAUTH_DDB_REGION = os.getenv("PEP_ORACLE_OAUTH_DDB_REGION", BEDROCK_REGION)
```

- [ ] **Step 3: Write the failing contract test** — create `tests/test_oauth_store.py` with a parametrized `store` fixture (DynamoDB backend is added in Task 3; start with SQLite only):

```python
import time

import pytest

from pep_oracle import oauth_store


@pytest.fixture
def store(request):
    """A fresh OAuthStore. Parametrized over backends as they land."""
    backend = getattr(request, "param", "sqlite")
    if backend == "sqlite":
        yield oauth_store.SqliteStore(":memory:")
    else:  # pragma: no cover - added in Task 3
        raise NotImplementedError(backend)


def test_client_roundtrip(store):
    created = store.put_client("c1", "My App", ["https://app.example/cb"])
    assert isinstance(created, int)
    rec = store.get_client("c1")
    assert rec is not None
    assert rec.client_id == "c1"
    assert rec.client_name == "My App"
    assert rec.redirect_uris == ["https://app.example/cb"]
    assert rec.created_at == created
    assert store.get_client("missing") is None


def test_auth_code_single_use(store):
    store.put_auth_code("abc", client_id="c1", code_challenge="chal",
                        redirect_uri="https://app/cb", ttl_seconds=60)
    rec = store.pop_auth_code("abc")
    assert rec is not None
    assert rec.client_id == "c1"
    assert rec.code_challenge == "chal"
    assert rec.redirect_uri == "https://app/cb"
    # single use — second pop is None
    assert store.pop_auth_code("abc") is None


def test_auth_code_expired_returns_none(store):
    store.put_auth_code("old", client_id="c1", code_challenge="x",
                        redirect_uri="https://app/cb", ttl_seconds=-1)
    assert store.pop_auth_code("old") is None


def test_refresh_roundtrip_and_revoke(store):
    store.put_refresh("t1", client_id="c1", family_id="f1", ttl_seconds=3600)
    rec = store.get_refresh("t1")
    assert rec is not None and rec.client_id == "c1" and rec.family_id == "f1"
    assert rec.revoked is False
    # conditional revoke: first call wins, second loses
    assert store.revoke_refresh("t1") is True
    assert store.revoke_refresh("t1") is False
    assert store.get_refresh("t1").revoked is True


def test_revoke_family_revokes_all_members(store):
    store.put_refresh("a", client_id="c1", family_id="fam", ttl_seconds=3600)
    store.put_refresh("b", client_id="c1", family_id="fam", ttl_seconds=3600)
    store.put_refresh("other", client_id="c1", family_id="zzz", ttl_seconds=3600)
    store.revoke_family("fam")
    assert store.get_refresh("a").revoked is True
    assert store.get_refresh("b").revoked is True
    assert store.get_refresh("other").revoked is False


def test_revoke_missing_token_returns_false(store):
    assert store.revoke_refresh("nope") is False
```

- [ ] **Step 4: Run to verify it fails**

Run: `uv pip install -e ".[server,aws]" && uv run pytest tests/test_oauth_store.py -v`
Expected: FAIL — `oauth_store` has no `SqliteStore` / records (collection or attribute error).

- [ ] **Step 5: Create the interface + records skeleton** in `src/pep_oracle/oauth_store.py` (the SQLite implementation body is Task 2; here define the records, the Protocol, and a `get_store` stub so the module imports):

```python
"""Pluggable OAuth state store: clients, single-use auth codes, refresh tokens.

Two backends behind one `OAuthStore` interface — `SqliteStore` (local default,
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
```

> NOTE: `SqliteStore` and `DynamoDbStore` are referenced by `get_store` but defined in Tasks 2 and 3. To keep the module importable after this task, ALSO append minimal stub classes at the end now and replace their bodies in the next tasks — OR (cleaner) implement `SqliteStore` fully here as part of Task 2's step. This plan folds the `SqliteStore` body into Task 2; so after Task 1, add a one-line `class DynamoDbStore: ...` placeholder that raises `NotImplementedError` in `__init__` (replaced in Task 3) and write `SqliteStore` in Task 2. The contract test only constructs `SqliteStore`, so the placeholder is never hit until Task 3.

Append this temporary placeholder (replaced in Task 3):

```python
class DynamoDbStore:  # replaced in Task 3
    def __init__(self, *a, **k):
        raise NotImplementedError("DynamoDbStore lands in Task 3")
```

- [ ] **Step 6: Commit (interface only — contract tests still red until Task 2)**

```bash
git add pyproject.toml src/pep_oracle/config.py src/pep_oracle/oauth_store.py tests/test_oauth_store.py
git commit -m "feat(oauth): OAuthStore interface + records + config (sqlite|dynamodb)"
```

---

## Task 2: SqliteStore implementation

**Files:**
- Modify: `src/pep_oracle/oauth_store.py`
- Test: `tests/test_oauth_store.py` (already written in Task 1)

- [ ] **Step 1: Confirm the contract tests are red for the missing SqliteStore**

Run: `uv run pytest tests/test_oauth_store.py -v`
Expected: FAIL — `oauth_store` has no `SqliteStore`.

- [ ] **Step 2: Implement `SqliteStore`** — add to `src/pep_oracle/oauth_store.py` (before the `DynamoDbStore` placeholder):

```python
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
```

- [ ] **Step 3: Run the contract tests (sqlite)**

Run: `uv run pytest tests/test_oauth_store.py -v`
Expected: PASS (all 6 contract tests against SqliteStore).

- [ ] **Step 4: Commit**

```bash
git add src/pep_oracle/oauth_store.py
git commit -m "feat(oauth): SqliteStore (clients, single-use auth codes, conditional-revoke refresh)"
```

---

## Task 3: DynamoDbStore implementation (moto-tested)

**Files:**
- Modify: `src/pep_oracle/oauth_store.py` (replace the `DynamoDbStore` placeholder)
- Test: `tests/test_oauth_store.py`

- [ ] **Step 1: Parametrize the contract tests over both backends** — in `tests/test_oauth_store.py`, replace the `store` fixture with one that runs every contract test against BOTH sqlite and a moto-backed DynamoDbStore:

```python
import boto3
import pytest
from moto import mock_aws

from pep_oracle import oauth_store


@pytest.fixture(params=["sqlite", "dynamodb"])
def store(request):
    if request.param == "sqlite":
        yield oauth_store.SqliteStore(":memory:")
        return
    with mock_aws():
        boto3.client("dynamodb", region_name="ap-southeast-2")  # ensure region in moto
        s = oauth_store.DynamoDbStore("test-oauth", region="ap-southeast-2")
        s.ensure_table()
        yield s
```

(Remove the old single-backend `store` fixture. The 6 contract test bodies are unchanged — they now run twice.)

- [ ] **Step 2: Run to verify the DynamoDB variant fails**

Run: `uv run pytest tests/test_oauth_store.py -v`
Expected: the `[sqlite]` params PASS; the `[dynamodb]` params FAIL (`DynamoDbStore` raises NotImplementedError / has no `ensure_table`).

- [ ] **Step 3: Implement `DynamoDbStore`** — replace the placeholder `class DynamoDbStore` in `src/pep_oracle/oauth_store.py` with:

```python
class DynamoDbStore:
    """DynamoDB OAuthStore (single table + family GSI).

    Item shapes (pk = type#id):
      client#<id>   : client_name, redirect_uris(list), created_at
      code#<code>   : client_id, code_challenge, redirect_uri, expires_at, ttl
      refresh#<tok> : client_id, issued_at, expires_at, revoked(0/1), family_id, ttl
    `family_id` is a GSI so revoke_family can find a family's tokens. `ttl` drives
    DynamoDB native expiry (cleanup only — reads still check expires_at, since TTL
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
            GlobalSecondaryIndexes=[{
                "IndexName": self.GSI,
                "KeySchema": [{"AttributeName": "family_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "KEYS_ONLY"},
            }],
        )

    # --- clients ---
    def put_client(self, client_id: str, client_name: str, redirect_uris: list[str]) -> int:
        now = int(time.time())
        self._table.put_item(Item={
            "pk": f"client#{client_id}", "client_id": client_id,
            "client_name": client_name or "", "redirect_uris": redirect_uris,
            "created_at": now,
        })
        return now

    def get_client(self, client_id: str) -> Optional[ClientRecord]:
        item = self._table.get_item(Key={"pk": f"client#{client_id}"}).get("Item")
        if item is None:
            return None
        return ClientRecord(item["client_id"], item.get("client_name", ""),
                            list(item["redirect_uris"]), int(item["created_at"]))

    # --- auth codes ---
    def put_auth_code(self, code: str, *, client_id: str, code_challenge: str,
                      redirect_uri: str, ttl_seconds: int) -> None:
        expires_at = time.time() + ttl_seconds
        self._table.put_item(Item={
            "pk": f"code#{code}", "client_id": client_id,
            "code_challenge": code_challenge, "redirect_uri": redirect_uri,
            "expires_at": str(expires_at), "ttl": int(expires_at) + 5,
        })

    def pop_auth_code(self, code: str) -> Optional[AuthCodeRecord]:
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
        return AuthCodeRecord(item["client_id"], item["code_challenge"],
                             item["redirect_uri"], expires_at)

    # --- refresh tokens ---
    def put_refresh(self, token: str, *, client_id: str, family_id: str, ttl_seconds: int) -> None:
        now = int(time.time())
        self._table.put_item(Item={
            "pk": f"refresh#{token}", "client_id": client_id,
            "issued_at": now, "expires_at": now + ttl_seconds, "revoked": 0,
            "family_id": family_id, "ttl": now + ttl_seconds + 5,
        })

    def get_refresh(self, token: str) -> Optional[RefreshRecord]:
        item = self._table.get_item(Key={"pk": f"refresh#{token}"}).get("Item")
        if item is None:
            return None
        return RefreshRecord(token, item["client_id"], int(item["issued_at"]),
                            int(item["expires_at"]), bool(int(item["revoked"])),
                            item["family_id"])

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
```

- [ ] **Step 4: Run the contract tests against both backends**

Run: `uv run pytest tests/test_oauth_store.py -v`
Expected: PASS — all 6 contract tests for BOTH `[sqlite]` and `[dynamodb]` (12 total).

- [ ] **Step 5: Commit**

```bash
git add src/pep_oracle/oauth_store.py tests/test_oauth_store.py
git commit -m "feat(oauth): DynamoDbStore (single table + family GSI, conditional rotation, TTL)"
```

---

## Task 4: Concurrency-safe rotation contract test

**Files:**
- Test: `tests/test_oauth_store.py`

- [ ] **Step 1: Write the failing race test** — append to `tests/test_oauth_store.py`:

```python
def test_concurrent_revoke_exactly_one_wins(store):
    """Two threads racing to rotate the same refresh token: the conditional
    revoke must let exactly ONE win (the rotation), the other loses cleanly."""
    import threading

    store.put_refresh("race", client_id="c1", family_id="f1", ttl_seconds=3600)
    results = []
    barrier = threading.Barrier(2)

    def attempt():
        barrier.wait()  # maximize contention
        results.append(store.revoke_refresh("race"))

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [False, True]  # exactly one winner
    assert store.get_refresh("race").revoked is True
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_oauth_store.py -k concurrent_revoke -v`
Expected: PASS for both `[sqlite]` and `[dynamodb]`. (SQLite's `UPDATE ... WHERE revoked=0` + `rowcount` and DynamoDB's `ConditionExpression` each guarantee a single winner. The SQLite `:memory:` store serializes via its lock, so the two writes are ordered — still exactly one sees `revoked=0`. moto serializes too.)

- [ ] **Step 3: Commit**

```bash
git add tests/test_oauth_store.py
git commit -m "test(oauth): conditional-revoke rotation lets exactly one writer win"
```

---

## Task 5: Refactor `oauth.py` onto the store interface

**Files:**
- Modify: `src/pep_oracle/oauth.py`
- Modify: `tests/test_oauth.py`

- [ ] **Step 1: Update the existing `test_oauth.py` setup to construct a store** — the suite currently calls `register_oauth_routes(app, SIGNING_KEY, PUBLIC_URL, ":memory:")` and pokes `oauth._auth_codes`. Make these changes:

(a) The `client` fixture — build a `SqliteStore` and pass it:

```python
from pep_oracle import oauth_store

@pytest.fixture
def client():
    app = FastAPI()
    store = oauth_store.SqliteStore(":memory:")
    register_oauth_routes(app, SIGNING_KEY, PUBLIC_URL, store)
    return TestClient(app)
```

(b) Delete the autouse fixture that does `oauth._auth_codes.clear()` (no module global anymore; each test gets a fresh `:memory:` store).

(c) `test_token_rejects_expired_code` — replace the internal poke with time advancement. Replace the body that did `oauth._auth_codes[code]["expires_at"] = time.time() - 1` with monkeypatching the store clock via `oauth_store.time`:

```python
def test_token_rejects_expired_code(client, monkeypatch):
    # ... obtain `code` from /oauth/authorize as before ...
    # advance the store's clock past the 60s code TTL, then exchange:
    real = time.time()
    monkeypatch.setattr(oauth_store.time, "time", lambda: real + 120)
    resp = client.post("/oauth/token", data={...})  # same exchange as before
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"
```

(d) `test_register_oauth_routes_idempotent_schema` (file-DB) — update to pass a `SqliteStore(str(db))` twice (constructing the store twice against the same file must not error):

```python
def test_register_oauth_routes_idempotent_schema(tmp_path):
    db = tmp_path / "oauth.db"
    oauth_store.SqliteStore(str(db))
    oauth_store.SqliteStore(str(db))  # CREATE TABLE IF NOT EXISTS -> no error
```

- [ ] **Step 2: Run the existing suite to confirm it's red against the old signature**

Run: `uv run pytest tests/test_oauth.py -v`
Expected: FAIL — `register_oauth_routes` still takes a `db_path` string and builds its own `_Store`; tests now pass a store object and reference `oauth_store`.

- [ ] **Step 3: Refactor `oauth.py`.** Make these concrete edits:

(a) Replace the imports/module-state: remove `import sqlite3`, `import threading`, the `_auth_codes` dict, the `_connect`/`_SCHEMA`/`_Store` definitions, and the free functions `_pop_auth_code`, `_persist_refresh`, `_lookup_refresh`, `_revoke_refresh`, `_revoke_family`, `_lookup_client`, `_persist_client`. Add `from pep_oracle.oauth_store import OAuthStore`.

(b) `_issue_token_pair` takes a store and uses it:

```python
def _issue_token_pair(store, signing_key, issuer, client_id, family_id=None):
    access = mint_access_token(signing_key, client_id, issuer=issuer)
    refresh = secrets.token_urlsafe(32)
    if family_id is None:
        family_id = secrets.token_urlsafe(16)
    store.put_refresh(refresh, client_id=client_id, family_id=family_id,
                      ttl_seconds=REFRESH_TTL_SECONDS)
    return {
        "access_token": access, "token_type": "Bearer",
        "expires_in": ACCESS_TTL_SECONDS, "refresh_token": refresh, "scope": SCOPE,
    }
```

(c) `register_oauth_routes` signature becomes `(app, signing_key, public_url, store)`; drop `store = _Store(db_path)`. The route bodies change to call store methods and use record attributes instead of `sqlite3.Row` indexing:

- `register`: `issued_at = store.put_client(client_id, client_name, redirect_uris)`.
- `authorize`: `client = store.get_client(client_id)`; `if client is None: ...`; `if redirect_uri not in client.redirect_uris: ...`; then `store.put_auth_code(code, client_id=client_id, code_challenge=code_challenge, redirect_uri=redirect_uri, ttl_seconds=AUTH_CODE_TTL_SECONDS)`.
- `token` (authorization_code): `entry = store.pop_auth_code(code)`; `if entry is None: ...`; `if entry.client_id != client_id or entry.redirect_uri != redirect_uri: ...`; PKCE compares `entry.code_challenge`.
- `token` (refresh_token) — the concurrency-safe rotation:

```python
        if grant_type == "refresh_token":
            if not (refresh_token and client_id):
                return _err(400, "invalid_request", "missing required fields")
            rec = store.get_refresh(refresh_token)
            if rec is None:
                return _err(400, "invalid_grant", "unknown refresh_token")
            if rec.revoked:
                # Token already revoked at read time = reuse of a rotated token
                # (RFC 9700 §4.13.2): possible compromise -> revoke the family.
                store.revoke_family(rec.family_id)
                logger.warning("Refresh token reuse detected — revoking family family_id=%s client_id=%s",
                               rec.family_id, client_id)
                return _err(400, "invalid_grant", "refresh_token revoked")
            if rec.expires_at <= int(time.time()):
                return _err(400, "invalid_grant", "refresh_token expired")
            if rec.client_id != client_id:
                return _err(400, "invalid_grant", "client_id mismatch")
            if not store.revoke_refresh(refresh_token):
                # Lost a concurrent rotation race (another request revoked it between
                # our read and write). Benign — clean 400, do NOT revoke the family.
                logger.info("refresh: lost rotation race for client_id=%s", client_id)
                return _err(400, "invalid_grant", "refresh_token already rotated")
            logger.info("refresh: rotated refresh_token for client_id=%s", client_id)
            return JSONResponse(_issue_token_pair(store, signing_key, issuer, client_id,
                                                  family_id=rec.family_id))
```

- `revoke`: `rec = store.get_refresh(token); if rec is not None: store.revoke_refresh(token)`.

(d) Keep `mint_access_token`, `verify_access_token`, `_pkce_s256`, `_validate_redirect_uri`, `_err`, the discovery doc, and all the request validation unchanged. Add `REFRESH_TTL_SECONDS` import from `oauth_store` (or keep the constant in oauth.py and pass it). Use `from pep_oracle.oauth_store import OAuthStore, REFRESH_TTL_SECONDS`.

- [ ] **Step 4: Run the full OAuth suite**

Run: `uv run pytest tests/test_oauth.py tests/test_oauth_store.py -v`
Expected: PASS — all 31 `test_oauth.py` tests (now over the injected SqliteStore) + all store-contract tests. Pay attention to `test_refresh_rotation`, `test_refresh_reuse_revokes_family`, and `test_authcode_grant_creates_new_family` — they exercise the rotation/family logic the refactor touched.

- [ ] **Step 5: Commit**

```bash
git add src/pep_oracle/oauth.py tests/test_oauth.py
git commit -m "refactor(oauth): depend on OAuthStore; concurrency-safe conditional rotation"
```

---

## Task 6: Wire the server mount to the configured store

**Files:**
- Modify: `src/pep_oracle/server.py`
- Test: `tests/test_server.py`

- [ ] **Step 1: Write the failing test** — append to `tests/test_server.py`:

```python
def test_mount_builds_oauth_store_from_config(monkeypatch):
    """mount_mcp_if_configured passes a store object (not a db path) built from
    config to register_oauth_routes."""
    from pep_oracle import oauth_store, server

    captured = {}

    def fake_register(app, signing_key, public_url, store):
        captured["store"] = store

    monkeypatch.setenv("PEP_ORACLE_PUBLIC_URL", "https://pep-oracle.example")
    monkeypatch.setenv("PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH", "1")
    monkeypatch.setattr(server.oauth, "register_oauth_routes", fake_register)
    monkeypatch.setattr(server, "_resolve_signing_key", lambda: "k")
    # avoid actually mounting the MCP sub-app in this unit test:
    monkeypatch.setattr(server, "_mount_mcp_app", lambda *a, **k: None, raising=False)

    from fastapi import FastAPI
    server.mount_mcp_if_configured(FastAPI())
    assert isinstance(captured["store"], oauth_store.OAuthStore.__mro__[0]) or hasattr(captured["store"], "get_refresh")
```

> NOTE: `OAuthStore` is a `Protocol`; assert structurally (`hasattr(store, "get_refresh")`) rather than via isinstance. The test above keeps the `hasattr` fallback for that reason. If `_mount_mcp_app` doesn't exist as a seam, instead monkeypatch `pep_oracle.mcp_server.mcp` interactions minimally, OR assert on `captured` after letting the real mount run against a sqlite store (default config) — simplest is to assert `hasattr(captured["store"], "get_refresh")`.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_server.py -k oauth_store -v`
Expected: FAIL — `mount_mcp_if_configured` still calls `register_oauth_routes(app, signing_key, public_url, str(data_dir / "oauth.db"))` with a path string, so `captured["store"]` is a `str` without `get_refresh`.

- [ ] **Step 3: Implement** — in `src/pep_oracle/server.py`, change the `register_oauth_routes` call inside `mount_mcp_if_configured`. Replace:

```python
    oauth.register_oauth_routes(app, signing_key, public_url, str(data_dir / "oauth.db"))
```

with:

```python
    from pep_oracle import oauth_store

    store = oauth_store.get_store()
    oauth.register_oauth_routes(app, signing_key, public_url, store)
```

(`get_store()` returns a `SqliteStore` at `DATA_DIR/oauth.db` by default, or a `DynamoDbStore` when `PEP_ORACLE_OAUTH_STORE=dynamodb`. The `data_dir` local is still used elsewhere in the function for the signing key path — leave that.)

- [ ] **Step 4: Run to verify it passes + the whole suite**

Run: `uv run pytest tests/test_server.py -k oauth_store -v && uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pep_oracle/server.py tests/test_server.py
git commit -m "feat(oauth): mount builds the OAuth store from config (sqlite|dynamodb)"
```

---

## Task 7: Docs + CLAUDE.md

**Files:**
- Create: `docs/aws/phase2b-oauth-store.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Write the doc** — create `docs/aws/phase2b-oauth-store.md`:

````markdown
# Phase 2b — OAuth state in DynamoDB

The OAuth provider's state (registered clients, single-use auth codes, refresh
tokens) lives behind `oauth_store.OAuthStore`. `PEP_ORACLE_OAUTH_STORE` selects the
backend: `sqlite` (local default, `~/.pep-oracle/oauth.db`) or `dynamodb` (the
Lambda). Auth codes moved out of the in-process `_auth_codes` dict into the store so
any stateless Lambda container can complete an authorize→token exchange.

## Why DynamoDB for the Lambda
Refresh rotation must be race-safe across concurrent containers. `revoke_refresh`
is a conditional write (`revoked = 0` guard) returning won/lost; on `/oauth/token`
refresh, the winner issues the new pair and the loser gets a clean 400 — **no**
spurious family revocation. Genuine reuse (a token already revoked at read time)
still revokes the whole family (RFC 9700 §4.13.2).

## DynamoDB table (provisioned by CDK in Phase 2c)
Single table, `pk` = `client#…` / `code#…` / `refresh#…`, a `family-index` GSI on
`family_id` (for family revocation), and native `ttl` (cleanup only — reads still
check `expires_at`). `DynamoDbStore.ensure_table()` creates it for local/moto; prod
comes from CDK.

## Local
Default is SQLite — nothing to run. The DynamoDB path is covered by the contract
tests (`tests/test_oauth_store.py`, moto) which run every behavior against BOTH
backends, so SQLite and DynamoDB are held to one spec.

## Out of scope (Phase 2b2 / 2c)
JWT signing seam (HS256 from SSM) + Cognito gate on `/oauth/authorize` are **2b2**.
The real DynamoDB table + IAM are **2c**.
````

- [ ] **Step 2: Add a CLAUDE.md bullet** — after the serving-source-seam bullet (Phase 2a), insert:

```markdown
- **OAuth store seam** (`oauth_store.py`): clients + single-use auth codes + refresh tokens behind `OAuthStore`; `PEP_ORACLE_OAUTH_STORE` selects `sqlite` (local default, `~/.pep-oracle/oauth.db`) or `dynamodb` (Lambda). Auth codes moved out of the in-process `_auth_codes` dict into the store (stateless-Lambda requirement). Rotation is race-safe via a conditional `revoke_refresh` (won/lost) — concurrent refreshes → exactly one rotation, loser gets a clean 400, no spurious family revoke; genuine reuse still revokes the family. DynamoDB = single table + `family-index` GSI + native TTL; `DynamoDbStore.ensure_table()` is local/moto only (prod table from CDK, Phase 2c). Contract tests (`tests/test_oauth_store.py`, moto) run every behavior against both backends. Phase 2b of the AWS migration; signing seam + Cognito gate are 2b2.
```

Confirm `wc -l CLAUDE.md` stays well under 300.

- [ ] **Step 3: Final full-suite run**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add docs/aws/phase2b-oauth-store.md CLAUDE.md
git commit -m "docs(oauth): Phase 2b OAuth store + CLAUDE.md note"
```

---

## Self-Review (completed by plan author)

**Spec coverage (OAuth-state slice of Section 1 concurrency + Section 4 secrets):**
- OAuth state → DynamoDB (on-demand) → Tasks 1–3 (`DynamoDbStore`, `PAY_PER_REQUEST`). ✓
- Conditional-write token rotation, exactly one winner, loser clean 400, no wrongful family revoke → Task 3 `revoke_refresh` + Task 5 refactored refresh path + Task 4 race test. ✓
- Stateless serving (auth codes not in-process) → auth codes moved into the store (Tasks 1–3, 5). ✓
- Local/CI parity, one spec for both backends → the parametrized contract suite (Tasks 1, 3, 4). ✓
- TTL on codes/tokens → DynamoDB `ttl` attribute + read-time `expires_at` checks (Task 3). ✓
- **Deferred (correctly out of 2b):** JWT signing-backend seam + HS256-from-SSM, Cognito gate (Phase 2b2); real DynamoDB table + IAM + KMS-at-rest (Phase 2c). JWT mint/verify stays HS256 with the existing `signing_key`.

**Placeholder scan:** Every code/test step has complete content. The one soft spot is Task 6's `_mount_mcp_app` seam note — the test asserts structurally (`hasattr(store, "get_refresh")`) and the implementation is a concrete two-line change; the NOTE gives the executor a fallback if the mount internals differ. No "TBD"/"add validation".

**Type consistency:** `OAuthStore` methods (`put_client`/`get_client`/`put_auth_code`/`pop_auth_code`/`put_refresh`/`get_refresh`/`revoke_refresh`/`revoke_family`) and the record dataclasses (`ClientRecord.redirect_uris`, `AuthCodeRecord.code_challenge`/`.redirect_uri`/`.client_id`, `RefreshRecord.revoked`/`.family_id`/`.expires_at`/`.client_id`) are defined in Task 1 and used identically in the SqliteStore (Task 2), DynamoDbStore (Task 3), the oauth.py refactor (Task 5), and the server wiring (Task 6). `revoke_refresh -> bool` (won/lost) semantics are consistent across both stores and the refresh path. `register_oauth_routes(app, signing_key, public_url, store)` signature matches Task 5 (definition) and Task 6 (caller). `get_store()` (Task 1) is called in Task 6.

---

## Execution Handoff

Phase 2b plan saved to `docs/superpowers/plans/2026-06-03-aws-migration-phase2b-oauth-dynamodb.md`. Per the standing preference I'll execute it subagent-driven (fresh subagent per task, two-stage review) in an isolated worktree, unless you'd rather review first. After 2b: **2b2** (JWT signing seam HS256/SSM + Cognito gate) and **2c** (CDK + deploy).
