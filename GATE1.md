# GHI-TIME — Gate 1 deliverable

**Status: awaiting operator approval. No application code has been written.**

This document accompanies [`schema.sql`](schema.sql) and contains the full
route list, the screen list, the design decisions embedded in the schema, and
the open questions that need an operator answer before or during Gate 2.

AI-assisted (Claude Code); to be reviewed by Noah Gustafson before use.

---

## 1. Schema summary

`schema.sql` (which ships verbatim as `migrations/001_init.sql` on approval)
defines **19 tables, 5 views, 45 triggers**. It has been machine-validated:
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
| `time_entry_version` | the core: append-only entry versions | **UPDATE and DELETE blocked by triggers**; `version_no` must be contiguous (trigger); `change_reason` required from v2 on (CHECK); v1 must be `draft` (CHECK — a phone can never sync an entry into existence as approved) |
| `submission` / `submission_entry` | attestation events + the exact versions attested | **append-only (triggers)** |
| `approval` / `approval_entry` | approve/reject events; reject requires reason (CHECK); approving an entry with an open data-integrity flag requires `flags_ack_reason` (trigger — badge flags don't gate); self-approval flagged | **append-only (triggers)** |
| `rate_pay` | effective-dated pay rates, worker-visible | **append-only** — a raise never rewrites the past |
| `rate_bill` | effective-dated bill rates, admin-only | **append-only** |
| `config` | `ot_threshold_hours_per_week`, `ot_multiplier` (both ship **UNSET/NULL**, `value_tag='SOURCE'` — figure-valued keys carry their tag onto every screen/report/export that renders them), `ot_pay_preview_enabled` ('0'), `workweek_start_dow` (unset — see open questions), `pay_period_anchor` (reserved, unused) | value update allowed (audit-logged); **key DELETE blocked; key rename blocked** |
| `entry_flag` | surfaced anomalies: overlap, >16h, duplicate, future-dated, end-not-after-start, break-exceeds-duration, plus badge types self_approval and post_approval_correction | core fields immutable (trigger); only resolution fields writable; resolution requires reason (CHECK); **DELETE blocked** |
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
predates the work date), `v_open_flags`.

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
| GET | `/` | any | role-aware home. Worker view: this week's totals (CALCULATED), unsubmitted count, open flags on own entries, gross preview if rate set (blank + "rate not set" flag otherwise), OT flag once threshold set, submit button |
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
| GET | `/admin` | admin | dashboard: per-person unsubmitted counts, open flag count, open sync conflicts, **"OT threshold unset — bookkeeper advises"** warning, last successful backup (from `ops_event`; stale ⇒ warning), last restore-verification, per-device last sync |
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
| GET | `/admin/config` | admin | view config incl. UNSET states |
| POST | `/admin/config` | admin | set OT threshold / multiplier / preview toggle / workweek start (audit-logged; can also re-unset) |
| GET | `/admin/audit?actor&action&entity&from&to` | admin | audit log viewer, filterable |
| GET | `/admin/sync-status` | admin | per person/device: last sync, counts, conflicts (RUNBOOK: "check a phone's sync status") |

### Reports (HTML + `.csv` twin; every figure tag-paired; money admin-only)

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/reports` | hub |
| GET | `/admin/reports/hours?group=person\|job\|person_job&period=day\|week\|month\|quarter\|year&from&to` (+ `.csv`) | hours rollups (CALCULATED) |
| GET | `/admin/reports/ot?from&to` (+ `.csv`) | weekly OT hours past threshold; **blank + flagged if threshold or workweek start unset** |
| GET | `/admin/reports/labor?basis=pay\|bill&period&from&to` (+ `.csv`) | "Labor cost (pay)" / "Billable labor (bill)", both CALCULATED. Report titles may never contain "margin" or "profit" (README rule) |

### Exports

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/export` | export hub |
| GET | `/admin/export/payroll/employees.csv?from&to` | bookkeeper payroll-prep: person, date, job, hours(+tag), break(+tag), OT-hours-past-threshold(+tag; blank + flag column if threshold unset), pay rate(+tag) if set else blank+flag, gross preview (CALCULATED), correction badges. **Employees only.** OT pay preview column only when `ot_pay_preview_enabled` AND multiplier set. |
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
   (CALCULATED, blank+flag without a rate), OT flag once threshold set,
   submit button, link to capture.
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
16. **Config** — OT threshold / multiplier / preview toggle / workweek start;
    UNSET states shown explicitly.
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
9. **`workweek_start_dow` added to config, shipping UNSET** — weekly OT math
   needs a week boundary and inventing one (Sunday vs Monday) would violate
   the never-default rule. See open question 1.
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

## 5. Open questions (answer at approval, or accept the proposal)

1. **Workweek boundary** — weekly totals and the OT flag need a week start.
   Proposal: display grouping defaults to Mon–Sun *labeled "display
   grouping"*, but OT figures stay blank+flagged until the bookkeeper
   confirms both the threshold **and** the workweek start day. Alternative:
   pick the FLSA workweek day now and I hard-code it.
2. **Owner as bootstrap admin** — `flask create-admin` creates the owner
   account with both roles (admin + worker, employee type). OK?
3. **Session length** — proposal: 30-day rolling expiry, revoked on password
   reset/change. Shorter?
4. **Outbox warning threshold N** — proposal: warn at 20 unsynced entries
   (iOS eviction risk). Different number?
5. **Duplicate flag definition** — proposal: same person + date + identical
   start/end on two different entry uuids ⇒ `duplicate` flag on both. Looser
   (overlapping counts already catch partial copies)?

---

*Gate 2 (on approval): migrations runner, Flask app, PWA capture module,
pytest suite (incl. immutability proofs already prototyped for this gate),
seed data, README, RUNBOOK, Docker Compose, backup/restore tooling.*
