"""Session auth. Tailnet restricts transport; app auth is still mandatory
(lost-phone case). Sessions are server-side rows so a password reset actually
revokes a lost phone's session. Login attempts are rate-limited durably via
the login_attempt table (survives restarts).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session as flask_session,  # unused; sessions are server-side rows
    url_for,
)

from .db import audit, get_db, utcnow

bp = Blueprint("auth", __name__)
hasher = PasswordHasher()

SESSION_COOKIE = "ghitime_session"
SESSION_DAYS = 30  # resolved question 3: 30-day rolling, revoked on reset
RATE_LIMIT_WINDOW_MIN = 15
RATE_LIMIT_MAX_FAILURES = 8


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def create_session(conn: sqlite3.Connection, person_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = utcnow()
    conn.execute(
        "INSERT INTO session (token_hash, person_id, created_at, last_seen_at, expires_at)"
        " VALUES (?,?,?,?,?)",
        (_hash_token(token), person_id, now, now, _expiry()),
    )
    return token


def revoke_sessions(conn: sqlite3.Connection, person_id: int, except_token: str | None = None):
    keep = _hash_token(except_token) if except_token else None
    conn.execute(
        "UPDATE session SET revoked_at=? WHERE person_id=? AND revoked_at IS NULL"
        " AND (? IS NULL OR token_hash<>?)",
        (utcnow(), person_id, keep, keep),
    )


def load_current_user() -> None:
    g.user = None
    g.session_token = None
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return
    conn = get_db()
    row = conn.execute(
        "SELECT s.token_hash, s.expires_at, s.last_seen_at, p.*"
        " FROM session s JOIN person p ON p.id = s.person_id"
        " WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at > ?",
        (_hash_token(token), utcnow()),
    ).fetchone()
    if row is None or not row["active"]:
        return
    g.user = row
    g.session_token = token
    # rolling expiry, refreshed at most hourly to keep writes low
    if row["last_seen_at"] < (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ"):
        conn.execute(
            "UPDATE session SET last_seen_at=?, expires_at=? WHERE token_hash=?",
            (utcnow(), _expiry(), row["token_hash"]),
        )
        conn.commit()


def csrf_token() -> str:
    if not g.get("session_token"):
        return ""
    return hmac.new(
        current_app.secret_key.encode(), g.session_token.encode(), hashlib.sha256
    ).hexdigest()


def check_csrf() -> None:
    """Form POSTs carry _csrf; /api/* JSON carries the X-GHITIME header
    (custom headers cannot be sent cross-site without CORS preflight, and the
    capture module must work offline where a stale form token would strand
    the outbox)."""
    if request.method not in ("POST", "PUT", "DELETE"):
        return
    if request.path.startswith("/api/"):
        if request.headers.get("X-GHITIME") != "1":
            abort(403, description="missing X-GHITIME header")
        return
    if request.path == "/login":
        return
    supplied = request.form.get("_csrf", "")
    if not supplied or not hmac.compare_digest(supplied, csrf_token()):
        abort(403, description="bad CSRF token")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            if request.path.startswith("/api/"):
                return {"error": "login required"}, 401
            return redirect(url_for("auth.login", next=request.path))
        if g.user["must_change_pw"] and request.endpoint not in (
            "auth.password", "auth.logout", "static"
        ):
            return redirect(url_for("auth.password"))
        return view(*args, **kwargs)

    return wrapped


def worker_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not g.user["is_worker"]:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not g.user["is_admin"]:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def _rate_limited(conn: sqlite3.Connection, username: str, addr: str | None) -> bool:
    since = (
        datetime.now(timezone.utc) - timedelta(minutes=RATE_LIMIT_WINDOW_MIN)
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    limit = current_app.config.get("RATE_LIMIT_MAX_FAILURES", RATE_LIMIT_MAX_FAILURES)
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM login_attempt"
        " WHERE success=0 AND attempted_at>? AND (username_tried=? OR remote_addr=?)",
        (since, username, addr or ""),
    ).fetchone()
    return row["n"] >= limit


@bp.get("/login")
def login():
    if g.user:
        return redirect(url_for("entries.home"))
    return render_template("login.html")


@bp.post("/login")
def login_post():
    conn = get_db()
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    addr = request.remote_addr

    if _rate_limited(conn, username, addr):
        audit(conn, None, "auth.rate_limited", "person", username)
        conn.commit()
        return render_template("login.html", error="Too many attempts. Wait 15 minutes."), 429

    row = conn.execute(
        "SELECT * FROM person WHERE username=? AND active=1", (username,)
    ).fetchone()
    ok = False
    if row is not None:
        try:
            ok = hasher.verify(row["password_hash"], password)
        except VerifyMismatchError:
            ok = False
    conn.execute(
        "INSERT INTO login_attempt (username_tried, remote_addr, attempted_at, success)"
        " VALUES (?,?,?,?)",
        (username, addr, utcnow(), 1 if ok else 0),
    )
    if not ok:
        conn.commit()
        return render_template("login.html", error="Wrong username or password."), 401

    token = create_session(conn, row["id"])
    audit(conn, row["id"], "auth.login", "person", row["id"])
    conn.commit()
    resp = redirect(url_for("entries.home"))
    resp.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        samesite="Lax",
        secure=current_app.config.get("COOKIE_SECURE", False),
    )
    return resp


@bp.post("/logout")
def logout():
    if g.user:
        conn = get_db()
        conn.execute(
            "UPDATE session SET revoked_at=? WHERE token_hash=?",
            (utcnow(), _hash_token(g.session_token)),
        )
        audit(conn, g.user["id"], "auth.logout", "person", g.user["id"])
        conn.commit()
    resp = redirect(url_for("auth.login"))
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@bp.get("/password")
@login_required
def password():
    return render_template("password.html", forced=bool(g.user["must_change_pw"]))


@bp.post("/password")
@login_required
def password_post():
    conn = get_db()
    current = request.form.get("current") or ""
    new = request.form.get("new") or ""
    if len(new) < 8:
        return render_template("password.html", error="New password: 8+ characters."), 400
    try:
        hasher.verify(g.user["password_hash"], current)
    except VerifyMismatchError:
        return render_template("password.html", error="Current password is wrong."), 401
    conn.execute(
        "UPDATE person SET password_hash=?, must_change_pw=0 WHERE id=?",
        (hasher.hash(new), g.user["id"]),
    )
    revoke_sessions(conn, g.user["id"], except_token=g.session_token)
    audit(conn, g.user["id"], "auth.password_change", "person", g.user["id"])
    conn.commit()
    flash("Password changed. Other devices were signed out.")
    return redirect(url_for("entries.home"))


@bp.post("/theme")
@login_required
def theme():
    """Per-device theme cookie; server renders the class (design decision 13)."""
    choice = request.form.get("theme")
    resp = redirect(request.form.get("back") or url_for("entries.home"))
    if choice in ("light", "dark"):
        resp.set_cookie("ghitime_theme", choice, max_age=365 * 86400, samesite="Lax")
    else:
        resp.delete_cookie("ghitime_theme")  # back to OS preference
    return resp
