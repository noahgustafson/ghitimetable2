# GHI-TIME — Gate 1 deliverable

**Status: approved 2026-07-08 conditional on three schema changes (person_id
immutability, uuid/version coherence, effective-dated ot_policy) — applied
below; schema diff awaiting operator confirmation before Gate 2 begins. No
application code has been written.**

This document accompanies [`schema.sql`](schema.sql) and contains the full
route list, the screen list, the design decisions embedded in the schema, and
the open questions that need an operator answer before or during Gate 2.

AI-assisted (Claude Code); to be reviewed by Noah Gustafson before use.

---

## 1. Schema summary

`schema.sql` (which ships verbatim as `migrations/001_init.sql` on approval)
defines **20 tables, 6 views, 51 triggers**. It has been machine-validated:
the DDL loads clean in SQLite, and every append-only guarantee below was
exercised and confirmed rejected — including plain UPDATE/DELETE, **INSERT OR
REPLACE, UPDATE OR REPLACE, and UPSERT (`ON CONFLICT DO UPDATE`) bypass
attempts run with `recursive_triggers` OFF** (SQLite's default, under which
REPLACE conflict resolution skips DELETE triggers). Dedicated
`*_no_replace` guard triggers block every displacement path, so append-only
holds even for a cron/CLI writer that forgets the required
`PRAGMA recursive_triggers = ON` documented in the engine notes.

Re-run the proof yourself: `python3 validate_schema.py` (in this repo; needs
only the standard library). It becomes the seed of the Gate 2 pytest suite.

| Table | Purpose | Mutability |
|---|---|---|
| `person` | roster; role flags; `worker_type` employee/subcontractor | update allowed; **DELETE blocked by trigger** (deactivate instead) |
| `job` | admin-created jobs; only `active` sync to offline picker | update allowed; **DELETE blocked** |
| `time_entry_version` | the core: append-only entry versions | **UPDATE and DELETE blocked by triggers**; `version_no` must be contiguous (trigger); `change_reason` required from v2 on (CHECK); v1 must be `draft` (CHECK — a phone can never sync an entry into existence as approved); **`person_id` immutable across versions (trigger — an entry never changes owner; wrong-person entries are voided and re-entered; `job_id` stays changeable)** |
| `submission` / `submission_entry` | attestation events + the exact versions attested | **append-only (triggers)** |
| `approval` / `approval_entry` | approve/reject events; reject requires reason (CHECK); approving an entry with an open data-integrity flag requires `flags_ack_reason` (trigger — badge flags don't gate); `entry_uuid` must match the version rows referenced by `acted_on_version_id`/`resulting_version_id` (coherence trigger); self-approval flagged | **append-only (triggers)** |
| `rate_pay` | effective-dated pay rates, worker-visible | **append-only** — a raise never rewrites the past |
| `rate_bill` | effective-dated bill rates, admin-only | **append-only** |
| `ot_policy` | effective-dated OT policy on the rate_pay pattern: `threshold_hours` (>0) + `multiplier` (>0), both required — no partial policy rows; "no policy in force" is represented only by row absence — with pinned SOURCE tags, `effective_date`, `entered_by`, `entered_at`. Each week's OT computes under the policy in force at that week's start; weeks with no policy in force render blank + flagged; OT columns carry the threshold applied; re-running a past range reproduces the figures generated under the policy in force then. Ships **empty** | **append-only (no_update / no_delete / no_replace triggers)** |
| `config` | settings only — figures live in `ot_policy`: `ot_pay_preview_enabled` ('0'; even enabled, the preview needs an OT policy in force for the period), `workweek_start_dow` (ships unset; set once at go-live — Monday per operator once confirmed against payroll practice; changing it mid-history out of scope for V1, stated in README), `pay_period_anchor` (reserved, unused) | value update allowed (audit-logged); **key DELETE blocked; key rename blocked** |
| `entry_flag` | surfaced anomalies: overlap, >16h, duplicate, future-dated, end-not-after-start, break-exceeds-duration, plus badge types self_approval and post_approval_correction | core fields immutable (trigger); only resolution fields writable; resolution requires reason (CHECK); `entry_uuid` must match `trigger_version_id`'s uuid (coherence trigger — a mismatched flag can no longer badge or gate the wrong entry); **DELETE blocked** |
| `sync_conflict` | same (uuid, version) with different payload — stored row wins, rejected payload preserved verbatim and surfaced | core fields immutable; **DELETE blocked** |
| `audit_log` | actor, action, entity, reason, details | **append-only** |
| `session` | server-side sessions so password reset revokes a lost phone | app-managed |
| `login_attempt` | durable login rate-limiting | app-managed |
| `sync_log` | one row per device sync call — feeds "check a phone's sync status" | app-managed |
| `ops_event` | backup / restore-verify / checkpoint results written by host cron; dashboard reads last successful backup | **append-only** |
| `figure_tag` | closed tag vocabulary: SOURCE, CALCULATED, ALLOCATED, ESTIMATED, EXTERNAL | seed data |
| `schema_migrations` | migration runner bookkeeping | runner-managed |

Views bake the figure rules: `v_time_entry_current` (latest version = current
state), `v_time_entry_minutes` (`span_minutes` and `worked_minutes`, each with
its `CALCULATED` tag column, span doubling as the visible derivation; **NULL —
blank, UI-flagged — when end ≤ start or the break exceeds the span**, never
auto-corrected or clamped), `v_rate_pay_effective` / `v_rate_bill_effective`
(rate history; "rate as of date" resolved by app, blank + flagged when no rate
predates the work date), `v_ot_policy_effective` (OT policy history, latest
`entered_at` per `effective_date` wins; "policy in force for a week" = the
greatest `effective_date` ≤ that week's start; no row ⇒ OT blank + flagged),
`v_open_flags`.

Key conventions: UTC ISO-8601 for all `*_at` server timestamps; `work_date` /
`start_time` / `end_time` stored exactly as entered (America/Chicago,
single-timezone); money as integer cents; stored quantities carry a
CHECK-pinned `SOURCE` tag column, derived quantities exist only in views and
exports with a `CALCULATED` tag column.

How the version chain expresses the lifecycle (every transition = a new
version row, so history is one queryable chain):

| Event | New version | status | author |
|---|---|---|---|
| capture (offline or online) | v1 | draft | worker |
| edit before approval | v+1, reason required | draft/submitted (unchanged) | worker |
| submit (one tap, attests through today) | v+1 per entry | submitted | worker |
| approve | v+1 | approved | admin |
| reject | v+1, admin's reason attached | draft | admin |
| post-approval correction | v+1, reason required, audit-logged, badged | approved | admin |
| void: worker voids own draft; admin voids any state (reason, audit-logged) | v+1 | void | worker or admin |

Future additive tables (mileage, per_diem, equipment_hours) reuse the same
pattern — `(uuid, version_no)` versions, tag column, status, flags — as new
tables in later migrations. Nothing in this schema obstructs them; they are
**not** built.

---

## 2. Full route list

All pages server-rendered (Jinja2 + htmx). JSON exists only under `/api/` for
the offline capture module. Access column: `public` (pre-auth), `worker`,
`admin`, `any` (any authenticated user). Admin routes require `is_admin`;
worker routes require `is_worker` (an owner-admin with both flags sees both).
Every POST is CSRF-protected; login is rate-limited via `login_attempt`.

### Auth and shell

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/login` | public | login form |
| POST | `/login` | public | authenticate; rate-limited; sets server-side session |
| POST | `/logout` | any | destroy session |
| GET | `/password` | any | forced/voluntary password change form |
| POST | `/password` | any | change own password (revokes other sessions) |
| GET | `/healthz` | tailnet | liveness probe (RUNBOOK restart checks) |
| GET | `/manifest.webmanifest` | public | PWA manifest |
| GET | `/sw.js` | public | service worker — precaches ONLY the capture module |
| GET | `/static/…` | public | css/js/icons |

### Offline capture module + sync API (the only offline surface)

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/capture` | worker | capture module shell (precached; works offline): entry form (create AND edit not-yet-synced drafts offline), outbox list with per-entry sync state, pending count, cached-job-list as-of timestamp, manual sync button (sync also fires on app open and on connectivity regain), eviction warning past N unsynced |
| GET | `/api/jobs` | worker | active jobs for the offline picker; response carries `as_of` (UTC) |
| POST | `/api/sync` | worker | append-only batch POST of entry versions. Per-item result: `accepted` \| `duplicate` (identical resubmission — idempotent no-op) \| `conflict` (same (uuid, version_no), different payload → `sync_conflict` row, surfaced) \| `rejected` (validation, with reason). Duplicate vs. conflict is discriminated by a SELECT pre-check on (uuid, version_no) inside the write transaction — not by catching constraint errors, since the version-contiguity trigger fires before the UNIQUE constraint and its abort is indistinguishable from a gap/out-of-order rejection. Writes `sync_log`. |
| GET | `/api/sync/state` | worker | server's (uuid → latest version_no, status) for the calling worker's entries — lets a device reconcile its outbox after iOS storage eviction |

### Worker pages

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/` | any | role-aware home. Worker view: this week's totals (CALCULATED), unsubmitted count, open flags on own entries, gross preview if rate set (blank + "rate not set" flag otherwise), OT flag once an OT policy is in force, submit button |
| GET | `/entries?from&to&status` | worker | own entries by date range; status badges; flag badges |
| GET | `/entries/new` | worker | online entry form (same fields as capture) |
| POST | `/entries` | worker | create entry (v1 draft; server generates uuid for online path) |
| GET | `/entries/<uuid>` | worker | entry detail: current state + full version history with author/timestamp/reason |
| POST | `/entries/<uuid>/edit` | worker | new version; reason required |
| POST | `/entries/<uuid>/void` | worker | void own draft; reason required |
| POST | `/theme` | any | set light/dark preference in a per-device cookie (plain form/htmx POST; server renders the theme class — keeps client JS limited to the service worker + capture module) |
| POST | `/submit` | worker | one tap: attests ALL unsubmitted entries through today → `submission` + one `submitted` version per entry |
| GET | `/me/record` | worker | **my-record printout** (print CSS): every entry as first submitted, every later version with author/timestamp/reason, approvals, self-approval badges, post-approval-correction badges — proves the employer never edited hours unseen |
| GET | `/me/record.csv` | worker | same content as CSV (money columns tag-paired) |
| GET | `/me/rate` | worker | own current pay rate + history (SOURCE); never shows bill rates |

Theme (light/dark): OS `prefers-color-scheme` via CSS media query when no
cookie is set; the manual toggle POSTs to `/theme`, which persists the choice
in a per-device cookie and the server renders the theme class. No client JS
beyond the spec's allowed service worker + capture module.

### Admin pages

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/admin` | admin | dashboard: per-person unsubmitted counts, open flag count, open sync conflicts, **"no OT policy in force — bookkeeper advises"** warning, last successful backup (from `ops_event`; stale ⇒ warning), last restore-verification, per-device last sync |
| GET | `/admin/queue` | admin | approval queue grouped by submission; inline version diffs; flag badges |
| POST | `/admin/approve` | admin | approve selected entries or a whole submission; requires `flags_ack_reason` if any covered entry has an open data-integrity flag (schema-enforced by trigger) and records that reason in the `audit_log` row; self-approval auto-flagged + audit-logged |
| POST | `/admin/reject` | admin | reject with required reason → new `draft` version carrying the reason |
| POST | `/admin/entries/<uuid>/void` | admin | void an entry in any state (e.g. an erroneously approved one); reason required; audit-logged |
| GET | `/admin/flags` | admin | flag review queue: overlaps, >16h, duplicates, future-dated, end-not-after-start, break-exceeds-duration; filter by type/person/job |
| POST | `/admin/flags/<id>/resolve` | admin | resolve with required reason (audit-logged) |
| GET | `/admin/conflicts` | admin | open sync conflicts |
| GET | `/admin/conflicts/<id>` | admin | server row vs. rejected device payload, side by side |
| POST | `/admin/conflicts/<id>/resolve` | admin | mark resolved with note (server state always wins; if device version was right, admin/worker appends a NEW version) |
| POST | `/admin/entries/<uuid>/correct` | admin | post-approval correction: new `approved` version; reason required; audit-logged; badge follows the entry onto printouts and every export |
| GET | `/admin/people` | admin | roster incl. deactivated |
| POST | `/admin/people` | admin | create account (temp password, forced change) |
| GET | `/admin/people/<id>` | admin | detail: roles, worker_type, active, pay & bill rate history |
| POST | `/admin/people/<id>` | admin | update roles / worker_type / active (no deletes) |
| POST | `/admin/people/<id>/password` | admin | reset password (revokes sessions; forced change) |
| POST | `/admin/people/<id>/rate-pay` | admin | append new effective-dated pay rate |
| POST | `/admin/people/<id>/rate-bill` | admin | append new effective-dated bill rate |
| GET | `/admin/people/<id>/record` | admin | that person's record printout (same as worker's own) |
| GET | `/admin/people/<id>/record.csv` | admin | employee record export |
| GET | `/admin/jobs` | admin | job list |
| POST | `/admin/jobs` | admin | create job |
| POST | `/admin/jobs/<id>/complete` | admin | complete (drops from offline picker at next job-list sync) |
| POST | `/admin/jobs/<id>/reactivate` | admin | reactivate |
| GET | `/admin/config` | admin | view settings incl. UNSET states, plus the full OT policy history (`v_ot_policy_effective`) |
| POST | `/admin/config` | admin | set preview toggle / workweek start (audit-logged; can also re-unset) |
| POST | `/admin/ot-policy` | admin | append a new effective-dated OT policy row (threshold, optional multiplier); never edits history; audit-logged |
| GET | `/admin/audit?actor&action&entity&from&to` | admin | audit log viewer, filterable |
| GET | `/admin/sync-status` | admin | per person/device: last sync, counts, conflicts (RUNBOOK: "check a phone's sync status") |

### Reports (HTML + `.csv` twin; every figure tag-paired; money admin-only)

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/reports` | hub |
| GET | `/admin/reports/hours?group=person\|job\|person_job&period=day\|week\|month\|quarter\|year&from&to` (+ `.csv`) | hours rollups (CALCULATED) |
| GET | `/admin/reports/ot?from&to` (+ `.csv`) | weekly OT hours past threshold, computed per week under the policy in force at that week's start; **weeks with no policy in force (or workweek start unset) render blank + flagged**; OT columns carry the threshold value applied |
| GET | `/admin/reports/labor?basis=pay\|bill&period&from&to` (+ `.csv`) | "Labor cost (pay)" / "Billable labor (bill)", both CALCULATED. Report titles may never contain "margin" or "profit" (README rule) |

### Exports

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/export` | export hub |
| GET | `/admin/export/payroll/employees.csv?from&to` | bookkeeper payroll-prep: person, date, job, hours(+tag), break(+tag), OT-hours-past-threshold(+tag; computed under the policy in force per week, paired with the threshold applied; blank + flag column for weeks with no policy in force), pay rate(+tag) if set else blank+flag, gross preview (CALCULATED), correction badges. **Employees only.** OT pay preview column only when `ot_pay_preview_enabled` AND an OT policy is in force for the period. Re-running a past range reproduces the figures generated under the policy in force then. |
| GET | `/admin/export/payroll/subcontractors.csv?from&to` | identical format, subcontractors only, `SUBCONTRACTOR` label in filename and header row. **Never mixed with employees.** |
| GET | `/admin/export/job-labor.csv?from&to&basis` | hours / labor cost / billable by job and period |
| GET | `/admin/export/full.zip` | every table as CSV + JSON (data ownership) |

### CLI commands (Flask CLI; not HTTP)

| Command | Purpose |
|---|---|
| `flask migrate` | apply pending numbered forward-only migrations |
| `flask create-admin` | bootstrap the first admin account |
| `flask seed-demo` | fake-name seed data covering every state (Gate 2) |
| `flask export-all <dir>` | full export, every table to CSV and JSON, one command |

Host cron (documented in README/RUNBOOK, not app code): nightly
`sqlite3 .backup` + restic offsite copy; weekly restore-verification of the
latest backup; both record an `ops_event` row the dashboard reads. WAL
checkpoint + log rotation on the same no-shorter-than-daily cycle.

---

## 3. Screen list

Shared shell: light/dark from OS preference, manual toggle persisted per
device (cookie via `POST /theme`, server-rendered); offline banner on
non-capture pages when connection is lost (they require connection by
design).

**Worker-facing**
1. **Login** — username/password; rate-limit lockout message.
2. **Forced password change** — first login / after admin reset.
3. **Worker home** — weekly totals, unsubmitted count, gross preview
   (CALCULATED, blank+flag without a rate), OT flag once an OT policy is in
   force, submit button, link to capture.
4. **Capture module** (installable, the ONLY offline screen) — entry form
   (date backdate-allowed / future-blocked, job picker from cached list with
   as-of timestamp, start/end, break, note), offline editing of
   not-yet-synced drafts in the outbox, outbox with per-entry sync state
   + pending count, manual sync button (auto-sync on open and on
   connectivity regain), unsynced-entry eviction warning.
5. **Entry list** — date-range filter, status badges, flag badges.
6. **Entry detail / history** — current values + every version (author,
   timestamp, reason), approvals, correction badges.
7. **Submit (attest) confirmation** — lists what will be attested through
   today; one tap confirms.
8. **My record printout** — print-CSS page + CSV download (core requirement:
   worker-verifiable history).
9. **My rate** — current pay rate + history.

**Admin-facing**
10. **Admin dashboard** — unsubmitted per person, open flags, open conflicts,
    OT-threshold-unset warning, last successful backup, last
    restore-verification, per-device sync recency.
11. **Approval queue** — grouped by submission, inline version diffs,
    per-entry + batch approve, reject-with-reason; flagged entries demand an
    acknowledgment reason.
12. **Flag review queue** — filterable by type/person/job.
13. **Sync conflict detail** — server row vs. device payload side by side;
    resolve with note.
14. **People list** (incl. deactivated) and **Person detail** — roles, type,
    active toggle, password reset, pay/bill rate history + append, record
    printout link.
15. **Jobs admin** — create, complete, reactivate.
16. **Config** — preview toggle / workweek start with UNSET states shown
    explicitly, plus OT policy history (append-only) and the
    append-new-policy form.
17. **Reports hub + report view** — parameterized (group, period, range),
    every figure tagged, CSV twin.
18. **Export hub** — payroll-prep (employee + subcontractor files), job
    labor, employee record, full export.
19. **Audit log viewer** — filterable.
20. **Sync status** — per person/device.

---

## 4. Design decisions for review

1. **Status lives on version rows; every transition is a new version.** The
   version chain is the complete story of an entry — no separate mutable
   "head" table to drift from history. Lifecycle versions carry
   system-generated `change_reason` values.
2. **v1 must be `draft`** (schema CHECK) — a compromised or buggy device can
   never sync an entry into existence as submitted/approved.
3. **Contiguous version numbers** (schema trigger) — a resubmission or
   out-of-order version can never leapfrog. Because this trigger fires before
   the UNIQUE (uuid, version_no) constraint, `POST /api/sync` discriminates
   duplicate vs. conflict with a SELECT pre-check inside its write
   transaction (identical payload → idempotent `duplicate`; different →
   `sync_conflict` + `conflict`; absent → INSERT, where a trigger abort maps
   to `rejected`) — never by interpreting constraint errors.
4. **Nonsensical time arithmetic is never auto-resolved.** end ≤ start ⇒
   worked minutes blank + `end_not_after_start` flag (the worker splits an
   overnight shift at midnight; auto-adding 24h would invent a figure).
   Likewise break > span ⇒ worked minutes blank + `break_exceeds_duration`
   flag, never clamped.
5. **Money = integer cents.** No floats anywhere.
6. **Server-side sessions** (not signed cookies) so a password reset actually
   revokes a lost phone's session — the stated lost-phone threat.
7. **Rate history keeps same-day corrections**: multiple rows per
   (person, effective_date) allowed; latest `entered_at` wins; nothing is
   overwritten.
8. **Self-approval and post-approval corrections reuse `entry_flag`** as
   badge types (they don't clutter the review queue but travel with the entry
   onto printouts and exports through one mechanism).
9. **`workweek_start_dow` in config, shipping UNSET** — resolved at
   approval: Mon–Sun is the display grouping; the value is set to Monday at
   go-live via admin config once confirmed against payroll practice — never
   hard-coded. OT figures stay blank+flagged until `ot_policy` has a row in
   force. Changing it mid-history is out of scope for V1 (README states
   this).
10. **Backup visibility via `ops_event`**: host cron INSERTs a row after each
    backup / restore-verify; dashboard warns when the latest successful
    backup is older than expected.
11. **REPLACE/UPSERT hardening.** `PRAGMA recursive_triggers = ON` is
    required on every writing connection (engine notes), and independent
    `*_no_replace` BEFORE INSERT guard triggers abort any INSERT that would
    displace an existing row — so `INSERT OR REPLACE` cannot silently rewrite
    append-only history even from a CLI session with default pragmas.
    Machine-verified with `recursive_triggers` OFF.
12. **Offline edits of a not-yet-synced draft mutate the outbox payload in
    place** — still version 1, no change reason required, because the entry
    does not exist server-side yet and the outbox is a transmission buffer,
    never an authority. Server-side versioning (reasons and all) starts at
    first successful sync. Accepted consequence: if a sync response is lost
    and the device edits before retrying, the resubmitted v1 differs from the
    stored v1 and surfaces as a `sync_conflict` — surfaced, never merged.
13. **Theme toggle is a cookie set by `POST /theme`**, rendered server-side;
    `prefers-color-scheme` CSS is the no-cookie default. Keeps the client-JS
    surface exactly at the spec's limit (service worker + capture module).
14. **An entry never changes owner** (approval condition 1): `person_id` is
    trigger-enforced to match v1 across all versions — a reassigning version
    would silently remove the entry from the original worker's my-record
    printout. Wrong-person entries are corrected by void + new entry.
    `job_id` stays changeable via normal versioning.
15. **uuid/version coherence** (approval condition 2): `entry_flag` and
    `approval_entry` rows are trigger-verified to reference version rows of
    the entry they name — a mismatched flag can no longer badge, or gate the
    approval of, the wrong entry. The `resulting_version_id` check is kept
    (operator-confirmed): any column referencing a `time_entry_version` row
    must match that row's `entry_uuid`. Gate 2 write ordering follows from
    it: the new version row is inserted before the approval row, in one
    transaction.
16. **OT policy is effective-dated history, not a mutable scalar** (approval
    condition 3): append-only `ot_policy` on the rate_pay pattern with
    pinned SOURCE tags; `v_ot_policy_effective` resolves latest `entered_at`
    per `effective_date`; each week computes under the policy in force at
    its start; weeks before the first policy row render blank + flagged;
    reports/exports carry the threshold applied; re-running a past range
    reproduces the figures generated under the policy in force then.
    Both figures are required on every row (operator-confirmed:
    `NOT NULL`, `CHECK (multiplier > 0)`, `CHECK (threshold_hours > 0)`) —
    no partial policy rows; "no policy in force" is represented only by row
    absence. The OT pay preview requires `ot_pay_preview_enabled` and a
    policy in force for the period.

## 5. Questions resolved at Gate 1 approval (2026-07-08)

1. **Workweek**: Mon–Sun confirmed as display grouping. `workweek_start_dow`
   set to Monday at go-live via admin config once confirmed against payroll
   practice; OT figures stay blank+flagged until `ot_policy` has a row in
   force. No value hard-coded anywhere.
2. **Bootstrap**: `flask create-admin` takes username, display name, and
   role flags as arguments. The operator bootstraps his own admin account;
   further accounts are created through people admin.
3. **Sessions**: 30-day rolling expiry, revoked on password reset/change.
4. **Outbox warning**: N = 20 unsynced entries.
5. **Duplicate flag**: same person + date + identical start/end on two
   different entry uuids ⇒ `duplicate` flag on both.

## 6. Gate 2 pytest scope — binding additions from approval

Beyond the spec's original test list, the Gate 2 suite must prove:

1. **Illegal status transitions rejected at the app layer** per the
   transition table in §1, including any worker-authored version after
   `approved`.
2. **`status='void'` excluded everywhere figures are produced**: weekly
   totals, all reports, gross preview, and every export — proven with a
   voided entry in the seed data.
3. **Future-dated sync never strands or loses an entry**: a future-dated
   entry syncs successfully (`accepted`) and raises a `future_dated` flag; a
   wrong device clock cannot cause data loss.
4. **OT correctness under policy history**: weeks before the first
   `ot_policy` row are blank+flagged; a policy change affects only weeks on
   or after its effective date; re-running an export for a past range
   reproduces the same OT figures.
5. **CI**: a GitHub Actions workflow runs `validate_schema.py` and the full
   pytest suite on every PR to `main`.

---

*Gate 2 (on schema-diff confirmation): migrations runner, Flask app, PWA
capture module, pytest suite (incl. the proofs prototyped in
`validate_schema.py` and the binding additions in §6), seed data, README,
RUNBOOK, Docker Compose, backup/restore tooling.*
