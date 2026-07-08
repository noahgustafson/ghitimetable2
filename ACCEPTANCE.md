# GHI-TIME — Gate 2 Acceptance Record

> This build is AI-assisted (Claude Code) and operator-reviewed before use.

Binding spec: [GATE1.md](GATE1.md) (schema summary, full route list, screen
list, design decisions, §6 pytest scope). This record documents what shipped,
how it was verified, and every deviation.

## Provenance — declared prominently

`origin/gate2-staging` did **not** exist at execution time. The six delivery
commits on the `gate2` branch are the verified local sequence built in the
same session that produced them (identical scope to the described staging:
ci, core, app, tests, ops, seed-scrub atop the operator-verified schema head
`5fe6305`). They were cherry-picked onto `main` (whose squash-merged tree is
byte-identical to `5fe6305`'s tree — verified before picking), pushed
normally, and never force-pushed. This is NOT a from-scratch rebuild; no
work was lost or reconstructed.

## Test results

- `pytest`: **42 passed, 0 failed** (local, Python 3.11 and in CI on 3.12).
- `python validate_schema.py`: **ALL CHECKS PASSED** — full output below.
- Boot smoke: seeded database serves all 19 screens/endpoints (exercised
  during development; reproducible via `flask seed-demo` + browsing).

## CI run URLs (PR #2 "Gate 2 delivery")

- Run 1 (six delivery commits, head `133899c`): SUCCESS —
  <https://github.com/noahgustafson/ghitimetable2/actions/runs/28942747500>
- Final pre-merge run (this ACCEPTANCE commit) appears on the PR checks tab:
  <https://github.com/noahgustafson/ghitimetable2/pull/2/checks> — merge
  occurs only on its green conclusion (constraint: merge only on green CI).

## Seed data confirmation — explicit

Seed/demo data contains **no real names and no real rates**. All five seed
people are invented (Vern Ostrander, Marta Vlasek, DeShawn Pratt, Ollie
Trask, Pia Lindqvist); the proof-script fixture is invented (Ada Verity);
all rates ($26.00→$28.50, $24.00 pay; $65.00 bill) and the OT policy
(40 h × 1.5) are arbitrary demonstration values, not GHI figures. A
code-wide sweep for real or company-adjacent names was run and comes back
clean (commit "seed: demonstration data uses only invented names and
arbitrary rates").

One scoped exception, deliberate: the operator-attribution lines required by
the original build spec ("AI-assisted (Claude Code); to be reviewed by
<operator> before use") in GATE1.md and the migration header predate this
constraint, are review attribution rather than data, and the migration file
is immutable by design (never edited after shipping; pushed history is
append-only). No name appears in any seed row, fixture, or export.

## Deviations from GATE1.md, with reasons

1. **`GET /submit` added** (attestation confirmation page). GATE1 lists only
   `POST /submit`; screen 7 ("Submit confirmation") needs a URL to render.
2. **`GET /entries/<uuid>` is also readable by an admin** (GATE1 marks it
   worker-only). Supports admin review links; other workers still receive
   404 so entry uuids are never confirmed across workers.
3. **Sync API accepts `version_no == 1` only**, rejecting anything else
   visibly. Tightening consistent with design decision 12 (the capture
   module edits not-yet-synced drafts in place, so a device never produces
   v>1); prevents devices from forging post-sync history.
4. **Payroll export grain**: per-entry rows plus `week_total` rows
   (discriminated by a `row_type` column); weekly OT figures and the
   threshold applied live on the `week_total` rows. GATE1 lists the columns
   without fixing a grain; OT is inherently weekly.
5. **Payroll export includes approved entries only** — payroll runs on
   approved hours. Unapproved entries in range are surfaced per person/week
   as a CALCULATED `unapproved_entries_in_range` count, never silently
   dropped (figure rule: conflicts surfaced).
6. **Worker weekly totals** include draft+submitted+approved (void always
   excluded, per §6 binding 2). GATE1 did not specify the status set; a
   worker's "this week" should show everything they have recorded.
7. **htmx 1.9.12 vendored** from the official GitHub release tag (fixed
   stack requires htmx; the tailnet-only deployment cannot use a CDN).
8. **PWA icons are plain solid-color placeholders** (192/512 PNG) — no
   branding was specified.
9. **CI also runs on the Gate 2 PR itself** — required to satisfy
   "merge only on green CI" for this very delivery.

No other route, screen, schema object, figure rule, or §6 scope item
deviates from GATE1.md.

## Schema proof output (`python validate_schema.py`)

```
schema loaded OK
time_entry_version:
  ok (allowed): insert v1 draft
  ok (blocked): UPDATE version row -> time_entry_version is append-only: UPDATE forbidden
  ok (blocked): DELETE version row -> time_entry_version is append-only: DELETE forbidden
  ok (blocked): v2 without change_reason -> CHECK constraint failed: version_no = 1
  ok (blocked): skip version_no (v5 after v1) -> time_entry_version: version_no must be exactly max(version_no)+1
  ok (blocked): duplicate (uuid, version_no) -> time_entry_version: version_no must be exactly max(version_no)+1
  ok (blocked): new entry with v1 status=approved -> CHECK constraint failed: version_no > 1 OR status = 'draft'
  ok (blocked): bad time format 8:00 -> CHECK constraint failed: start_time GLOB '[0-2][0-9]:[0-5][0-9]' AND start_time < '24:00'
  ok (blocked): bad date 2026-13-40 -> CHECK constraint failed: work_date IS date(work_date)
  ok (blocked): break_minutes omitted (no silent default) -> NOT NULL constraint failed: time_entry_version.break_minutes
  ok (allowed): insert v2 submitted with reason
  ok (blocked): v3 reassigning person_id (owner change forbidden) -> time_entry_version: person_id is immutable across versions; void and re-enter instead
  ok (allowed): v3 same person, different job (job_id stays changeable)
OR REPLACE / UPSERT bypass attempts (recursive_triggers OFF):
  ok (blocked): INSERT OR REPLACE tev reusing rowid id -> time_entry_version: INSERT would replace an existing row
  ok (blocked): UPSERT ON CONFLICT DO UPDATE on (uuid,version) -> time_entry_version: version_no must be exactly max(version_no)+1
  ok (blocked): INSERT OR REPLACE audit_log id=1 -> audit_log: INSERT would replace an existing row
  ok (blocked): INSERT OR REPLACE rate_pay id=1 -> rate_pay: INSERT would replace an existing row
  ok (blocked): INSERT OR REPLACE ops_event (fake backup timestamp) -> ops_event: INSERT would replace an existing row
  ok (blocked): INSERT OR REPLACE person via username conflict -> person: username already exists
  ok (blocked): UPDATE OR REPLACE person onto other username -> person: username already exists
  ok (blocked): person id change -> person: id is immutable
  ok (blocked): INSERT OR REPLACE config key -> config: key exists; UPDATE its value instead
  ok (blocked): config key rename -> config: key is immutable
  ok (blocked): INSERT OR REPLACE figure_tag -> figure_tag: tag already exists
  ok (blocked): INSERT OR REPLACE job via code conflict -> job: code already exists
views:
  ok: v_time_entry_current -> v3 submitted
  ok: span=480 worked=450 tag=CALCULATED
  ok: end<=start -> span/worked NULL (blank, flagged)
  ok: break>span -> worked NULL (blank, flagged), span=60 shows derivation
  ok: break==span -> worked 0 (arithmetic truth, not invented)
other append-only tables:
  ok (blocked): UPDATE audit_log -> audit_log is append-only
  ok (blocked): DELETE audit_log -> audit_log is append-only
  ok (blocked): UPDATE rate_pay -> rate_pay is append-only
  ok (blocked): DELETE rate_pay -> rate_pay is append-only
  ok (blocked): UPDATE submission -> submission is append-only
  ok (blocked): DELETE submission -> submission is append-only
  ok (blocked): UPDATE ops_event -> ops_event is append-only
approval + flags_ack enforcement:
  ok (blocked): approve flagged entry without flags_ack_reason -> approval_entry: approving a flagged entry requires flags_ack_reason
  ok (allowed): approve flagged entry WITH flags_ack_reason
  ok (blocked): reject without reason -> CHECK constraint failed: action <> 'reject'
  ok (allowed): reject a flagged entry needs no ack (reason already required)
  ok (blocked): UPDATE approval -> approval is append-only
  ok (allowed): open badge flag (self_approval) does not gate approval
uuid/version coherence:
  ok (blocked): approval_entry uuid mismatched to acted_on_version_id -> approval_entry: entry_uuid does not match acted_on_version_id
  ok (blocked): approval_entry resulting_version_id from another entry -> approval_entry: entry_uuid does not match resulting_version_id
  ok (blocked): approval_entry with dangling acted_on_version_id -> approval_entry: entry_uuid does not match acted_on_version_id
  ok (blocked): entry_flag uuid mismatched to trigger_version_id -> entry_flag: entry_uuid does not match trigger_version_id
  ok (blocked): entry_flag with dangling trigger_version_id -> entry_flag: entry_uuid does not match trigger_version_id
person/job/config protection:
  ok (blocked): DELETE person -> person rows are never deleted; set active = 0 instead
  ok (blocked): DELETE job -> job rows are never deleted; set status = completed instead
  ok (blocked): DELETE config key -> config keys are never deleted; set value = NULL to unset
  ok (allowed): deactivate person
  ok (allowed): legit username rename
  ok: workweek_start_dow ships unset (NULL); set once at go-live, never hard-coded
entry_flag partial immutability:
  ok (blocked): mutate flag_type -> entry_flag core fields are immutable; only resolution fields may change
  ok (blocked): resolve without reason -> CHECK constraint failed: resolved_at IS NULL
  ok (allowed): resolve with reason
  ok (blocked): DELETE entry_flag -> entry_flag rows are never deleted; resolve them instead
  ok (blocked): INSERT OR REPLACE entry_flag id=1 -> entry_flag: INSERT would replace an existing row
  ok (allowed): break_exceeds_duration flag type accepted
ot_policy (effective-dated, append-only, no partial rows):
  ok (blocked): partial policy row (multiplier omitted) — absence is the only unset state -> NOT NULL constraint failed: ot_policy.multiplier
  ok (allowed): append complete policy row (40h x 1.5)
  ok (blocked): UPDATE ot_policy -> ot_policy is append-only
  ok (blocked): DELETE ot_policy -> ot_policy is append-only
  ok (blocked): INSERT OR REPLACE ot_policy id=1 -> ot_policy: INSERT would replace an existing row
  ok (blocked): threshold_hours = 0 -> CHECK constraint failed: threshold_hours > 0 AND threshold_hours <= 168
  ok (blocked): multiplier = 0 -> CHECK constraint failed: multiplier > 0
  ok: same-date correction -> latest entered_at wins (44h x1.5, SOURCE tags), both rows preserved
  ok: policy change is a new effective_date row; past ranges recompute under the policy in force then
  ok: OT scalars removed from config; preview switch / workweek / anchor keys remain
UNIQUE effective-date keys (identical-timestamp duplicates rejected):
  ok (blocked): rate_pay duplicate (person_id, effective_date, entered_at) -> rate_pay: duplicate (person_id, effective_date, entered_at)
  ok (allowed): rate_pay same date, later entered_at (correction)
  ok: v_rate_pay_effective returns exactly one row per (person, date) -> 3000
  ok (blocked): rate_bill duplicate (person_id, effective_date, entered_at) -> rate_bill: duplicate (person_id, effective_date, entered_at)
  ok (blocked): ot_policy duplicate (effective_date, entered_at) -> ot_policy: duplicate (effective_date, entered_at)
  ok (blocked): INSERT OR REPLACE via duplicate natural key (recursive_triggers OFF) -> ot_policy: duplicate (effective_date, entered_at)
  ok: v_ot_policy_effective returns exactly one row per effective_date

20 tables, 6 views, 51 triggers
ALL CHECKS PASSED
```
