"""Export correctness: tag columns, employee/subcontractor separation,
blank-and-flag for unset OT policy and missing rates, OT under policy
history (weeks before the first row blank+flagged; changes affect only weeks
on/after effective date; past ranges reproduce), full export completeness."""
import csv
import io
import zipfile
from datetime import timedelta

from ghitime.db import today_local
from ghitime.figures import week_start

from .conftest import form, login
from .helpers import (
    add_entry, add_ot_policy, add_rate, person, set_workweek_monday,
    submit_and_approve,
)


def _rows(body: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(body)))


def _monday(weeks_ago: int) -> str:
    return (week_start(today_local(), 0) - timedelta(weeks=weeks_ago)).isoformat()


def _work_week(db, worker, admin, monday_iso, hours_per_day=10, days=5):
    """days x hours approved entries starting monday_iso."""
    from datetime import date
    start = date.fromisoformat(monday_iso)
    uuids = []
    for i in range(days):
        u, _ = add_entry(db, worker, work_date=(start + timedelta(days=i)).isoformat(),
                         start="06:30", end=f"{6 + hours_per_day}:30", brk=0)
        submit_and_approve(db, u, worker, admin)
        uuids.append(u)
    return uuids


def test_payroll_export_tags_and_blank_flags(client, app, db):
    marta, gus = person(db, "marta"), person(db, "gus")
    set_workweek_monday(db)
    mon = _monday(2)
    _work_week(db, marta, gus, mon, hours_per_day=8, days=2)

    admin = app.test_client()
    login(admin, "gus")
    dto = (today_local()).isoformat()
    body = admin.get(f"/admin/export/payroll/employees.csv?from={mon}&to={dto}").data.decode()
    rows = _rows(body)
    entries = [r for r in rows if r["row_type"] == "entry"]
    weeks = [r for r in rows if r["row_type"] == "week_total"]
    assert entries and weeks

    for r in entries:
        assert r["file_label"] == "EMPLOYEE"
        assert r["hours_tag"] == "CALCULATED"
        assert r["break_minutes_tag"] == "SOURCE"
        # marta has NO pay rate in this test: blank + stated flag, never 0
        assert r["pay_rate"] == "" and r["gross_preview"] == ""
        assert "pay rate not set" in r["flag"]
    for r in weeks:
        # no OT policy in force yet: blank + stated flag, never a default
        assert r["ot_hours_past_threshold"] == ""
        assert "no OT policy in force" in r["flag"]
        assert r["unapproved_tag"] == "CALCULATED"


def test_employee_and_subcontractor_never_mixed(client, app, db):
    marta, ollie, gus = person(db, "marta"), person(db, "ollie"), person(db, "gus")
    d = (today_local() - timedelta(days=1)).isoformat()
    u1, _ = add_entry(db, marta, work_date=d)
    submit_and_approve(db, u1, marta, gus)
    u2, _ = add_entry(db, ollie, work_date=d, start="07:00", end="15:00", brk=0)
    submit_and_approve(db, u2, ollie, gus)

    admin = app.test_client()
    login(admin, "gus")
    emp = admin.get(f"/admin/export/payroll/employees.csv?from={d}&to={d}").data.decode()
    sub = admin.get(f"/admin/export/payroll/subcontractors.csv?from={d}&to={d}").data.decode()
    assert "Marta" in emp and "Ollie" not in emp
    assert "Ollie" in sub and "Marta" not in sub
    assert all(r["file_label"] == "SUBCONTRACTOR" for r in _rows(sub))
    assert "SUBCONTRACTOR" in sub.splitlines()[1]


def test_ot_policy_history_correctness(client, app, db):
    """Weeks before the first policy row are blank+flagged; a policy change
    affects only weeks on/after its effective date; re-running a past range
    reproduces identical figures."""
    marta, gus = person(db, "marta"), person(db, "gus")
    set_workweek_monday(db)
    add_rate(db, gus, marta["id"], 2000, "2020-01-01")

    w3, w2, w1 = _monday(3), _monday(2), _monday(1)  # three 50h weeks
    for m in (w3, w2, w1):
        _work_week(db, marta, gus, m, hours_per_day=10, days=5)

    # policy #1 (40h) effective from week2; week3 predates any policy
    add_ot_policy(db, gus, 40, 1.5, w2)

    admin = app.test_client()
    login(admin, "gus")
    dto = (today_local()).isoformat()
    url = f"/admin/export/payroll/employees.csv?from={w3}&to={dto}"
    first_run = admin.get(url).data.decode()
    weeks = {r["week_start"]: r for r in _rows(first_run) if r["row_type"] == "week_total"}

    assert weeks[w3]["ot_hours_past_threshold"] == ""      # before first policy
    assert "no OT policy in force" in weeks[w3]["flag"]
    assert weeks[w2]["ot_hours_past_threshold"] == "10.00"  # 50h - 40
    assert weeks[w2]["ot_threshold_applied"] == "40"
    assert weeks[w2]["ot_threshold_tag"] == "SOURCE"
    assert weeks[w1]["ot_hours_past_threshold"] == "10.00"

    # policy #2 (35h) effective this week: past weeks must NOT change
    add_ot_policy(db, gus, 35, 1.5, _monday(0))
    second_run = admin.get(url).data.decode()
    weeks2 = {r["week_start"]: r for r in _rows(second_run) if r["row_type"] == "week_total"}
    assert weeks2[w2]["ot_hours_past_threshold"] == "10.00"
    assert weeks2[w1]["ot_hours_past_threshold"] == "10.00"
    assert second_run == first_run, \
        "re-running a past range must reproduce the figures generated under" \
        " the policy in force then"


def test_ot_pay_preview_column_gated(client, app, db):
    marta, gus = person(db, "marta"), person(db, "gus")
    set_workweek_monday(db)
    add_rate(db, gus, marta["id"], 2000, "2020-01-01")
    mon = _monday(1)
    _work_week(db, marta, gus, mon, hours_per_day=10, days=5)
    add_ot_policy(db, gus, 40, 1.5, mon)

    admin = app.test_client()
    login(admin, "gus")
    dto = today_local().isoformat()
    url = f"/admin/export/payroll/employees.csv?from={mon}&to={dto}"

    body = admin.get(url).data.decode()
    assert "ot_pay_preview" not in body.splitlines()[0], "column only when enabled"

    db.execute("UPDATE config SET value='1' WHERE key='ot_pay_preview_enabled'")
    db.commit()
    body = admin.get(url).data.decode()
    week = [r for r in _rows(body) if r["row_type"] == "week_total"][0]
    # 10 OT hours x $20.00 x 1.5 = $300.00 — a CALCULATED preview only
    assert week["ot_pay_preview"] == "300.00"
    assert week["ot_pay_preview_tag"] == "CALCULATED"


def test_full_export_covers_every_table_csv_and_json(client, app, db):
    from ghitime.reports import ALL_TABLES

    tables = {r["name"] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == set(ALL_TABLES), "full export must cover EVERY table"

    admin = app.test_client()
    login(admin, "gus")
    z = zipfile.ZipFile(io.BytesIO(admin.get("/admin/export/full.zip").data))
    names = set(z.namelist())
    for t in ALL_TABLES:
        assert f"{t}.csv" in names and f"{t}.json" in names


def test_export_all_cli(app, tmp_path):
    runner = app.test_cli_runner()
    out = runner.invoke(args=["export-all", str(tmp_path / "dump")])
    assert "wrote" in out.output
    assert (tmp_path / "dump" / "person.csv").exists()
    assert (tmp_path / "dump" / "person.json").exists()
