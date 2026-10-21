# VMMS migration receiver — 0.3.1

Install on the **new Redhive site**, alongside `erpnext`, `onerc_core`, `vmmsx` and `buzz`.
Install companion `onerc_vmms_export` on the **old v15 VMMS site**.
The new site initiates authenticated, manually started server-to-server transfers.
No database backup or files archive needs to pass through the operator's computer.
Both sites need a long worker, Redis and sufficient private storage.

## Resumable batches (0.3.0)

Upgrade the **receiver on the destination**, run `bench --site NEW migrate`,
then build/reload its assets. The exporter does not need an update for this
feature. Let the previous migration job finish/fail before deploying; a worker
restart can interrupt an active job. Preserve the captured snapshot and keys.

For a persistent test on a disposable destination:

1. Prepare references, review mappings and run the plan/self-test as usual.
2. In **VMMS Migration Settings → 3. Validate → Start batch test**, choose 1,000
   records, confirm the target backup and disposable test destination, and type
   `TEST`. The worker pauses the scheduler and mutes email before loading.
3. A preflight plan first reports missing mapped lookup targets and phone values
   that Frappe would reject, before
   retaining or importing the full dataset. Strict mode stops for review; it does
   not silently discard phone values.
   Each job handles **up to** 1,000 records. Accounts, people and operational
   history also yield after about four minutes, before beginning another record.
   A slow individual record can still cause that one job to time out; the next
   attempt resumes its saved checkpoint. There is no completion-time guarantee.
4. The server automatically queues the next batch after each commit. You can
   close the browser or turn off the computer. Progress shows the current phase,
   committed batch count, completed records in that phase and its total when
   known. Operational history follows dependency order; its final total is not
   guessed while adapters are still running.
5. On validation errors, sequencing stops and a private `batch-…-failures.csv`
   identifies the failed records. Successful records from that batch remain;
   **Resume / retry batches** revisits the same range using the migration ledger
   and conversion index, so those successes are reused. Unexpected exceptions
   and timeouts roll back the entire current uncommitted batch.
6. **Pause batches** takes effect after the current batch commits. Fix missing
   references/target configuration, then resume. Code/settings/mapping changes
   require explicit review to resume a **test** session and are recorded in its
   reports. A different capture is always refused. Existing committed person
   values are not overwritten to match new mappings; reconciliation exposes
   those differences for review. Live import inputs cannot change midway.
7. Final verification checks retained source versions and native links, password
   continuity, people fields in batches, source-exclusion limits and reviewed
   attachment exceptions. A private `batch-session.json` contains the result.

**A batch test is persistent, not a rollback rehearsal.** It creates accounts,
Red Profiles, native affiliations, memberships and history on the test target.

It verifies captured attachment bytes without installing real attachment files.
Do not open this destination to users. Its records remain after testing; restore
its backup or rebuild the test site afterwards. Keep the source capture, reports,
receiver private key and site encryption key before any site rebuild.

Batch tests do **not** set the successful-full-rehearsal authorization or permit
live cutover. The existing rollback rehearsal remains available, and its
approval requirements remain enforced. Once a live import is authorized, the
**Import captured data** dialog now defaults to batches of 1,000 too. It retains
all backup, source-freeze, exclusion, attachment and login-delivery gates, copies
actual attachment files and applies the login policy only after final checks.
Finish cutover remains blocked until successful verification. Restoring services
never undoes committed records.

Enable **Allow phone numbers requiring review** to leave invalid profile and
professional-reference phones blank rather than block the person. An emergency
contact with an invalid required primary phone is omitted, while its original
data remains in the protected capture/archive. Each affected people batch writes
a `batch-…-people-phone-review.csv` with the person, field, original value and
action for later correction. Valid phone values and ordinary signup rules are
unchanged. A blocked test can resume after deployment with explicit review of
the code/settings change; committed people are not recreated.

**Reviewed history branch mappings** accepts a JSON object of old Company names
to destination Geo Node IDs. Use this only for reviewed operational history
placements when the people branch mapping is intentionally excluded. For example,
a national project may keep its national node while volunteers and memberships
stay county-only and the national test people remain excluded. Defaults are
unchanged; the setting is included in test-session change review.

Buzz installs standard categories (Conferences, Local, Meetups, Webinars and
Zoom Meeting). History links these source categories to existing destination
categories without changing their values, and reports differing source fields.
Original source values stay archived. This reuse does not apply to hosts,
venues, custom category names or other record collisions.

Historical TOR stakeholder phones use the same explicit phone-review setting.
Invalid values are omitted without discarding the stakeholder, and original
values remain in the archive and private batch warning reports. Missing TOR
resource UOM labels are created under their recorded names; no unit conversion
or mapping to a different unit is inferred. Existing UOMs are reused. History
batches expose warnings and exclusions as private CSVs as well as JSON summaries.

Enable **Archive history with missing source dependencies** only after reviewing
source gaps. Required User, Employee or Terms of Reference links absent from the
capture can then leave the dependent record archived with an `Excluded` conversion
status and review note. Standalone assignments with neither a usable deployment
nor structured TOR are also retained, including their original file references.
No replacement identity, deployment scope or approval is invented.
Dependents of already excluded people are also retained and reported. Exclusion
dispositions commit with the checkpoint and are reloaded for the same capture,
so retries do not loop over those records. A source-present record with an
unresolved mapping remains a failure. The ordinary source-exclusion approval
and final percentage limits still apply; this setting does not waive them.

After a **successful persistent batch test**, the disposable destination can
receive actual files without another people import. In **Trial attachments**:

1. Use **Prepare attachment plan**. This reads the existing capture and native
   identities, rechecks native reconciliation and creates private reports. It
   copies no files. Download `attachment-review.json` (summary),
   `attachment-held.csv` (exceptions) and `attachment-plan.json` (full plan).
2. Review missing files, unknown owners, shared accounts/contents and conflicting
   attachment slots. These are held out of automatic copying; their original
   bytes remain in the capture. The missing-file setting never supplies absent
   bytes or resolves ambiguous ownership.
3. Back up the trial database/files, then use **Copy reviewed trial files**.
   Confirm the disposable site and reviewed plan; enter `COPY FILES`. Start with
   100 attachments per job. Each job commits copied File rows, exact native field
   links and a separate attachment checkpoint together. The successful test
   checkpoint is preserved; accounts, affiliations and history are not reloaded.
4. Use **Pause attachment copy** or **Resume attachment copy** as needed. Native
   owner links, capture, settings, mappings and plan must remain unchanged. A
   deployed code-only fix needs explicit review on resume. Retry checks private
   owner links and SHA-256 before reusing a copy.
5. `copied` means the planned tasks finished, including private verification of
   bytes and owner links. It does not include held items or establish user access.
   Test each representative owner's view, a different user's view and Guest
   direct-URL access before restoring trial services. No live-import approval,
   login-policy change or automatic service restoration occurs.

All newly installed trial copies, including photos and event images, are private.
Public image presentation therefore needs separate review; this mode does not
silently publish personal documents. Private-only handling here does not change
the separate full-import workflow. Reports contain private source identifiers.

Checkpoint storage is a restricted InnoDB singleton separate from the settings
form. Every record batch and its checkpoint commit in the **same transaction**.
Redis is only the exclusive job lock/pause control; private JSON files are reports,
not recovery authority. A positively failed/finished RQ job can be unlocked by
Resume; queued/running/unknown jobs are never stopped or assumed dead. There is
one session per destination; trial sessions cannot be promoted into live imports.

## Data and account behavior

The full capture includes allowlisted VMMS/Buzz business records, dependent ERP
records, their original child rows, source schemas and attachment manifests.
The receiver retains original records in **Legacy VMMS Document**, restricted to
System Managers. Retention and conversion into usable native records are reported
separately. Keep this app installed while the retained history is needed.

Native adapters cover accounts, volunteer/member profiles, memberships,
applications, geography/vocabularies, projects/tasks, terms of reference,
deployments/assignments, availability, submitted timesheet hours, Buzz events,
bookings, tickets and calendar events. Other captured records, including LMS,
training registration, historical communications and financial records, are
retained for inspection; they are not recreated as operational transactions.
No historical approval, attendance, payment or qualification is invented.
Historical terms available only as an attachment/URL get a labelled draft
carrier; the original evidence is retained without inventing structured approval.
Bookings with no evidenced account have no account owner.

Bulk Export can preserve an existing supported local password hash through the
separately enabled encrypted login channel. Normal record pages and reports never
contain password hashes. Existing different destination credentials cause a
conflict rather than being overwritten. Accounts without a source local hash
cannot acquire password continuity from this mechanism. Audit the live source
before cutover; social sign-in also requires working provider configuration.
The captured source login policy is recorded with the snapshot. When the source
uses email-link login, that route is preserved without requiring a password
reset; destination MFA is never disabled. Configure and test outgoing login-link
email before importing. Historical staff roles are reported for review rather than automatically granted.
A source identifier without a recorded type is shown in a labelled, read-only
legacy field; it is not assigned an invented identification type.

## Install and operate

After obtaining the app repository on the appropriate bench:

```sh
bench --site NEW install-app onerc_vmms_migrate
bench --site NEW migrate
bench build --app onerc_vmms_migrate
bench restart
```

1. Back up the new site database, public/private files and site configuration.
2. Open **VMMS Migration Settings** on the new site. Select **Bulk Export**,
   enter the old site's public HTTPS URL and dedicated migration API credentials.
3. Enable **Preserve existing passwords**. Obtain the receiving public key and
   register it on the source as described in the export app README. Never copy
   the receiving private key or the site's encryption key to the source.
4. Run the live login audit, then **Capture / resume old site**. Interrupted
   pages/chunks resume; unchanged attachment contents are reused. Source-private
   attachments remain private. Missing/changed bytes are explicitly reported.
5. Run the configuration check, native self-test, source profile and reference
   preparation. Review the mappings and migration plan, including all warnings.
   Reference preparation creates lookup/geography records on the destination.
6. Run **Rehearse full import**. It exercises archive, accounts, people and native
   history in one destination transaction, then rolls that transaction back.
   It verifies captured bytes without copying real attachments to the destination.
   A code/configuration/mapping change invalidates the rehearsal.
7. Configure/test destination email-link delivery when the old site uses that
   method. The import dialog requires confirmation of that test.
8. For the final run, freeze writes on the old site. Clear the trial capture,
   take a fresh capture, review mappings and rehearse again. Take a fresh target
   backup. Exclude target users during the import. The browser gate requires a
   successful full rehearsal, backup/freeze acknowledgements and `LOAD`.
9. Import. The worker pauses destination scheduling and mutes email, and checks
   outbox counts. Per-record failures are recorded without changing source data.
   The final import can commit successful destination records with a failure
   report; do not reopen until reconciliation and review are complete.
10. Reconcile, review the private reports and sample native screens/files/logins.
   **Finish cutover** restores prior destination scheduler/email settings.
   On failure services remain paused until reviewed. Recovery of committed data
   is by restoring the destination backup; service restoration does not undo data.
11. Disable source export and revoke its dedicated API credentials after cutover.
    Agree retention and deletion of private captures/reports with hosting.

Source identities that cannot safely become native profiles/accounts are listed
separately and retain their original records. The explicit **Allow reviewed source
identity exceptions** option permits at most 1% of people and separately 1% of
accounts. History cascading from those approved identity exclusions is reported
separately rather than counted again as new source gaps. New or unclassified
history gaps remain capped at 1% of history records. Runtime failures
always block successful qualification. Review counts for each priority area.

Missing files block a successful rehearsal unless the operator explicitly enables
and reviews **Allow missing files**. This exception preserves data and reports
missing bytes; it cannot reconstruct them. A completed capture is not itself
proof of a complete or successful native migration.

## Historical registration and joining dates

After a completed persistent test/import, keep cutover active and use
**Historical dates → Preview historical dates**. This writes private
`historical-dates-review.json`, `historical-dates-plan.json` (before/after values),
and `historical-dates-held.csv` when there are exceptions. Preview does not change
business records. Run site migration after deploying version 0.4.3 to install the
independent date checkpoint.

Review the plan and take a current database backup, then use **Repair reviewed
dates**, confirm the backup/review checkbox, and type `REPAIR DATES`. Jobs commit
at most 1,000 records at a time. **Pause date repair** finishes the current batch;
**Resume date repair** continues the committed cursor. If a record changed since
preview, the current batch rolls back and stops. Review the conflict and prepare
a fresh preview if needed; earlier reports and committed repairs remain intact.

The repair is limited to creation dates on migration-created Users, Red Profiles,
Volunteers, Members, Memberships and Volunteer Applications, plus captured
Volunteer/Member joining dates. Merged identities use their earliest supported
source creation date. Joining dates are not substituted for record creation.
A missing source joining date can clear a migration-day fallback; a different
existing date is held for review. Missing or invalid creation evidence stays
reported and unchanged. Adopted destination records are preserved.

Native `modified`, ownership, login credentials, roles, attachment relationships
and migration ledger timestamps are not rewritten. Original source owner and
modification metadata remain in the restricted archive. No business save or
activation hooks run during this repair. The current growth chart counts native
creation dates, so restoring source creation dates removes the migration-day
spike where evidence exists; it is still a registration trend, not proof of the
date somebody first volunteered. Refresh the chart and run Reconcile again.
Finished means the reviewed changes committed, not that missing dates were found.

## Updates and version qualification

This release supports repeated captures **before** import. It is manually
initiated on the destination and is not continuous synchronization. A capture
cannot be replaced once migration ledger records exist. Already imported changed
records are reported rather than overwritten. Do not use this release to update
an active migrated site without a separately designed conflict-aware update path.
New source records created after the final capture are absent from that capture;
keep the source frozen through cutover.

Local qualification uses the actual v15 source code and a separate restored
source database, plus the current vmmsx/Buzz receiver code. The destination's
exact production Frappe, core, vmmsx and Buzz revisions must pass the native
self-test and full rehearsal after installation. Local tests on a development
Frappe build do not establish support for every v16 maintenance build. Native
File support is checked before import; unsupported builds fail explicitly.

## Reports and terminal entry points

Reports, mappings, sealed credentials and captures live under the destination
site's `private/onerc_vmms_migrate/`. They contain personal data. Do not publish
or commit them. Transfer uses standard token authentication over HTTPS; tokens
are never forwarded across redirects.

The browser is the supported cutover workflow. Hosting may use:

```sh
bench --site NEW execute onerc_vmms_migrate.selftest.run
bench --site NEW execute onerc_vmms_migrate.full_run.prepare
bench --site NEW execute onerc_vmms_migrate.full_run.execute
bench --site NEW execute onerc_vmms_migrate.full_reconcile.execute
```

Do not substitute the older people-only `run.load` for the full browser workflow.
Direct terminal `full_run.execute(confirm='LOAD')` bypasses browser acknowledgements
and must be reserved for a controlled, backed-up cutover.

MIT license.

Overnight HRMS availability is expanded into consecutive-day windows. Native Time cannot express 24:00, so the evening segment closes conservatively at 23:59:59; original exact times remain in its description and archive. This does not create worked-hour or attendance claims.
