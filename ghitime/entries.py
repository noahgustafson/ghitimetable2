"""Worker-facing pages: home, entry list/detail/edit, one-tap submit
attestation, my-record printout (+CSV), my rate. A worker sees ONLY their own
entries and pay rate; bill rates never render on any page in this module.
"""
from __future__ import annotations

import csv
import io
import sqlite3
from datetime import timedelta

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from . import figures, flags as flag_mod
from .auth import login_required, worker_required
from .db import audit, config_get, get_db, today_local, utcnow
from .lifecycle import (
    TransitionError,
    current_version,
    insert_version,
    new_uuid,
    versions,
)
from .sync import validate_entry_fields

bp = Blueprint("entries", __name__)


def _own_entry_or_404(conn: sqlite3.Connection, entry_uuid: str) -> sqlite3.Row:
    cur = current_version(conn, entry_uuid)
    if cur is None:
        abort(404)
    if cur["person_id"] != g.user["id"] and not g.user["is_admin"]:
        abort(404)  # 404, not 403: don't confirm other workers' uuids exist
    return cur


def _week_context(conn: sqlite3.Connection, person_id: int) -> dict:
    ws_conf = config_get(conn, "workweek_start_dow")
    display_only = ws_conf is None
    start_dow = figures.DISPLAY_WEEK_START if display_only else int(ws_conf)
    wk_start = figures.week_start(today_local(), start_dow)
    total_min, blanks = figures.weekly_minutes(conn, person_id, wk_start)
    rate = figures.rate_cents_as_of(conn, "rate_pay", person_id, today_local().isoformat())
    gross = figures.gross_preview_cents(total_min, rate)
    policy = None if display_only else figures.ot_policy_in_force(conn, wk_start.isoformat())
    ot = None if policy is None else figures.ot_hours_past_threshold(total_min, policy)
    return {
        "week_start": wk_start,
        "week_end": wk_start + timedelta(days=6),
        "week_display_only": display_only,
        "week_minutes": total_min,
        "week_hours": figures.minutes_to_hours(total_min),
        "week_blank_count": blanks,
        "rate_cents": rate,
        "gross_preview_cents": gross,
        "ot_policy": policy,
        "ot_hours": ot,
        "cents": figures.cents_to_str,
    }


@bp.get("/")
@login_required
def home():
    conn = get_db()
    if not g.user["is_worker"]:
        return redirect(url_for("admin.dashboard"))
    unsubmitted = conn.execute(
        "SELECT COUNT(*) AS n FROM v_time_entry_current"
        " WHERE person_id=? AND status='draft' AND work_date<=?",
        (g.user["id"], today_local().isoformat()),
    ).fetchone()["n"]
    open_flag_rows = conn.execute(
        "SELECT f.* FROM v_open_flags f JOIN v_time_entry_current c ON c.entry_uuid=f.entry_uuid"
        " WHERE c.person_id=? ORDER BY f.created_at DESC",
        (g.user["id"],),
    ).fetchall()
    return render_template(
        "home_worker.html",
        unsubmitted=unsubmitted,
        open_flags=open_flag_rows,
        **_week_context(conn, g.user["id"]),
    )


@bp.get("/entries")
@worker_required
def entry_list():
    conn = get_db()
    today = today_local()
    dfrom = request.args.get("from") or (today - timedelta(days=14)).isoformat()
    dto = request.args.get("to") or today.isoformat()
    status = request.args.get("status") or ""
    q = (
        "SELECT m.*, j.code AS job_code FROM v_time_entry_minutes m"
        " JOIN job j ON j.id=m.job_id"
        " WHERE m.person_id=? AND m.work_date>=? AND m.work_date<=?"
    )
    params: list = [g.user["id"], dfrom, dto]
    if status:
        q += " AND m.status=?"
        params.append(status)
    q += " ORDER BY m.work_date DESC, m.start_time"
    rows = conn.execute(q, params).fetchall()
    flag_map = _open_flag_map(conn, [r["entry_uuid"] for r in rows])
    return render_template(
        "entry_list.html", rows=rows, dfrom=dfrom, dto=dto, status=status,
        flag_map=flag_map, hours=figures.minutes_to_hours,
    )


def _open_flag_map(conn, uuids: list[str]) -> dict[str, list[str]]:
    if not uuids:
        return {}
    qmarks = ",".join("?" for _ in uuids)
    out: dict[str, list[str]] = {}
    for r in conn.execute(
        f"SELECT entry_uuid, flag_type FROM v_open_flags WHERE entry_uuid IN ({qmarks})",
        uuids,
    ):
        out.setdefault(r["entry_uuid"], []).append(r["flag_type"])
    return out


def _form_payload(conn) -> tuple[dict | None, str | None]:
    try:
        break_minutes = int(request.form.get("break_minutes", ""))
    except ValueError:
        return None, "break_minutes must be a whole number (0 is fine, blank is not)"
    data = {
        "job_id": int(request.form["job_id"]) if request.form.get("job_id", "").isdigit() else None,
        "work_date": (request.form.get("work_date") or "").strip(),
        "start_time": (request.form.get("start_time") or "").strip()[:5],
        "end_time": (request.form.get("end_time") or "").strip()[:5],
        "break_minutes": break_minutes,
        "note": (request.form.get("note") or "").strip() or None,
        "status": "draft",
    }
    reason = validate_entry_fields(conn, data)
    if reason is None and data["work_date"] > today_local().isoformat():
        # online form blocks future dates outright (offline capture blocks
        # client-side; sync accepts+flags because a device clock may be wrong,
        # but there is no wrong-clock excuse when talking to the server live)
        reason = "future dates are not allowed"
    return (None, reason) if reason else (data, None)


@bp.get("/entries/new")
@worker_required
def entry_new():
    conn = get_db()
    jobs = conn.execute("SELECT * FROM job WHERE status='active' ORDER BY code").fetchall()
    return render_template("entry_form.html", jobs=jobs, entry=None,
                           today=today_local().isoformat())


@bp.post("/entries")
@worker_required
def entry_create():
    conn = get_db()
    data, err = _form_payload(conn)
    if err:
        flash(err)
        return redirect(url_for("entries.entry_new"))
    uuid = new_uuid()
    vid = insert_version(
        conn, entry_uuid=uuid, person_id=g.user["id"], status="draft",
        author=g.user, change_reason=None,
        **{k: data[k] for k in ("job_id", "work_date", "start_time", "end_time",
                                "break_minutes", "note")},
    )
    row = conn.execute("SELECT * FROM time_entry_version WHERE id=?", (vid,)).fetchone()
    flag_mod.recompute_for_version(conn, row)
    conn.commit()
    return redirect(url_for("entries.entry_detail", entry_uuid=uuid))


@bp.get("/entries/<entry_uuid>")
@worker_required
def entry_detail(entry_uuid):
    conn = get_db()
    cur = _own_entry_or_404(conn, entry_uuid)
    hist = versions(conn, entry_uuid)
    approvals = conn.execute(
        "SELECT a.*, ae.acted_on_version_id, ae.resulting_version_id, p.display_name AS approver"
        " FROM approval_entry ae JOIN approval a ON a.id=ae.approval_id"
        " JOIN person p ON p.id=a.approver_id WHERE ae.entry_uuid=? ORDER BY a.created_at",
        (entry_uuid,),
    ).fetchall()
    all_flags = conn.execute(
        "SELECT * FROM entry_flag WHERE entry_uuid=? ORDER BY created_at", (entry_uuid,)
    ).fetchall()
    jobs = conn.execute("SELECT * FROM job ORDER BY code").fetchall()
    editable = cur["status"] in ("draft", "submitted") and cur["person_id"] == g.user["id"]
    return render_template(
        "entry_detail.html", cur=cur, hist=hist, approvals=approvals,
        all_flags=all_flags, jobs=jobs, editable=editable,
        today=today_local().isoformat(),
    )


@bp.post("/entries/<entry_uuid>/edit")
@worker_required
def entry_edit(entry_uuid):
    conn = get_db()
    cur = _own_entry_or_404(conn, entry_uuid)
    reason = (request.form.get("change_reason") or "").strip()
    if not reason:
        flash("A reason is required for every edit.")
        return redirect(url_for("entries.entry_detail", entry_uuid=entry_uuid))
    data, err = _form_payload(conn)
    if err:
        flash(err)
        return redirect(url_for("entries.entry_detail", entry_uuid=entry_uuid))
    try:
        vid = insert_version(
            conn, entry_uuid=entry_uuid, person_id=cur["person_id"],
            status=cur["status"], author=g.user, change_reason=reason,
            **{k: data[k] for k in ("job_id", "work_date", "start_time", "end_time",
                                    "break_minutes", "note")},
        )
    except TransitionError as exc:
        flash(str(exc))
        return redirect(url_for("entries.entry_detail", entry_uuid=entry_uuid))
    row = conn.execute("SELECT * FROM time_entry_version WHERE id=?", (vid,)).fetchone()
    flag_mod.recompute_for_version(conn, row)
    conn.commit()
    return redirect(url_for("entries.entry_detail", entry_uuid=entry_uuid))


@bp.post("/entries/<entry_uuid>/void")
@worker_required
def entry_void(entry_uuid):
    conn = get_db()
    cur = _own_entry_or_404(conn, entry_uuid)
    reason = (request.form.get("change_reason") or "").strip()
    if not reason:
        flash("A reason is required to void an entry.")
        return redirect(url_for("entries.entry_detail", entry_uuid=entry_uuid))
    if cur["status"] != "draft" or cur["person_id"] != g.user["id"]:
        flash("You can only void your own drafts; ask the admin otherwise.")
        return redirect(url_for("entries.entry_detail", entry_uuid=entry_uuid))
    try:
        vid = insert_version(
            conn, entry_uuid=entry_uuid, person_id=cur["person_id"],
            job_id=cur["job_id"], work_date=cur["work_date"],
            start_time=cur["start_time"], end_time=cur["end_time"],
            break_minutes=cur["break_minutes"], note=cur["note"],
            status="void", author=g.user, change_reason=reason,
        )
    except TransitionError as exc:
        flash(str(exc))
        return redirect(url_for("entries.entry_detail", entry_uuid=entry_uuid))
    row = conn.execute("SELECT * FROM time_entry_version WHERE id=?", (vid,)).fetchone()
    flag_mod.recompute_for_version(conn, row)
    audit(conn, g.user["id"], "entry.void", "time_entry", entry_uuid, reason)
    conn.commit()
    return redirect(url_for("entries.entry_list"))


@bp.get("/submit")
@worker_required
def submit_confirm():
    conn = get_db()
    rows = conn.execute(
        "SELECT c.*, j.code AS job_code FROM v_time_entry_current c JOIN job j ON j.id=c.job_id"
        " WHERE c.person_id=? AND c.status='draft' AND c.work_date<=?"
        " ORDER BY c.work_date, c.start_time",
        (g.user["id"], today_local().isoformat()),
    ).fetchall()
    return render_template("submit_confirm.html", rows=rows)


@bp.post("/submit")
@worker_required
def submit():
    """One tap: attests ALL unsubmitted entries through today."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM v_time_entry_current"
        " WHERE person_id=? AND status='draft' AND work_date<=?",
        (g.user["id"], today_local().isoformat()),
    ).fetchall()
    if not rows:
        flash("Nothing to submit.")
        return redirect(url_for("entries.home"))
    cur = conn.execute(
        "INSERT INTO submission (person_id, submitted_at) VALUES (?,?)",
        (g.user["id"], utcnow()),
    )
    submission_id = cur.lastrowid
    for r in rows:
        vid = insert_version(
            conn, entry_uuid=r["entry_uuid"], person_id=r["person_id"],
            job_id=r["job_id"], work_date=r["work_date"], start_time=r["start_time"],
            end_time=r["end_time"], break_minutes=r["break_minutes"], note=r["note"],
            status="submitted", author=g.user, change_reason="Submitted",
        )
        conn.execute(
            "INSERT INTO submission_entry (submission_id, time_entry_version_id) VALUES (?,?)",
            (submission_id, vid),
        )
        vrow = conn.execute("SELECT * FROM time_entry_version WHERE id=?", (vid,)).fetchone()
        flag_mod.recompute_for_version(conn, vrow)
    audit(conn, g.user["id"], "entry.submit", "submission", submission_id,
          None, {"entries": len(rows)})
    conn.commit()
    flash(f"Submitted {len(rows)} entr{'y' if len(rows) == 1 else 'ies'}. Thank you.")
    return redirect(url_for("entries.home"))


# --- my-record printout: core requirement, not a report ---------------------

def record_rows(conn: sqlite3.Connection, person_id: int) -> list[dict]:
    """Chronological, verifiable history: every version with author/time/
    reason, approvals, corrections. Lets the worker verify the employer never
    edited their hours without their knowledge."""
    out = []
    for v in conn.execute(
        "SELECT v.*, j.code AS job_code, a.display_name AS author_name"
        " FROM time_entry_version v JOIN job j ON j.id=v.job_id"
        " JOIN person a ON a.id=v.author_id"
        " WHERE v.person_id=? ORDER BY v.entry_uuid, v.version_no",
        (person_id,),
    ):
        out.append({"kind": "version", "row": v})
    for ap in conn.execute(
        "SELECT ae.entry_uuid, a.*, p.display_name AS approver_name"
        " FROM approval_entry ae JOIN approval a ON a.id=ae.approval_id"
        " JOIN person p ON p.id=a.approver_id"
        " JOIN time_entry_version v ON v.id=ae.acted_on_version_id"
        " WHERE v.person_id=? ORDER BY a.created_at",
        (person_id,),
    ):
        out.append({"kind": "approval", "row": ap})
    return out


@bp.get("/me/record")
@worker_required
def my_record():
    conn = get_db()
    badge_rows = conn.execute(
        "SELECT f.entry_uuid, f.flag_type, f.created_at FROM entry_flag f"
        " JOIN time_entry_version v ON v.id=f.trigger_version_id"
        " WHERE v.person_id=? AND f.flag_type IN ('self_approval','post_approval_correction')",
        (g.user["id"],),
    ).fetchall()
    return render_template(
        "record.html", person=g.user, rows=record_rows(conn, g.user["id"]),
        badges=badge_rows, generated_at=utcnow(),
    )


@bp.get("/me/record.csv")
@worker_required
def my_record_csv():
    conn = get_db()
    return _record_csv_response(conn, g.user)


def _record_csv_response(conn, person) -> Response:
    """Shared with the admin person-record export (same content as printout)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "record_for", "event", "entry_uuid", "version_no", "at_utc", "author",
        "status", "work_date", "start", "end", "break_minutes", "break_minutes_tag",
        "job", "note", "change_reason", "approval_action", "approval_reason",
        "flags_ack_reason", "self_approval",
    ])
    for item in record_rows(conn, person["id"]):
        r = item["row"]
        if item["kind"] == "version":
            w.writerow([
                person["display_name"], "version", r["entry_uuid"], r["version_no"],
                r["server_synced_at"], r["author_name"], r["status"], r["work_date"],
                r["start_time"], r["end_time"], r["break_minutes"], "SOURCE",
                r["job_code"], r["note"] or "", r["change_reason"] or "", "", "", "", "",
            ])
        else:
            w.writerow([
                person["display_name"], "approval", r["entry_uuid"], "",
                r["created_at"], r["approver_name"], "", "", "", "", "", "",
                "", "", "", r["action"], r["reason"] or "",
                r["flags_ack_reason"] or "", "yes" if r["is_self_approval"] else "",
            ])
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition":
                 f"attachment; filename=record_{person['username']}.csv"},
    )


@bp.get("/me/rate")
@worker_required
def my_rate():
    conn = get_db()
    hist = conn.execute(
        "SELECT hourly_rate_cents, rate_tag, effective_date, entered_at"
        " FROM v_rate_pay_effective WHERE person_id=? ORDER BY effective_date DESC",
        (g.user["id"],),
    ).fetchall()
    current = figures.rate_cents_as_of(conn, "rate_pay", g.user["id"],
                                       today_local().isoformat())
    return render_template("rate.html", hist=hist, current=current,
                           cents=figures.cents_to_str)


@bp.get("/capture")
@worker_required
def capture():
    conn = get_db()
    jobs = conn.execute(
        "SELECT id, code, name FROM job WHERE status='active' ORDER BY code"
    ).fetchall()
    return render_template("capture.html", jobs=jobs, as_of=utcnow(),
                           today=today_local().isoformat())
