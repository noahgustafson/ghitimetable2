"""Seed data demonstrates every state and refuses to double-run."""
from ghitime.db import connect, migrate
from ghitime.seed import seed_demo


def test_seed_produces_every_state(tmp_path):
    conn = connect(str(tmp_path / "seed.db"))
    migrate(conn)
    summary = seed_demo(conn)
    assert "seeded" in summary

    statuses = {r["status"] for r in conn.execute(
        "SELECT DISTINCT status FROM v_time_entry_current")}
    assert statuses == {"draft", "submitted", "approved", "void"}

    flag_types = {r["flag_type"] for r in conn.execute(
        "SELECT DISTINCT flag_type FROM entry_flag")}
    assert {"overlap", "over_16h", "duplicate", "future_dated",
            "end_not_after_start", "break_exceeds_duration",
            "self_approval", "post_approval_correction"} <= flag_types

    assert conn.execute("SELECT COUNT(*) AS n FROM submission").fetchone()["n"] >= 2
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM approval WHERE action='approve'").fetchone()["n"] >= 3
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM approval WHERE is_self_approval=1").fetchone()["n"] >= 1
    rejected = conn.execute(
        "SELECT COUNT(*) AS n FROM time_entry_version WHERE change_reason LIKE 'Rejected:%'"
    ).fetchone()["n"]
    assert rejected >= 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM person WHERE worker_type='subcontractor'"
    ).fetchone()["n"] >= 1
    assert conn.execute("SELECT COUNT(*) AS n FROM ot_policy").fetchone()["n"] == 1

    # refuses to double-run
    assert seed_demo(conn) == "refusing: database already has people"
