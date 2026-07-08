"""Auth: rate limiting, session revocation on password reset (lost phone),
role separation, and the rate_bill never-shown-to-workers rule."""
from datetime import timedelta

from ghitime.db import today_local

from .conftest import PASSWORD, form, login
from .helpers import add_entry, add_rate, person


def test_login_rate_limited_after_failures(client, app, db):
    for _ in range(3):  # RATE_LIMIT_MAX_FAILURES=3 in tests
        client.post("/login", data={"username": "marta", "password": "wrong"})
    r = client.post("/login", data={"username": "marta", "password": PASSWORD})
    assert r.status_code == 429
    assert db.execute("SELECT COUNT(*) AS n FROM login_attempt WHERE success=0"
                      ).fetchone()["n"] == 3


def test_password_reset_revokes_sessions(client, app, db):
    login(client, "marta")
    assert client.get("/entries").status_code == 200

    admin = app.test_client()
    login(admin, "vern")
    pid = person(db, "marta")["id"]
    admin.post(f"/admin/people/{pid}/password",
               data=form(app, admin, temp_password="newtemp123"),
               follow_redirects=True)

    r = client.get("/entries")  # the lost phone's session is now dead
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_worker_cannot_reach_admin_or_other_entries(client, app, db):
    deshawn = person(db, "deshawn")
    other_uuid, _ = add_entry(db, deshawn,
                              work_date=(today_local() - timedelta(days=1)).isoformat())
    login(client, "marta")
    assert client.get("/admin").status_code == 403
    assert client.get("/admin/export/payroll/employees.csv").status_code == 403
    # 404, not 403: don't confirm another worker's entry uuid exists
    assert client.get(f"/entries/{other_uuid}").status_code == 404


def test_bill_rate_never_rendered_to_workers(client, app, db):
    gus = person(db, "vern")
    marta = person(db, "marta")
    add_rate(db, gus, marta["id"], 2850, "2020-01-01", table="rate_pay")
    add_rate(db, gus, marta["id"], 9999, "2020-01-01", table="rate_bill")
    login(client, "marta")
    for path in ("/", "/me/rate", "/me/record", "/entries"):
        body = client.get(path).data.decode()
        assert "99.99" not in body, f"bill rate leaked on {path}"
    assert "28.50" in client.get("/me/rate").data.decode()


def test_must_change_pw_forces_password_page(client, app, db):
    db.execute("UPDATE person SET must_change_pw=1 WHERE username='marta'")
    db.commit()
    login(client, "marta")
    r = client.get("/entries")
    assert r.status_code == 302 and "/password" in r.headers["Location"]


def test_csrf_required_on_forms(client, app, db):
    login(client, "marta")
    r = client.post("/submit", data={})  # no token
    assert r.status_code == 403
