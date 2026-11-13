# Changelog

## 0.7.7 — 2026-11-12

- Resolve explicit branch/residence mappings before creating geography. Reuse the destination's existing nodes for approved aliases, including the repaired county/subcounty mappings; report missing mapped targets instead of recreating them.
- Match apostrophe and whitespace variants consistently. Resolve an unmapped county within its national tree even when the source omits its destination region; keep automatic subcounty matching within the resolved parent. Ambiguous matches and incompatible historical codes require review.
- Report created/reused Geo Node counts during preparation. Existing Geo Node labels, parents, IDs, finance links and imported people are retained.

## 0.7.6 — 2026-11-12

- Reuse an existing source User-to-User migration mapping during the account phase, including mappings created by the earlier people phase. Keep the original person key, outcome and source hash; create a separate account-only mapping only when no source mapping exists.
- Refuse mappings pointing to a different destination account. Count loaded/existing accounts only after their mapping succeeds, so rolled-back failures are not also counted as successful.

## 0.7.5 — 2026-11-12

- Resolve placeholder identities against the complete source snapshot before paging users in the people release's accounts phase. A page can otherwise omit the invalid source login that links related employee/member records and falsely report an alias shared by unlinked identities.
- Carry the prepared identity map into each account page without changing existing placeholder addresses or weakening the registry's real-conflict checks. Keep the original account cursor, committed records and pinned migration inputs.

## 0.7.4 — 2026-11-11

- Restore the captured source password for the explicitly approved existing-login conflict during its people retry. Approval is restricted to the destination/source/capture/session/login combination; all other credential conflicts retain the existing refusal. Roles and enabled state of adopted accounts are preserved.
- Commit credential restoration, Activity Log audit and person ledger together. Session cleanup runs after commit and checks that its audit survived a person savepoint rollback. Reports include successful restorations without credential material.
- Add a default-on automatic-continuation option to Resume / retry batches, with strict boolean serialization, so a repaired people page can continue the remaining run.

## 0.7.3 — 2026-11-11

- Create a missing County Coordinator Role Profile from the destination's existing, enabled County Coordinator role. Require native leadership-role setup if that role is absent; existing profiles are checked and never silently broadened.
- Assign coordinator profiles through Frappe's current User.role_profiles child table. Keep each coordinator's approved extra staff roles and already-derived native portal roles in separate role bundles, so ordinary saves retain them without granting them to other coordinators.
- Apply new people's account-role policy after native affiliation refreshes. Adopted destination accounts retain their existing roles. Existing batch checkpoints and code-only resume verification remain unchanged.

## 0.7.2 — 2026-11-11

- Permit a blank modern payment Source only on marked historical memberships during trusted import, or on proven imported rows whose Source was already blank. Native mandatory fields remain enforced for ordinary registrations and other required values; Historical accounting exclusion is retained.
- Resume a blocked people session after a reviewed receiver update only when its capture, settings, mappings and placeholder assignments match. Older 0.7.1 sessions are verified against a checked-in, non-executable source-code baseline; new sessions pin inputs separately. Record code-upgrade approvals without resetting the session or cursor.
- Correct the resume checkbox wording and send its value as a JSON boolean. Retry uses existing person ledgers to keep committed successes.

## 0.7.1 — 2026-11-11

- Align the plan with the existing imported-profile child-field exceptions. In review mode, missing education institutions are review items rather than person blockers; long original institution text remains preserved.
- Share the native child-controller allowlist between hooks and planning. Strict mode, unrelated mandatory site fields, unknown child controllers and missing link targets retain their blocking checks.
- Add regression coverage for the reported education-row failure; 333 Python unit tests pass.

## 0.7.0 — 2026-11-11

- Add a production people workflow with a first-page pause, continuous resume, durable file checkpoints and original Version history after people. No rehearsal or source freeze is required for this workflow.
- Exclude historical memberships from fee posting by their stored accounting status; isolate notifications, queued approval repair and expiry while the people copy is active.
- Preserve original review values, emergency contacts on volunteers and applications, motivation through generated CSVs, membership facts and source timestamps. Keep generated applications labelled created by migration.
- Exclude skills and lower residence data; map HQ to national and resolve unprefixed regions on the destination. Preserve account state, native self-service roles and the County Coordinator role profile.
- Support later captures from the same source. Report changed people without overwriting them, including changes to email; preserve older ledger fingerprints when enriching captured evidence.
- Separate external URLs from missing files and recover falsely corrupt blobs only when captured SHA256 and a source File row agree.



## 0.6.0 — 2026-11-08

- Populate existing native registration fields and child tables instead of obsolete custom answers; retain grade/major, typed documents, certifications, work location, social links and User biography.
- Honor effective child metadata in direct source reads, normalize equivalent choice casing, preserve zero/unknown semantics and exclude legacy insurance/bio-data consent from active answers.
- Report installed-schema/value blockers before batch imports and expand native profile reconciliation beyond six fields. Refuse silent missing-field drops and shared member-number merges.
- Require exporter 0.4.0/profile mapping capture version 2 for new or resumed imports. Original captures remain unchanged evidence; mapping-version changes invalidate prior person hashes.


## 0.4.4 — 2026-10-21

- Carry recorded approval statuses without inventing Administrator decisions or
  displaying migration commentary as an approval event. Original source audit
  evidence and the migration ledger remain private and unchanged.
- A site migration patch exports a private audit before removing only the exact
  generated approval rows on migration-created applications/memberships. Genuine
  decisions, states and modification timestamps remain unchanged; conflicts are
  reported. Replace generated terms/deployment notes with neutral factual wording.
- Finish or pause an active date/attachment repair before deploying this update.

## 0.4.3 — 2026-10-21

- Preview and repair historical creation/joining dates on migration-created people,
  accounts, memberships and applications without importing them again. Source
  timestamps remain separate from joining dates, destination modifications and
  the migration ledger. Preserve adopted records and report missing evidence.
- Require review of an immutable before/after plan and a current backup. Commit
  at most 1,000 records per server job, retain the cursor with its changes, and
  stop if native records or ledger mappings change after preview. The existing
  creation-based growth chart then reflects source registration history.

## 0.4.2 — 2026-10-21

- Send attachment confirmation and reviewed-code checkboxes as JSON booleans,
  matching the strict copy/resume endpoints. Checked Frappe controls return numeric
  values; previously the copy dialog rejected valid operator confirmations.

## 0.4.1 — 2026-10-21

- Limit attachment planning to converted identities and referenced archive parent
  types. Skip source tables with no native conversions before loading their rows,
  avoiding a full comments/communications scan for every attachment plan.

## 0.4.0 — 2026-10-21

- Add a separate attachment review and resumable copy workflow after a successful
  persistent batch test. Preserve its test checkpoint and existing people/history;
  do not promote trial data to live import or change login policy.
- Prepare private plans before any copy. Hold missing bytes, unknown/shared-person
  ownership, identical contents used by different people, and conflicting child
  attachment slots. Include transformed Red Profile documents, historical Attach
  fields/HTML, avatars and file-based TOR evidence.
- Require explicit plan review, backup and disposable-site confirmation. Copy only
  planned files, privately, with SHA-256/owner checks and atomic independent batch
  checkpoints. Reuse verified copies on retry; never overwrite another field value.
- Add progress, pause and retry controls. Capture/settings/mapping changes stop the
  copy; explicitly reviewed code-only changes can resume its unchanged plan.
  Held exceptions and manual cross-user/Guest access checks remain outstanding.

## 0.3.7 — 2026-10-20

- Compare native IDs and archived target links as exact text during final
  reconciliation. Prevent false missing-target failures for numeric Buzz Event
  and Event Ticket Type IDs without hiding genuinely absent records or merging
  identifiers with leading zeros. Missing-target reports now include the
  destination doctype and ID.

## 0.3.6 — 2026-10-20

- Reuse Buzz's installed standard Event Categories by name and record their
  source conversion links, avoiding duplicate insertion failures on retry.
  Preserve destination values and report differences; original source values
  remain archived. Custom categories and other record collisions still block.

## 0.3.5 — 2026-10-20

- Extend reviewed archive-only handling to history requiring absent Employees
  and standalone assignments without a usable deployment or structured TOR.
  Keep source records and original attachment references; never fabricate scope.
- Report history cascading from approved source-person exclusions separately
  from new history gaps. Keep source-person approval gates and the 1% limit for
  new or unclassified history gaps, in both batch and full-run verification.
- Missing Employee mappings still block when the Employee exists in the source,
  or the source Employee identity index is unavailable. Attachment ownership and
  missing-byte gates remain unchanged.

## 0.3.4 — 2026-10-20

- Extend invalid-phone review to historical TOR stakeholder phones. Preserve
  recorded TOR resource UOM labels without inventing unit conversions.
- Add explicit review for history requiring Users or Terms of Reference absent
  from the capture. Retain source payloads and persist exclusion dispositions
  with batch checkpoints, so retries and final verification account for them.
  Propagate approved person exclusions to dependent history; unresolved mappings
  for source-present records still block.
- Write per-batch history warning/exclusion CSVs and identify missing link fields
  in errors. Existing live-import gates and exclusion limits remain enforced.

## 0.3.3 — 2026-10-20

- Allow explicit, reviewed geography mappings for operational history without
  reopening excluded volunteer or membership affiliations. Validate target nodes
  and include the configuration in the existing test-session review fingerprint.

## 0.3.2 — 2026-10-20

- Apply the reviewed invalid-phone setting to professional references and
  emergency contacts as well as the main profile phone. Keep the person and
  reference details; omit a contact only when its required primary phone is
  invalid. Preserve source values and write private per-batch phone-review
  reports. Strict mode and normal application validation remain unchanged.

## 0.3.1 — 2026-10-19

- Stop batch preflight before loading when a mapped lookup target is missing.
  Preserve the crosswalk usage collected during planning so missing targets
  appear in reports. Recheck them during final verification. Reviewed residence
  fallbacks remain warnings.

## 0.3.0 — 2026-10-19

- Add persistent test imports in resumable batches of up to 1,000 records, with
  independent background jobs, atomic database checkpoints, pause/resume and
  private reports for every batch. Failed rows stop automatic sequencing.
- Add the same batching option to authorized live imports while retaining the
  successful rollback rehearsal and cutover confirmation requirements.
- Split people reconciliation into batches and skip previously converted history
  through the source conversion index. Test batches never authorize live cutover.
- Show actual worker progress instead of stale file totals from capture.
- Honor the existing invalid-phone review setting for Frappe's native phone
  validation as well as society-specific patterns; strict mode remains strict.
- Send structured browser requests as JSON, preserving numeric limits and arrays.

Upgrade: deploy on the destination, migrate its schema and reload assets. Finish
or stop the previous job through hosting before restarting workers. Test batches
keep destination records; use a disposable destination and preserve its backup,
capture and receiving keys before testing or rebuilding it.
