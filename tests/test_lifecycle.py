"""draft -> submitted -> approved flow, rejection path, illegal transitions
(incl. any worker-authored version after approved), and void exclusion."""
from datetime import timedelta

import pytest

from ghitime.db import today_local
from ghitime.lifecycle import TransitionError, insert_version

from .conftest import form, login
from .helpers import add_entry, advance, person, submit_and_approve

YESTERDAY = lambda: (today_local() - timedelta(days=1)).isoformat()


def test_full_flow_via_routes(client, app, db):
    marta = person(db, "marta")
    uuid, _ = add_entry(db, marta, work_date=YESTERDAY())

    login(client, "marta")
    r = client.post("/submit", data=form(app, client), follow_redirects=True)
    assert b"Submitted 1" in r.data
    cur = db.execute("SELECT * FROM v_time_entry_current WHERE entry_uuid=?", (uuid,)).fetchone()
    assert (cur["status"], cur["version_no"]) == ("submitted", 2)
    sub = db.execute(
        "SELECT se.* FROM submission_entry se JOIN time_entry_version v"
        " ON v.id=se.time_entry_version_id WHERE v.entry_uuid=?", (uuid,)
    ).fetchone()
    assert sub is not None, "submission must record the exact attested version"

    admin = app.test_client()
    login(admin, "vern")
    r = admin.post("/admin/approve", data=form(app, admin, entry_uuid=uuid),
                   follow_redirects=True)
    assert b"Approved 1" in r.data
    cur = db.execute("SELECT * FROM v_time_entry_current WHERE entry_uuid=?", (uuid,)).fetchone()
    assert (cur["status"], cur["version_no"]) == ("approved", 3)

    ae = db.execute("SELECT * FROM approval_entry WHERE entry_uuid=?", (uuid,)).fetchone()
    assert ae["resulting_version_id"] is not None
    assert db.execute("SELECT entry_uuid FROM time_entry_version WHERE id=?",
                      (ae["resulting_version_id"],)).fetchone()["entry_uuid"] == uuid


def test_rejection_returns_to_draft_with_reason_attached(client, app, db):
    marta, gus = person(db, "marta"), person(db, "vern")
    uuid, _ = add_entry(db, marta, work_date=YESTERDAY())
    advance(db, uuid, "submitted", marta, "Submitted")

    admin = app.test_client()
    login(admin, "vern")
    r = admin.post("/admin/reject",
                   data=form(app, admin, entry_uuid=uuid, reason="break missing"),
                   follow_redirects=True)
    assert b"Rejected 1" in r.data
    cur = db.execute("SELECT * FROM v_time_entry_current WHERE entry_uuid=?", (uuid,)).fetchone()
    assert cur["status"] == "draft"
    assert "Rejected: break missing" in cur["change_reason"]
    ae = db.execute("SELECT * FROM approval_entry WHERE entry_uuid=?", (uuid,)).fetchone()
    assert ae["resulting_version_id"] is not None  # reject creates a version -> linked


def test_reject_without_reason_refused(client, app, db):
    marta = person(db, "marta")
    uuid, _ = add_entry(db, marta, work_date=YESTERDAY())
    advance(db, uuid, "submitted", marta, "Submitted")
    admin = app.test_client()
    login(admin, "vern")
    admin.post("/admin/reject", data=form(app, admin, entry_uuid=uuid, reason=""),
               follow_redirects=True)
    cur = db.execute("SELECT status FROM v_time_entry_current WHERE entry_uuid=?",
                     (uuid,)).fetchone()
    assert cur["status"] == "submitted"


ILLEGAL = [
    ("draft", "approved"),      # approve requires submitted
    ("void", "draft"),          # void is terminal
    ("void", "submitted"),
    ("approved", "submitted"),
    ("approved", "draft"),
    ("draft", "draft-by-admin-ok-but-see-below",),
]


def test_illegal_transitions_rejected_at_app_layer(db):
    marta, gus = person(db, "marta"), person(db, "vern")

    def fresh(status_chain=()):
        uuid, _ = add_entry(db, marta, work_date=YESTERDAY())
        for st, author, reason in status_chain:
            advance(db, uuid, st, author, reason)
        return uuid

    def attempt(uuid, status, author):
        cv = db.execute("SELECT * FROM v_time_entry_current WHERE entry_uuid=?",
                        (uuid,)).fetchone()
        insert_version(
            db, entry_uuid=uuid, person_id=cv["person_id"], job_id=cv["job_id"],
            work_date=cv["work_date"], start_time=cv["start_time"],
            end_time=cv["end_time"], break_minutes=cv["break_minutes"],
            note=cv["note"], status=status, author=author, change_reason="x",
        )

    # draft -> approved directly: illegal
    u = fresh()
    with pytest.raises(TransitionError):
        attempt(u, "approved", gus)

    # void is terminal
    u = fresh([("void", marta, "typo")])
    for target in ("draft", "submitted", "approved", "void"):
        with pytest.raises(TransitionError):
            attempt(u, target, gus)

    # approved -> draft/submitted: illegal even for admin
    u = fresh([("submitted", marta, "Submitted"), ("approved", gus, "Approved")])
    for target in ("draft", "submitted"):
        with pytest.raises(TransitionError):
            attempt(u, target, gus)

    # ANY worker-authored version after approved: illegal (binding #1)
    for target in ("draft", "submitted", "approved", "void"):
        with pytest.raises(TransitionError):
            attempt(u, target, marta)

    # admin-only transitions refused for workers
    u = fresh([("submitted", marta, "Submitted")])
    with pytest.raises(TransitionError):
        attempt(u, "approved", marta)   # workers cannot approve
    with pytest.raises(TransitionError):
        attempt(u, "void", marta)       # pulling back submitted is admin's call

    # a worker cannot author versions on someone else's entry
    deshawn = person(db, "deshawn")
    u = fresh()
    with pytest.raises(TransitionError):
        attempt(u, "draft", deshawn)


def test_worker_edit_after_approval_rejected_via_route(client, app, db):
    marta, gus = person(db, "marta"), person(db, "vern")
    uuid, _ = add_entry(db, marta, work_date=YESTERDAY())
    submit_and_approve(db, uuid, marta, gus)
    login(client, "marta")
    j1 = db.execute("SELECT id FROM job WHERE code='J1'").fetchone()["id"]
    client.post(f"/entries/{uuid}/edit", data=form(
        app, client, work_date=YESTERDAY(), job_id=str(j1), start_time="07:00",
        end_time="15:00", break_minutes="30", note="", change_reason="sneaky edit",
    ), follow_redirects=True)
    cur = db.execute("SELECT * FROM v_time_entry_current WHERE entry_uuid=?", (uuid,)).fetchone()
    assert (cur["status"], cur["start_time"]) == ("approved", "08:00"), \
        "approved entry must be untouched by worker edits"


def test_void_excluded_from_totals_reports_and_exports(client, app, db):
    from ghitime.figures import week_start, weekly_minutes

    marta, gus = person(db, "marta"), person(db, "vern")
    d = YESTERDAY()
    keep_uuid, _ = add_entry(db, marta, work_date=d, start="08:00", end="12:00", brk=0)
    void_uuid, _ = add_entry(db, marta, work_date=d, start="13:00", end="17:00", brk=0)
    advance(db, void_uuid, "void", marta, "entered twice")
    submit_and_approve(db, keep_uuid, marta, gus)

    wk = week_start(today_local() - timedelta(days=1), 0)
    total, _ = weekly_minutes(db, marta["id"], wk)
    assert total == 240, "voided entry must not add to weekly totals"

    admin = app.test_client()
    login(admin, "vern")
    csv_body = admin.get(f"/admin/reports/hours.csv?group=person&period=day&from={d}&to={d}"
                         ).data.decode()
    assert "4.00" in csv_body and "8.00" not in csv_body

    payroll = admin.get(f"/admin/export/payroll/employees.csv?from={d}&to={d}").data.decode()
    assert void_uuid not in payroll
    assert payroll.count('entry,Marta Worker') == 1

    full = admin.get("/admin/export/full.zip")
    assert full.status_code == 200  # void versions DO appear in the full table dump
