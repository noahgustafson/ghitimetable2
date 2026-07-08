"""Schema invariants: the full validate_schema.py proof runs as part of the
suite (immutability incl. UPDATE/DELETE and OR REPLACE/UPSERT bypasses,
reason rules, coherence, UNIQUE effective-date keys), plus migration-runner
behavior."""
import subprocess
import sys
from pathlib import Path

from ghitime.db import connect, migrate, pending_migrations

REPO = Path(__file__).resolve().parent.parent


def test_validate_schema_proof_passes():
    out = subprocess.run(
        [sys.executable, str(REPO / "validate_schema.py")],
        cwd=REPO, capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "ALL CHECKS PASSED" in out.stdout


def test_migrations_apply_once_and_are_recorded(tmp_path):
    conn = connect(str(tmp_path / "m.db"))
    applied = migrate(conn)
    assert applied and applied[0] == "001_init.sql"
    assert migrate(conn) == []  # forward-only, idempotent runner
    assert pending_migrations(conn) == []
    row = conn.execute("SELECT version, name FROM schema_migrations").fetchone()
    assert (row["version"], row["name"]) == (1, "001_init.sql")


def test_every_connection_gets_required_pragmas(tmp_path):
    conn = connect(str(tmp_path / "p.db"))
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
