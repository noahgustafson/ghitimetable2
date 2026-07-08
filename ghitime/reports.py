"""Reports and exports. Every money/quantity column is paired with its tag;
missing values render blank alongside a stated flag — never defaulted.
Money is admin-only. void entries appear in NOTHING here (Gate 2 binding #2).

Report titles never contain "margin" or "profit": only labor cost is
captured (rule restated in README for anyone adding reports later).
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import zipfile
from datetime import date, timedelta
from decimal import Decimal

from flask import Blueprint, Response, g, render_template, request

from . import figures
from .auth import admin_required
from .db import config_get, get_db, utcnow

bp = Blueprint("reports", __name__, url_prefix="/admin")

NON_VOID = ("draft", "submitted", "approved")


def _csv_response(rows: list[list], filename: str) -> Response:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _range_args() -> tuple[str, str]:
    today = date.today()
    dfrom = request.args.get("from") or (today - timedelta(days=30)).isoformat()
    dto = request.args.get("to") or today.isoformat()
    return dfrom, dto


def _period_expr(period: str) -> str:
    return {
        "day": "work_date",
        "week": "date(work_date, '-' || ((strftime('%w', work_date) + 6) % 7) || ' days')",
        "month": "strftime('%Y-%m', work_date)",
        "quarter": "strftime('%Y', work_date) || '-Q' ||"
                   " ((strftime('%m', work_date) + 2) / 3)",
        "year": "strftime('%Y', work_date)",
    }[period]


# --- hours report ------------------------------------------------------------

@bp.get("/reports")
@admin_required
def hub():
    return render_template("admin/reports.html")


@bp.get("/reports/hours")
@bp.get("/reports/hours.csv")
@admin_required
def hours():
    conn = get_db()
    dfrom, dto = _range_args()
    group = request.args.get("group") or "person"
    period = request.args.get("period") or "week"
    if group not in ("person", "job", "person_job") or period not in (
        "day", "week", "month", "quarter", "year"
    ):
        return "bad group/period", 400
    keys = {
        "person": "p.display_name",
        "job": "j.code",
        "person_job": "p.display_name, j.code",
    }[group]
    pexpr = _period_expr(period)
    qmarks = ",".join("?" for _ in NON_VOID)
    rows = conn.execute(
        f"SELECT {keys}, {pexpr} AS period,"
        " SUM(m.worked_minutes) AS minutes,"
        " SUM(CASE WHEN m.worked_minutes IS NULL THEN 1 ELSE 0 END) AS blank_entries"
        " FROM v_time_entry_minutes m"
        " JOIN person p ON p.id=m.person_id JOIN job j ON j.id=m.job_id"
        f" WHERE m.work_date>=? AND m.work_date<=? AND m.status IN ({qmarks})"
        f" GROUP BY {keys}, period ORDER BY period, {keys}",
        (dfrom, dto, *NON_VOID),
    ).fetchall()
    header_keys = ["person"] if group == "person" else (
        ["job"] if group == "job" else ["person", "job"])
    out = [header_keys + ["period", "hours", "hours_tag", "entries_with_blank_duration"]]
    for r in rows:
        keyvals = [r[i] for i in range(len(header_keys))]
        hours_val = figures.minutes_to_hours(r["minutes"]) if r["minutes"] is not None else None
        out.append(keyvals + [
            r["period"],
            "" if hours_val is None else str(hours_val),
            figures.CALCULATED,
            r["blank_entries"],
        ])
    if request.path.endswith(".csv"):
        return _csv_response(out, f"hours_{group}_{period}_{dfrom}_{dto}.csv")
    return render_template("admin/report_table.html",
                           title=f"Hours by {group} per {period}",
                           header=out[0], rows=out[1:], dfrom=dfrom, dto=dto)


# --- OT report ---------------------------------------------------------------

def weekly_ot_rows(conn: sqlite3.Connection, dfrom: str, dto: str) -> list[dict]:
    """Per person per week: minutes, OT hours under the policy in force at
    that week's start (blank + reason when none), threshold applied."""
    ws_conf = config_get(conn, "workweek_start_dow")
    if ws_conf is None:
        # OT figures require a SET workweek boundary — display grouping never
        # feeds an OT figure. Everything renders blank+flagged.
        return [{"error": "workweek start unset"}]
    start_dow = int(ws_conf)
    qmarks = ",".join("?" for _ in NON_VOID)
    rows = conn.execute(
        "SELECT m.person_id, p.display_name, m.work_date, m.worked_minutes"
        " FROM v_time_entry_minutes m JOIN person p ON p.id=m.person_id"
        f" WHERE m.work_date>=? AND m.work_date<=? AND m.status IN ({qmarks})",
        (dfrom, dto, *NON_VOID),
    ).fetchall()
    weeks: dict[tuple[int, str], dict] = {}
    for r in rows:
        wk = figures.week_start(date.fromisoformat(r["work_date"]), start_dow)
        key = (r["person_id"], wk.isoformat())
        w = weeks.setdefault(key, {"person": r["display_name"], "week_start": wk.isoformat(),
                                   "minutes": 0, "blanks": 0})
        if r["worked_minutes"] is None:
            w["blanks"] += 1
        else:
            w["minutes"] += r["worked_minutes"]
    out = []
    for (pid, wk), w in sorted(weeks.items(), key=lambda kv: (kv[1]["week_start"],
                                                              kv[1]["person"])):
        policy = figures.ot_policy_in_force(conn, wk)
        ot = figures.ot_hours_past_threshold(w["minutes"], policy)
        out.append({
            **w,
            "person_id": pid,
            "policy": policy,
            "ot_hours": ot,
            "flag": "" if policy else "no OT policy in force for this week — bookkeeper advises",
        })
    return out


@bp.get("/reports/ot")
@bp.get("/reports/ot.csv")
@admin_required
def ot_report():
    conn = get_db()
    dfrom, dto = _range_args()
    data = weekly_ot_rows(conn, dfrom, dto)
    header = ["person", "week_start", "hours", "hours_tag",
              "ot_hours_past_threshold", "ot_hours_tag",
              "ot_threshold_applied", "ot_threshold_tag", "flag",
              "entries_with_blank_duration"]
    out = [header]
    if data and "error" in data[0]:
        out.append(["", "", "", "", "", "", "", "",
                    "workweek start unset — set it in Settings; OT is blank until then", ""])
    else:
        for w in data:
            out.append([
                w["person"], w["week_start"],
                str(figures.minutes_to_hours(w["minutes"])), figures.CALCULATED,
                "" if w["ot_hours"] is None else str(w["ot_hours"]),
                figures.CALCULATED,
                "" if w["policy"] is None else str(w["policy"].threshold_hours),
                figures.SOURCE, w["flag"], w["blanks"],
            ])
    if request.path.endswith(".csv"):
        return _csv_response(out, f"weekly_ot_{dfrom}_{dto}.csv")
    return render_template("admin/report_table.html", title="Weekly OT hours past threshold",
                           header=out[0], rows=out[1:], dfrom=dfrom, dto=dto)


# --- labor report (cost basis pay | billable basis bill) ---------------------

@bp.get("/reports/labor")
@bp.get("/reports/labor.csv")
@admin_required
def labor():
    conn = get_db()
    dfrom, dto = _range_args()
    basis = request.args.get("basis") or "pay"
    if basis not in ("pay", "bill"):
        return "bad basis", 400
    title = "Labor cost (pay)" if basis == "pay" else "Billable labor (bill)"
    table = "rate_pay" if basis == "pay" else "rate_bill"
    qmarks = ",".join("?" for _ in NON_VOID)
    rows = conn.execute(
        "SELECT m.*, j.code AS job_code, p.display_name AS person_name"
        " FROM v_time_entry_minutes m JOIN job j ON j.id=m.job_id"
        " JOIN person p ON p.id=m.person_id"
        f" WHERE m.work_date>=? AND m.work_date<=? AND m.status IN ({qmarks})"
        " ORDER BY j.code, p.display_name, m.work_date",
        (dfrom, dto, *NON_VOID),
    ).fetchall()
    agg: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["job_code"], r["person_name"])
        a = agg.setdefault(key, {"minutes": 0, "cents": 0, "blanks": 0, "missing_rate": 0})
        if r["worked_minutes"] is None:
            a["blanks"] += 1
            continue
        a["minutes"] += r["worked_minutes"]
        rate = figures.rate_cents_as_of(conn, table, r["person_id"], r["work_date"])
        if rate is None:
            a["missing_rate"] += 1
        else:
            a["cents"] += figures.gross_preview_cents(r["worked_minutes"], rate) or 0
    header = ["job", "person", "hours", "hours_tag", f"{basis}_amount", "amount_tag", "flag"]
    out = [header]
    for (job, person), a in sorted(agg.items()):
        flag_bits = []
        if a["blanks"]:
            flag_bits.append(f"{a['blanks']} entries with blank duration")
        if a["missing_rate"]:
            flag_bits.append(f"{a['missing_rate']} entries with no {basis} rate set"
                             " — amount excludes them")
        out.append([
            job, person, str(figures.minutes_to_hours(a["minutes"])), figures.CALCULATED,
            figures.cents_to_str(a["cents"]) if not a["missing_rate"] else
            (figures.cents_to_str(a["cents"]) if a["cents"] else ""),
            figures.CALCULATED, "; ".join(flag_bits),
        ])
    if request.path.endswith(".csv"):
        return _csv_response(out, f"labor_{basis}_{dfrom}_{dto}.csv")
    return render_template("admin/report_table.html", title=title,
                           header=out[0], rows=out[1:], dfrom=dfrom, dto=dto)


# --- exports -----------------------------------------------------------------

@bp.get("/export")
@admin_required
def export_hub():
    conn = get_db()
    people = conn.execute("SELECT id, display_name FROM person ORDER BY display_name").fetchall()
    return render_template("admin/export.html", people=people)


def payroll_rows(conn: sqlite3.Connection, dfrom: str, dto: str,
                 worker_type: str) -> list[list]:
    """Bookkeeper payroll-prep rows for one worker_type — employees and
    subcontractors are NEVER mixed in one file. Approved entries only (that
    is what payroll runs on); unapproved entries in range are surfaced as a
    per-week CALCULATED count, never silently dropped."""
    label = "EMPLOYEE" if worker_type == "employee" else "SUBCONTRACTOR"
    preview_on = config_get(conn, "ot_pay_preview_enabled") == "1"
    ws_conf = config_get(conn, "workweek_start_dow")

    header = [
        "file_label", "row_type", "person", "week_start", "date", "job",
        "hours", "hours_tag", "break_minutes", "break_minutes_tag",
        "pay_rate", "pay_rate_tag", "gross_preview", "gross_preview_tag",
        "ot_hours_past_threshold", "ot_hours_tag",
        "ot_threshold_applied", "ot_threshold_tag",
    ]
    if preview_on:
        header += ["ot_pay_preview", "ot_pay_preview_tag"]
    header += ["correction_badge", "self_approval_badge", "flag",
               "unapproved_entries_in_range", "unapproved_tag"]

    out = [header]
    entries = conn.execute(
        "SELECT m.*, j.code AS job_code, p.display_name AS person_name"
        " FROM v_time_entry_minutes m JOIN job j ON j.id=m.job_id"
        " JOIN person p ON p.id=m.person_id"
        " WHERE m.work_date>=? AND m.work_date<=? AND m.status='approved'"
        "   AND p.worker_type=?"
        " ORDER BY p.display_name, m.work_date, m.start_time",
        (dfrom, dto, worker_type),
    ).fetchall()
    unapproved = {
        r["person_id"]: r["n"]
        for r in conn.execute(
            "SELECT c.person_id, COUNT(*) AS n FROM v_time_entry_current c"
            " JOIN person p ON p.id=c.person_id"
            " WHERE c.work_date>=? AND c.work_date<=? AND c.status IN ('draft','submitted')"
            "   AND p.worker_type=? GROUP BY c.person_id",
            (dfrom, dto, worker_type),
        )
    }
    badges = _badge_map(conn, [e["entry_uuid"] for e in entries])

    weeks: dict[tuple[int, str], dict] = {}
    for e in entries:
        wk = (figures.week_start(date.fromisoformat(e["work_date"]), int(ws_conf)).isoformat()
              if ws_conf is not None else "")
        key = (e["person_id"], wk)
        w = weeks.setdefault(key, {"person": e["person_name"], "minutes": 0})
        if e["worked_minutes"] is not None:
            w["minutes"] += e["worked_minutes"]

        rate = figures.rate_cents_as_of(conn, "rate_pay", e["person_id"], e["work_date"])
        gross = figures.gross_preview_cents(e["worked_minutes"], rate)
        flag_bits = []
        if e["worked_minutes"] is None:
            flag_bits.append("blank duration (see entry flags)")
        if rate is None:
            flag_bits.append("pay rate not set")
        row = [
            label, "entry", e["person_name"], wk, e["work_date"], e["job_code"],
            "" if e["worked_minutes"] is None else str(figures.minutes_to_hours(e["worked_minutes"])),
            figures.CALCULATED,
            e["break_minutes"], figures.SOURCE,
            figures.cents_to_str(rate), "" if rate is None else figures.SOURCE,
            figures.cents_to_str(gross), "" if gross is None else figures.CALCULATED,
            "", "", "", "",  # OT columns live on week_total rows
        ]
        if preview_on:
            row += ["", ""]
        eb = badges.get(e["entry_uuid"], set())
        row += [
            "CORRECTED-AFTER-APPROVAL" if "post_approval_correction" in eb else "",
            "SELF-APPROVED" if "self_approval" in eb else "",
            "; ".join(flag_bits), "", "",
        ]
        out.append(row)

    for (pid, wk), w in sorted(weeks.items(), key=lambda kv: (kv[1]["person"], kv[0][1])):
        if wk == "":
            policy, ot = None, None
            wk_flag = "workweek start unset — OT blank until it is set"
        else:
            policy = figures.ot_policy_in_force(conn, wk)
            ot = figures.ot_hours_past_threshold(w["minutes"], policy)
            wk_flag = "" if policy else \
                "no OT policy in force for this week — bookkeeper advises"
        row = [
            label, "week_total", w["person"], wk, "", "",
            str(figures.minutes_to_hours(w["minutes"])), figures.CALCULATED,
            "", "", "", "", "", "",
            "" if ot is None else str(ot), figures.CALCULATED,
            "" if policy is None else str(policy.threshold_hours),
            "" if policy is None else figures.SOURCE,
        ]
        if preview_on:
            # OT pay preview = ot_hours x rate x multiplier. A preview, never
            # payroll execution. Blank unless a policy is in force.
            prev = ""
            if ot is not None and policy is not None and ot > 0:
                rate = figures.rate_cents_as_of(conn, "rate_pay", pid, wk)
                if rate is not None:
                    cents = int((Decimal(rate) * ot * policy.multiplier)
                                .quantize(Decimal("1")))
                    prev = figures.cents_to_str(cents)
            row += [prev, figures.CALCULATED if prev else ""]
        row += ["", "", wk_flag, unapproved.get(pid, 0), figures.CALCULATED]
        out.append(row)
    return out


def _badge_map(conn, uuids: list[str]) -> dict[str, set]:
    if not uuids:
        return {}
    qmarks = ",".join("?" for _ in uuids)
    out: dict[str, set] = {}
    for r in conn.execute(
        f"SELECT entry_uuid, flag_type FROM entry_flag WHERE entry_uuid IN ({qmarks})"
        " AND flag_type IN ('self_approval','post_approval_correction')",
        uuids,
    ):
        out.setdefault(r["entry_uuid"], set()).add(r["flag_type"])
    return out


@bp.get("/export/payroll/employees.csv")
@admin_required
def payroll_employees():
    conn = get_db()
    dfrom, dto = _range_args()
    return _csv_response(payroll_rows(conn, dfrom, dto, "employee"),
                         f"payroll-prep_EMPLOYEES_{dfrom}_{dto}.csv")


@bp.get("/export/payroll/subcontractors.csv")
@admin_required
def payroll_subcontractors():
    conn = get_db()
    dfrom, dto = _range_args()
    return _csv_response(payroll_rows(conn, dfrom, dto, "subcontractor"),
                         f"payroll-prep_SUBCONTRACTOR_{dfrom}_{dto}.csv")


@bp.get("/export/job-labor.csv")
@admin_required
def job_labor():
    conn = get_db()
    dfrom, dto = _range_args()
    qmarks = ",".join("?" for _ in NON_VOID)
    rows = conn.execute(
        "SELECT m.*, j.code AS job_code FROM v_time_entry_minutes m"
        " JOIN job j ON j.id=m.job_id"
        f" WHERE m.work_date>=? AND m.work_date<=? AND m.status IN ({qmarks})"
        " ORDER BY j.code, m.work_date",
        (dfrom, dto, *NON_VOID),
    ).fetchall()
    agg: dict[str, dict] = {}
    for r in rows:
        a = agg.setdefault(r["job_code"], {"minutes": 0, "pay": 0, "bill": 0,
                                           "missing_pay": 0, "missing_bill": 0, "blanks": 0})
        if r["worked_minutes"] is None:
            a["blanks"] += 1
            continue
        a["minutes"] += r["worked_minutes"]
        for basis, table in (("pay", "rate_pay"), ("bill", "rate_bill")):
            rate = figures.rate_cents_as_of(conn, table, r["person_id"], r["work_date"])
            if rate is None:
                a[f"missing_{basis}"] += 1
            else:
                a[basis] += figures.gross_preview_cents(r["worked_minutes"], rate) or 0
    out = [["job", "hours", "hours_tag", "labor_cost_pay", "labor_cost_tag",
            "billable_labor", "billable_tag", "flag"]]
    for job, a in sorted(agg.items()):
        flags = []
        if a["blanks"]:
            flags.append(f"{a['blanks']} blank-duration entries")
        if a["missing_pay"]:
            flags.append(f"{a['missing_pay']} entries missing pay rate — cost excludes them")
        if a["missing_bill"]:
            flags.append(f"{a['missing_bill']} entries missing bill rate — billable excludes them")
        out.append([job, str(figures.minutes_to_hours(a["minutes"])), figures.CALCULATED,
                    figures.cents_to_str(a["pay"]), figures.CALCULATED,
                    figures.cents_to_str(a["bill"]), figures.CALCULATED, "; ".join(flags)])
    return _csv_response(out, f"job-labor_{dfrom}_{dto}.csv")


# --- full export (data ownership) --------------------------------------------

ALL_TABLES = (
    "schema_migrations", "figure_tag", "person", "job", "time_entry_version",
    "submission", "submission_entry", "approval", "approval_entry",
    "rate_pay", "rate_bill", "ot_policy", "config", "entry_flag",
    "sync_conflict", "audit_log", "session", "login_attempt", "sync_log",
    "ops_event",
)


def full_export_files(conn: sqlite3.Connection) -> dict[str, str]:
    """Every table as CSV and JSON. Data ownership is a requirement."""
    files: dict[str, str] = {}
    for table in ALL_TABLES:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        cols = [d[0] for d in conn.execute(f"SELECT * FROM {table} LIMIT 0").description]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        for r in rows:
            w.writerow([r[c] for c in cols])
        files[f"{table}.csv"] = buf.getvalue()
        files[f"{table}.json"] = json.dumps(
            [dict(zip(cols, [r[c] for c in cols])) for r in rows],
            indent=1, sort_keys=True,
        )
    return files


@bp.get("/export/full.zip")
@admin_required
def full_zip():
    conn = get_db()
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in full_export_files(conn).items():
            zf.writestr(name, content)
        zf.writestr("EXPORTED_AT.txt", utcnow())
    mem.seek(0)
    return Response(
        mem.read(), mimetype="application/zip",
        headers={"Content-Disposition": "attachment; filename=ghitime-full-export.zip"},
    )
