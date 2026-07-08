"""Direct-DB helpers mirroring the app's write paths, for test setup."""
from __future__ import annotations

import sqlite3

from ghitime import flags as flag_mod
from ghitime.db import utcnow
from ghitime.lifecycle import insert_version, new_uuid


def person(conn: sqlite3.Connection, username: str) -> sqlite3.Row:
    return conn.execute("SELECT * FROM person WHERE username=?", (username,)).fetchone()


def job_id(conn: sqlite3.Connection, code: str) -> int:
    return conn.execute("SELECT id FROM job WHERE code=?", (code,)).fetchone()["id"]


def add_entry(conn, author_row, *, work_date, start="08:00", end="16:00", brk=30,
              job="J1", note=None, uuid=None, person_id=None):
    uuid = uuid or new_uuid()
    vid = insert_version(
        conn, entry_uuid=uuid, person_id=person_id or author_row["id"],
        job_id=job_id(conn, job), work_date=work_date, start_time=start,
        end_time=end, break_minutes=brk, note=note, status="draft",
        author=author_row, change_reason=None,
    )
    row = conn.execute("SELECT * FROM time_entry_version WHERE id=?", (vid,)).fetchone()
    flag_mod.recompute_for_version(conn, row)
    conn.commit()
    return uuid, vid


def advance(conn, uuid, status, author_row, reason):
    cv = conn.execute("SELECT * FROM v_time_entry_current WHERE entry_uuid=?", (uuid,)).fetchone()
    vid = insert_version(
        conn, entry_uuid=uuid, person_id=cv["person_id"], job_id=cv["job_id"],
        work_date=cv["work_date"], start_time=cv["start_time"], end_time=cv["end_time"],
        break_minutes=cv["break_minutes"], note=cv["note"], status=status,
        author=author_row, change_reason=reason,
    )
    row = conn.execute("SELECT * FROM time_entry_version WHERE id=?", (vid,)).fetchone()
    flag_mod.recompute_for_version(conn, row)
    conn.commit()
    return vid


def submit_and_approve(conn, uuid, worker_row, admin_row, ack=None):
    advance(conn, uuid, "submitted", worker_row, "Submitted")
    cv = conn.execute("SELECT * FROM v_time_entry_current WHERE entry_uuid=?", (uuid,)).fetchone()
    ap = conn.execute(
        "INSERT INTO approval (approver_id, action, flags_ack_reason, is_self_approval,"
        " created_at) VALUES (?,?,?,?,?)",
        (admin_row["id"], "approve", ack,
         1 if cv["person_id"] == admin_row["id"] else 0, utcnow()),
    )
    vid = advance(conn, uuid, "approved", admin_row, "Approved")
    conn.execute(
        "INSERT INTO approval_entry (approval_id, entry_uuid, acted_on_version_id,"
        " resulting_version_id) VALUES (?,?,?,?)",
        (ap.lastrowid, uuid, cv["id"], vid),
    )
    conn.commit()
    return vid


def set_workweek_monday(conn):
    conn.execute("UPDATE config SET value='0' WHERE key='workweek_start_dow'")
    conn.commit()


def add_ot_policy(conn, admin_row, threshold, multiplier, effective_date):
    conn.execute(
        "INSERT INTO ot_policy (threshold_hours, multiplier, effective_date,"
        " entered_by, entered_at) VALUES (?,?,?,?,?)",
        (threshold, multiplier, effective_date, admin_row["id"], utcnow()),
    )
    conn.commit()


def add_rate(conn, admin_row, person_id, cents, effective_date, table="rate_pay"):
    conn.execute(
        f"INSERT INTO {table} (person_id, hourly_rate_cents, effective_date,"
        " entered_by, entered_at) VALUES (?,?,?,?,?)",
        (person_id, cents, effective_date, admin_row["id"], utcnow()),
    )
    conn.commit()
