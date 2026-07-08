"""Flag engine: conflicts are surfaced, never silently reconciled.

Data-integrity flag types feed the review queue and gate approval (schema
trigger demands flags_ack_reason); badge types travel onto printouts and
exports. Flags are raised at write/sync time against the version that
triggered them; when a newer version no longer exhibits a condition, the open
flag is resolved with a stated system reason — resolution is recorded, the
flag row itself is never deleted.
"""
from __future__ import annotations

import json
import sqlite3

from .db import today_local, utcnow

DATA_INTEGRITY_TYPES = (
    "overlap",
    "over_16h",
    "duplicate",
    "future_dated",
    "end_not_after_start",
    "break_exceeds_duration",
)
BADGE_TYPES = ("self_approval", "post_approval_correction")

OVER_16H_MINUTES = 16 * 60


def _open_types(conn: sqlite3.Connection, entry_uuid: str) -> dict[str, int]:
    return {
        r["flag_type"]: r["id"]
        for r in conn.execute(
            "SELECT id, flag_type FROM entry_flag WHERE entry_uuid=? AND resolved_at IS NULL",
            (entry_uuid,),
        )
    }


def raise_flag(
    conn: sqlite3.Connection,
    entry_uuid: str,
    version_id: int,
    flag_type: str,
    detail: dict | None = None,
) -> None:
    if flag_type in _open_types(conn, entry_uuid) and flag_type != "post_approval_correction":
        return  # one open flag per type per entry; badges for corrections stack
    conn.execute(
        "INSERT INTO entry_flag (entry_uuid, trigger_version_id, flag_type, detail, created_at)"
        " VALUES (?,?,?,?,?)",
        (entry_uuid, version_id, flag_type, json.dumps(detail) if detail else None, utcnow()),
    )


def _resolve(conn: sqlite3.Connection, flag_id: int, resolver_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE entry_flag SET resolved_at=?, resolved_by=?, resolution_reason=?"
        " WHERE id=? AND resolved_at IS NULL",
        (utcnow(), resolver_id, reason, flag_id),
    )


def recompute_for_version(conn: sqlite3.Connection, version_row: sqlite3.Row) -> list[str]:
    """Evaluate all data-integrity conditions for a just-inserted version.
    Raises missing flags; resolves open ones whose condition cleared.
    Returns the list of condition types currently present."""
    uuid = version_row["entry_uuid"]
    vid = version_row["id"]
    present: dict[str, dict | None] = {}

    minutes = conn.execute(
        "SELECT span_minutes, worked_minutes FROM v_time_entry_minutes WHERE entry_uuid=?",
        (uuid,),
    ).fetchone()

    if version_row["end_time"] <= version_row["start_time"]:
        present["end_not_after_start"] = None
    elif minutes and minutes["span_minutes"] is not None:
        if version_row["break_minutes"] > minutes["span_minutes"]:
            present["break_exceeds_duration"] = None
        if (
            minutes["worked_minutes"] is not None
            and minutes["worked_minutes"] > OVER_16H_MINUTES
        ):
            present["over_16h"] = None

    if version_row["work_date"] > today_local().isoformat():
        present["future_dated"] = None

    if version_row["status"] != "void":
        others = conn.execute(
            "SELECT entry_uuid, start_time, end_time FROM v_time_entry_minutes"
            " WHERE person_id=? AND work_date=? AND entry_uuid<>? AND status<>'void'",
            (version_row["person_id"], version_row["work_date"], uuid),
        ).fetchall()
        for other in others:
            if (
                other["start_time"] == version_row["start_time"]
                and other["end_time"] == version_row["end_time"]
            ):
                present["duplicate"] = {"other_entry_uuid": other["entry_uuid"]}
                # duplicate flag goes on BOTH entries (resolved question 5)
                other_vid = conn.execute(
                    "SELECT id FROM v_time_entry_current WHERE entry_uuid=?",
                    (other["entry_uuid"],),
                ).fetchone()["id"]
                raise_flag(conn, other["entry_uuid"], other_vid, "duplicate",
                           {"other_entry_uuid": uuid})
            elif (
                version_row["end_time"] > version_row["start_time"]
                and other["end_time"] > other["start_time"]
                and version_row["start_time"] < other["end_time"]
                and other["start_time"] < version_row["end_time"]
            ):
                present["overlap"] = {"other_entry_uuid": other["entry_uuid"]}
                other_vid = conn.execute(
                    "SELECT id FROM v_time_entry_current WHERE entry_uuid=?",
                    (other["entry_uuid"],),
                ).fetchone()["id"]
                raise_flag(conn, other["entry_uuid"], other_vid, "overlap",
                           {"other_entry_uuid": uuid})

    open_now = _open_types(conn, uuid)
    for ftype, detail in present.items():
        raise_flag(conn, uuid, vid, ftype, detail)
    for ftype, flag_id in open_now.items():
        if ftype in DATA_INTEGRITY_TYPES and ftype not in present:
            _resolve(
                conn,
                flag_id,
                version_row["author_id"],
                f"Auto-resolved: condition absent in v{version_row['version_no']}",
            )
    return sorted(present)


def open_flags(conn: sqlite3.Connection, entry_uuid: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM v_open_flags WHERE entry_uuid=? ORDER BY created_at",
        (entry_uuid,),
    ).fetchall()


def open_integrity_flags(conn: sqlite3.Connection, entry_uuid: str) -> list[sqlite3.Row]:
    qmarks = ",".join("?" for _ in DATA_INTEGRITY_TYPES)
    return conn.execute(
        f"SELECT * FROM v_open_flags WHERE entry_uuid=? AND flag_type IN ({qmarks})",
        (entry_uuid, *DATA_INTEGRITY_TYPES),
    ).fetchall()
