"""Reports: rollups, tags, blank-and-flag for unset threshold/rates, the
no-margin/no-profit title rule, and worker record content."""
import csv
import io
from datetime import timedelta
from pathlib import Path

from ghitime.db import today_local

from .conftest import login
from .helpers import (
    add_entry, add_ot_policy, add_rate, person, set_workweek_monday,
    submit_and_approve,
)

REPO = Path(__file__).resolve().parent.parent


def test_no_report_title_contains_margin_or_profit(client, app, db):
    source = (REPO / "ghitime" / "reports.py").read_text().lower()
    for word in ("margin", "profit"):
        # the words may appear only in the comment stating the rule itself
        for line in source.splitlines():
            if word in line and "rule" not in line and "never contain" not in line:
                raise AssertionError(f"suspicious {word!r} in reports.py: {line.strip()}")
    admin = app.test_client()
    login(admin, "gus")
    body = admin.get("/admin/reports").data.decode().lower()
    assert "margin" not in body.replace("margin\" or \"profit", "")


def test_ot_report_blank_when_workweek_unset(client, app, db):
    admin = app.test_client()
    login(admin, "gus")
    body = admin.get("/admin/reports/ot.csv").data.decode()
    assert "workweek start unset" in body


def test_hours_rollup_periods(client, app, db):
    marta, gus = person(db, "marta"), person(db, "gus")
    d = (today_local() - timedelta(days=1)).isoformat()
    u, _ = add_entry(db, marta, work_date=d, start="08:00", end="12:00", brk=0)
    submit_and_approve(db, u, marta, gus)
    admin = app.test_client()
    login(admin, "gus")
    for period in ("day", "week", "month", "quarter", "year"):
        body = admin.get(
            f"/admin/reports/hours.csv?group=person&period={period}&from={d}&to={d}"
        ).data.decode()
        rows = list(csv.DictReader(io.StringIO(body)))
        assert rows and rows[0]["hours"] == "4.00", (period, body)
        assert rows[0]["hours_tag"] == "CALCULATED"


def test_labor_report_flags_missing_rates(client, app, db):
    marta, gus = person(db, "marta"), person(db, "gus")
    d = (today_local() - timedelta(days=1)).isoformat()
    u, _ = add_entry(db, marta, work_date=d, start="08:00", end="12:00", brk=0)
    submit_and_approve(db, u, marta, gus)
    admin = app.test_client()
    login(admin, "gus")
    body = admin.get(f"/admin/reports/labor.csv?basis=pay&from={d}&to={d}").data.decode()
    assert "no pay rate set" in body

    add_rate(db, gus, marta["id"], 2000, "2020-01-01")
    body = admin.get(f"/admin/reports/labor.csv?basis=pay&from={d}&to={d}").data.decode()
    rows = list(csv.DictReader(io.StringIO(body)))
    assert rows[0]["pay_amount"] == "80.00" and rows[0]["amount_tag"] == "CALCULATED"


def test_worker_record_shows_full_history(client, app, db):
    marta, gus = person(db, "marta"), person(db, "gus")
    d = (today_local() - timedelta(days=1)).isoformat()
    u, _ = add_entry(db, marta, work_date=d)
    submit_and_approve(db, u, marta, gus)
    login(client, "marta")
    body = client.get("/me/record.csv").data.decode()
    rows = list(csv.DictReader(io.StringIO(body)))
    events = [(r["event"], r["status"]) for r in rows if r["entry_uuid"] == u]
    assert ("version", "draft") in events
    assert ("version", "submitted") in events
    assert ("version", "approved") in events
    assert any(e == "approval" for e, _ in events)
