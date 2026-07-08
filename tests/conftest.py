import hashlib
import hmac

import pytest
from argon2 import PasswordHasher

from ghitime import create_app
from ghitime.db import connect, migrate, utcnow

PASSWORD = "testpass123"
PW_HASH = PasswordHasher().hash(PASSWORD)  # hash once; argon2 is deliberately slow


@pytest.fixture()
def app(tmp_path):
    app = create_app({
        "DATABASE": str(tmp_path / "t.db"),
        "TESTING": True,
        "RATE_LIMIT_MAX_FAILURES": 3,
        "SECRET_KEY": "test-secret",
    })
    conn = connect(app.config["DATABASE"])
    migrate(conn)
    now = utcnow()
    people = [
        ("gus", "Gus Admin", 1, 1, "employee"),
        ("marta", "Marta Worker", 1, 0, "employee"),
        ("deshawn", "DeShawn Worker", 1, 0, "employee"),
        ("ollie", "Ollie Sub", 1, 0, "subcontractor"),
    ]
    for u, d, w, a, t in people:
        conn.execute(
            "INSERT INTO person (username, password_hash, display_name, is_worker,"
            " is_admin, worker_type, active, must_change_pw, created_at)"
            " VALUES (?,?,?,?,?,?,1,0,?)",
            (u, PW_HASH, d, w, a, t, now),
        )
    for code, name in (("J1", "Test kitchen"), ("J2", "Test bath")):
        conn.execute(
            "INSERT INTO job (code, name, status, created_at, created_by)"
            " VALUES (?,?, 'active', ?, 1)",
            (code, name, now),
        )
    conn.commit()
    conn.close()
    return app


@pytest.fixture()
def db(app):
    conn = connect(app.config["DATABASE"])
    yield conn
    conn.close()


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client, username, password=PASSWORD):
    r = client.post("/login", data={"username": username, "password": password})
    assert r.status_code in (302, 303), r.data
    return client


def csrf_for(app, client):
    tok = client.get_cookie("ghitime_session").value
    return hmac.new(app.secret_key.encode(), tok.encode(), hashlib.sha256).hexdigest()


def form(app, client, **fields):
    """Form payload with the CSRF token attached."""
    return {"_csrf": csrf_for(app, client), **fields}
