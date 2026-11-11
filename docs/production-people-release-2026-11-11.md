# Production people release — 0.7.0 (11 November 2026)

This release changes the migration companion app only. No VMMSX/onerc_core
source changes, deployment, site migration, rehearsal or production import were
performed in this run. Bank names and Awards nominations are deferred. Original
Version history is included after the people and their file passes.

## Browser operation on Frappe Cloud

1. Deploy this receiver revision and let the normal site migration install its
   companion fields, review defaults and Historical accounting-status option.
2. In **VMMS Migration Settings**, save the intended capture and review settings.
   Prepare references, Generate mappings and Plan migration using this target's
   own names. Do not copy Geo Node IDs or CSVs from a local clone.
3. In **People import**, use **Check people file support**. This checks the actual
   hosted Frappe existing-blob File seam before any business insert.
4. Use **Import first 50 people**. The worker imports a bounded page, applications
   and available files, then pauses. Existing people are recognised by their
   ledger; this also provides the deferred file pass for preloaded people.
5. Inspect the committed records and reports. Verify enabled/disabled sign-in,
   original numbers, pending memberships, native roles, photos and profile-row
   evidence access for the person and an authorised reviewer. Hosted access has
   deliberately not been claimed as verified by these unit tests.
6. Use **Continue people continuously**, or **Import next 50 people**. **Pause
   people import** stops after the current commit. Runtime failures block the
   saved cursor; Resume retries with ledger/file receipts. Each people/file page
   has its own transaction. The source may stay live.

The remaining account-only logins and original Version rows follow the people
pages. Projects, TORs, deployment requests and other operational history do not
run in this scope. The target stays isolated until the operator restores services;
finishing people is not a declaration that full-site cutover is complete.

## Implementation map

- `people_run.py:135` owns people-only orchestration; `people_run.py:292` advances
  its durable phases. `batches.step` commits records and the checkpoint together.
  The first page pauses after its file copies, without a rehearsal prerequisite.
- `membership_import.py:6` writes Historical accounting status, a mandatory legacy
  membership ID, original status/payment/QR/existing-member facts and no stage.
  Outstanding-fee SQL excludes these rows by stored data. `silence.py:106` also
  blocks manual reposting of historical fees and protects runtime jobs while the
  people isolation flag is active. Contacts run inline instead of filling RQ.
- `import_review.py:23` preserves reversed date pairs with original-value review;
  companion mixins restrict missing-field/anchor exceptions to trusted imports
  and provenance-backed existing rows. Ordinary new registrations stay strict.
  Unknown contact relationships keep the contact; bad phones remove only the
  phone. Contacts are written to volunteers and applications.
- `transform.py` carries motivation through generated CSVs, disability None → No
  through a CSV, Other disability descriptions, raw/additional phone evidence,
  employee-first birth/marital data and membership/member source timestamps.
  Names are not split or recased and long education text is not truncated.
- `reference.py` excludes skills and lower residence nodes, resolves unprefixed
  region names, and writes HQ → national for both people and history. Neither
  native skills nor additional skills are imported. `account_roles.py:23` uses
  the existing County Coordinator role profile, preserves other approved roles,
  and leaves native volunteer/member self-service roles to VMMSX.
- `people_applications.py:9` retains rejection and information-request reasons.
  Missing source decision actors/stages/times remain unknown rather than invented.
  Generated Approved applications are labelled created by migration. Source
  Accepted applications without an Employee cannot allocate a new number on an
  unrelated later save.
- `attachment_plan.profile_binding` checks actual child identity/values before
  binding evidence. File tasks use the existing private-copy/checksum/ownership
  guards, immutable task records and durable cursors. File.owner follows the
  person's account. `file_recovery.py:16` requires captured SHA256 plus at least
  one source File MD5/size match; stale/missing bytes remain held. External web
  links have their own report. Direct database file phases are explicitly refused.
- `people_versions.py:67` retains captured source Version payloads, source actors
  and dates, remaps only the document reference through the ledger, and uses
  deterministic native IDs for retry. Original source field names and exporter
  credential redactions remain in the diffs. Unresolved references are archived
  and reported, not replayed as document changes. SQLite uses keyset pagination.
- `people_run.py:53` compares stable and extended evidence hashes. Enriching an
  older capture does not invalidate its existing ledger hash. A later capture
  from the same source adds new people and reports changed existing people,
  including renamed emails; it never silently overwrites them. Previous captures
  and placeholder-email assignments remain retained.
- Existing `NamingContinuity.reserve_people` reserves source/imported counter
  floors monotonically before loading and again at completion. Generated native
  application counters advance through their normal naming path.

## Reports and limits

The Settings dashboard exposes private downloads for field-review originals,
not_migrated, status_mapping, per-page held/missing/external files, source changes,
account issues, Version holds and people-counters. The status report flags the
existing Replied → In Review and submitted Open → Draft mappings for approval.
Source identity conflicts/orphan memberships remain explicit exceptions. Available
source originals stay in the capture/archive; rows absent from a capture cannot
be reconstructed by the receiver.

VMMSX still owns derived member status and expiry behaviour. Keep those product
settings/behaviours under review before restoring services: the supplied brief
identified pending-member status derivation and application expiry as product or
operator follow-ups. File/portal access, current hosted capability and first-page
business results are the operator's next checks, not completed site tests.

## Verification

329 Python unit tests passed. The browser-dialog unit test and Settings JavaScript
syntax check passed. Changed Python files passed undefined-name/local-variable
checks. Deliberately removing the first-page pause caused its regression test to
fail, then the source was restored. No site rehearsal or production call was run.


## 0.7.1 follow-up — education institution preflight

The production plan exposed one `missing_required_value` on an education
institution. The imported-child loader already supports retaining this blank
under the approved review policy, including full long institution text in
`legacy_institution` and the original-value report. The planner had not applied
that exception. It now shares the installed child-controller allowlist and
reports the field as review when Import people with field review is enabled.
Invalid link targets and unrelated mandatory fields remain errors.

Deploy receiver 0.7.1, keep the selected capture and saved mappings, and run Plan
migration again. No new capture, lookup preparation or rehearsal is required for
this fix. The full Python unit suite passes 333 tests; no production call or
native app source change was made.


## 0.7.2 follow-up — required membership Source and resuming a code update

The first hosted people batch reported 46 loaded and four failed people. All four
failures were native mandatory validation on VMMS Membership.membership_source.
The accounting adapter deliberately leaves modern Source blank while storing
Historical accounting status, but had not exempted that blank from the native
required-field validator. A companion mixin now allows only this one field on a
marked historical import. Later saves require a matching persisted historical
marker, an originally blank Source and a created-membership ledger. Ordinary new
memberships and other required fields retain native validation. No VMMSX source
or existing person data is changed by installing this fix.

The original people checkpoint is retained, including its zero retry cursor.
Successful people in the partial batch were committed with the blocked checkpoint;
failed people rolled back to their individual savepoints. Retrying the same page
skips the successes by ledger and attempts only missing people. Attachments follow
once the people page succeeds.

After deploying receiver 0.7.2, hard-refresh Settings and use **Resume / retry
batches**, size **50**, checking **I reviewed the deployed migration code update**.
Keep the selected capture, mapping files, settings and placeholder assignments
unchanged. Keep cutover isolation active. The first page still pauses after its
files; **Continue people continuously** remains a later operator action.

Code-only resume checks the pinned inputs independently of code. The already
running 0.7.1 session stored only a combined inputs/code fingerprint. Its trusted
release source is retained as compressed, non-executable bytes so the receiver can
recompute the original combined fingerprint with current inputs before accepting
this update. The baseline contains repository code only and is reproducible with
scripts/build_resume_baseline.py from commit 6a3d44e. It is never imported or
executed. Verification failure leaves the existing checkpoint intact. New sessions
store inputs_fingerprint directly. Accepted code updates are recorded in the
checkpoint's versions_reviewed/code_upgrades audit.

Verification for 0.7.2: 346 Python tests pass, including the actual VMMSX required
fields with Frappe's mandatory checker, historical insertion, normal-registration
validation, a 46-existing/four-missing retry, and legacy code-only resume with
changed mapping/configuration/placeholder rejection. Browser-dialog units and
Settings JavaScript syntax checks pass. These are local code tests; the hosted
batch facts above come from the operator's downloaded CSV/JSON reports.
