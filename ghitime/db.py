"""Database access for GHI-TIME.

One SQLite database is the sole source of truth. Every connection sets the
pragmas the schema's engine notes require — recursive_triggers in particular
is load-bearing for the append-only guarantee (see migrations/001_init.sql).
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import current_app, g

LOCAL_TZ = ZoneInfo("America/Chicago")
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def utcnow() -> str:
    """UTC ISO-8601 with microseconds and trailing Z.

    Sub-second precision is required: rate_pay/rate_bill/ot_policy carry
    UNIQUE (…, entered_at) so legitimate rapid corrections must never
    collide on the timestamp (GATE1.md design decision 7).
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def today_local() -> date:
    """Today in America/Chicago — the timezone all clock times are entered in."""
    return datetime.now(LOCAL_TZ).date()


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA recursive_triggers = ON")
    return conn


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(exc=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


_MIGRATION_RE = re.compile(r"^(\d{3})_.+\.sql$")


def pending_migrations(conn: sqlite3.Connection) -> list[tuple[int, Path]]:
    have = set()
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if row:
        have = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    out = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = _MIGRATION_RE.match(path.name)
        if not m:
            raise ValueError(f"migration filename not numbered: {path.name}")
        version = int(m.group(1))
        if version not in have:
            out.append((version, path))
    return out


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply pending numbered forward-only migrations. Returns applied names."""
    applied = []
    for version, path in pending_migrations(conn):
        conn.executescript(path.read_text())
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?,?,?)",
            (version, path.name, utcnow()),
        )
        conn.commit()
        applied.append(path.name)
    return applied


def config_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def config_set(conn: sqlite3.Connection, key: str, value: str | None, actor_id: int) -> None:
    conn.execute(
        "UPDATE config SET value=?, updated_by=?, updated_at=? WHERE key=?",
        (value, actor_id, utcnow(), key),
    )
    audit(conn, actor_id, "config.set", "config", key, None, {"value": value})


def audit(
    conn: sqlite3.Connection,
    actor_id: int | None,
    action: str,
    entity_type: str | None = None,
    entity_id: str | int | None = None,
    reason: str | None = None,
    details: dict | None = None,
) -> None:
    import json

    conn.execute(
        "INSERT INTO audit_log (actor_id, at, action, entity_type, entity_id, reason, details)"
        " VALUES (?,?,?,?,?,?,?)",
        (
            actor_id,
            utcnow(),
            action,
            entity_type,
            str(entity_id) if entity_id is not None else None,
            reason,
            json.dumps(details, sort_keys=True) if details is not None else None,
        ),
    )
