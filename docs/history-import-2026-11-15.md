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
| Deployment whose creator was not imported, or was the old Administrator account | The county's one active County Coordinator as coordinator, else Administrator; reported either way (16 Nov 2026) |
| Deployment and event creator | Kept as owner where the account was imported (16 Nov 2026): a deployment's owner hears volunteers' answers |
| Deployment notes | The old notes as written; nothing is appended about the status (16 Nov 2026) |
| Booking whose account was not imported | An unclaimed booking, as designed |

Not imported:
- RRA Training Registration (decided: later, separately);
- `future_deployment_date` (it would send asks).

A TOR stakeholder phone that is not a phone number follows the
**drop invalid phones** setting, the same as people. With the setting off, that
TOR is reported as not imported, and so are its deployments.

## Who counts as left out (0.9.1, 16 November 2026)

A record's history is held back only when the people release left the record
out as a whole: the person was held for review, shared an account with other
volunteer records, or had no usable email. A single old field that stays in the
archive (a ward, a skill, a designation) no longer holds back its record. Before
0.9.1 it did, and the production preview held back 2,630 of 4,694 deployment
requests and every account for that reason alone.

Since 0.10.0, anyone the people session brought across (anyone in Legacy
Migration Record) is never held back, even when today's Settings or crosswalks
would flag them. Before that, switching off "Import people with field review"
after the people import held back the history of people who were already in.

Run Import history only with 0.9.1 or later. A live import marks each held-back
row Excluded in the archive, and later runs do not attempt those rows again.

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

   - `statuses.coordinators`: imported deployments with Administrator as
     coordinator (volunteers see the coordinator as their contact);
   - `statuses.events`: published / unpublished, events without a place,
     published events whose banner is private, and bookings claimed /
     unclaimed. Unclaimed bookings are linked later by the Event bookings
     repair.

   Also read `history-not-imported.csv` (each record and why),
   `history-warnings.csv`, and `history-fields-not-imported.csv`: every old
   field with a value that the new records do not carry, with its count and
   why. Those values stay in the archive. The old M-Pesa records are archived
   too, readable as Legacy M-Pesa Payment.
4. **Import history.** Do this before Finish cutover: it is refused once
   the cutover is finished and the scheduler runs. Confirm the backup and type `HISTORY`. It runs on the
   long worker and writes the same reports. A stopped run is simply started
   again, and it skips what is already in.

   Since 0.10.2 the first run (Preview or Import) of a capture works out who
   the people session held back, which needs the whole capture in memory
   (about a gigabyte), saves that with the small part of the capture the
   history reads (`captures/<id>/history-inputs.json`), and queues the same
   action again as a new job. That job, and every later run, reads only the
   saved part: on kenyaclone 180 MB to start instead of 790 MB, about 600 MB
   at the most instead of 1.1 GB, with the same reports. A change to the
   capture, the settings, the crosswalks, the code or the people ledger works
   it out again.

   Since 0.10.3 an Import keeps out of the way of the server's 4-minute marks.
   On Frappe Cloud a memory guard stops the largest process on the server when
   memory runs short, and a shared server runs short when every site's
   scheduled jobs start together, at each 4-minute mark (Frappe's "All"
   frequency). Every stopped job had died one to three seconds after one, on
   any plan. So a job starts 45 seconds after a mark and stops at a commit
   before the next one; a new job carries on 45 seconds after that mark from
   the place the run kept (`history-progress.json`: the phase, the place in
   the archive, the report's lists and totals). The pages are the ones an
   uninterrupted run takes and the report is that run's report, written once
   at the end; its `jobs` says how many jobs it took. The panel shows
   "waiting for the 4-minute mark" between them. A run that stands aside four
   times without getting further stops with its place kept; Import history
   carries it on. The 3 GB history store is hashed once and then only again
   when it changes (`transfer.sqlite.verified`).

   Since 0.10.4 the files stop at safe points too: on Frappe Cloud copying the
   history's attachments took longer than a whole stretch, so the files step
   commits and keeps its place every 25 files (and every 200 records whose
   attachment fields it rewrites), with its counts and the files it already
   matched, and the next job carries on from there.

   Since 0.10.5 no job is alive across a mark, not even a sleeping one: on
   Frappe Cloud a job that slept through a mark was stopped too. A job that
   stands aside, prepares, or starts outside a stretch ends at once and the
   run shows "waiting"; the open migration panel (which asks for the status
   every 4 seconds) queues the next job when the next stretch begins. When the
   server stops a job anyway, the panel sees it and the run waits for the next
   stretch, carrying on from its place; four stops in a row at the same place
   end it with an error and the place kept. **Keep the Migration Settings page
   open while the history imports.** Closed, the run pauses; opened again, it
   carries on.
5. Fix the causes in `history-not-imported.csv`, then run Import history again.
   Only what is left is attempted.

No email, SMS or alert is sent. Each page checks Email Queue, Notification Log,
SMS Log, Communication and ToDo before it commits, and stops the run if this
worker left anything there.

## Helpdesk tickets (0.10.0, 16 November 2026)

The old helpdesk tickets come in as part of Import history (`helpdesk_load.py`),
after the time logs. It needs Helpdesk installed here, and a capture taken with
exporter **0.5.0** on the old site; with an older capture the preview shows a
`phase_errors` entry for `helpdesk` and the rest of the history still runs.

| Old record | Comes in as |
| --- | --- |
| HD Ticket | The same ticket number, raiser, status, dates, SLA dates and feedback. `source` is Portal when it was raised on the portal, else Email. The contact is the Contact with the raiser's email here, if any. |
| A ticket number already used here | Not imported, never overwritten: `history-helpdesk-collisions.csv` and `history-not-imported.csv`. Its thread waits with it. |
| Merged tickets | The merge link, set once both tickets are in |
| Emails (Communication) | As recorded, sent and received, including the automated "new ticket" messages |
| Ticket comments | Core Comment under the old name (helpdesk develop). The copy helpdesk 1.31 had already made is the same comment, linked once. |
| Assignment, attachment and share notes (Comment) | As recorded |
| Assignments (ToDo) | Open and cancelled as recorded, and the ticket's assignment list; the assignee's account must be here |
| Change history (Version) | As recorded: the status timeline reads it |
| Statuses, priorities, types, feedback options, holiday list | Added when missing; one already here (helpdesk seeds Open, Billing, Default...) is used as it is and any difference is a warning |
| SLA "Default" | Imported switched off and not the default (decision 2 of the design) |
| Teams | Members whose accounts are here, no assignment rule, so nothing is handed out automatically |
| Agents | Switched off; staff switch one on to give it tickets |
| HD Ticket Activity, articles, notifications, saved views, the ticket template | Archive only (`archived_only` in `records`) |

Not carried, counted in `history-helpdesk-drops.csv` and
`history-fields-not-imported.csv`: the outgoing Email Account on sent emails,
email timeline links to contacts and customers, ToDo assignment rules, and the
old field `custom_mode_of_support_request` (45 tickets, all its default
"Portal"; the value stays in the archive).

Only the ticket rows of the Communication, Comment, ToDo and Version tables are
archived, converted, given their files and reported. Each page's outbox check
allows exactly the Communications and ToDos this import inserted, linked to a
ticket; any other new row, and anything in Email Queue, Notification Log or SMS
Log, still stops the run. Restored ticket files are private and add no new
"Attachment" comment.

`statuses.helpdesk` gives tickets per status, tickets without a team, assigned
tickets and the thread counts.

Before the import on the live site, read in System Console: the helpdesk
version, HD Ticket count and highest number, the `''` series counter, and HD
Settings `auto_close_tickets`, `send_acknowledgement_email` and
`enable_email_ticket_feedback`. After it, run `helpdesk.search.build_index()`
once so the old tickets are searchable.
