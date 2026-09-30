"""Journal behavior: append-only, subject filters, scoped writers."""

from __future__ import annotations

import pytest

from vouch_agent.contracts.common import RunMode, new_id
from vouch_agent.contracts.journal import CostCategory, CostEntry, EventKind, EventRecord
from vouch_agent.errors import ContractError
from vouch_agent.storage.journal import SqliteJournal


def _event(subject: str = "run_1", kind: EventKind = EventKind.RUN_STARTED, **data) -> EventRecord:
    return EventRecord(
        event_id=new_id("evt"),
        kind=kind,
        subject=subject,
        data=dict(data),
        mode=RunMode.FIXTURE,
    )


def _cost(subject: str = "run_1", amount: float | None = 0.01) -> CostEntry:
    return CostEntry(
        entry_id=new_id("cost"),
        category=CostCategory.MODEL,
        subject=subject,
        amount_usd=amount,
        measurable=amount is not None,
        mode=RunMode.FIXTURE,
    )


def test_append_and_read_events_in_order(tmp_path):
    journal = SqliteJournal(tmp_path / "journal.db")
    journal.append(_event(kind=EventKind.RUN_STARTED))
    journal.append(_event(kind=EventKind.RUN_COMPLETED))
    events = journal.events()
    assert [e.kind for e in events] == [EventKind.RUN_STARTED, EventKind.RUN_COMPLETED]
    assert all(e.mode is RunMode.FIXTURE for e in events)


def test_events_filtered_by_subject(tmp_path):
    journal = SqliteJournal(tmp_path / "journal.db")
    journal.append(_event(subject="run_a"))
    journal.append(_event(subject="run_b"))
    journal.append(_event(subject="run_a"))
    assert len(journal.events()) == 3
    only_a = journal.events(subject="run_a")
    assert [e.subject for e in only_a] == ["run_a", "run_a"]


def test_duplicate_event_id_refused(tmp_path):
    journal = SqliteJournal(tmp_path / "journal.db")
    event = _event()
    journal.append(event)
    with pytest.raises(ContractError, match="duplicate"):
        journal.append(event)


def test_cost_entries_roundtrip_and_filter(tmp_path):
    journal = SqliteJournal(tmp_path / "journal.db")
    journal.append_cost(_cost(subject="att_1", amount=0.02))
    journal.append_cost(_cost(subject="att_2", amount=0.03))
    unmeasurable = CostEntry(
        entry_id=new_id("cost"),
        category=CostCategory.TOOL,
        subject="att_3",
        amount_usd=None,
        measurable=False,
        mode=RunMode.FIXTURE,
        note="search api pricing unknown",
    )
    journal.append_cost(unmeasurable)
    entries = journal.cost_entries()
    assert [e.subject for e in entries] == ["att_1", "att_2", "att_3"]
    assert entries[2].amount_usd is None and entries[2].measurable is False
    only_2 = journal.cost_entries(subject="att_2")
    assert len(only_2) == 1 and only_2[0].amount_usd == pytest.approx(0.03)


def test_duplicate_cost_entry_id_refused(tmp_path):
    journal = SqliteJournal(tmp_path / "journal.db")
    entry = _cost()
    journal.append_cost(entry)
    with pytest.raises(ContractError, match="duplicate"):
        journal.append_cost(entry)


def test_journal_survives_reopen(tmp_path):
    db = tmp_path / "journal.db"
    journal = SqliteJournal(db)
    journal.append(_event(subject="run_x", step=7))
    journal.append_cost(_cost(subject="run_x"))
    reopened = SqliteJournal(db)
    assert reopened.events(subject="run_x")[0].data == {"step": 7}
    assert reopened.cost_entries(subject="run_x")[0].amount_usd is not None


def test_subject_writer_pins_subject(tmp_path):
    journal = SqliteJournal(tmp_path / "journal.db")
    writer = journal.subject_writer("att_worker_1")
    writer.append(_event(subject="", kind=EventKind.ATTEMPT_STARTED))
    writer.append_cost(_cost(subject="att_worker_1"))
    events = journal.events(subject="att_worker_1")
    assert len(events) == 1
    assert journal.cost_entries(subject="att_worker_1")[0].category is CostCategory.MODEL


def test_subject_writer_refuses_foreign_subjects(tmp_path):
    journal = SqliteJournal(tmp_path / "journal.db")
    writer = journal.subject_writer("att_worker_1")
    with pytest.raises(ContractError, match="cannot append"):
        writer.append(_event(subject="someone_else"))
    with pytest.raises(ContractError, match="cannot append"):
        writer.append_cost(_cost(subject="someone_else"))
    # nothing leaked
    assert journal.events() == [] and journal.cost_entries() == []
