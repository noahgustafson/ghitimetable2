"""Conflict flagging: every data-integrity flag type raises; approving a
flagged entry demands a stated reason recorded in the audit log; flags
auto-resolve (with reason) when a new version clears the condition."""
from datetime import timedelta

from ghitime.db import today_local

from .conftest import form, login
from .helpers import add_entry, advance, person

D = lambda n=1: (today_local() - timedelta(days=n)).isoformat()


def _open(db, uuid):
    return {r["flag_type"] for r in db.execute(
        "SELECT flag_type FROM v_open_flags WHERE entry_uuid=?", (uuid,))}


def test_each_condition_raises_its_flag(db):
    marta = person(db, "marta")
    u1, _ = add_entry(db, marta, work_date=D(), start="05:00", end="23:30", brk=15)
    assert "over_16h" in _open(db, u1)
    u2, _ = add_entry(db, marta, work_date=D(2), start="22:00", end="06:00", brk=0)
    assert "end_not_after_start" in _open(db, u2)
    u3, _ = add_entry(db, marta, work_date=D(3), start="08:00", end="09:00", brk=120)
    assert "break_exceeds_duration" in _open(db, u3)
    u4, _ = add_entry(db, marta, work_date=(today_local() + timedelta(days=1)).isoformat())
    assert "future_dated" in _open(db, u4)


def test_overlap_and_duplicate_flag_both_entries(db):
    marta = person(db, "marta")
    a, _ = add_entry(db, marta, work_date=D(4), start="07:00", end="12:00", brk=0)
    b, _ = add_entry(db, marta, work_date=D(4), start="11:00", end="15:00", brk=0)
    assert "overlap" in _open(db, a) and "overlap" in _open(db, b)

    c, _ = add_entry(db, marta, work_date=D(5), start="08:00", end="16:00", brk=30)
    d, _ = add_entry(db, marta, work_date=D(5), start="08:00", end="16:00", brk=30)
    assert "duplicate" in _open(db, c) and "duplicate" in _open(db, d)


def test_flag_gates_approval_and_ack_lands_in_audit(client, app, db):
    marta = person(db, "marta")
    uuid, _ = add_entry(db, marta, work_date=D(), start="05:00", end="23:30", brk=15)
    advance(db, uuid, "submitted", marta, "Submitted")

    admin = app.test_client()
    login(admin, "vern")
    r = admin.post("/admin/approve", data=form(app, admin, entry_uuid=uuid),
                   follow_redirects=True)
    assert b"Nothing approved" in r.data
    cur = db.execute("SELECT status FROM v_time_entry_current WHERE entry_uuid=?",
                     (uuid,)).fetchone()
    assert cur["status"] == "submitted"

    r = admin.post("/admin/approve", data=form(
        app, admin, entry_uuid=uuid,
        flags_ack_reason="verified with Marta: concrete pour ran long"),
        follow_redirects=True)
    assert b"Approved 1" in r.data
    audit = db.execute(
        "SELECT reason FROM audit_log WHERE action='entry.approve' AND entity_id=?",
        (uuid,),
    ).fetchone()
    assert "concrete pour ran long" in audit["reason"], \
        "the stated reason must be recorded in the audit log"


def test_badge_flags_do_not_gate_and_flags_autoresolve(client, app, db):
    marta = person(db, "marta")
    uuid, _ = add_entry(db, marta, work_date=D(), start="08:00", end="09:00", brk=120)
    assert "break_exceeds_duration" in _open(db, uuid)

    # worker fixes the break -> condition clears with a stated system reason
    login(client, "marta")
    j1 = db.execute("SELECT id FROM job WHERE code='J1'").fetchone()["id"]
    client.post(f"/entries/{uuid}/edit", data=form(
        app, client, work_date=D(), job_id=str(j1), start_time="08:00",
        end_time="09:00", break_minutes="15", note="", change_reason="typo in break",
    ), follow_redirects=True)
    assert "break_exceeds_duration" not in _open(db, uuid)
    resolved = db.execute(
        "SELECT resolution_reason FROM entry_flag WHERE entry_uuid=?"
        " AND flag_type='break_exceeds_duration'", (uuid,)
    ).fetchone()
    assert "Auto-resolved" in resolved["resolution_reason"]


def test_self_approval_is_flagged_and_audited(client, app, db):
    gus = person(db, "vern")
    uuid, _ = add_entry(db, gus, work_date=D())
    advance(db, uuid, "submitted", gus, "Submitted")
    admin = app.test_client()
    login(admin, "vern")
    admin.post("/admin/approve", data=form(app, admin, entry_uuid=uuid),
               follow_redirects=True)
    assert "self_approval" in {
        r["flag_type"] for r in db.execute(
            "SELECT flag_type FROM entry_flag WHERE entry_uuid=?", (uuid,))
    }
    ap = db.execute("SELECT is_self_approval FROM approval ORDER BY id DESC LIMIT 1").fetchone()
    assert ap["is_self_approval"] == 1
    au = db.execute(
        "SELECT details FROM audit_log WHERE action='entry.approve' AND entity_id=?",
        (uuid,)).fetchone()
    assert '"self_approval": true' in au["details"]
