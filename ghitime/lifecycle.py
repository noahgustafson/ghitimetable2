"""Entry version lifecycle. Every status change is a new append-only version;
the schema enforces immutability/contiguity/ownership, this module enforces
the legal transition table (GATE1.md §1) and who may author what.

    draft     -> draft | submitted | void
    submitted -> submitted | approved | draft (reject) | void
    approved  -> approved (admin post-approval correction only) | void (admin)

Any worker-authored version after approved is illegal (Gate 2 binding #1).
"""
from __future__ import annotations

import sqlite3
import uuid as uuidlib

from .db import utcnow

LEGAL = {
    ("draft", "draft"),
    ("draft", "submitted"),
    ("draft", "void"),
    ("submitted", "submitted"),
    ("submitted", "approved"),
    ("submitted", "draft"),
    ("submitted", "void"),
    ("approved", "approved"),
    ("approved", "void"),
}

# transitions only an admin may author (regardless of who owns the entry)
ADMIN_ONLY = {
    ("submitted", "approved"),
    ("submitted", "draft"),     # reject
    ("approved", "approved"),   # post-approval correction
    ("approved", "void"),
    ("submitted", "void"),      # pulling a submitted entry back is admin's call
}


class TransitionError(ValueError):
    """Illegal status transition or unauthorized author."""


def new_uuid() -> str:
    return str(uuidlib.uuid4())


def current_version(conn: sqlite3.Connection, entry_uuid: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM v_time_entry_current WHERE entry_uuid=?", (entry_uuid,)
    ).fetchone()


def versions(conn: sqlite3.Connection, entry_uuid: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM time_entry_version WHERE entry_uuid=? ORDER BY version_no",
        (entry_uuid,),
    ).fetchall()


def insert_version(
    conn: sqlite3.Connection,
    *,
    entry_uuid: str,
    person_id: int,
    job_id: int,
    work_date: str,
    start_time: str,
    end_time: str,
    break_minutes: int,
    note: str | None,
    status: str,
    author: sqlite3.Row,
    change_reason: str | None,
    device_created_at: str | None = None,
) -> int:
    """Append a version after enforcing the transition table. Returns row id.
    Caller owns the transaction and flag recomputation."""
    cur = current_version(conn, entry_uuid)
    author_is_admin = bool(author["is_admin"])
    author_is_owner = author["id"] == person_id

    if cur is None:
        if status != "draft":
            raise TransitionError("new entries start as draft")
        if not (author_is_owner or author_is_admin):
            raise TransitionError("author must be the entry's person or an admin")
    else:
        if cur["person_id"] != person_id:
            raise TransitionError("person_id is immutable; void and re-enter instead")
        pair = (cur["status"], status)
        if pair not in LEGAL:
            raise TransitionError(f"illegal transition {cur['status']} -> {status}")
        if pair in ADMIN_ONLY and not author_is_admin:
            raise TransitionError(f"transition {cur['status']} -> {status} requires admin")
        if cur["status"] == "approved" and not author_is_admin:
            # belt-and-suspenders for binding #1: nothing worker-authored
            # may follow an approved version
            raise TransitionError("approved entries accept admin-authored versions only")
        if not (author_is_owner or author_is_admin):
            raise TransitionError("author must be the entry's person or an admin")
        if cur["status"] == "void":
            raise TransitionError("void entries accept no further versions")

    version_no = 1 if cur is None else cur["version_no"] + 1
    cursor = conn.execute(
        "INSERT INTO time_entry_version (entry_uuid, version_no, person_id, job_id,"
        " work_date, start_time, end_time, break_minutes, note, status, author_id,"
        " change_reason, device_created_at, server_synced_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            entry_uuid,
            version_no,
            person_id,
            job_id,
            work_date,
            start_time,
            end_time,
            break_minutes,
            note,
            status,
            author["id"],
            change_reason,
            device_created_at,
            utcnow(),
        ),
    )
    return cursor.lastrowid


def payload_fields(row) -> dict:
    """The client-authored payload of a version — the identity used for sync
    dedup (identical resubmission) vs conflict (same key, different payload)."""
    keys = ("job_id", "work_date", "start_time", "end_time", "break_minutes", "note", "status")
    if isinstance(row, sqlite3.Row):
        return {k: row[k] for k in keys}
    return {k: row.get(k) for k in keys}
