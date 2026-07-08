"""Gate 1 smoke-validation of schema.sql: loads, and core invariants hold.

Deliberately runs WITHOUT `PRAGMA recursive_triggers=ON` so the OR-REPLACE
bypass attempts exercise the worst case (a CLI writer with default pragmas):
the *_no_replace guard triggers must hold on their own.
"""
import sqlite3, sys

db = sqlite3.connect(":memory:")
db.execute("PRAGMA foreign_keys = ON")
sql = open("migrations/001_init.sql").read()
db.executescript(sql)
print("schema loaded OK")
assert db.execute("PRAGMA recursive_triggers").fetchone()[0] == 0, "test must run with default pragmas"
assert db.execute("SELECT COUNT(*) FROM ot_policy").fetchone()[0] == 0, "ot_policy must ship empty (no policy in force)"

fails = []
def expect_abort(desc, stmt, params=()):
    try:
        db.execute(stmt, params)
        fails.append(f"NOT BLOCKED: {desc}")
        print(f"  FAIL (allowed): {desc}")
    except (sqlite3.IntegrityError, sqlite3.OperationalError) as e:
        print(f"  ok (blocked): {desc} -> {str(e).splitlines()[0][:90]}")

def expect_ok(desc, stmt, params=()):
    try:
        db.execute(stmt, params)
        print(f"  ok (allowed): {desc}")
    except Exception as e:
        fails.append(f"BLOCKED UNEXPECTEDLY: {desc} -> {e}")
        print(f"  FAIL (blocked): {desc} -> {e}")

# --- seed minimal rows ------------------------------------------------------
db.execute("INSERT INTO person (id, username, password_hash, display_name, is_admin, worker_type, created_at)"
           " VALUES (1,'noah','x','Noah Gustafson',1,'employee','2026-07-07T00:00:00Z')")
db.execute("INSERT INTO person (id, username, password_hash, display_name, worker_type, created_at, created_by)"
           " VALUES (2,'worker1','x','Test Worker','employee','2026-07-07T00:00:00Z',1)")
db.execute("INSERT INTO job (id, code, name, created_at, created_by) VALUES (1,'J100','Smith kitchen','2026-07-07T00:00:00Z',1)")

UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
TEV_COLS = ("INSERT INTO time_entry_version (entry_uuid,version_no,person_id,job_id,work_date,"
            "start_time,end_time,break_minutes,status,author_id,change_reason,server_synced_at) VALUES ")
def ins_version(vno, status, reason=None, start="08:00", end="16:00", brk=30):
    db.execute(TEV_COLS + "(?,?,2,1,'2026-07-06',?,?,?,?,2,?,'2026-07-07T01:00:00Z')",
               (UUID, vno, start, end, brk, status, reason))

print("time_entry_version:")
ins_version(1, "draft"); print("  ok (allowed): insert v1 draft")
expect_abort("UPDATE version row", "UPDATE time_entry_version SET note='hacked' WHERE version_no=1")
expect_abort("DELETE version row", "DELETE FROM time_entry_version WHERE version_no=1")
expect_abort("v2 without change_reason",
             TEV_COLS + f"('{UUID}',2,2,1,'2026-07-06','08:00','16:00',30,'draft',2,NULL,'2026-07-07T01:01:00Z')")
expect_abort("skip version_no (v5 after v1)",
             TEV_COLS + f"('{UUID}',5,2,1,'2026-07-06','08:00','16:00',30,'draft',2,'r','2026-07-07T01:01:00Z')")
expect_abort("duplicate (uuid, version_no)",
             TEV_COLS + f"('{UUID}',1,2,1,'2026-07-06','09:00','17:00',30,'draft',2,NULL,'2026-07-07T01:02:00Z')")
expect_abort("new entry with v1 status=approved",
             TEV_COLS + "('f0000000-0000-0000-0000-000000000000',1,2,1,'2026-07-06','08:00','16:00',30,'approved',2,NULL,'2026-07-07T01:00:00Z')")
expect_abort("bad time format 8:00",
             TEV_COLS + "('f0000000-0000-0000-0000-000000000001',1,2,1,'2026-07-06','8:00','16:00',30,'draft',2,NULL,'2026-07-07T01:00:00Z')")
expect_abort("bad date 2026-13-40",
             TEV_COLS + "('f0000000-0000-0000-0000-000000000002',1,2,1,'2026-13-40','08:00','16:00',30,'draft',2,NULL,'2026-07-07T01:00:00Z')")
expect_abort("break_minutes omitted (no silent default)",
             "INSERT INTO time_entry_version (entry_uuid,version_no,person_id,job_id,work_date,start_time,end_time,status,author_id,server_synced_at)"
             " VALUES ('f0000000-0000-0000-0000-000000000003',1,2,1,'2026-07-06','08:00','16:00','draft',2,'2026-07-07T01:00:00Z')")
ins_version(2, "submitted", "Submitted"); print("  ok (allowed): insert v2 submitted with reason")
expect_abort("v3 reassigning person_id (owner change forbidden)",
             TEV_COLS + f"('{UUID}',3,1,1,'2026-07-06','08:00','16:00',30,'submitted',1,'reassign attempt','2026-07-07T01:05:00Z')")
db.execute("INSERT INTO job (id, code, name, created_at, created_by) VALUES (2,'J200','Jones bath','2026-07-07T00:00:00Z',1)")
expect_ok("v3 same person, different job (job_id stays changeable)",
          TEV_COLS + f"('{UUID}',3,2,2,'2026-07-06','08:00','16:00',30,'submitted',2,'wrong job picked','2026-07-07T01:06:00Z')")

print("OR REPLACE / UPSERT bypass attempts (recursive_triggers OFF):")
expect_abort("INSERT OR REPLACE tev reusing rowid id",
             "INSERT OR REPLACE INTO time_entry_version (id,entry_uuid,version_no,person_id,job_id,work_date,start_time,end_time,break_minutes,status,author_id,change_reason,server_synced_at)"
             " VALUES (1,'f0000000-0000-0000-0000-000000000004',1,2,1,'2026-07-06','08:00','16:00',30,'draft',2,NULL,'2026-07-07T01:00:00Z')")
expect_abort("UPSERT ON CONFLICT DO UPDATE on (uuid,version)",
             "INSERT INTO time_entry_version (entry_uuid,version_no,person_id,job_id,work_date,start_time,end_time,break_minutes,status,author_id,change_reason,server_synced_at)"
             f" VALUES ('{UUID}',1,2,1,'2026-07-06','09:00','17:00',30,'draft',2,NULL,'2026-07-07T01:00:00Z')"
             " ON CONFLICT(entry_uuid, version_no) DO UPDATE SET note='hacked'")
db.execute("INSERT INTO audit_log (id, actor_id, at, action) VALUES (1,1,'2026-07-07T02:00:00Z','entry.approve')")
expect_abort("INSERT OR REPLACE audit_log id=1",
             "INSERT OR REPLACE INTO audit_log (id, actor_id, at, action) VALUES (1,1,'2026-07-07T02:00:00Z','login')")
db.execute("INSERT INTO rate_pay (id, person_id, hourly_rate_cents, effective_date, entered_by, entered_at) VALUES (1,2,2850,'2026-01-01',1,'2026-07-07T02:00:00Z')")
expect_abort("INSERT OR REPLACE rate_pay id=1",
             "INSERT OR REPLACE INTO rate_pay (id, person_id, hourly_rate_cents, effective_date, entered_by, entered_at) VALUES (1,2,100,'2026-01-01',1,'2026-07-07T02:00:00Z')")
db.execute("INSERT INTO ops_event (id, kind, at, ok) VALUES (1,'backup','2026-07-01T08:00:00Z',1)")
expect_abort("INSERT OR REPLACE ops_event (fake backup timestamp)",
             "INSERT OR REPLACE INTO ops_event (id, kind, at, ok) VALUES (1,'backup','2026-07-07T08:00:00Z',1)")
expect_abort("INSERT OR REPLACE person via username conflict",
             "INSERT OR REPLACE INTO person (username, password_hash, display_name, worker_type, created_at)"
             " VALUES ('worker1','y','Impostor','employee','2026-07-07T00:00:00Z')")
expect_abort("UPDATE OR REPLACE person onto other username",
             "UPDATE OR REPLACE person SET username='worker1' WHERE id=1")
expect_abort("person id change", "UPDATE person SET id=99 WHERE id=2")
expect_abort("INSERT OR REPLACE config key",
             "INSERT OR REPLACE INTO config (key, value) VALUES ('workweek_start_dow','1')")
expect_abort("config key rename", "UPDATE config SET key='x' WHERE key='pay_period_anchor'")
expect_abort("INSERT OR REPLACE figure_tag",
             "INSERT OR REPLACE INTO figure_tag (tag, description) VALUES ('SOURCE','rewritten')")
expect_abort("INSERT OR REPLACE job via code conflict",
             "INSERT OR REPLACE INTO job (code, name, created_at, created_by) VALUES ('J100','Evil','2026-07-07T00:00:00Z',1)")

print("views:")
row = db.execute(f"SELECT version_no, status FROM v_time_entry_current WHERE entry_uuid='{UUID}'").fetchone()
assert row == (3, "submitted"), f"current view wrong: {row}"
print(f"  ok: v_time_entry_current -> v{row[0]} {row[1]}")
row = db.execute(f"SELECT span_minutes, worked_minutes, worked_minutes_tag FROM v_time_entry_minutes WHERE entry_uuid='{UUID}'").fetchone()
assert row == (480, 450, "CALCULATED"), f"minutes wrong: {row}"
print(f"  ok: span={row[0]} worked={row[1]} tag={row[2]}")
db.execute(TEV_COLS + "('f0000000-0000-0000-0000-00000000000e',1,2,1,'2026-07-06','22:00','06:00',0,'draft',2,NULL,'2026-07-07T01:00:00Z')")
row = db.execute("SELECT span_minutes, worked_minutes FROM v_time_entry_minutes WHERE entry_uuid='f0000000-0000-0000-0000-00000000000e'").fetchone()
assert row == (None, None), f"end<=start should be NULL: {row}"
print("  ok: end<=start -> span/worked NULL (blank, flagged)")
db.execute(TEV_COLS + "('f0000000-0000-0000-0000-00000000000d',1,2,1,'2026-07-06','08:00','09:00',120,'draft',2,NULL,'2026-07-07T01:00:00Z')")
row = db.execute("SELECT span_minutes, worked_minutes FROM v_time_entry_minutes WHERE entry_uuid='f0000000-0000-0000-0000-00000000000d'").fetchone()
assert row == (60, None), f"break>span should be NULL worked: {row}"
print("  ok: break>span -> worked NULL (blank, flagged), span=60 shows derivation")
db.execute(TEV_COLS + "('f0000000-0000-0000-0000-00000000000c',1,2,1,'2026-07-06','08:00','09:00',60,'draft',2,NULL,'2026-07-07T01:00:00Z')")
row = db.execute("SELECT worked_minutes FROM v_time_entry_minutes WHERE entry_uuid='f0000000-0000-0000-0000-00000000000c'").fetchone()
assert row == (0,), f"break==span should be 0: {row}"
print("  ok: break==span -> worked 0 (arithmetic truth, not invented)")

print("other append-only tables:")
expect_abort("UPDATE audit_log", "UPDATE audit_log SET action='x'")
expect_abort("DELETE audit_log", "DELETE FROM audit_log")
expect_abort("UPDATE rate_pay", "UPDATE rate_pay SET hourly_rate_cents=1")
expect_abort("DELETE rate_pay", "DELETE FROM rate_pay")
db.execute("INSERT INTO submission (id, person_id, submitted_at) VALUES (1,2,'2026-07-07T02:00:00Z')")
expect_abort("UPDATE submission", "UPDATE submission SET submitted_at='x'")
expect_abort("DELETE submission", "DELETE FROM submission")
expect_abort("UPDATE ops_event", "UPDATE ops_event SET at='2026-07-07T08:00:00Z'")

print("approval + flags_ack enforcement:")
vid = db.execute(f"SELECT id FROM time_entry_version WHERE entry_uuid='{UUID}' AND version_no=2").fetchone()[0]
db.execute(f"INSERT INTO entry_flag (id, entry_uuid, trigger_version_id, flag_type, created_at) VALUES (1,'{UUID}',{vid},'over_16h','2026-07-07T03:00:00Z')")
db.execute("INSERT INTO approval (id, approver_id, action, created_at) VALUES (1,1,'approve','2026-07-07T03:10:00Z')")
expect_abort("approve flagged entry without flags_ack_reason",
             f"INSERT INTO approval_entry (approval_id, entry_uuid, acted_on_version_id) VALUES (1,'{UUID}',{vid})")
db.execute("INSERT INTO approval (id, approver_id, action, flags_ack_reason, created_at) VALUES (2,1,'approve','verified: long pour day','2026-07-07T03:11:00Z')")
expect_ok("approve flagged entry WITH flags_ack_reason",
          f"INSERT INTO approval_entry (approval_id, entry_uuid, acted_on_version_id) VALUES (2,'{UUID}',{vid})")
expect_abort("reject without reason", "INSERT INTO approval (approver_id, action, created_at) VALUES (1,'reject','2026-07-07T03:12:00Z')")
db.execute("INSERT INTO approval (id, approver_id, action, reason, created_at) VALUES (3,1,'reject','times look wrong','2026-07-07T03:13:00Z')")
expect_ok("reject a flagged entry needs no ack (reason already required)",
          f"INSERT INTO approval_entry (approval_id, entry_uuid, acted_on_version_id) VALUES (3,'{UUID}',{vid})")
expect_abort("UPDATE approval", "UPDATE approval SET action='approve' WHERE id=3")
# open badge-type flag must NOT gate approval
BUUID = "f0000000-0000-0000-0000-00000000000c"
bvid = db.execute(f"SELECT id FROM time_entry_version WHERE entry_uuid='{BUUID}'").fetchone()[0]
db.execute(f"INSERT INTO entry_flag (entry_uuid, trigger_version_id, flag_type, created_at) VALUES ('{BUUID}',{bvid},'self_approval','2026-07-07T03:14:00Z')")
db.execute("INSERT INTO approval (id, approver_id, action, created_at) VALUES (4,1,'approve','2026-07-07T03:15:00Z')")
expect_ok("open badge flag (self_approval) does not gate approval",
          f"INSERT INTO approval_entry (approval_id, entry_uuid, acted_on_version_id) VALUES (4,'{BUUID}',{bvid})")

print("uuid/version coherence:")
db.execute("INSERT INTO approval (id, approver_id, action, flags_ack_reason, created_at) VALUES (5,1,'approve','ack for coherence tests','2026-07-07T03:16:00Z')")
expect_abort("approval_entry uuid mismatched to acted_on_version_id",
             f"INSERT INTO approval_entry (approval_id, entry_uuid, acted_on_version_id) VALUES (5,'{BUUID}',{vid})")
expect_abort("approval_entry resulting_version_id from another entry",
             f"INSERT INTO approval_entry (approval_id, entry_uuid, acted_on_version_id, resulting_version_id) VALUES (5,'{UUID}',{vid},{bvid})")
expect_abort("approval_entry with dangling acted_on_version_id",
             f"INSERT INTO approval_entry (approval_id, entry_uuid, acted_on_version_id) VALUES (5,'{UUID}',99999)")
expect_abort("entry_flag uuid mismatched to trigger_version_id",
             f"INSERT INTO entry_flag (entry_uuid, trigger_version_id, flag_type, created_at) VALUES ('{BUUID}',{vid},'overlap','2026-07-07T03:17:00Z')")
expect_abort("entry_flag with dangling trigger_version_id",
             f"INSERT INTO entry_flag (entry_uuid, trigger_version_id, flag_type, created_at) VALUES ('{UUID}',99999,'overlap','2026-07-07T03:17:00Z')")

print("person/job/config protection:")
expect_abort("DELETE person", "DELETE FROM person WHERE id=2")
expect_abort("DELETE job", "DELETE FROM job WHERE id=1")
expect_abort("DELETE config key", "DELETE FROM config WHERE key='ot_pay_preview_enabled'")
expect_ok("deactivate person", "UPDATE person SET active=0 WHERE id=2")
expect_ok("legit username rename", "UPDATE person SET username='worker1b' WHERE id=2")
row = db.execute("SELECT COUNT(*) FROM config WHERE key='workweek_start_dow' AND value IS NULL").fetchone()
assert row[0] == 1, "workweek_start_dow must ship unset"
print("  ok: workweek_start_dow ships unset (NULL); set once at go-live, never hard-coded")

print("entry_flag partial immutability:")
expect_abort("mutate flag_type", "UPDATE entry_flag SET flag_type='overlap' WHERE id=1")
expect_abort("resolve without reason", "UPDATE entry_flag SET resolved_at='2026-07-07T04:00:00Z', resolved_by=1 WHERE id=1")
expect_ok("resolve with reason", "UPDATE entry_flag SET resolved_at='2026-07-07T04:00:00Z', resolved_by=1, resolution_reason='verified long day' WHERE id=1")
expect_abort("DELETE entry_flag", "DELETE FROM entry_flag WHERE id=1")
expect_abort("INSERT OR REPLACE entry_flag id=1",
             f"INSERT OR REPLACE INTO entry_flag (id, entry_uuid, trigger_version_id, flag_type, created_at) VALUES (1,'{UUID}',{vid},'overlap','2026-07-07T05:00:00Z')")
dvid = db.execute("SELECT id FROM time_entry_version WHERE entry_uuid='f0000000-0000-0000-0000-00000000000d'").fetchone()[0]
expect_ok("break_exceeds_duration flag type accepted",
          f"INSERT INTO entry_flag (entry_uuid, trigger_version_id, flag_type, created_at) VALUES ('f0000000-0000-0000-0000-00000000000d',{dvid},'break_exceeds_duration','2026-07-07T05:00:00Z')")

print("ot_policy (effective-dated, append-only, no partial rows):")
expect_abort("partial policy row (multiplier omitted) — absence is the only unset state",
             "INSERT INTO ot_policy (id, threshold_hours, effective_date, entered_by, entered_at) VALUES (1,40,'2026-01-05',1,'2026-07-07T06:00:00Z')")
expect_ok("append complete policy row (40h x 1.5)",
          "INSERT INTO ot_policy (id, threshold_hours, multiplier, effective_date, entered_by, entered_at) VALUES (1,40,1.5,'2026-01-05',1,'2026-07-07T06:00:00Z')")
expect_abort("UPDATE ot_policy", "UPDATE ot_policy SET threshold_hours=35 WHERE id=1")
expect_abort("DELETE ot_policy", "DELETE FROM ot_policy WHERE id=1")
expect_abort("INSERT OR REPLACE ot_policy id=1",
             "INSERT OR REPLACE INTO ot_policy (id, threshold_hours, multiplier, effective_date, entered_by, entered_at) VALUES (1,60,2.0,'2026-01-05',1,'2026-07-07T06:01:00Z')")
expect_abort("threshold_hours = 0", "INSERT INTO ot_policy (threshold_hours, multiplier, effective_date, entered_by, entered_at) VALUES (0,1.5,'2026-07-01',1,'2026-07-07T06:02:00Z')")
expect_abort("multiplier = 0", "INSERT INTO ot_policy (threshold_hours, multiplier, effective_date, entered_by, entered_at) VALUES (40,0,'2026-07-01',1,'2026-07-07T06:03:00Z')")
# same-effective-date correction: both rows preserved, latest entered_at wins in the view
db.execute("INSERT INTO ot_policy (threshold_hours, multiplier, effective_date, entered_by, entered_at) VALUES (44,1.5,'2026-01-05',1,'2026-07-07T06:04:00Z')")
row = db.execute("SELECT threshold_hours, multiplier, threshold_tag, multiplier_tag FROM v_ot_policy_effective WHERE effective_date='2026-01-05'").fetchone()
assert row == (44, 1.5, 'SOURCE', 'SOURCE'), f"effective view wrong: {row}"
assert db.execute("SELECT COUNT(*) FROM ot_policy WHERE effective_date='2026-01-05'").fetchone()[0] == 2
print(f"  ok: same-date correction -> latest entered_at wins ({row[0]}h x{row[1]}, SOURCE tags), both rows preserved")
db.execute("INSERT INTO ot_policy (threshold_hours, multiplier, effective_date, entered_by, entered_at) VALUES (40,1.5,'2026-06-01',1,'2026-07-07T06:05:00Z')")
rows = db.execute("SELECT effective_date, threshold_hours FROM v_ot_policy_effective ORDER BY effective_date").fetchall()
assert rows == [('2026-01-05', 44), ('2026-06-01', 40)], f"policy history wrong: {rows}"
print("  ok: policy change is a new effective_date row; past ranges recompute under the policy in force then")
keys = {r[0] for r in db.execute("SELECT key FROM config")}
assert 'ot_threshold_hours_per_week' not in keys and 'ot_multiplier' not in keys, keys
assert {'ot_pay_preview_enabled', 'workweek_start_dow', 'pay_period_anchor'} <= keys, keys
print("  ok: OT scalars removed from config; preview switch / workweek / anchor keys remain")

print("UNIQUE effective-date keys (identical-timestamp duplicates rejected):")
expect_abort("rate_pay duplicate (person_id, effective_date, entered_at)",
             "INSERT INTO rate_pay (person_id, hourly_rate_cents, effective_date, entered_by, entered_at) VALUES (2,3000,'2026-01-01',1,'2026-07-07T02:00:00Z')")
expect_ok("rate_pay same date, later entered_at (correction)",
          "INSERT INTO rate_pay (person_id, hourly_rate_cents, effective_date, entered_by, entered_at) VALUES (2,3000,'2026-01-01',1,'2026-07-07T02:00:01Z')")
rows = db.execute("SELECT hourly_rate_cents FROM v_rate_pay_effective WHERE person_id=2 AND effective_date='2026-01-01'").fetchall()
assert rows == [(3000,)], f"effective view must return exactly one row per date: {rows}"
print("  ok: v_rate_pay_effective returns exactly one row per (person, date) -> 3000")
db.execute("INSERT INTO rate_bill (person_id, hourly_rate_cents, effective_date, entered_by, entered_at) VALUES (2,5500,'2026-01-01',1,'2026-07-07T02:00:00Z')")
expect_abort("rate_bill duplicate (person_id, effective_date, entered_at)",
             "INSERT INTO rate_bill (person_id, hourly_rate_cents, effective_date, entered_by, entered_at) VALUES (2,6000,'2026-01-01',1,'2026-07-07T02:00:00Z')")
expect_abort("ot_policy duplicate (effective_date, entered_at)",
             "INSERT INTO ot_policy (threshold_hours, multiplier, effective_date, entered_by, entered_at) VALUES (50,2.0,'2026-01-05',1,'2026-07-07T06:04:00Z')")
expect_abort("INSERT OR REPLACE via duplicate natural key (recursive_triggers OFF)",
             "INSERT OR REPLACE INTO ot_policy (threshold_hours, multiplier, effective_date, entered_by, entered_at) VALUES (50,2.0,'2026-01-05',1,'2026-07-07T06:04:00Z')")
assert db.execute("SELECT COUNT(*) FROM v_ot_policy_effective WHERE effective_date='2026-01-05'").fetchone()[0] == 1
print("  ok: v_ot_policy_effective returns exactly one row per effective_date")

tags = [r[0] for r in db.execute("SELECT tag FROM figure_tag ORDER BY tag")]
assert tags == ['ALLOCATED','CALCULATED','ESTIMATED','EXTERNAL','SOURCE'], tags
n_tables = db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
n_views = db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='view'").fetchone()[0]
n_triggers = db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0]
print(f"\n{n_tables} tables, {n_views} views, {n_triggers} triggers")

if fails:
    print("\nFAILURES:"); [print(" -", f) for f in fails]; sys.exit(1)
print("ALL CHECKS PASSED")
