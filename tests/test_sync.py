"""Offline sync: dedup + idempotency, different-payload conflict surfacing,
future-dated acceptance (never strand an entry), validation rejections."""
import uuid as uuidlib
from datetime import timedelta

from ghitime.db import today_local

from .conftest import login
from .helpers import job_id


def _hdr():
    return {"X-GHITIME": "1"}


def _entry(conn, **over):
    base = {
        "uuid": str(uuidlib.uuid4()),
        "version_no": 1,
        "job_id": job_id(conn, "J1"),
        "work_date": (today_local() - timedelta(days=1)).isoformat(),
        "start_time": "08:00",
        "end_time": "16:00",
        "break_minutes": 30,
        "note": "from phone",
        "device_created_at": "2026-07-07T12:00:00.000Z",
    }
    base.update(over)
    return base


def _post(client, entries, device="dev-1"):
    return client.post("/api/sync", json={"device_id": device, "entries": entries},
                       headers=_hdr())


def test_accept_then_idempotent_duplicate_then_conflict(client, app, db):
    login(client, "marta")
    e = _entry(db)

    r = _post(client, [e])
    assert r.status_code == 200
    res = r.get_json()["results"][0]
    assert res["result"] == "accepted" and res["status"] == "draft"

    # identical resubmission (retry after lost response) is a no-op
    r = _post(client, [e])
    res = r.get_json()["results"][0]
    assert res["result"] == "duplicate"
    n = db.execute("SELECT COUNT(*) AS n FROM time_entry_version WHERE entry_uuid=?",
                   (e["uuid"],)).fetchone()["n"]
    assert n == 1

    # same key, different payload => surfaced conflict; server state wins
    changed = dict(e, end_time="17:00")
    r = _post(client, [changed])
    res = r.get_json()["results"][0]
    assert res["result"] == "conflict"
    row = db.execute("SELECT * FROM sync_conflict WHERE entry_uuid=?", (e["uuid"],)).fetchone()
    assert row is not None and row["resolved_at"] is None
    assert '"17:00"' in row["conflicting_payload"]  # device copy preserved verbatim
    stored = db.execute("SELECT end_time FROM v_time_entry_current WHERE entry_uuid=?",
                        (e["uuid"],)).fetchone()
    assert stored["end_time"] == "16:00"  # server copy untouched

    counts = db.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
    assert counts["conflict_count"] == 1


def test_future_dated_entry_is_accepted_and_flagged(client, app, db):
    login(client, "marta")
    e = _entry(db, work_date=(today_local() + timedelta(days=2)).isoformat())
    r = _post(client, [e])
    res = r.get_json()["results"][0]
    assert res["result"] == "accepted", "a wrong device clock must never strand an entry"
    assert "future_dated" in res["flags"]
    fl = db.execute(
        "SELECT flag_type FROM v_open_flags WHERE entry_uuid=?", (e["uuid"],)
    ).fetchall()
    assert "future_dated" in {f["flag_type"] for f in fl}


def test_validation_rejections(client, app, db):
    login(client, "marta")
    cases = [
        (_entry(db, start_time="8:00"), "start_time"),
        (_entry(db, work_date="2026-13-40"), "work_date"),
        (_entry(db, break_minutes=None), "break_minutes"),   # never defaulted
        (_entry(db, job_id=999), "unknown job"),
        (_entry(db, version_no=2), "version_no 1 only"),
        (_entry(db, uuid="NOT-A-UUID"), "uuid"),
    ]
    r = _post(client, [c[0] for c in cases])
    results = r.get_json()["results"]
    for (payload, needle), res in zip(cases, results):
        assert res["result"] == "rejected", (needle, res)
        assert needle.split()[0] in res["reason"], (needle, res["reason"])


def test_uuid_of_other_person_rejected(client, app, db):
    login(client, "marta")
    e = _entry(db)
    _post(client, [e])
    client2 = app.test_client()
    login(client2, "deshawn")
    r = _post(client2, [e], device="dev-2")
    assert r.get_json()["results"][0]["result"] == "rejected"


def test_sync_state_reconciles_outbox(client, app, db):
    login(client, "marta")
    e = _entry(db)
    _post(client, [e])
    r = client.get("/api/sync/state", headers=_hdr())
    state = r.get_json()["entries"]
    assert state[e["uuid"]] == {"version_no": 1, "status": "draft"}


def test_jobs_endpoint_carries_as_of_and_only_active(client, app, db):
    db.execute("UPDATE job SET status='completed' WHERE code='J2'")
    db.commit()
    login(client, "marta")
    r = client.get("/api/jobs", headers=_hdr())
    data = r.get_json()
    assert data["as_of"]
    assert [j["code"] for j in data["jobs"]] == ["J1"]


def test_api_requires_custom_header(client, app, db):
    login(client, "marta")
    r = client.post("/api/sync", json={"entries": []})
    assert r.status_code == 403
