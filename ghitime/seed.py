"""Seed data (fake names) demonstrating every state: drafts, submissions,
approvals, rejections, every flag type, a post-approval correction, a voided
entry, a subcontractor, rates, and an OT policy. Dev/demo only — refuses to
run on a database that already has people.
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta

from argon2 import PasswordHasher

from . import flags as flag_mod
from .db import audit, today_local, utcnow
from .lifecycle import insert_version, new_uuid

PW = PasswordHasher().hash("password123")  # every seed account; forced change off for demo


def seed_demo(conn: sqlite3.Connection) -> str:
    if conn.execute("SELECT COUNT(*) AS n FROM person").fetchone()["n"]:
        return "refusing: database already has people"

    now = utcnow()
    people = [
        # username, display, worker, admin, type
        ("vern", "Vern Ostrander (owner)", 1, 1, "employee"),
        ("marta", "Marta Vlasek", 1, 0, "employee"),
        ("deshawn", "DeShawn Pratt", 1, 0, "employee"),
        ("ollie", "Ollie Trask (sub)", 1, 0, "subcontractor"),
        ("pia", "Pia Lindqvist (inactive)", 1, 0, "employee"),
    ]
    ids = {}
    for u, d, w, a, t in people:
        cur = conn.execute(
            "INSERT INTO person (username, password_hash, display_name, is_worker,"
            " is_admin, worker_type, active, must_change_pw, created_at)"
            " VALUES (?,?,?,?,?,?,1,0,?)",
            (u, PW, d, w, a, t, now),
        )
        ids[u] = cur.lastrowid
    conn.execute("UPDATE person SET active=0 WHERE id=?", (ids["pia"],))

    jobs = [("KIT-14", "Kowalski kitchen remodel"), ("BATH-7", "Ferris bath"),
            ("DECK-3", "Alvarez deck (completed)")]
    jids = {}
    for code, name in jobs:
        cur = conn.execute(
            "INSERT INTO job (code, name, status, created_at, created_by)"
            " VALUES (?,?, 'active', ?, ?)", (code, name, now, ids["vern"]))
        jids[code] = cur.lastrowid
    conn.execute("UPDATE job SET status='completed' WHERE id=?", (jids["DECK-3"],))

    # rates: marta+deshawn have pay rates (marta got a raise); deshawn has a
    # bill rate; OLLIE (sub) and GUS have NO pay rate -> blank+flag paths
    t = today_local()
    conn.execute("INSERT INTO rate_pay (person_id, hourly_rate_cents, effective_date,"
                 " entered_by, entered_at) VALUES (?,?,?,?,?)",
                 (ids["marta"], 2600, (t - timedelta(days=200)).isoformat(), ids["vern"], utcnow()))
    conn.execute("INSERT INTO rate_pay (person_id, hourly_rate_cents, effective_date,"
                 " entered_by, entered_at) VALUES (?,?,?,?,?)",
                 (ids["marta"], 2850, (t - timedelta(days=30)).isoformat(), ids["vern"], utcnow()))
    conn.execute("INSERT INTO rate_pay (person_id, hourly_rate_cents, effective_date,"
                 " entered_by, entered_at) VALUES (?,?,?,?,?)",
                 (ids["deshawn"], 2400, (t - timedelta(days=100)).isoformat(), ids["vern"], utcnow()))
    conn.execute("INSERT INTO rate_bill (person_id, hourly_rate_cents, effective_date,"
                 " entered_by, entered_at) VALUES (?,?,?,?,?)",
                 (ids["deshawn"], 6500, (t - timedelta(days=100)).isoformat(), ids["vern"], utcnow()))

    # OT policy in force since 90 days ago (40h x 1.5); workweek Monday
    conn.execute("INSERT INTO ot_policy (threshold_hours, multiplier, effective_date,"
                 " entered_by, entered_at) VALUES (40, 1.5, ?, ?, ?)",
                 ((t - timedelta(days=90)).isoformat(), ids["vern"], utcnow()))
    conn.execute("UPDATE config SET value='0', updated_by=?, updated_at=?"
                 " WHERE key='workweek_start_dow'", (ids["vern"], utcnow()))

    person_rows = {u: conn.execute("SELECT * FROM person WHERE id=?", (ids[u],)).fetchone()
                   for u in ids}

    def entry(user, job, days_ago, start, end, brk, note=None, status_chain=()):
        uuid = new_uuid()
        vid = insert_version(
            conn, entry_uuid=uuid, person_id=ids[user], job_id=jids[job],
            work_date=(t - timedelta(days=days_ago)).isoformat(),
            start_time=start, end_time=end, break_minutes=brk, note=note,
            status="draft", author=person_rows[user], change_reason=None,
        )
        row = conn.execute("SELECT * FROM time_entry_version WHERE id=?", (vid,)).fetchone()
        flag_mod.recompute_for_version(conn, row)
        for status, author_u, reason in status_chain:
            cv = conn.execute("SELECT * FROM v_time_entry_current WHERE entry_uuid=?",
                              (uuid,)).fetchone()
            vid = insert_version(
                conn, entry_uuid=uuid, person_id=cv["person_id"], job_id=cv["job_id"],
                work_date=cv["work_date"], start_time=cv["start_time"],
                end_time=cv["end_time"], break_minutes=cv["break_minutes"],
                note=cv["note"], status=status, author=person_rows[author_u],
                change_reason=reason,
            )
            row = conn.execute("SELECT * FROM time_entry_version WHERE id=?", (vid,)).fetchone()
            flag_mod.recompute_for_version(conn, row)
        return uuid, vid

    # plain drafts
    entry("marta", "KIT-14", 1, "07:30", "16:00", 30, "cabinet install")
    entry("deshawn", "BATH-7", 1, "08:00", "15:30", 30)

    # submitted awaiting approval (with submission rows)
    for user, job, days in (("marta", "KIT-14", 3), ("deshawn", "BATH-7", 3)):
        uuid, vid = entry(user, job, days, "07:00", "15:30", 30,
                          status_chain=[("submitted", user, "Submitted")])
        cur = conn.execute("INSERT INTO submission (person_id, submitted_at) VALUES (?,?)",
                           (ids[user], utcnow()))
        conn.execute("INSERT INTO submission_entry (submission_id, time_entry_version_id)"
                     " VALUES (?,?)", (cur.lastrowid, vid))

    # approved entries (a full week for marta incl. an OT week)
    def approve(uuid, acted_vid, approver="vern", ack=None):
        cv = conn.execute("SELECT * FROM v_time_entry_current WHERE entry_uuid=?",
                          (uuid,)).fetchone()
        ap = conn.execute(
            "INSERT INTO approval (approver_id, action, flags_ack_reason,"
            " is_self_approval, created_at) VALUES (?,?,?,?,?)",
            (ids[approver], "approve", ack,
             1 if cv["person_id"] == ids[approver] else 0, utcnow()))
        vid2 = insert_version(
            conn, entry_uuid=uuid, person_id=cv["person_id"], job_id=cv["job_id"],
            work_date=cv["work_date"], start_time=cv["start_time"], end_time=cv["end_time"],
            break_minutes=cv["break_minutes"], note=cv["note"], status="approved",
            author=person_rows[approver], change_reason="Approved",
        )
        conn.execute(
            "INSERT INTO approval_entry (approval_id, entry_uuid, acted_on_version_id,"
            " resulting_version_id) VALUES (?,?,?,?)", (ap.lastrowid, uuid, cv["id"], vid2))
        if cv["person_id"] == ids[approver]:
            flag_mod.raise_flag(conn, uuid, vid2, "self_approval")
        audit(conn, ids[approver], "entry.approve", "time_entry", uuid, ack)
        return vid2

    # last full Monday-week: marta 5x10h -> 50h => 10 OT hours
    monday = t - timedelta(days=t.weekday() + 7)
    approved_uuids = []
    for i in range(5):
        uuid, vid = entry("marta", "KIT-14", (t - (monday + timedelta(days=i))).days,
                          "06:30", "17:00", 30,
                          status_chain=[("submitted", "marta", "Submitted")])
        approve(uuid, vid)
        approved_uuids.append(uuid)

    # owner logs + SELF-APPROVES his own hours (flagged + badged)
    uuid, vid = entry("vern", "BATH-7", 4, "09:00", "13:00", 0,
                      status_chain=[("submitted", "vern", "Submitted")])
    approve(uuid, vid, approver="vern")

    # subcontractor approved hours (separate payroll file)
    uuid, vid = entry("ollie", "KIT-14", 5, "08:00", "16:00", 30,
                      status_chain=[("submitted", "ollie", "Submitted")])
    approve(uuid, vid)

    # rejection path: submitted -> rejected back to draft with reason
    entry("deshawn", "KIT-14", 6, "07:00", "19:30", 0,
          status_chain=[("submitted", "deshawn", "Submitted"),
                        ("draft", "vern", "Rejected: lunch break missing — add it")])

    # post-approval correction (badged)
    uuid, vid = entry("deshawn", "BATH-7", 8, "08:00", "16:00", 30,
                      status_chain=[("submitted", "deshawn", "Submitted")])
    approve(uuid, vid)
    cv = conn.execute("SELECT * FROM v_time_entry_current WHERE entry_uuid=?", (uuid,)).fetchone()
    vid3 = insert_version(
        conn, entry_uuid=uuid, person_id=cv["person_id"], job_id=cv["job_id"],
        work_date=cv["work_date"], start_time="08:00", end_time="15:00",
        break_minutes=30, note=cv["note"], status="approved",
        author=person_rows["vern"],
        change_reason="Post-approval correction: site closed early, confirmed with DeShawn",
    )
    flag_mod.raise_flag(conn, uuid, vid3, "post_approval_correction",
                        {"reason": "site closed early"})
    audit(conn, ids["vern"], "entry.correct_post_approval", "time_entry", uuid,
          "site closed early, confirmed with DeShawn")

    # voided entry (must appear in NO totals/reports/exports)
    entry("marta", "KIT-14", 9, "07:00", "15:00", 30, "duplicate day, voided",
          status_chain=[("void", "marta", "Entered twice by mistake")])

    # flag zoo: overlap pair, >16h, future-dated, end<=start, break>span, duplicate pair
    entry("deshawn", "KIT-14", 2, "07:00", "12:00", 0)
    entry("deshawn", "BATH-7", 2, "11:00", "15:00", 0)          # overlap with above
    entry("marta", "KIT-14", 10, "05:00", "23:30", 15)          # over 16h
    entry("marta", "KIT-14", -2, "08:00", "12:00", 0)           # future-dated
    entry("deshawn", "BATH-7", 11, "22:00", "06:00", 0)         # end<=start
    entry("deshawn", "BATH-7", 12, "08:00", "09:00", 120)       # break exceeds span
    entry("marta", "BATH-7", 13, "08:00", "16:00", 30)
    entry("marta", "BATH-7", 13, "08:00", "16:00", 30)          # duplicate pair

    conn.commit()
    n = conn.execute("SELECT COUNT(*) AS n FROM time_entry_version").fetchone()["n"]
    f = conn.execute("SELECT COUNT(*) AS n FROM entry_flag").fetchone()["n"]
    return (f"seeded {len(people)} people, {len(jobs)} jobs, {n} entry versions, "
            f"{f} flags. All seed logins use password 'password123'.")
