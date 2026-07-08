"""Offline capture sync API.

The device outbox is a transmission buffer, never an authority; server state
wins for all reads. Sync is an append-only POST of v1 versions. Dedup vs
conflict is discriminated by a SELECT pre-check inside the write transaction
(design decision 3) — never by interpreting constraint errors:

    row exists + identical payload  -> 'duplicate' (idempotent no-op)
    row exists + different payload  -> sync_conflict row + 'conflict'
    row absent                      -> INSERT (validation/trigger failure -> 'rejected')

A future-dated entry is ACCEPTED and flagged future_dated — a wrong device
clock must never strand or lose an entry (Gate 2 binding #3).
"""
from __future__ import annotations

import json
import re
import sqlite3

from flask import Blueprint, g, jsonify, request

from . import flags as flag_mod
from .db import audit, get_db, utcnow
from .lifecycle import TransitionError, insert_version, payload_fields

bp = Blueprint("sync", __name__, url_prefix="/api")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MAX_NOTE_LEN = 2000
MAX_BATCH = 200


def validate_entry_fields(conn, data: dict) -> str | None:
    """Returns a rejection reason or None. Future dates are NOT rejected."""
    from datetime import date

    wd = data.get("work_date") or ""
    if not _DATE_RE.match(wd):
        return "work_date must be YYYY-MM-DD"
    try:
        date.fromisoformat(wd)
    except ValueError:
        return "work_date is not a real date"
    for f in ("start_time", "end_time"):
        if not _TIME_RE.match(data.get(f) or ""):
            return f"{f} must be HH:MM (24h)"
    br = data.get("break_minutes")
    if not isinstance(br, int) or isinstance(br, bool) or not (0 <= br <= 1440):
        return "break_minutes must be an integer 0-1440"
    note = data.get("note")
    if note is not None and (not isinstance(note, str) or len(note) > MAX_NOTE_LEN):
        return f"note must be a string of at most {MAX_NOTE_LEN} chars"
    job = conn.execute("SELECT id FROM job WHERE id=?", (data.get("job_id"),)).fetchone()
    if job is None:
        return "unknown job"
    return None


def _worker_required():
    if g.user is None:
        return jsonify({"error": "login required"}), 401
    if not g.user["is_worker"]:
        return jsonify({"error": "worker role required"}), 403
    return None


@bp.get("/jobs")
def jobs():
    err = _worker_required()
    if err:
        return err
    conn = get_db()
    rows = conn.execute(
        "SELECT id, code, name FROM job WHERE status='active' ORDER BY code"
    ).fetchall()
    return jsonify({"as_of": utcnow(), "jobs": [dict(r) for r in rows]})


@bp.get("/sync/state")
def sync_state():
    """Server's (uuid -> latest version_no, status) for the calling worker —
    lets a device reconcile its outbox after iOS storage eviction."""
    err = _worker_required()
    if err:
        return err
    conn = get_db()
    rows = conn.execute(
        "SELECT entry_uuid, version_no, status FROM v_time_entry_current WHERE person_id=?",
        (g.user["id"],),
    ).fetchall()
    return jsonify(
        {
            "as_of": utcnow(),
            "entries": {
                r["entry_uuid"]: {"version_no": r["version_no"], "status": r["status"]}
                for r in rows
            },
        }
    )


@bp.post("/sync")
def sync():
    err = _worker_required()
    if err:
        return err
    conn = get_db()
    body = request.get_json(silent=True) or {}
    device_id = (body.get("device_id") or "")[:64] or None
    client_info = (body.get("client_info") or "")[:200] or None
    items = body.get("entries")
    if not isinstance(items, list) or len(items) > MAX_BATCH:
        return jsonify({"error": f"entries must be a list of at most {MAX_BATCH}"}), 400

    results = []
    counts = {"received": len(items), "accepted": 0, "duplicate": 0, "conflict": 0, "rejected": 0}

    for item in items:
        results.append(_sync_one(conn, item, device_id, counts))

    conn.execute(
        "INSERT INTO sync_log (person_id, device_id, synced_at, received_count,"
        " accepted_count, duplicate_count, conflict_count, rejected_count, client_info)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (
            g.user["id"],
            device_id,
            utcnow(),
            counts["received"],
            counts["accepted"],
            counts["duplicate"],
            counts["conflict"],
            counts["rejected"],
            client_info,
        ),
    )
    conn.commit()
    return jsonify({"results": results, "counts": counts})


def _sync_one(conn: sqlite3.Connection, item, device_id: str | None, counts: dict) -> dict:
    if not isinstance(item, dict):
        counts["rejected"] += 1
        return {"result": "rejected", "reason": "entry must be an object"}
    uuid = item.get("uuid") or ""
    out = {"uuid": uuid}
    if not _UUID_RE.match(uuid):
        counts["rejected"] += 1
        return out | {"result": "rejected", "reason": "uuid must be a lowercase UUIDv4"}

    version_no = item.get("version_no")
    if version_no != 1:
        # The capture module edits unsynced drafts in place (design decision
        # 12), so a device only ever produces v1. Anything else is rejected —
        # visibly, never silently.
        counts["rejected"] += 1
        return out | {"result": "rejected", "reason": "sync accepts version_no 1 only"}

    existing = conn.execute(
        "SELECT * FROM time_entry_version WHERE entry_uuid=? AND version_no=1", (uuid,)
    ).fetchone()

    incoming_payload = {
        "job_id": item.get("job_id"),
        "work_date": item.get("work_date"),
        "start_time": item.get("start_time"),
        "end_time": item.get("end_time"),
        "break_minutes": item.get("break_minutes"),
        "note": item.get("note") or None,
        "status": "draft",
    }

    if existing is not None:
        if existing["person_id"] != g.user["id"]:
            counts["rejected"] += 1
            return out | {"result": "rejected", "reason": "uuid belongs to another person"}
        current = conn.execute(
            "SELECT version_no, status FROM v_time_entry_current WHERE entry_uuid=?", (uuid,)
        ).fetchone()
        if payload_fields(existing) == incoming_payload:
            counts["duplicate"] += 1
            return out | {
                "result": "duplicate",
                "current_version_no": current["version_no"],
                "status": current["status"],
            }
        conn.execute(
            "INSERT INTO sync_conflict (entry_uuid, version_no, existing_version_id,"
            " conflicting_payload, device_id, person_id, received_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                uuid,
                1,
                existing["id"],
                json.dumps(item, sort_keys=True),
                device_id,
                g.user["id"],
                utcnow(),
            ),
        )
        audit(conn, g.user["id"], "sync.conflict", "time_entry", uuid,
              None, {"device_id": device_id})
        counts["conflict"] += 1
        return out | {
            "result": "conflict",
            "reason": "same (uuid, version) already stored with different payload;"
            " server state wins and the difference is surfaced to admin",
            "current_version_no": current["version_no"],
            "status": current["status"],
        }

    reason = validate_entry_fields(conn, incoming_payload)
    if reason:
        counts["rejected"] += 1
        return out | {"result": "rejected", "reason": reason}

    dca = item.get("device_created_at")
    if dca is not None and not isinstance(dca, str):
        dca = None
    try:
        vid = insert_version(
            conn,
            entry_uuid=uuid,
            person_id=g.user["id"],
            job_id=incoming_payload["job_id"],
            work_date=incoming_payload["work_date"],
            start_time=incoming_payload["start_time"],
            end_time=incoming_payload["end_time"],
            break_minutes=incoming_payload["break_minutes"],
            note=incoming_payload["note"],
            status="draft",
            author=g.user,
            change_reason=None,
            device_created_at=dca[:64] if dca else None,
        )
    except (TransitionError, sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
        counts["rejected"] += 1
        return out | {"result": "rejected", "reason": str(exc)}

    row = conn.execute("SELECT * FROM time_entry_version WHERE id=?", (vid,)).fetchone()
    raised = flag_mod.recompute_for_version(conn, row)
    counts["accepted"] += 1
    return out | {"result": "accepted", "current_version_no": 1, "status": "draft",
                  "flags": raised}
