# History import — 15 November 2026

This brings the old site's operational history across after the people:
- projects and tasks;
- terms of reference;
- deployments and their volunteer requests;
- events, bookings and tickets;
- availability and time logs;
- the files attached to all of these.

The code is `history_import.py`. Run it from **VMMS Migration Settings → History import**.
Nothing runs on deploy.

## Why it is needed

The production people session has no history phase. The full-import batch
scope would run accounts and people again. The archive held only people
records, and the history adapters keep their re-run ledger on the archive
rows. This action runs the history on its own, against the selected capture:

1. **Archive.** Every captured row of the converted doctypes and their child
   tables goes into Legacy VMMS Document. The rows are kept as they are.
2. **Convert.** The adapters run in dependency order, 500 new records at a time,
   and each page is committed with its ledger marks.
3. **Files.** Only the attachments of the converted records are copied. People's
   files are not touched.
4. **Reconcile.** For each source doctype the report counts records captured,
   converted, excluded and not imported.

## Decisions applied (15 November 2026)

| Old record | Comes in as |
| --- | --- |
| Deployment whose end date has passed | Completed |
| Deployment running today | Active |
| Deployment not started | Planned |
| Request still Pending on a finished deployment | Expired |
| Request answered Accepted / Rejected / Cancelled | Accepted / Declined / Withdrawn, as before |
| Terms of reference | Switched off. They stay on their deployments and in history, and staff can switch one back on. |
| Deployment given as a file only | An inactive draft TOR. The file goes in its TOR attachment (the field vmmsx shows staff) and in the legacy field. |
| Free-text methodology the register lacks | Added to VMMS TOR Methodology exactly as written, switched off |
| Project Type the destination lacks | Added with its old name |
| Deployment location | Site name |
| "Require contract before deployment" | Requires contract |
| Event branch company | The event's place (geo node) |
| Deployment whose creator was not imported | Administrator as coordinator, reported |
| Booking whose account was not imported | An unclaimed booking, as designed |

Not imported:
- RRA Training Registration (decided: later, separately);
- `future_deployment_date` (it would send asks).

A TOR stakeholder phone that is not a phone number follows the
**drop invalid phones** setting, the same as people. With the setting off, that
TOR is reported as not imported, and so are its deployments.

## Operating

1. Deploy. The site migration changes no records.
2. If the old site has new deployments since the last capture:
   - **Capture / resume old site**;
   - then the people catch-up (People import), so new volunteers exist before
     their requests;
   - then **Preview repairs** and **Apply repairs** again for the newcomers.
3. **Preview history.** This runs the whole import and rolls it back. Read
   `history-summary.json`, and check:
   - `phase_errors`: setup problems, such as the legal Company, Project numbering,
     or Buzz not installed;
   - `records`: per doctype, captured / converted / excluded / not imported;
   - `statuses`: deployment and request states, and whether TORs are active.

   Also read `history-not-imported.csv` (each record and why) and
   `history-warnings.csv`.
4. **Import history.** Confirm the backup and type `HISTORY`. It runs on the
   long worker and writes the same reports. A stopped run is simply started
   again, and it skips what is already in.
5. Fix the causes in `history-not-imported.csv`, then run Import history again.
   Only what is left is attempted.

No email, SMS or alert is sent. Each page checks Email Queue, Notification Log,
SMS Log, Communication and ToDo before it commits, and stops the run if this
worker left anything there.
