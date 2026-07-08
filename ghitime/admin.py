"""Admin: dashboard, approval queue, flags, conflicts, corrections, people,
jobs, config + OT policy, audit viewer, sync status. Money is admin-only.
"""
from __future__ import annotations

import sqlite3

from argon2 import PasswordHasher
from flask import (
    Blueprint,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from . import figures, flags as flag_mod
from .auth import admin_required, revoke_sessions
from .db import audit, config_get, config_set, get_db, today_local, utcnow
from .entries import _record_csv_response, record_rows
from .lifecycle import TransitionError, current_version, insert_version, versions

bp = Blueprint("admin", __name__, url_prefix="/admin")
hasher = PasswordHasher()


@bp.get("")
@admin_required
def dashboard():
    conn = get_db()
    unsubmitted = conn.execute(
        "SELECT p.display_name, COUNT(*) AS n FROM v_time_entry_current c"
        " JOIN person p ON p.id=c.person_id WHERE c.status='draft'"
        " GROUP BY c.person_id ORDER BY p.display_name"
    ).fetchall()
    open_flags = conn.execute(
        "SELECT flag_type, COUNT(*) AS n FROM v_open_flags"
        " WHERE flag_type IN ({}) GROUP BY flag_type".format(
            ",".join("?" for _ in flag_mod.DATA_INTEGRITY_TYPES)
        ),
        flag_mod.DATA_INTEGRITY_TYPES,
    ).fetchall()
    open_conflicts = conn.execute(
        "SELECT COUNT(*) AS n FROM sync_conflict WHERE resolved_at IS NULL"
    ).fetchone()["n"]
    policy = figures.ot_policy_in_force(conn, today_local().isoformat())
    workweek = config_get(conn, "workweek_start_dow")
    last_backup = conn.execute(
        "SELECT at FROM ops_event WHERE kind='backup' AND ok=1 ORDER BY at DESC LIMIT 1"
    ).fetchone()
    last_verify = conn.execute(
        "SELECT at, ok FROM ops_event WHERE kind='restore_verify' ORDER BY at DESC LIMIT 1"
    ).fetchone()
    sync_status = conn.execute(
        "SELECT p.display_name, s.device_id, MAX(s.synced_at) AS last_sync"
        " FROM sync_log s JOIN person p ON p.id=s.person_id"
        " GROUP BY s.person_id, s.device_id ORDER BY p.display_name"
    ).fetchall()
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM v_time_entry_current WHERE status='submitted'"
    ).fetchone()["n"]
    return render_template(
        "admin/dashboard.html", unsubmitted=unsubmitted, open_flags=open_flags,
        open_conflicts=open_conflicts, policy=policy, workweek=workweek,
        last_backup=last_backup, last_verify=last_verify, sync_status=sync_status,
        pending=pending,
    )


# --- approval queue ----------------------------------------------------------

@bp.get("/queue")
@admin_required
def queue():
    conn = get_db()
    # submitted entries grouped by the submission that attested them
    rows = conn.execute(
        "SELECT c.*, j.code AS job_code, p.display_name AS person_name,"
        "       s.id AS submission_id, s.submitted_at"
        " FROM v_time_entry_minutes c"
        " JOIN job j ON j.id=c.job_id JOIN person p ON p.id=c.person_id"
        " LEFT JOIN submission_entry se ON se.time_entry_version_id ="
        "   (SELECT id FROM time_entry_version tv WHERE tv.entry_uuid=c.entry_uuid"
        "     AND tv.status='submitted' ORDER BY tv.version_no DESC LIMIT 1)"
        " LEFT JOIN submission s ON s.id=se.submission_id"
        " WHERE c.status='submitted'"
        " ORDER BY s.submitted_at, p.display_name, c.work_date",
    ).fetchall()
    groups: dict = {}
    for r in rows:
        key = (r["submission_id"], r["person_name"], r["submitted_at"])
        groups.setdefault(key, []).append(r)
    flag_map = {}
    for r in rows:
        fl = flag_mod.open_integrity_flags(conn, r["entry_uuid"])
        if fl:
            flag_map[r["entry_uuid"]] = [f["flag_type"] for f in fl]
    diffs = {r["entry_uuid"]: _version_diff(conn, r["entry_uuid"]) for r in rows}
    return render_template("admin/queue.html", groups=groups, flag_map=flag_map,
                           diffs=diffs, hours=figures.minutes_to_hours)


def _version_diff(conn, entry_uuid) -> list[dict]:
    """Inline diffs between consecutive versions (changed fields only)."""
    hist = versions(conn, entry_uuid)
    fields = ("job_id", "work_date", "start_time", "end_time", "break_minutes", "note", "status")
    out = []
    for prev, cur in zip(hist, hist[1:]):
        changed = {
            f: (prev[f], cur[f]) for f in fields if prev[f] != cur[f]
        }
        out.append({"version_no": cur["version_no"], "author_id": cur["author_id"],
                    "change_reason": cur["change_reason"], "changed": changed})
    return out


def _approve_one(conn, entry_uuid: str, approval_id: int, flags_ack_reason: str | None):
    cur = current_version(conn, entry_uuid)
    if cur is None or cur["status"] != "submitted":
        raise TransitionError(f"{entry_uuid}: only submitted entries can be approved")
    open_fl = flag_mod.open_integrity_flags(conn, entry_uuid)
    if open_fl and not (flags_ack_reason or "").strip():
        raise TransitionError(
            f"{entry_uuid}: has open flags ({', '.join(f['flag_type'] for f in open_fl)});"
            " a stated reason is required to approve"
        )
    vid = insert_version(
        conn, entry_uuid=entry_uuid, person_id=cur["person_id"], job_id=cur["job_id"],
        work_date=cur["work_date"], start_time=cur["start_time"], end_time=cur["end_time"],
        break_minutes=cur["break_minutes"], note=cur["note"], status="approved",
        author=g.user, change_reason="Approved",
    )
    conn.execute(
        "INSERT INTO approval_entry (approval_id, entry_uuid, acted_on_version_id,"
        " resulting_version_id) VALUES (?,?,?,?)",
        (approval_id, entry_uuid, cur["id"], vid),
    )
    self_approval = cur["person_id"] == g.user["id"]
    if self_approval:
        flag_mod.raise_flag(conn, entry_uuid, vid, "self_approval")
    audit(conn, g.user["id"], "entry.approve", "time_entry", entry_uuid,
          flags_ack_reason or None,
          {"self_approval": self_approval,
           "acked_flags": [f["flag_type"] for f in open_fl]})
    vrow = conn.execute("SELECT * FROM time_entry_version WHERE id=?", (vid,)).fetchone()
    flag_mod.recompute_for_version(conn, vrow)
    return self_approval


@bp.post("/approve")
@admin_required
def approve():
    conn = get_db()
    uuids = request.form.getlist("entry_uuid")
    submission_id = request.form.get("submission_id")
    flags_ack_reason = (request.form.get("flags_ack_reason") or "").strip() or None
    if submission_id and not uuids:
        uuids = [
            r["entry_uuid"]
            for r in conn.execute(
                "SELECT DISTINCT v.entry_uuid FROM submission_entry se"
                " JOIN time_entry_version v ON v.id=se.time_entry_version_id"
                " WHERE se.submission_id=?",
                (submission_id,),
            )
            if (cv := current_version(conn, r["entry_uuid"])) and cv["status"] == "submitted"
        ]
    if not uuids:
        flash("Nothing selected to approve.")
        return redirect(url_for("admin.queue"))
    # the approval row is append-only, so self-approval must be known BEFORE
    # the insert — it can never be patched on afterwards
    qmarks = ",".join("?" for _ in uuids)
    any_self = conn.execute(
        f"SELECT COUNT(*) AS n FROM v_time_entry_current"
        f" WHERE entry_uuid IN ({qmarks}) AND person_id=?",
        (*uuids, g.user["id"]),
    ).fetchone()["n"] > 0
    try:
        cur = conn.execute(
            "INSERT INTO approval (approver_id, submission_id, action, flags_ack_reason,"
            " is_self_approval, created_at) VALUES (?,?,?,?,?,?)",
            (g.user["id"], submission_id or None, "approve", flags_ack_reason,
             1 if any_self else 0, utcnow()),
        )
        approval_id = cur.lastrowid
        for u in uuids:
            _approve_one(conn, u, approval_id, flags_ack_reason)
        conn.commit()
        flash(f"Approved {len(uuids)} entr{'y' if len(uuids) == 1 else 'ies'}.")
    except (TransitionError, sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
        conn.rollback()
        flash(f"Nothing approved: {exc}")
    return redirect(url_for("admin.queue"))


@bp.post("/reject")
@admin_required
def reject():
    conn = get_db()
    uuids = request.form.getlist("entry_uuid")
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("Rejection requires a reason.")
        return redirect(url_for("admin.queue"))
    if not uuids:
        flash("Nothing selected to reject.")
        return redirect(url_for("admin.queue"))
    try:
        cur = conn.execute(
            "INSERT INTO approval (approver_id, action, reason, created_at)"
            " VALUES (?,?,?,?)",
            (g.user["id"], "reject", reason, utcnow()),
        )
        approval_id = cur.lastrowid
        for u in uuids:
            cv = current_version(conn, u)
            if cv is None or cv["status"] != "submitted":
                raise TransitionError(f"{u}: only submitted entries can be rejected")
            vid = insert_version(
                conn, entry_uuid=u, person_id=cv["person_id"], job_id=cv["job_id"],
                work_date=cv["work_date"], start_time=cv["start_time"],
                end_time=cv["end_time"], break_minutes=cv["break_minutes"],
                note=cv["note"], status="draft", author=g.user,
                change_reason=f"Rejected: {reason}",
            )
            conn.execute(
                "INSERT INTO approval_entry (approval_id, entry_uuid,"
                " acted_on_version_id, resulting_version_id) VALUES (?,?,?,?)",
                (approval_id, u, cv["id"], vid),
            )
            audit(conn, g.user["id"], "entry.reject", "time_entry", u, reason)
            vrow = conn.execute("SELECT * FROM time_entry_version WHERE id=?", (vid,)).fetchone()
            flag_mod.recompute_for_version(conn, vrow)
        conn.commit()
        flash(f"Rejected {len(uuids)} entr{'y' if len(uuids) == 1 else 'ies'} back to draft.")
    except (TransitionError, sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
        conn.rollback()
        flash(f"Nothing rejected: {exc}")
    return redirect(url_for("admin.queue"))


@bp.post("/entries/<entry_uuid>/correct")
@admin_required
def correct(entry_uuid):
    """Post-approval correction: admin-only, new version, reason required,
    audit-logged, badged on printout and every export containing the entry."""
    conn = get_db()
    cv = current_version(conn, entry_uuid)
    if cv is None:
        abort(404)
    reason = (request.form.get("change_reason") or "").strip()
    if not reason:
        flash("A stated reason is required for a post-approval correction.")
        return redirect(url_for("admin.queue"))
    if cv["status"] != "approved":
        flash("Post-approval correction applies to approved entries only.")
        return redirect(url_for("admin.queue"))
    from .sync import validate_entry_fields

    try:
        break_minutes = int(request.form.get("break_minutes", ""))
    except ValueError:
        flash("break_minutes must be a whole number.")
        return redirect(url_for("admin.queue"))
    data = {
        "job_id": int(request.form["job_id"]) if request.form.get("job_id", "").isdigit() else None,
        "work_date": (request.form.get("work_date") or "").strip(),
        "start_time": (request.form.get("start_time") or "").strip()[:5],
        "end_time": (request.form.get("end_time") or "").strip()[:5],
        "break_minutes": break_minutes,
        "note": (request.form.get("note") or "").strip() or None,
        "status": "approved",
    }
    err = validate_entry_fields(conn, data)
    if err:
        flash(err)
        return redirect(url_for("admin.queue"))
    vid = insert_version(
        conn, entry_uuid=entry_uuid, person_id=cv["person_id"], status="approved",
        author=g.user, change_reason=reason,
        **{k: data[k] for k in ("job_id", "work_date", "start_time", "end_time",
                                "break_minutes", "note")},
    )
    flag_mod.raise_flag(conn, entry_uuid, vid, "post_approval_correction",
                        {"reason": reason})
    audit(conn, g.user["id"], "entry.correct_post_approval", "time_entry",
          entry_uuid, reason)
    vrow = conn.execute("SELECT * FROM time_entry_version WHERE id=?", (vid,)).fetchone()
    flag_mod.recompute_for_version(conn, vrow)
    conn.commit()
    flash("Correction recorded; the entry now carries a correction badge.")
    return redirect(url_for("admin.queue"))


@bp.post("/entries/<entry_uuid>/void")
@admin_required
def admin_void(entry_uuid):
    conn = get_db()
    cv = current_version(conn, entry_uuid)
    if cv is None:
        abort(404)
    reason = (request.form.get("change_reason") or "").strip()
    if not reason:
        flash("A reason is required to void an entry.")
        return redirect(url_for("admin.queue"))
    try:
        vid = insert_version(
            conn, entry_uuid=entry_uuid, person_id=cv["person_id"], job_id=cv["job_id"],
            work_date=cv["work_date"], start_time=cv["start_time"], end_time=cv["end_time"],
            break_minutes=cv["break_minutes"], note=cv["note"], status="void",
            author=g.user, change_reason=reason,
        )
    except TransitionError as exc:
        flash(str(exc))
        return redirect(url_for("admin.queue"))
    audit(conn, g.user["id"], "entry.void", "time_entry", entry_uuid, reason)
    vrow = conn.execute("SELECT * FROM time_entry_version WHERE id=?", (vid,)).fetchone()
    flag_mod.recompute_for_version(conn, vrow)
    conn.commit()
    flash("Entry voided.")
    return redirect(url_for("admin.queue"))


# --- flags & conflicts -------------------------------------------------------

@bp.get("/flags")
@admin_required
def flags_queue():
    conn = get_db()
    ftype = request.args.get("type") or ""
    q = (
        "SELECT f.*, c.person_id, c.work_date, c.start_time, c.end_time, c.status,"
        " p.display_name AS person_name"
        " FROM v_open_flags f"
        " JOIN v_time_entry_current c ON c.entry_uuid=f.entry_uuid"
        " JOIN person p ON p.id=c.person_id"
        " WHERE f.flag_type IN ({})".format(
            ",".join("?" for _ in flag_mod.DATA_INTEGRITY_TYPES))
    )
    params: list = list(flag_mod.DATA_INTEGRITY_TYPES)
    if ftype:
        q += " AND f.flag_type=?"
        params.append(ftype)
    q += " ORDER BY f.created_at"
    rows = conn.execute(q, params).fetchall()
    return render_template("admin/flags.html", rows=rows, ftype=ftype,
                           types=flag_mod.DATA_INTEGRITY_TYPES)


@bp.post("/flags/<int:flag_id>/resolve")
@admin_required
def flag_resolve(flag_id):
    conn = get_db()
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("Flag resolution requires a stated reason.")
        return redirect(url_for("admin.flags_queue"))
    conn.execute(
        "UPDATE entry_flag SET resolved_at=?, resolved_by=?, resolution_reason=?"
        " WHERE id=? AND resolved_at IS NULL",
        (utcnow(), g.user["id"], reason, flag_id),
    )
    audit(conn, g.user["id"], "flag.resolve", "entry_flag", flag_id, reason)
    conn.commit()
    return redirect(url_for("admin.flags_queue"))


@bp.get("/conflicts")
@admin_required
def conflicts():
    conn = get_db()
    rows = conn.execute(
        "SELECT sc.*, p.display_name AS person_name FROM sync_conflict sc"
        " LEFT JOIN person p ON p.id=sc.person_id"
        " WHERE sc.resolved_at IS NULL ORDER BY sc.received_at"
    ).fetchall()
    return render_template("admin/conflicts.html", rows=rows)


@bp.get("/conflicts/<int:cid>")
@admin_required
def conflict_detail(cid):
    conn = get_db()
    c = conn.execute("SELECT * FROM sync_conflict WHERE id=?", (cid,)).fetchone()
    if c is None:
        abort(404)
    server_row = conn.execute(
        "SELECT * FROM time_entry_version WHERE id=?", (c["existing_version_id"],)
    ).fetchone()
    import json

    return render_template(
        "admin/conflict_detail.html", c=c, server_row=server_row,
        device_payload=json.loads(c["conflicting_payload"]),
    )


@bp.post("/conflicts/<int:cid>/resolve")
@admin_required
def conflict_resolve(cid):
    conn = get_db()
    note = (request.form.get("note") or "").strip()
    if not note:
        flash("Conflict resolution requires a note.")
        return redirect(url_for("admin.conflict_detail", cid=cid))
    conn.execute(
        "UPDATE sync_conflict SET resolved_at=?, resolved_by=?, resolution_note=?"
        " WHERE id=? AND resolved_at IS NULL",
        (utcnow(), g.user["id"], note, cid),
    )
    audit(conn, g.user["id"], "sync_conflict.resolve", "sync_conflict", cid, note)
    conn.commit()
    flash("Conflict resolved (server state wins; append a new version if the"
          " device was right).")
    return redirect(url_for("admin.conflicts"))


# --- people ------------------------------------------------------------------

@bp.get("/people")
@admin_required
def people():
    conn = get_db()
    rows = conn.execute("SELECT * FROM person ORDER BY active DESC, display_name").fetchall()
    return render_template("admin/people.html", rows=rows)


@bp.post("/people")
@admin_required
def person_create():
    conn = get_db()
    username = (request.form.get("username") or "").strip()
    display_name = (request.form.get("display_name") or "").strip()
    worker_type = request.form.get("worker_type") or "employee"
    temp_pw = request.form.get("temp_password") or ""
    if not username or not display_name or len(temp_pw) < 8:
        flash("Username, display name, and a temp password (8+ chars) are required.")
        return redirect(url_for("admin.people"))
    try:
        cur = conn.execute(
            "INSERT INTO person (username, password_hash, display_name, is_worker,"
            " is_admin, worker_type, active, must_change_pw, created_at, created_by)"
            " VALUES (?,?,?,?,?,?,1,1,?,?)",
            (
                username, hasher.hash(temp_pw), display_name,
                1 if request.form.get("is_worker") else 0,
                1 if request.form.get("is_admin") else 0,
                worker_type, utcnow(), g.user["id"],
            ),
        )
    except sqlite3.IntegrityError as exc:
        flash(str(exc))
        return redirect(url_for("admin.people"))
    audit(conn, g.user["id"], "person.create", "person", cur.lastrowid)
    conn.commit()
    flash(f"Created {display_name}. They must change the temp password at first login.")
    return redirect(url_for("admin.people"))


@bp.get("/people/<int:pid>")
@admin_required
def person_detail(pid):
    conn = get_db()
    p = conn.execute("SELECT * FROM person WHERE id=?", (pid,)).fetchone()
    if p is None:
        abort(404)
    pay_hist = conn.execute(
        "SELECT * FROM v_rate_pay_effective WHERE person_id=? ORDER BY effective_date DESC",
        (pid,),
    ).fetchall()
    bill_hist = conn.execute(
        "SELECT * FROM v_rate_bill_effective WHERE person_id=? ORDER BY effective_date DESC",
        (pid,),
    ).fetchall()
    return render_template("admin/person_detail.html", p=p, pay_hist=pay_hist,
                           bill_hist=bill_hist, cents=figures.cents_to_str)


@bp.post("/people/<int:pid>")
@admin_required
def person_update(pid):
    conn = get_db()
    p = conn.execute("SELECT * FROM person WHERE id=?", (pid,)).fetchone()
    if p is None:
        abort(404)
    conn.execute(
        "UPDATE person SET is_worker=?, is_admin=?, worker_type=?, active=? WHERE id=?",
        (
            1 if request.form.get("is_worker") else 0,
            1 if request.form.get("is_admin") else 0,
            request.form.get("worker_type") or p["worker_type"],
            1 if request.form.get("active") else 0,
            pid,
        ),
    )
    if not request.form.get("active"):
        revoke_sessions(conn, pid)
    audit(conn, g.user["id"], "person.update", "person", pid, None,
          {k: request.form.get(k) for k in ("is_worker", "is_admin", "worker_type", "active")})
    conn.commit()
    flash("Saved.")
    return redirect(url_for("admin.person_detail", pid=pid))


@bp.post("/people/<int:pid>/password")
@admin_required
def person_password(pid):
    conn = get_db()
    temp_pw = request.form.get("temp_password") or ""
    if len(temp_pw) < 8:
        flash("Temp password: 8+ characters.")
        return redirect(url_for("admin.person_detail", pid=pid))
    conn.execute(
        "UPDATE person SET password_hash=?, must_change_pw=1 WHERE id=?",
        (hasher.hash(temp_pw), pid),
    )
    revoke_sessions(conn, pid)  # lost-phone case: reset kills the session
    audit(conn, g.user["id"], "person.password_reset", "person", pid)
    conn.commit()
    flash("Password reset; all their sessions are signed out.")
    return redirect(url_for("admin.person_detail", pid=pid))


def _append_rate(table: str, pid: int):
    conn = get_db()
    try:
        cents = int(round(float(request.form.get("rate", "")) * 100))
    except ValueError:
        flash("Rate must be a number like 28.50.")
        return redirect(url_for("admin.person_detail", pid=pid))
    eff = (request.form.get("effective_date") or "").strip()
    if cents < 0 or not eff:
        flash("Rate and effective date are required.")
        return redirect(url_for("admin.person_detail", pid=pid))
    conn.execute(
        f"INSERT INTO {table} (person_id, hourly_rate_cents, effective_date,"
        " entered_by, entered_at) VALUES (?,?,?,?,?)",
        (pid, cents, eff, g.user["id"], utcnow()),
    )
    audit(conn, g.user["id"], f"{table}.append", "person", pid, None,
          {"cents": cents, "effective_date": eff})
    conn.commit()
    flash("Rate added (history preserved — a raise never rewrites the past).")
    return redirect(url_for("admin.person_detail", pid=pid))


@bp.post("/people/<int:pid>/rate-pay")
@admin_required
def rate_pay(pid):
    return _append_rate("rate_pay", pid)


@bp.post("/people/<int:pid>/rate-bill")
@admin_required
def rate_bill(pid):
    return _append_rate("rate_bill", pid)


@bp.get("/people/<int:pid>/record")
@admin_required
def person_record(pid):
    conn = get_db()
    p = conn.execute("SELECT * FROM person WHERE id=?", (pid,)).fetchone()
    if p is None:
        abort(404)
    badge_rows = conn.execute(
        "SELECT f.entry_uuid, f.flag_type, f.created_at FROM entry_flag f"
        " JOIN time_entry_version v ON v.id=f.trigger_version_id"
        " WHERE v.person_id=? AND f.flag_type IN ('self_approval','post_approval_correction')",
        (pid,),
    ).fetchall()
    return render_template("record.html", person=p, rows=record_rows(conn, pid),
                           badges=badge_rows, generated_at=utcnow())


@bp.get("/people/<int:pid>/record.csv")
@admin_required
def person_record_csv(pid):
    conn = get_db()
    p = conn.execute("SELECT * FROM person WHERE id=?", (pid,)).fetchone()
    if p is None:
        abort(404)
    return _record_csv_response(conn, p)


# --- jobs --------------------------------------------------------------------

@bp.get("/jobs")
@admin_required
def jobs():
    conn = get_db()
    rows = conn.execute("SELECT * FROM job ORDER BY status, code").fetchall()
    return render_template("admin/jobs.html", rows=rows)


@bp.post("/jobs")
@admin_required
def job_create():
    conn = get_db()
    code = (request.form.get("code") or "").strip()
    name = (request.form.get("name") or "").strip()
    if not code or not name:
        flash("Job code and name are required.")
        return redirect(url_for("admin.jobs"))
    try:
        cur = conn.execute(
            "INSERT INTO job (code, name, status, created_at, created_by)"
            " VALUES (?,?, 'active', ?, ?)",
            (code, name, utcnow(), g.user["id"]),
        )
    except sqlite3.IntegrityError as exc:
        flash(str(exc))
        return redirect(url_for("admin.jobs"))
    audit(conn, g.user["id"], "job.create", "job", cur.lastrowid)
    conn.commit()
    return redirect(url_for("admin.jobs"))


@bp.post("/jobs/<int:jid>/complete")
@admin_required
def job_complete(jid):
    conn = get_db()
    conn.execute("UPDATE job SET status='completed' WHERE id=?", (jid,))
    audit(conn, g.user["id"], "job.complete", "job", jid)
    conn.commit()
    return redirect(url_for("admin.jobs"))


@bp.post("/jobs/<int:jid>/reactivate")
@admin_required
def job_reactivate(jid):
    conn = get_db()
    conn.execute("UPDATE job SET status='active' WHERE id=?", (jid,))
    audit(conn, g.user["id"], "job.reactivate", "job", jid)
    conn.commit()
    return redirect(url_for("admin.jobs"))


# --- config + OT policy ------------------------------------------------------

@bp.get("/config")
@admin_required
def config_page():
    conn = get_db()
    cfg = {r["key"]: r for r in conn.execute("SELECT * FROM config")}
    policy_hist = conn.execute(
        "SELECT * FROM v_ot_policy_effective ORDER BY effective_date DESC"
    ).fetchall()
    return render_template("admin/config.html", cfg=cfg, policy_hist=policy_hist)


@bp.post("/config")
@admin_required
def config_post():
    conn = get_db()
    preview = "1" if request.form.get("ot_pay_preview_enabled") else "0"
    config_set(conn, "ot_pay_preview_enabled", preview, g.user["id"])
    ws = (request.form.get("workweek_start_dow") or "").strip()
    if ws == "":
        config_set(conn, "workweek_start_dow", None, g.user["id"])
    elif ws.isdigit() and 0 <= int(ws) <= 6:
        config_set(conn, "workweek_start_dow", ws, g.user["id"])
    else:
        flash("Workweek start must be 0 (Monday) through 6 (Sunday), or blank for unset.")
    conn.commit()
    flash("Settings saved (audit-logged).")
    return redirect(url_for("admin.config_page"))


@bp.post("/ot-policy")
@admin_required
def ot_policy_append():
    conn = get_db()
    try:
        threshold = float(request.form.get("threshold_hours", ""))
        multiplier = float(request.form.get("multiplier", ""))
    except ValueError:
        flash("Threshold and multiplier are both required (no partial policy rows).")
        return redirect(url_for("admin.config_page"))
    eff = (request.form.get("effective_date") or "").strip()
    try:
        conn.execute(
            "INSERT INTO ot_policy (threshold_hours, multiplier, effective_date,"
            " entered_by, entered_at) VALUES (?,?,?,?,?)",
            (threshold, multiplier, eff, g.user["id"], utcnow()),
        )
    except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
        flash(str(exc))
        return redirect(url_for("admin.config_page"))
    audit(conn, g.user["id"], "ot_policy.append", "ot_policy", eff, None,
          {"threshold_hours": threshold, "multiplier": multiplier})
    conn.commit()
    flash("OT policy row appended — history is never rewritten.")
    return redirect(url_for("admin.config_page"))


# --- audit + sync status -----------------------------------------------------

@bp.get("/audit")
@admin_required
def audit_view():
    conn = get_db()
    q = "SELECT a.*, p.display_name AS actor FROM audit_log a" \
        " LEFT JOIN person p ON p.id=a.actor_id WHERE 1=1"
    params: list = []
    for field, col in (("actor", "p.display_name"), ("action", "a.action"),
                       ("entity", "a.entity_type")):
        val = request.args.get(field)
        if val:
            q += f" AND {col} LIKE ?"
            params.append(f"%{val}%")
    if request.args.get("from"):
        q += " AND a.at >= ?"
        params.append(request.args["from"])
    if request.args.get("to"):
        q += " AND a.at <= ?"
        params.append(request.args["to"] + "T23:59:59.999999Z")
    q += " ORDER BY a.at DESC LIMIT 500"
    rows = conn.execute(q, params).fetchall()
    return render_template("admin/audit.html", rows=rows, args=request.args)


@bp.get("/sync-status")
@admin_required
def sync_status():
    conn = get_db()
    rows = conn.execute(
        "SELECT p.display_name, s.device_id, MAX(s.synced_at) AS last_sync,"
        " SUM(s.accepted_count) AS accepted, SUM(s.duplicate_count) AS duplicates,"
        " SUM(s.conflict_count) AS conflicts, SUM(s.rejected_count) AS rejected"
        " FROM sync_log s JOIN person p ON p.id=s.person_id"
        " GROUP BY s.person_id, s.device_id ORDER BY last_sync DESC"
    ).fetchall()
    return render_template("admin/sync_status.html", rows=rows)
