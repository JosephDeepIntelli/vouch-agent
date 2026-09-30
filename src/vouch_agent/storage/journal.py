"""Append-only event + cost journal on SQLite (design §5, §10).

The journal is the evidence trail: every run/attempt/gate/budget event and
every booked cost line, append-only, ordered by insertion sequence. Records
are never updated; corrections are new events. Duplicate ids are refused —
a replayed event is a bug, not a no-op.

Worker sessions do not get this object; they get
:meth:`SqliteJournal.subject_writer`, which forces every appended record onto
one subject so a worker cannot write journal rows about other subjects.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from vouch_agent.contracts.common import canonical_json
from vouch_agent.contracts.journal import CostEntry, EventRecord
from vouch_agent.errors import ContractError
from vouch_agent.storage.store import BUSY_TIMEOUT_MS

_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS journal_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    subject TEXT NOT NULL,
    data TEXT NOT NULL,
    mode TEXT NOT NULL
)
"""

_COSTS_DDL = """
CREATE TABLE IF NOT EXISTS journal_costs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    subject TEXT NOT NULL,
    amount_usd REAL,
    human_minutes REAL,
    measurable INTEGER NOT NULL,
    mode TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    note TEXT NOT NULL
)
"""


class _JournalLike(Protocol):
    def append(self, event: EventRecord) -> None: ...

    def append_cost(self, entry: CostEntry) -> None: ...


class SqliteJournal:
    """Append-only event + cost journal, readable with subject filters.

    Each operation opens its own short-lived SQLite connection. That is a
    deliberate choice: it makes one instance safely usable from multiple
    threads *and* multiple processes without any in-process locking protocol,
    at the cost of a connection setup per append (microseconds on a local
    filesystem).
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_EVENTS_DDL)
            conn.execute(_COSTS_DDL)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None
        )
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        return conn

    # -- Journal port ----------------------------------------------------------

    def append(self, event: EventRecord) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO journal_events "
                "(event_id, kind, occurred_at, actor, subject, data, mode) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.kind.value,
                    event.occurred_at,
                    event.actor,
                    event.subject,
                    canonical_json(event.data),
                    event.mode.value,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ContractError(f"duplicate event id {event.event_id!r}: {exc}") from exc
        finally:
            conn.close()

    def append_cost(self, entry: CostEntry) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO journal_costs "
                "(entry_id, category, subject, amount_usd, human_minutes, measurable, "
                " mode, recorded_at, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.entry_id,
                    entry.category.value,
                    entry.subject,
                    entry.amount_usd,
                    entry.human_minutes,
                    1 if entry.measurable else 0,
                    entry.mode.value,
                    entry.recorded_at,
                    entry.note,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ContractError(f"duplicate cost entry id {entry.entry_id!r}: {exc}") from exc
        finally:
            conn.close()

    def events(self, subject: str | None = None) -> list[EventRecord]:
        conn = self._connect()
        try:
            if subject is None:
                rows = conn.execute(
                    "SELECT event_id, kind, occurred_at, actor, subject, data, mode "
                    "FROM journal_events ORDER BY seq"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT event_id, kind, occurred_at, actor, subject, data, mode "
                    "FROM journal_events WHERE subject = ? ORDER BY seq",
                    (subject,),
                ).fetchall()
        finally:
            conn.close()
        return [
            EventRecord.from_dict(
                {
                    "schemaVersion": "1",
                    "eventId": row[0],
                    "kind": row[1],
                    "occurredAt": row[2],
                    "actor": row[3],
                    "subject": row[4],
                    "data": _loads_object(row[5], row[0]),
                    "mode": row[6],
                }
            )
            for row in rows
        ]

    def cost_entries(self, subject: str | None = None) -> list[CostEntry]:
        conn = self._connect()
        try:
            if subject is None:
                rows = conn.execute(
                    "SELECT entry_id, category, subject, amount_usd, human_minutes, "
                    "measurable, mode, recorded_at, note FROM journal_costs ORDER BY seq"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT entry_id, category, subject, amount_usd, human_minutes, "
                    "measurable, mode, recorded_at, note "
                    "FROM journal_costs WHERE subject = ? ORDER BY seq",
                    (subject,),
                ).fetchall()
        finally:
            conn.close()
        return [
            CostEntry.from_dict(
                {
                    "schemaVersion": "1",
                    "entryId": row[0],
                    "category": row[1],
                    "subject": row[2],
                    "amountUsd": row[3],
                    "humanMinutes": row[4],
                    "measurable": bool(row[5]),
                    "mode": row[6],
                    "recordedAt": row[7],
                    "note": row[8],
                }
            )
            for row in rows
        ]

    # -- worker-facing scoped writer ---------------------------------------------

    def subject_writer(self, subject: str) -> SubjectJournal:
        """A journal writer pinned to one subject — what a worker session gets.

        Events carrying a different subject are refused rather than rewritten,
        so a misbehaving worker cannot even accidentally file events under
        another subject's name; empty subjects are filled in.
        """
        if not isinstance(subject, str) or not subject:
            raise ContractError("subject must be a non-empty string")
        return SubjectJournal(self, subject)


class SubjectJournal:
    """Journal view restricted to a single subject (worker-facing)."""

    def __init__(self, journal: _JournalLike, subject: str) -> None:
        self._journal = journal
        self._subject = subject

    @property
    def subject(self) -> str:
        return self._subject

    def append(self, event: EventRecord) -> None:
        if event.subject and event.subject != self._subject:
            raise ContractError(
                f"subject-scoped writer for {self._subject!r} cannot append "
                f"an event about {event.subject!r}"
            )
        self._journal.append(replace(event, subject=self._subject))

    def append_cost(self, entry: CostEntry) -> None:
        if entry.subject and entry.subject != self._subject:
            raise ContractError(
                f"subject-scoped writer for {self._subject!r} cannot append "
                f"a cost entry about {entry.subject!r}"
            )
        self._journal.append_cost(replace(entry, subject=self._subject))


def _loads_object(payload: str, ref: str) -> dict:
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ContractError(f"journal event {ref!r} carries non-object data")
    return parsed
