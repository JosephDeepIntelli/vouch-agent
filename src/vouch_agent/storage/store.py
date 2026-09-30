"""SQLite metadata store + content-addressed artifact store (design §5, §10).

The design doc's local persistence recommendation is "SQLite 元数据事务 +
按内容寻址的工件文件". This module implements the two v1 storage ports from
``vouch_agent.storage.interfaces``:

* :class:`SqliteMetadataStore` — typed kind+id record persistence with
  explicit transactions. WAL mode; writer transactions take
  ``BEGIN IMMEDIATE`` so concurrent writers serialize at the database,
  not in Python.
* :class:`FileArtifactStore` — content-addressed file tree under
  ``<root>/artifacts/``. Names are ``sha256:<hex>`` digests, non-conforming
  names are refused (which is what blocks path escapes), and every read
  re-verifies the content digest.

Record kind naming convention used across Vouch (the store itself is
kind-agnostic): ``project``, ``task-pack``, ``task-case``, ``candidate``,
``rubric``, ``evaluation-run``, ``acceptance-decision``, ``release-record``,
``skill-entry``.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from vouch_agent.contracts.common import DIGEST_PREFIX, canonical_json, digest_bytes
from vouch_agent.errors import ContractError, DigestMismatchError

#: SQLite busy timeout for all connections (ms). Long enough to survive
#: cross-process ``BEGIN IMMEDIATE`` contention, short enough to fail loudly.
BUSY_TIMEOUT_MS = 30_000

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")

_RECORDS_DDL = """
CREATE TABLE IF NOT EXISTS records (
    kind TEXT NOT NULL,
    record_id TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (kind, record_id)
)
"""


def _require_nonempty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{field} must be a non-empty string, got {value!r}")
    return value


class SqliteMetadataStore:
    """Transactional metadata persistence for contract records.

    One record = (kind, record_id) -> canonical JSON blob. Saves are upserts;
    immutability of frozen records is a digest concern (approval bindings),
    not a row concern — overwriting a record changes its digest and thereby
    invalidates anything bound to the old digest, which is exactly the
    designed behavior.

    Thread safety: one connection with ``check_same_thread=False`` guarded by
    an RLock; a single store instance is usable from multiple threads within
    one process. Multiple processes should each open their own instance;
    WAL + ``BEGIN IMMEDIATE`` + busy timeout keep them consistent.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self._path),
            timeout=BUSY_TIMEOUT_MS / 1000,
            check_same_thread=False,
            isolation_level=None,  # explicit transaction control
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        self._tx_depth = 0
        with self._lock:
            self._conn.execute(_RECORDS_DDL)

    # -- MetadataStore port ---------------------------------------------------

    def save(self, kind: str, record_id: str, data: dict) -> None:
        kind = _require_nonempty_str(kind, "kind")
        record_id = _require_nonempty_str(record_id, "record_id")
        if not isinstance(data, dict):
            raise ContractError(f"record data must be a dict, got {type(data).__name__}")
        try:
            payload = canonical_json(data)
        except (TypeError, ValueError) as exc:
            raise ContractError(f"record {kind}/{record_id} is not serializable: {exc}") from exc
        with self._lock:
            self._conn.execute(
                "INSERT INTO records (kind, record_id, data) VALUES (?, ?, ?) "
                "ON CONFLICT (kind, record_id) DO UPDATE SET data = excluded.data",
                (kind, record_id, payload),
            )

    def load(self, kind: str, record_id: str) -> dict | None:
        kind = _require_nonempty_str(kind, "kind")
        record_id = _require_nonempty_str(record_id, "record_id")
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM records WHERE kind = ? AND record_id = ?",
                (kind, record_id),
            ).fetchone()
        if row is None:
            return None
        parsed = json.loads(row[0])
        if not isinstance(parsed, dict):
            raise ContractError(f"stored record {kind}/{record_id} is not a dict")
        return parsed

    def list_ids(self, kind: str) -> list[str]:
        kind = _require_nonempty_str(kind, "kind")
        with self._lock:
            rows = self._conn.execute(
                "SELECT record_id FROM records WHERE kind = ? ORDER BY record_id", (kind,)
            ).fetchall()
        return [row[0] for row in rows]

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group saves into one atomic SQLite transaction.

        The first (outermost) ``transaction()`` takes ``BEGIN IMMEDIATE`` so
        the write lock is acquired up front; nested uses map to savepoints.
        An exception rolls back the whole outermost transaction.
        """
        with self._lock:
            if self._tx_depth > 0:
                savepoint = f"vouch_sp_{self._tx_depth}_{uuid.uuid4().hex[:8]}"
                self._conn.execute(f"SAVEPOINT {savepoint}")
                self._tx_depth += 1
                try:
                    yield
                except BaseException:
                    self._tx_depth -= 1
                    self._conn.execute(f"ROLLBACK TO {savepoint}")
                    self._conn.execute(f"RELEASE {savepoint}")
                    raise
                else:
                    self._tx_depth -= 1
                    self._conn.execute(f"RELEASE {savepoint}")
                    return
            self._conn.execute("BEGIN IMMEDIATE")
            self._tx_depth += 1
            try:
                yield
            except BaseException:
                self._tx_depth -= 1
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._tx_depth -= 1
                self._conn.execute("COMMIT")

    # -- lifecycle --------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            if self._tx_depth > 0:  # defensive: caller leaked a transaction
                self._conn.execute("ROLLBACK")
                self._tx_depth = 0
            self._conn.close()

    def __enter__(self) -> SqliteMetadataStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class FileArtifactStore:
    """Content-addressed artifact bytes under ``<root>/artifacts/``.

    Names are ``sha256:<hex>`` digests. Anything that is not exactly that
    shape is refused before it ever reaches the filesystem, which is what
    makes path escapes (``../``, absolute paths, symlink-ish names) impossible
    rather than merely filtered. Reads re-verify the content digest so on-disk
    tampering surfaces as :class:`DigestMismatchError` instead of silently
    returning different bytes than a digest promised.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._dir = self._root / "artifacts"
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, digest: str) -> Path:
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            raise ContractError(f"artifact name must be a 'sha256:<64 hex>' digest, got {digest!r}")
        # Only [0-9a-f]{64} ever reaches the filesystem — no separators,
        # no dot segments, no way to leave the artifact directory.
        return self._dir / digest.removeprefix(DIGEST_PREFIX)

    def put(self, payload: bytes) -> str:
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise ContractError(
                f"artifact payload must be bytes-like, got {type(payload).__name__}"
            )
        data = bytes(payload)
        digest = digest_bytes(data)
        path = self._path_for(digest)
        if path.exists():
            return digest  # content addressing makes re-puts idempotent
        tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:12]}")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)  # atomic within the artifact directory
        finally:
            if tmp.exists():
                tmp.unlink()
        return digest

    def get(self, digest: str) -> bytes:
        path = self._path_for(digest)
        if not path.exists():
            raise ContractError(f"artifact {digest} not found")
        data = path.read_bytes()
        if digest_bytes(data) != digest:
            raise DigestMismatchError(
                f"artifact {digest} content does not match its name — tampering or corruption"
            )
        return data

    def exists(self, digest: str) -> bool:
        return self._path_for(digest).exists()
