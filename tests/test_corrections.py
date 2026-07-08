"""Post-approval correction with audit + badge propagation, and the
resulting_version_id presence rule (Gate 2 binding: approval paths that
create a version link it; the schema separately proves match-when-set)."""
from datetime import timedelta

from ghitime.db import today_local

from .conftest import form, login
from .helpers import add_entry, advance, person, submit_and_approve

D = lambda n=1: (today_local() - timedelta(days=n)).isoformat()


def test_post_approval_correction(client, app, db):
    marta, gus = person(db, "marta"), person(db, "vern")
    uuid, _ = add_entry(db, marta, work_date=D())
    submit_and_approve(db, uuid, marta, gus)

    admin = app.test_client()
    login(admin, "vern")
    j1 = db.execute("SELECT id FROM job WHERE code='J1'").fetchone()["id"]
    r = admin.post(f"/admin/entries/{uuid}/correct", data=form(
        app, admin, work_date=D(), job_id=str(j1), start_time="08:00",
        end_time="15:00", break_minutes="30", note="",
        change_reason="site closed early, confirmed with Marta",
    ), follow_redirects=True)
    assert b"Correction recorded" in r.data

    cur = db.execute("SELECT * FROM v_time_entry_current WHERE entry_uuid=?", (uuid,)).fetchone()
    assert (cur["status"], cur["end_time"]) == ("approved", "15:00")
    assert cur["author_id"] == gus["id"]

    au = db.execute(
        "SELECT * FROM audit_log WHERE action='entry.correct_post_approval'"
        " AND entity_id=?", (uuid,)).fetchone()
    assert au is not None and "site closed early" in au["reason"]

    badge = db.execute(
        "SELECT * FROM entry_flag WHERE entry_uuid=? AND flag_type='post_approval_correction'",
        (uuid,)).fetchone()
    assert badge is not None

    # badge travels onto the worker's printout...
    login(client, "marta")
    page = client.get("/me/record").data.decode()
    assert "CORRECTED AFTER APPROVAL" in page
    # ...and onto every export containing the entry
    payroll = admin.get(
        f"/admin/export/payroll/employees.csv?from={D()}&to={D()}").data.decode()
    assert "CORRECTED-AFTER-APPROVAL" in payroll


def test_correction_requires_reason_and_approved_state(client, app, db):
    marta, gus = person(db, "marta"), person(db, "vern")
    uuid, _ = add_entry(db, marta, work_date=D())
    admin = app.test_client()
    login(admin, "vern")
    j1 = db.execute("SELECT id FROM job WHERE code='J1'").fetchone()["id"]
    common = dict(work_date=D(), job_id=str(j1), start_time="08:00",
                  end_time="15:00", break_minutes="30", note="")
    r = admin.post(f"/admin/entries/{uuid}/correct",
                   data=form(app, admin, change_reason="", **common),
                   follow_redirects=True)
    assert b"reason is required" in r.data
    r = admin.post(f"/admin/entries/{uuid}/correct",
                   data=form(app, admin, change_reason="x", **common),
                   follow_redirects=True)
    assert b"approved entries only" in r.data
    cur = db.execute("SELECT version_no FROM v_time_entry_current WHERE entry_uuid=?",
                     (uuid,)).fetchone()
    assert cur["version_no"] == 1


def test_resulting_version_id_presence_rule(client, app, db):
    """Every approval path that creates a version links it (non-NULL);
    nothing in V1 creates approval lines without a resulting version, so all
    rows must be non-NULL and coherent."""
    marta, gus = person(db, "marta"), person(db, "vern")
    u1, _ = add_entry(db, marta, work_date=D(2))
    submit_and_approve(db, u1, marta, gus)          # approve path
    u2, _ = add_entry(db, marta, work_date=D(3))
    advance(db, u2, "submitted", marta, "Submitted")
    admin = app.test_client()
    login(admin, "vern")
    admin.post("/admin/reject", data=form(app, admin, entry_uuid=u2, reason="nope"),
               follow_redirects=True)               # reject path

    rows = db.execute("SELECT * FROM approval_entry").fetchall()
    assert len(rows) >= 2
    for ae in rows:
        assert ae["resulting_version_id"] is not None, \
            "approval paths that create a version must link it"
        linked = db.execute("SELECT entry_uuid FROM time_entry_version WHERE id=?",
                            (ae["resulting_version_id"],)).fetchone()
        assert linked["entry_uuid"] == ae["entry_uuid"]
