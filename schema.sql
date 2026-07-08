-- ============================================================================
-- GHI-TIME — schema.sql (GATE 1 DELIVERABLE — for operator review)
-- Payroll-PREP time tracking for Gustafson Home Improvements.
-- AI-assisted (Claude Code); to be reviewed by Noah Gustafson before use.
--
-- On approval this file ships verbatim as migrations/001_init.sql.
-- Migrations are numbered and forward-only; this file is never edited after
-- it ships — changes arrive as 002_*.sql, 003_*.sql, ...
--
-- Engine notes (set by the app / migration runner at connection time, since
-- they are not part of the schema itself):
--   PRAGMA journal_mode = WAL;        -- persistent once set on the db file
--   PRAGMA foreign_keys = ON;         -- per-connection; runner + app set it
--   PRAGMA busy_timeout = 5000;
--   PRAGMA recursive_triggers = ON;   -- REQUIRED on EVERY connection that
--     writes (app, migration runner, cron/CLI scripts): without it SQLite
--     skips DELETE triggers during INSERT OR REPLACE conflict resolution,
--     which would let OR REPLACE silently rewrite append-only rows.
--     Defense in depth: the *_no_replace guard triggers below abort any
--     INSERT that would displace an existing row, so the append-only
--     guarantee holds even on a connection that forgot this pragma.
--
-- Time conventions:
--   *_at columns        TEXT, UTC, ISO-8601 with trailing 'Z' (server clock)
--   work_date           TEXT 'YYYY-MM-DD'  — as entered (America/Chicago)
--   start_time/end_time TEXT 'HH:MM' 24h   — as entered (America/Chicago)
--   device_created_at   client clock, informational only; never authoritative
--
-- Money convention: integer cents (hourly_rate_cents). No floats anywhere.
--
-- Figure rules baked in here:
--   * figure_tag is the closed vocabulary (SOURCE|CALCULATED|ALLOCATED|
--     ESTIMATED|EXTERNAL). Stored as-entered quantities carry a CHECK-pinned
--     'SOURCE' tag column; derived values appear only in views/exports with a
--     'CALCULATED' tag column and NULL (blank, to be flagged in UI) when any
--     input is missing. Nothing in V1 is ALLOCATED or ESTIMATED.
--   * Append-only tables are enforced with BEFORE UPDATE / BEFORE DELETE
--     RAISE(ABORT) triggers, plus *_no_replace BEFORE INSERT guards that
--     block the INSERT OR REPLACE / UPSERT bypass (proven by pytest in
--     Gate 2, including OR REPLACE attempts with recursive_triggers OFF).
--   * config values ship UNSET (NULL) — never defaulted in schema or code.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- migration bookkeeping (owned by the runner)
-- ---------------------------------------------------------------------------
CREATE TABLE schema_migrations (
    version     INTEGER PRIMARY KEY,            -- e.g. 1 for 001_init.sql
    name        TEXT    NOT NULL,
    applied_at  TEXT    NOT NULL                -- UTC
);

-- ---------------------------------------------------------------------------
-- figure_tag — closed vocabulary for every money/quantity value in the system
-- ---------------------------------------------------------------------------
CREATE TABLE figure_tag (
    tag         TEXT PRIMARY KEY,
    description TEXT NOT NULL
) WITHOUT ROWID;

INSERT INTO figure_tag (tag, description) VALUES
    ('SOURCE',     'Entered by a person; stored exactly as entered'),
    ('CALCULATED', 'Derived from SOURCE values; derivation available'),
    ('ALLOCATED',  'Split/apportioned by a stated method (unused in V1)'),
    ('ESTIMATED',  'Best-effort figure by a stated method (unused in V1)'),
    ('EXTERNAL',   'Provided by an outside system/party (unused in V1)');

CREATE TRIGGER trg_figure_tag_no_replace
BEFORE INSERT ON figure_tag
BEGIN
    SELECT CASE WHEN EXISTS (SELECT 1 FROM figure_tag WHERE tag = NEW.tag)
        THEN RAISE(ABORT, 'figure_tag: tag already exists') END;
END;

-- ---------------------------------------------------------------------------
-- person — crew roster. NO HARD DELETES (seasonal roster: deactivate instead).
-- One account may hold both roles (owner is worker + admin).
-- ---------------------------------------------------------------------------
CREATE TABLE person (
    id             INTEGER PRIMARY KEY,
    username       TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash  TEXT    NOT NULL,               -- argon2id via app
    display_name   TEXT    NOT NULL,
    is_worker      INTEGER NOT NULL DEFAULT 1 CHECK (is_worker  IN (0,1)),
    is_admin       INTEGER NOT NULL DEFAULT 0 CHECK (is_admin   IN (0,1)),
    worker_type    TEXT    NOT NULL CHECK (worker_type IN ('employee','subcontractor')),
    active         INTEGER NOT NULL DEFAULT 1 CHECK (active     IN (0,1)),
    must_change_pw INTEGER NOT NULL DEFAULT 1 CHECK (must_change_pw IN (0,1)),
    created_at     TEXT    NOT NULL,
    created_by     INTEGER REFERENCES person(id)   -- NULL only for bootstrap admin
);

CREATE TRIGGER trg_person_no_delete
BEFORE DELETE ON person
BEGIN
    SELECT RAISE(ABORT, 'person rows are never deleted; set active = 0 instead');
END;

-- Blocks INSERT OR REPLACE from displacing an existing row via id or
-- username conflict resolution (which would silently delete the victim row).
CREATE TRIGGER trg_person_no_replace
BEFORE INSERT ON person
BEGIN
    SELECT CASE WHEN NEW.id IS NOT NULL
                 AND EXISTS (SELECT 1 FROM person WHERE id = NEW.id)
        THEN RAISE(ABORT, 'person: INSERT would replace an existing row') END;
    SELECT CASE WHEN EXISTS (SELECT 1 FROM person WHERE username = NEW.username)
        THEN RAISE(ABORT, 'person: username already exists') END;
END;

CREATE TRIGGER trg_person_update_guard
BEFORE UPDATE ON person
BEGIN
    SELECT CASE WHEN NEW.id IS NOT OLD.id
        THEN RAISE(ABORT, 'person: id is immutable') END;
    SELECT CASE WHEN EXISTS (SELECT 1 FROM person
                              WHERE username = NEW.username AND id <> OLD.id)
        THEN RAISE(ABORT, 'person: username already exists') END;
END;

-- ---------------------------------------------------------------------------
-- job — admin-created. Only status='active' jobs sync to the offline picker.
-- ---------------------------------------------------------------------------
CREATE TABLE job (
    id         INTEGER PRIMARY KEY,
    code       TEXT    NOT NULL UNIQUE COLLATE NOCASE,   -- short code for phones
    name       TEXT    NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active','completed')),
    created_at TEXT    NOT NULL,
    created_by INTEGER NOT NULL REFERENCES person(id)
);

CREATE TRIGGER trg_job_no_delete
BEFORE DELETE ON job
BEGIN
    SELECT RAISE(ABORT, 'job rows are never deleted; set status = completed instead');
END;

CREATE TRIGGER trg_job_no_replace
BEFORE INSERT ON job
BEGIN
    SELECT CASE WHEN NEW.id IS NOT NULL
                 AND EXISTS (SELECT 1 FROM job WHERE id = NEW.id)
        THEN RAISE(ABORT, 'job: INSERT would replace an existing row') END;
    SELECT CASE WHEN EXISTS (SELECT 1 FROM job WHERE code = NEW.code)
        THEN RAISE(ABORT, 'job: code already exists') END;
END;

CREATE TRIGGER trg_job_update_guard
BEFORE UPDATE ON job
BEGIN
    SELECT CASE WHEN NEW.id IS NOT OLD.id
        THEN RAISE(ABORT, 'job: id is immutable') END;
    SELECT CASE WHEN EXISTS (SELECT 1 FROM job
                              WHERE code = NEW.code AND id <> OLD.id)
        THEN RAISE(ABORT, 'job: code already exists') END;
END;

-- ---------------------------------------------------------------------------
-- time_entry_version — APPEND-ONLY. The core table.
--
-- Current state of an entry = the row with MAX(version_no) for its entry_uuid
-- (see v_time_entry_current). History is always queryable. Rows are never
-- updated or deleted — triggers below abort both; pytest proves it in Gate 2.
--
-- Every status change is itself a new version (submit, approve, reject-to-
-- draft, void, post-approval correction), so the version chain IS the entry's
-- full story. change_reason is required on every version after the first;
-- lifecycle versions get a system-generated reason (e.g. 'Submitted',
-- 'Rejected: <admin reason>').
--
-- person_id is immutable across an entry's versions (trigger below): a
-- reassigning version would remove the entry from the original worker's
-- my-record printout. Wrong-person entries are corrected by void + new
-- entry. job_id stays changeable.
--
-- Server-side validation (app layer, Gate 2): person must be an active
-- worker; job must exist; author must be the entry's person or an admin;
-- status transitions restricted to:
--   draft     -> draft | submitted | void
--   submitted -> submitted | approved | draft (reject) | void
--   approved  -> approved (admin post-approval correction only) | void (admin)
-- v1 is forced to 'draft' by CHECK below, so a device can never sync an entry
-- into existence as submitted/approved.
-- ---------------------------------------------------------------------------
CREATE TABLE time_entry_version (
    id                INTEGER PRIMARY KEY,
    entry_uuid        TEXT    NOT NULL
                      CHECK (length(entry_uuid) = 36 AND entry_uuid = lower(entry_uuid)),
    version_no        INTEGER NOT NULL CHECK (version_no >= 1),
    person_id         INTEGER NOT NULL REFERENCES person(id),
    job_id            INTEGER NOT NULL REFERENCES job(id),
    work_date         TEXT    NOT NULL
                      -- IS, not =: date() returns NULL for invalid dates and a
                      -- NULL CHECK would silently pass
                      CHECK (work_date IS date(work_date)),         -- valid YYYY-MM-DD
    start_time        TEXT    NOT NULL
                      CHECK (start_time GLOB '[0-2][0-9]:[0-5][0-9]' AND start_time < '24:00'),
    end_time          TEXT    NOT NULL
                      CHECK (end_time   GLOB '[0-2][0-9]:[0-5][0-9]' AND end_time   < '24:00'),
    -- no DEFAULT: the break is a SOURCE figure and must be explicitly
    -- entered (0 included) — omitting it is a rejected write, never a
    -- silently invented zero
    break_minutes     INTEGER NOT NULL
                      CHECK (break_minutes BETWEEN 0 AND 1440),
    break_minutes_tag TEXT    NOT NULL DEFAULT 'SOURCE'
                      REFERENCES figure_tag(tag) CHECK (break_minutes_tag = 'SOURCE'),
    times_tag         TEXT    NOT NULL DEFAULT 'SOURCE'
                      REFERENCES figure_tag(tag) CHECK (times_tag = 'SOURCE'),
    note              TEXT,
    status            TEXT    NOT NULL CHECK (status IN ('draft','submitted','approved','void')),
    author_id         INTEGER NOT NULL REFERENCES person(id),   -- who wrote THIS version
    change_reason     TEXT
                      CHECK (version_no = 1
                             OR (change_reason IS NOT NULL AND trim(change_reason) <> '')),
    device_created_at TEXT,                                     -- client clock; informational
    server_synced_at  TEXT    NOT NULL,                         -- UTC; server clock; authoritative
    -- a device can never create an entry in any state but draft:
    CHECK (version_no > 1 OR status = 'draft'),
    UNIQUE (entry_uuid, version_no)
);

CREATE INDEX idx_tev_person_date ON time_entry_version (person_id, work_date);
CREATE INDEX idx_tev_job_date    ON time_entry_version (job_id, work_date);
CREATE INDEX idx_tev_synced      ON time_entry_version (server_synced_at);

CREATE TRIGGER trg_tev_no_update
BEFORE UPDATE ON time_entry_version
BEGIN
    SELECT RAISE(ABORT, 'time_entry_version is append-only: UPDATE forbidden');
END;

CREATE TRIGGER trg_tev_no_delete
BEFORE DELETE ON time_entry_version
BEGIN
    SELECT RAISE(ABORT, 'time_entry_version is append-only: DELETE forbidden');
END;

-- Blocks INSERT OR REPLACE from displacing an existing version row via id
-- conflict resolution (the (uuid, version_no) path is covered by the
-- contiguity trigger below).
CREATE TRIGGER trg_tev_no_replace
BEFORE INSERT ON time_entry_version
BEGIN
    SELECT CASE WHEN NEW.id IS NOT NULL
                 AND EXISTS (SELECT 1 FROM time_entry_version WHERE id = NEW.id)
        THEN RAISE(ABORT, 'time_entry_version: INSERT would replace an existing row') END;
END;

-- Version numbers must be contiguous per entry (next = max + 1).
-- NOTE: because this fires BEFORE the UNIQUE(entry_uuid, version_no)
-- constraint is checked, ANY resubmission of an existing (uuid, version_no)
-- aborts here — the UNIQUE error is unreachable and cannot be used to tell
-- duplicates from conflicts. POST /api/sync therefore discriminates with a
-- SELECT pre-check inside its write transaction:
--   row exists + identical payload  -> 'duplicate' (idempotent no-op)
--   row exists + different payload  -> sync_conflict row + 'conflict'
--   row absent                      -> INSERT (a trigger abort => 'rejected')
CREATE TRIGGER trg_tev_version_contiguous
BEFORE INSERT ON time_entry_version
BEGIN
    SELECT CASE
        WHEN NEW.version_no <> 1 + COALESCE(
                 (SELECT MAX(version_no) FROM time_entry_version
                   WHERE entry_uuid = NEW.entry_uuid), 0)
        THEN RAISE(ABORT, 'time_entry_version: version_no must be exactly max(version_no)+1')
    END;
END;

-- An entry can never change owner: every version must carry v1's person_id.
-- (A reassigning version would silently remove the entry from the original
-- worker's my-record printout.) Correct a wrong-person entry by voiding it
-- and creating a new one.
CREATE TRIGGER trg_tev_person_immutable
BEFORE INSERT ON time_entry_version
BEGIN
    SELECT CASE
        WHEN NEW.version_no > 1
         AND NEW.person_id <> (SELECT person_id FROM time_entry_version
                                WHERE entry_uuid = NEW.entry_uuid
                                  AND version_no = 1)
        THEN RAISE(ABORT, 'time_entry_version: person_id is immutable across versions; void and re-enter instead')
    END;
END;

-- ---------------------------------------------------------------------------
-- submission — attestation event ("I attest these hours are true").
-- Covers specific entry VERSIONS, so what was attested is frozen forever.
-- Append-only.
-- ---------------------------------------------------------------------------
CREATE TABLE submission (
    id           INTEGER PRIMARY KEY,
    person_id    INTEGER NOT NULL REFERENCES person(id),
    submitted_at TEXT    NOT NULL                       -- UTC
);

CREATE TABLE submission_entry (
    submission_id         INTEGER NOT NULL REFERENCES submission(id),
    time_entry_version_id INTEGER NOT NULL REFERENCES time_entry_version(id),
    PRIMARY KEY (submission_id, time_entry_version_id)
) WITHOUT ROWID;

CREATE INDEX idx_submission_person ON submission (person_id, submitted_at);

CREATE TRIGGER trg_submission_no_update BEFORE UPDATE ON submission
BEGIN SELECT RAISE(ABORT, 'submission is append-only'); END;
CREATE TRIGGER trg_submission_no_delete BEFORE DELETE ON submission
BEGIN SELECT RAISE(ABORT, 'submission is append-only'); END;
CREATE TRIGGER trg_submission_no_replace BEFORE INSERT ON submission
BEGIN
    SELECT CASE WHEN NEW.id IS NOT NULL
                 AND EXISTS (SELECT 1 FROM submission WHERE id = NEW.id)
        THEN RAISE(ABORT, 'submission: INSERT would replace an existing row') END;
END;
CREATE TRIGGER trg_submission_entry_no_update BEFORE UPDATE ON submission_entry
BEGIN SELECT RAISE(ABORT, 'submission_entry is append-only'); END;
CREATE TRIGGER trg_submission_entry_no_delete BEFORE DELETE ON submission_entry
BEGIN SELECT RAISE(ABORT, 'submission_entry is append-only'); END;
CREATE TRIGGER trg_submission_entry_no_replace BEFORE INSERT ON submission_entry
BEGIN
    SELECT CASE WHEN EXISTS (SELECT 1 FROM submission_entry
                              WHERE submission_id = NEW.submission_id
                                AND time_entry_version_id = NEW.time_entry_version_id)
        THEN RAISE(ABORT, 'submission_entry: INSERT would replace an existing row') END;
END;

-- ---------------------------------------------------------------------------
-- approval — approve/reject events. Append-only.
-- Self-approval (approver == entry person) is permitted; the app sets
-- is_self_approval = 1, writes an audit_log row, and printouts badge it.
-- Approving any entry with an open flag requires flags_ack_reason.
-- ---------------------------------------------------------------------------
CREATE TABLE approval (
    id               INTEGER PRIMARY KEY,
    approver_id      INTEGER NOT NULL REFERENCES person(id),
    submission_id    INTEGER REFERENCES submission(id),  -- NULL for per-entry actions
    action           TEXT    NOT NULL CHECK (action IN ('approve','reject')),
    reason           TEXT
                     CHECK (action <> 'reject'
                            OR (reason IS NOT NULL AND trim(reason) <> '')),
    flags_ack_reason TEXT,          -- required when covered entries have open
                                    -- data-integrity flags — enforced by
                                    -- trg_approval_entry_flag_ack below
    is_self_approval INTEGER NOT NULL DEFAULT 0 CHECK (is_self_approval IN (0,1)),
    created_at       TEXT    NOT NULL                    -- UTC
);

CREATE TABLE approval_entry (
    approval_id          INTEGER NOT NULL REFERENCES approval(id),
    entry_uuid           TEXT    NOT NULL,
    acted_on_version_id  INTEGER NOT NULL REFERENCES time_entry_version(id),
    resulting_version_id INTEGER          REFERENCES time_entry_version(id),
    PRIMARY KEY (approval_id, entry_uuid)
) WITHOUT ROWID;

CREATE INDEX idx_approval_entry_uuid ON approval_entry (entry_uuid);

CREATE TRIGGER trg_approval_no_update BEFORE UPDATE ON approval
BEGIN SELECT RAISE(ABORT, 'approval is append-only'); END;
CREATE TRIGGER trg_approval_no_delete BEFORE DELETE ON approval
BEGIN SELECT RAISE(ABORT, 'approval is append-only'); END;
CREATE TRIGGER trg_approval_no_replace BEFORE INSERT ON approval
BEGIN
    SELECT CASE WHEN NEW.id IS NOT NULL
                 AND EXISTS (SELECT 1 FROM approval WHERE id = NEW.id)
        THEN RAISE(ABORT, 'approval: INSERT would replace an existing row') END;
END;
CREATE TRIGGER trg_approval_entry_no_update BEFORE UPDATE ON approval_entry
BEGIN SELECT RAISE(ABORT, 'approval_entry is append-only'); END;
CREATE TRIGGER trg_approval_entry_no_delete BEFORE DELETE ON approval_entry
BEGIN SELECT RAISE(ABORT, 'approval_entry is append-only'); END;
CREATE TRIGGER trg_approval_entry_no_replace BEFORE INSERT ON approval_entry
BEGIN
    SELECT CASE WHEN EXISTS (SELECT 1 FROM approval_entry
                              WHERE approval_id = NEW.approval_id
                                AND entry_uuid = NEW.entry_uuid)
        THEN RAISE(ABORT, 'approval_entry: INSERT would replace an existing row') END;
END;

-- Coherence: the approval line's entry_uuid must be the uuid of the version
-- rows it references — a mismatch would attach an approval to the wrong
-- entry's record. IS NOT so a dangling acted_on_version_id also aborts even
-- without foreign_keys enabled; resulting_version_id is checked only when
-- present (it is NULLable).
CREATE TRIGGER trg_approval_entry_uuid_coherent
BEFORE INSERT ON approval_entry
BEGIN
    SELECT CASE WHEN NEW.entry_uuid IS NOT
            (SELECT entry_uuid FROM time_entry_version
              WHERE id = NEW.acted_on_version_id)
        THEN RAISE(ABORT, 'approval_entry: entry_uuid does not match acted_on_version_id')
    END;
    SELECT CASE WHEN NEW.resulting_version_id IS NOT NULL
                 AND NEW.entry_uuid IS NOT
            (SELECT entry_uuid FROM time_entry_version
              WHERE id = NEW.resulting_version_id)
        THEN RAISE(ABORT, 'approval_entry: entry_uuid does not match resulting_version_id')
    END;
END;

-- Figure rule, schema-enforced: approving an entry that carries an OPEN
-- data-integrity flag requires a stated reason on the approval
-- (flags_ack_reason), which the app also records in the audit_log row.
-- Badge types (self_approval, post_approval_correction) are annotations,
-- not review items, and do not gate approval.
CREATE TRIGGER trg_approval_entry_flag_ack
BEFORE INSERT ON approval_entry
BEGIN
    SELECT CASE WHEN
        (SELECT action FROM approval WHERE id = NEW.approval_id) = 'approve'
        AND EXISTS (SELECT 1 FROM entry_flag
                     WHERE entry_uuid = NEW.entry_uuid
                       AND resolved_at IS NULL
                       AND flag_type IN ('overlap','over_16h','duplicate',
                                         'future_dated','end_not_after_start',
                                         'break_exceeds_duration'))
        AND COALESCE(trim((SELECT flags_ack_reason FROM approval
                            WHERE id = NEW.approval_id)), '') = ''
        THEN RAISE(ABORT, 'approval_entry: approving a flagged entry requires flags_ack_reason')
    END;
END;

-- ---------------------------------------------------------------------------
-- rate_pay — effective-dated pay rates. Append-only: a raise never rewrites
-- the past. Visible to that worker and admin (app layer).
-- Multiple rows on one effective_date are allowed; latest entered_at wins.
-- entered_at must differ (UNIQUE below): an identical-timestamp duplicate
-- would make v_rate_pay_effective return two rows for one effective_date
-- and double-count downstream. Gate 2 stores entered_at with sub-second
-- precision so legitimate rapid corrections never collide.
-- ---------------------------------------------------------------------------
CREATE TABLE rate_pay (
    id                INTEGER PRIMARY KEY,
    person_id         INTEGER NOT NULL REFERENCES person(id),
    hourly_rate_cents INTEGER NOT NULL CHECK (hourly_rate_cents >= 0),
    rate_tag          TEXT    NOT NULL DEFAULT 'SOURCE'
                      REFERENCES figure_tag(tag) CHECK (rate_tag = 'SOURCE'),
    effective_date    TEXT    NOT NULL CHECK (effective_date IS date(effective_date)),
    entered_by        INTEGER NOT NULL REFERENCES person(id),
    entered_at        TEXT    NOT NULL                   -- UTC
);

CREATE UNIQUE INDEX idx_rate_pay_person ON rate_pay (person_id, effective_date, entered_at);

CREATE TRIGGER trg_rate_pay_no_update BEFORE UPDATE ON rate_pay
BEGIN SELECT RAISE(ABORT, 'rate_pay is append-only'); END;
CREATE TRIGGER trg_rate_pay_no_delete BEFORE DELETE ON rate_pay
BEGIN SELECT RAISE(ABORT, 'rate_pay is append-only'); END;
CREATE TRIGGER trg_rate_pay_no_replace BEFORE INSERT ON rate_pay
BEGIN
    SELECT CASE WHEN NEW.id IS NOT NULL
                 AND EXISTS (SELECT 1 FROM rate_pay WHERE id = NEW.id)
        THEN RAISE(ABORT, 'rate_pay: INSERT would replace an existing row') END;
    -- also guards the UNIQUE natural key so INSERT OR REPLACE cannot
    -- displace the existing row on a connection with default pragmas
    SELECT CASE WHEN EXISTS (SELECT 1 FROM rate_pay
                              WHERE person_id = NEW.person_id
                                AND effective_date = NEW.effective_date
                                AND entered_at = NEW.entered_at)
        THEN RAISE(ABORT, 'rate_pay: duplicate (person_id, effective_date, entered_at)') END;
END;

-- ---------------------------------------------------------------------------
-- rate_bill — effective-dated billable rates. ADMIN-ONLY: never rendered on
-- any worker-facing page or worker-facing export (app layer; stated in README
-- and covered by a pytest in Gate 2). Append-only.
-- ---------------------------------------------------------------------------
CREATE TABLE rate_bill (
    id                INTEGER PRIMARY KEY,
    person_id         INTEGER NOT NULL REFERENCES person(id),
    hourly_rate_cents INTEGER NOT NULL CHECK (hourly_rate_cents >= 0),
    rate_tag          TEXT    NOT NULL DEFAULT 'SOURCE'
                      REFERENCES figure_tag(tag) CHECK (rate_tag = 'SOURCE'),
    effective_date    TEXT    NOT NULL CHECK (effective_date IS date(effective_date)),
    entered_by        INTEGER NOT NULL REFERENCES person(id),
    entered_at        TEXT    NOT NULL                   -- UTC
);

-- UNIQUE for the same reason as rate_pay: v_rate_bill_effective has the
-- same latest-entered_at tie-break and the same double-count failure mode.
CREATE UNIQUE INDEX idx_rate_bill_person ON rate_bill (person_id, effective_date, entered_at);

CREATE TRIGGER trg_rate_bill_no_update BEFORE UPDATE ON rate_bill
BEGIN SELECT RAISE(ABORT, 'rate_bill is append-only'); END;
CREATE TRIGGER trg_rate_bill_no_delete BEFORE DELETE ON rate_bill
BEGIN SELECT RAISE(ABORT, 'rate_bill is append-only'); END;
CREATE TRIGGER trg_rate_bill_no_replace BEFORE INSERT ON rate_bill
BEGIN
    SELECT CASE WHEN NEW.id IS NOT NULL
                 AND EXISTS (SELECT 1 FROM rate_bill WHERE id = NEW.id)
        THEN RAISE(ABORT, 'rate_bill: INSERT would replace an existing row') END;
    SELECT CASE WHEN EXISTS (SELECT 1 FROM rate_bill
                              WHERE person_id = NEW.person_id
                                AND effective_date = NEW.effective_date
                                AND entered_at = NEW.entered_at)
        THEN RAISE(ABORT, 'rate_bill: duplicate (person_id, effective_date, entered_at)') END;
END;

-- ---------------------------------------------------------------------------
-- ot_policy — effective-dated OT policy, on the rate_pay pattern. OT is
-- policy history, not a mutable scalar: each week's OT computes under the
-- policy in force at that week's start; weeks with no policy in force render
-- blank + flagged; OT columns in reports/exports carry the threshold value
-- applied. Changing policy is always a NEW row, so re-running a past date
-- range reproduces the figures generated under the policy in force then.
-- Ships EMPTY (no policy in force — dashboard flags "no OT policy in force —
-- bookkeeper advises"); never defaulted.
-- threshold_hours/multiplier are quantities (not money) stored exactly as
-- entered (SOURCE); app-layer derivations use decimal arithmetic.
-- No partial policy rows: threshold and multiplier are both required on
-- every row — "no policy in force" is represented only by row ABSENCE.
-- Append-only.
-- ---------------------------------------------------------------------------
CREATE TABLE ot_policy (
    id              INTEGER PRIMARY KEY,
    threshold_hours NUMERIC NOT NULL
                    CHECK (threshold_hours > 0 AND threshold_hours <= 168),
    threshold_tag   TEXT    NOT NULL DEFAULT 'SOURCE'
                    REFERENCES figure_tag(tag) CHECK (threshold_tag = 'SOURCE'),
    multiplier      NUMERIC NOT NULL
                    CHECK (multiplier > 0),
    multiplier_tag  TEXT    NOT NULL DEFAULT 'SOURCE'
                    REFERENCES figure_tag(tag) CHECK (multiplier_tag = 'SOURCE'),
    effective_date  TEXT    NOT NULL CHECK (effective_date IS date(effective_date)),
    entered_by      INTEGER NOT NULL REFERENCES person(id),
    entered_at      TEXT    NOT NULL                  -- UTC
);

-- UNIQUE: an identical-timestamp duplicate would make v_ot_policy_effective
-- return two rows for one effective_date and double-count downstream.
CREATE UNIQUE INDEX idx_ot_policy ON ot_policy (effective_date, entered_at);

CREATE TRIGGER trg_ot_policy_no_update BEFORE UPDATE ON ot_policy
BEGIN SELECT RAISE(ABORT, 'ot_policy is append-only'); END;
CREATE TRIGGER trg_ot_policy_no_delete BEFORE DELETE ON ot_policy
BEGIN SELECT RAISE(ABORT, 'ot_policy is append-only'); END;
CREATE TRIGGER trg_ot_policy_no_replace BEFORE INSERT ON ot_policy
BEGIN
    SELECT CASE WHEN NEW.id IS NOT NULL
                 AND EXISTS (SELECT 1 FROM ot_policy WHERE id = NEW.id)
        THEN RAISE(ABORT, 'ot_policy: INSERT would replace an existing row') END;
    SELECT CASE WHEN EXISTS (SELECT 1 FROM ot_policy
                              WHERE effective_date = NEW.effective_date
                                AND entered_at = NEW.entered_at)
        THEN RAISE(ABORT, 'ot_policy: duplicate (effective_date, entered_at)') END;
END;

-- ---------------------------------------------------------------------------
-- config — key/value for settings (not figures; OT policy lives in the
-- append-only ot_policy table above). Keys are seeded; value NULL means
-- UNSET — a first-class, visible state, NEVER defaulted. Config changes are
-- audit-logged by the app.
-- workweek_start_dow is set once at go-live (Monday, per operator, once
-- confirmed against payroll practice — not hard-coded here); changing it
-- mid-history is out of scope for V1 (stated in README).
-- pay_period_anchor is reserved for later (additive) and unused in V1.
-- ---------------------------------------------------------------------------
CREATE TABLE config (
    key        TEXT PRIMARY KEY,
    value      TEXT,                                    -- NULL = unset
    -- V1 ships no figure-valued config keys; the tag column exists so a
    -- future figure-valued key cannot ship untagged. Switches and anchors
    -- are settings, not figures, and carry NULL.
    value_tag  TEXT REFERENCES figure_tag(tag),
    updated_by INTEGER REFERENCES person(id),
    updated_at TEXT
) WITHOUT ROWID;

INSERT INTO config (key, value, value_tag) VALUES
    ('ot_pay_preview_enabled', '0',  NULL),   -- default off (a switch, not a
                                              -- figure); even enabled, the
                                              -- preview needs an OT policy in
                                              -- force for the period
    ('workweek_start_dow',     NULL, NULL),   -- ships UNSET; set at go-live
    ('pay_period_anchor',      NULL, NULL);   -- reserved; unused in V1

CREATE TRIGGER trg_config_no_delete BEFORE DELETE ON config
BEGIN SELECT RAISE(ABORT, 'config keys are never deleted; set value = NULL to unset'); END;

CREATE TRIGGER trg_config_no_replace BEFORE INSERT ON config
BEGIN
    SELECT CASE WHEN EXISTS (SELECT 1 FROM config WHERE key = NEW.key)
        THEN RAISE(ABORT, 'config: key exists; UPDATE its value instead') END;
END;

CREATE TRIGGER trg_config_key_immutable BEFORE UPDATE ON config
BEGIN
    SELECT CASE WHEN NEW.key IS NOT OLD.key
        THEN RAISE(ABORT, 'config: key is immutable') END;
END;

-- ---------------------------------------------------------------------------
-- entry_flag — surfaced conflicts/anomalies. Never silently reconciled.
-- Raised by the app at write/sync time against a specific entry version.
-- Types:
--   overlap             two entries for one person overlap in time
--   over_16h            worked duration exceeds 16 hours
--   duplicate           near-duplicate of another entry (same person/date/times)
--   future_dated        work_date after 'today' (America/Chicago) at sync time
--   end_not_after_start end <= start: duration is blank, never auto-corrected
--   break_exceeds_duration  break longer than the shift span: duration is
--                       blank, never auto-corrected
--   self_approval       badge: approver approved their own hours
--   post_approval_correction  badge: admin corrected an approved entry
-- Data-integrity types feed the flag review queue; badge types feed printouts
-- and exports. Core fields are immutable; only resolution fields may change.
-- ---------------------------------------------------------------------------
CREATE TABLE entry_flag (
    id                 INTEGER PRIMARY KEY,
    entry_uuid         TEXT    NOT NULL,
    trigger_version_id INTEGER NOT NULL REFERENCES time_entry_version(id),
    flag_type          TEXT    NOT NULL CHECK (flag_type IN
                         ('overlap','over_16h','duplicate','future_dated',
                          'end_not_after_start','break_exceeds_duration',
                          'self_approval','post_approval_correction')),
    detail             TEXT,          -- JSON: e.g. {"other_entry_uuid": "..."}
    created_at         TEXT    NOT NULL,                 -- UTC
    resolved_at        TEXT,
    resolved_by        INTEGER REFERENCES person(id),
    resolution_reason  TEXT
                       CHECK (resolved_at IS NULL
                              OR (resolution_reason IS NOT NULL AND trim(resolution_reason) <> ''))
);

CREATE INDEX idx_entry_flag_uuid ON entry_flag (entry_uuid);
CREATE INDEX idx_entry_flag_open ON entry_flag (flag_type) WHERE resolved_at IS NULL;

CREATE TRIGGER trg_entry_flag_no_delete BEFORE DELETE ON entry_flag
BEGIN SELECT RAISE(ABORT, 'entry_flag rows are never deleted; resolve them instead'); END;

CREATE TRIGGER trg_entry_flag_no_replace BEFORE INSERT ON entry_flag
BEGIN
    SELECT CASE WHEN NEW.id IS NOT NULL
                 AND EXISTS (SELECT 1 FROM entry_flag WHERE id = NEW.id)
        THEN RAISE(ABORT, 'entry_flag: INSERT would replace an existing row') END;
END;

-- Coherence: the flag's entry_uuid must be the uuid of the version row it
-- points at — a mismatched flag would badge (and gate approval of) the
-- wrong entry. IS NOT (not <>) so a dangling trigger_version_id also aborts
-- even on a connection without foreign_keys enabled.
CREATE TRIGGER trg_entry_flag_uuid_coherent
BEFORE INSERT ON entry_flag
BEGIN
    SELECT CASE WHEN NEW.entry_uuid IS NOT
            (SELECT entry_uuid FROM time_entry_version
              WHERE id = NEW.trigger_version_id)
        THEN RAISE(ABORT, 'entry_flag: entry_uuid does not match trigger_version_id')
    END;
END;

CREATE TRIGGER trg_entry_flag_immutable_core
BEFORE UPDATE ON entry_flag
WHEN NEW.id                 IS NOT OLD.id
  OR NEW.entry_uuid         IS NOT OLD.entry_uuid
  OR NEW.trigger_version_id IS NOT OLD.trigger_version_id
  OR NEW.flag_type          IS NOT OLD.flag_type
  OR NEW.detail             IS NOT OLD.detail
  OR NEW.created_at         IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'entry_flag core fields are immutable; only resolution fields may change');
END;

-- ---------------------------------------------------------------------------
-- sync_conflict — same (entry_uuid, version_no) received with a DIFFERENT
-- payload than the stored row. The stored row always wins (one database is
-- the sole source of truth); the rejected payload is preserved verbatim here
-- and surfaced to admin. Identical resubmission is idempotent and never lands
-- here. Core fields immutable; only resolution fields may change.
-- ---------------------------------------------------------------------------
CREATE TABLE sync_conflict (
    id                  INTEGER PRIMARY KEY,
    entry_uuid          TEXT    NOT NULL,
    version_no          INTEGER NOT NULL,
    existing_version_id INTEGER NOT NULL REFERENCES time_entry_version(id),
    conflicting_payload TEXT    NOT NULL,   -- JSON, verbatim as received
    device_id           TEXT,
    person_id           INTEGER REFERENCES person(id),   -- authed uploader
    received_at         TEXT    NOT NULL,                -- UTC
    resolved_at         TEXT,
    resolved_by         INTEGER REFERENCES person(id),
    resolution_note     TEXT
                        CHECK (resolved_at IS NULL
                               OR (resolution_note IS NOT NULL AND trim(resolution_note) <> ''))
);

CREATE INDEX idx_sync_conflict_open ON sync_conflict (received_at) WHERE resolved_at IS NULL;

CREATE TRIGGER trg_sync_conflict_no_delete BEFORE DELETE ON sync_conflict
BEGIN SELECT RAISE(ABORT, 'sync_conflict rows are never deleted; resolve them instead'); END;

CREATE TRIGGER trg_sync_conflict_no_replace BEFORE INSERT ON sync_conflict
BEGIN
    SELECT CASE WHEN NEW.id IS NOT NULL
                 AND EXISTS (SELECT 1 FROM sync_conflict WHERE id = NEW.id)
        THEN RAISE(ABORT, 'sync_conflict: INSERT would replace an existing row') END;
END;

CREATE TRIGGER trg_sync_conflict_immutable_core
BEFORE UPDATE ON sync_conflict
WHEN NEW.id                  IS NOT OLD.id
  OR NEW.entry_uuid          IS NOT OLD.entry_uuid
  OR NEW.version_no          IS NOT OLD.version_no
  OR NEW.existing_version_id IS NOT OLD.existing_version_id
  OR NEW.conflicting_payload IS NOT OLD.conflicting_payload
  OR NEW.device_id           IS NOT OLD.device_id
  OR NEW.person_id           IS NOT OLD.person_id
  OR NEW.received_at         IS NOT OLD.received_at
BEGIN
    SELECT RAISE(ABORT, 'sync_conflict core fields are immutable; only resolution fields may change');
END;

-- ---------------------------------------------------------------------------
-- audit_log — append-only. actor NULL = system (cron/backup/migration).
-- Every security- or figure-relevant action lands here: logins, password
-- resets, approvals/rejections (incl. self-approval), post-approval
-- corrections, config changes, rate changes, flag resolutions, exports.
-- ---------------------------------------------------------------------------
CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY,
    actor_id    INTEGER REFERENCES person(id),   -- NULL for system actions
    at          TEXT    NOT NULL,                -- UTC
    action      TEXT    NOT NULL,                -- e.g. 'entry.approve', 'config.set'
    entity_type TEXT,                            -- e.g. 'time_entry', 'person'
    entity_id   TEXT,                            -- uuid or numeric id as text
    reason      TEXT,
    details     TEXT                             -- JSON
);

CREATE INDEX idx_audit_at     ON audit_log (at);
CREATE INDEX idx_audit_actor  ON audit_log (actor_id, at);
CREATE INDEX idx_audit_entity ON audit_log (entity_type, entity_id);

CREATE TRIGGER trg_audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER trg_audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER trg_audit_no_replace BEFORE INSERT ON audit_log
BEGIN
    SELECT CASE WHEN NEW.id IS NOT NULL
                 AND EXISTS (SELECT 1 FROM audit_log WHERE id = NEW.id)
        THEN RAISE(ABORT, 'audit_log: INSERT would replace an existing row') END;
END;

-- ---------------------------------------------------------------------------
-- auth/session support
-- Server-side sessions so a password reset can revoke a lost phone's session.
-- login_attempt backs durable login rate-limiting (survives restarts).
-- ---------------------------------------------------------------------------
CREATE TABLE session (
    token_hash   TEXT PRIMARY KEY,               -- sha256 of the cookie token
    person_id    INTEGER NOT NULL REFERENCES person(id),
    created_at   TEXT    NOT NULL,
    last_seen_at TEXT    NOT NULL,
    expires_at   TEXT    NOT NULL,
    revoked_at   TEXT
) WITHOUT ROWID;

CREATE INDEX idx_session_person ON session (person_id);

CREATE TABLE login_attempt (
    id             INTEGER PRIMARY KEY,
    username_tried TEXT    NOT NULL COLLATE NOCASE,
    remote_addr    TEXT,
    attempted_at   TEXT    NOT NULL,              -- UTC
    success        INTEGER NOT NULL CHECK (success IN (0,1))
);

CREATE INDEX idx_login_attempt ON login_attempt (username_tried, attempted_at);

-- ---------------------------------------------------------------------------
-- sync_log — one row per device sync call; feeds "check a phone's sync
-- status" on the admin dashboard. device_id is a client-generated stable id.
-- ---------------------------------------------------------------------------
CREATE TABLE sync_log (
    id              INTEGER PRIMARY KEY,
    person_id       INTEGER NOT NULL REFERENCES person(id),
    device_id       TEXT,
    synced_at       TEXT    NOT NULL,             -- UTC
    received_count  INTEGER NOT NULL DEFAULT 0,
    accepted_count  INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,   -- identical resubmissions (idempotent)
    conflict_count  INTEGER NOT NULL DEFAULT 0,
    rejected_count  INTEGER NOT NULL DEFAULT 0,   -- failed validation
    client_info     TEXT                          -- user-agent etc., informational
);

CREATE INDEX idx_sync_log_person ON sync_log (person_id, synced_at);
CREATE INDEX idx_sync_log_device ON sync_log (device_id, synced_at);

-- ---------------------------------------------------------------------------
-- ops_event — written by host cron jobs (backup, restore-verify) via the
-- sqlite3 CLI; the dashboard reads the latest successful 'backup' row for
-- "last successful backup". Append-only.
-- ---------------------------------------------------------------------------
CREATE TABLE ops_event (
    id     INTEGER PRIMARY KEY,
    kind   TEXT    NOT NULL CHECK (kind IN ('backup','restore_verify','wal_checkpoint')),
    at     TEXT    NOT NULL,                      -- UTC
    ok     INTEGER NOT NULL CHECK (ok IN (0,1)),
    detail TEXT
);

CREATE INDEX idx_ops_event ON ops_event (kind, at);

CREATE TRIGGER trg_ops_event_no_update BEFORE UPDATE ON ops_event
BEGIN SELECT RAISE(ABORT, 'ops_event is append-only'); END;
CREATE TRIGGER trg_ops_event_no_delete BEFORE DELETE ON ops_event
BEGIN SELECT RAISE(ABORT, 'ops_event is append-only'); END;
-- ops_event is written by host cron via the sqlite3 CLI, a writer especially
-- likely to run with default pragmas — the guard matters here.
CREATE TRIGGER trg_ops_event_no_replace BEFORE INSERT ON ops_event
BEGIN
    SELECT CASE WHEN NEW.id IS NOT NULL
                 AND EXISTS (SELECT 1 FROM ops_event WHERE id = NEW.id)
        THEN RAISE(ABORT, 'ops_event: INSERT would replace an existing row') END;
END;

-- ===========================================================================
-- VIEWS — where CALCULATED figures are born, always alongside their tag.
-- Anything derived is NULL (blank, UI-flagged) when an input is missing or
-- nonsensical. No defaults, no invention.
-- ===========================================================================

-- Current state of every entry = its latest version.
CREATE VIEW v_time_entry_current AS
SELECT tev.*
FROM time_entry_version AS tev
JOIN (SELECT entry_uuid, MAX(version_no) AS max_version
        FROM time_entry_version GROUP BY entry_uuid) AS latest
  ON latest.entry_uuid = tev.entry_uuid
 AND latest.max_version = tev.version_no;

-- Current entries with worked minutes.
-- span_minutes (end - start) and worked_minutes (span - break) are
-- CALCULATED; span_minutes doubles as the visible derivation.
-- Both are NULL — blank, UI-flagged, never invented or clamped — when the
-- inputs are nonsensical:
--   end <= start        -> NULL span and worked ('end_not_after_start' flag;
--                          overnight shifts are split at midnight by the
--                          worker, never auto-extended by the app)
--   break > span        -> NULL worked ('break_exceeds_duration' flag)
CREATE VIEW v_time_entry_minutes AS
SELECT s.*,
       CASE WHEN s.span_minutes IS NOT NULL
             AND s.break_minutes <= s.span_minutes
            THEN s.span_minutes - s.break_minutes
            ELSE NULL
       END              AS worked_minutes,
       'CALCULATED'     AS worked_minutes_tag
FROM (
    SELECT c.*,
           CASE WHEN c.end_time > c.start_time
                THEN (strftime('%s', c.work_date || 'T' || c.end_time)
                    - strftime('%s', c.work_date || 'T' || c.start_time)) / 60
                ELSE NULL
           END          AS span_minutes,
           'CALCULATED' AS span_minutes_tag
    FROM v_time_entry_current AS c
) AS s;

-- Latest pay rate per person per effective_date reached (history view).
-- "Rate as of a given work_date" is resolved by the app against this view;
-- if no rate row is on/before the date, the figure renders blank + flagged.
CREATE VIEW v_rate_pay_effective AS
SELECT rp.*
FROM rate_pay AS rp
JOIN (SELECT person_id, effective_date, MAX(entered_at) AS latest_entry
        FROM rate_pay GROUP BY person_id, effective_date) AS latest
  ON latest.person_id     = rp.person_id
 AND latest.effective_date = rp.effective_date
 AND latest.latest_entry   = rp.entered_at;

CREATE VIEW v_rate_bill_effective AS
SELECT rb.*
FROM rate_bill AS rb
JOIN (SELECT person_id, effective_date, MAX(entered_at) AS latest_entry
        FROM rate_bill GROUP BY person_id, effective_date) AS latest
  ON latest.person_id     = rb.person_id
 AND latest.effective_date = rb.effective_date
 AND latest.latest_entry   = rb.entered_at;

-- OT policy history, latest entered_at per effective_date wins. The app
-- resolves "policy in force for a week" as the row with the greatest
-- effective_date <= that week's start date; no row => OT figures blank +
-- flagged for that week. Re-running a past range reproduces past figures
-- because rows are append-only and same-date corrections keep both rows.
CREATE VIEW v_ot_policy_effective AS
SELECT op.*
FROM ot_policy AS op
JOIN (SELECT effective_date, MAX(entered_at) AS latest_entry
        FROM ot_policy GROUP BY effective_date) AS latest
  ON latest.effective_date = op.effective_date
 AND latest.latest_entry   = op.entered_at;

-- Open flags per entry, for list badges and the flag review queue.
CREATE VIEW v_open_flags AS
SELECT f.*
FROM entry_flag AS f
WHERE f.resolved_at IS NULL;
