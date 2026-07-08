# GHI-TIME Runbook

Plain-words instructions for running GHI-TIME day to day. No technical
background needed except where marked. When something here doesn't work,
stop and get help — don't improvise on the payroll system.

The app lives at **https://ghitime.\<your-tailnet\>.ts.net** (your MagicDNS
name). Sign in with your username and password.

---

## Add a worker

1. Sign in as admin → **Admin → People**.
2. Fill in "Add person": username (short, lowercase), display name, type
   (**employee** or **subcontractor** — this decides which payroll file they
   land in, so get it right), tick **worker**.
3. Enter a temp password (8+ characters) and press **Create**.
4. Tell them the username and temp password. The app forces them to pick
   their own password at first sign-in.

Worker leaving for the season? Open their page and untick **active** — never
ask for deletion; history must stay.

## Add a job

**Admin → Jobs** → enter a short code (what the crew sees on their phone,
e.g. `KIT-14`) and a name → **Create**. Finished jobs: press **Complete** —
they drop off the phones' job picker after each phone's next sync.

## Approve a week

1. **Admin → Approval queue**. Entries are grouped by who submitted and when.
2. Look at the hours, the **Changes** column (every edit with its reason),
   and any **flag badges** (overlap, over 16h, duplicate, future-dated…).
3. Approve the group, or untick lines and approve the rest.
   - Flagged entries **require a written reason** to approve — the app will
     refuse otherwise, and the reason lands in the permanent audit log.
   - Wrong entries: **Reject** with a reason — they go back to the worker as
     drafts with your note attached.
4. Approving your own hours is allowed but gets marked **SELF-APPROVED** on
   printouts — that's by design, not an error.

Fixing an entry AFTER it was approved: use the post-approval correction on
the queue page. It needs a written reason, and the entry is permanently
badged **CORRECTED-AFTER-APPROVAL** on the worker's record and every export.

## Run the bookkeeper export

1. **Admin → Exports → Bookkeeper payroll-prep**.
2. Pick the date range, download **Employees CSV**, then **Subcontractors
   CSV** (always two separate files — never mix them; the subcontractor file
   says SUBCONTRACTOR in it).
3. Send both files to the bookkeeper.
4. Read the `flag` column before sending: "no OT policy in force", "pay rate
   not set", or an `unapproved_entries_in_range` count above 0 means
   something needs attention first. Blank cells are deliberate — the app
   never invents a number it doesn't have.

## Read flags

**Admin → Flags** lists everything suspicious: two entries overlapping, a
day over 16 hours, duplicates, future dates, breaks longer than the shift.
For each one: check with the worker, then either have them fix the entry (the
flag clears itself and records why) or **Resolve** it with a written reason
if it's actually fine (e.g. genuinely worked a 17-hour day).

**Admin → Sync conflicts**: a phone tried to send a different copy of an
entry the server already has. The server copy always wins; the phone's copy
is shown side by side. If the phone was right, add a new version with the
correct values — nothing is ever overwritten silently.

## Check a phone's sync status

**Admin → Phone sync** shows each person/phone and when it last synced.
A phone that hasn't synced in days is probably holding entries — have its
owner open GHI-TIME (the Capture page) anywhere with signal; syncing happens
automatically on open. The Capture page on the phone also shows its own
pending count.

## Restart the service

*(needs: SSH to the server)*

```sh
cd /opt/ghitime
docker compose restart
```

Check it's back: open the app, or `curl http://127.0.0.1:8080/healthz` on
the server (should print `{"ok":true}`). The service also restarts itself
after crashes and reboots.

## Restore from backup

*(needs: SSH to the server. Do this with help the first time.)*

Backups run automatically every night (dashboard shows the last one) and are
test-restored automatically every week.

```sh
cd /opt/ghitime
docker compose down
. /etc/ghitime-backup.env
restic restore latest --tag ghitime --target /tmp/ghitime-restore
cp $(find /tmp/ghitime-restore -name 'ghitime-*.db' | sort | tail -1) data/ghitime.db
rm -f data/ghitime.db-wal data/ghitime.db-shm
docker compose up -d
```

Everything after the backup moment is gone — workers' phones will re-sync
any entries still in their outboxes; approvals done since the backup must be
redone.

## Add a phone to the tailnet

*(one-time per phone; needs: Tailscale admin console)*

1. In the Tailscale admin console → **Settings → Keys** →
   **Generate auth key** (one-off, reusable off, tags if you use them).
2. On the phone: install the **Tailscale** app, sign-in screen → **Use auth
   key**, paste the key.
3. In the console, find the new device → **⋯ → Disable key expiry** —
   otherwise the phone silently falls off the network months later, in the
   middle of a workweek.
4. On the phone open **https://ghitime.\<tailnet\>.ts.net**, sign in, open
   **Capture**, and use the browser's **Add to Home Screen** so it installs
   as an app that works offline.

## Monthly glance (nothing on a shorter cycle)

- Dashboard: last successful backup recent? last restore-verification OK?
- Any old open flags or sync conflicts?
- Any phone that hasn't synced in a long time?
