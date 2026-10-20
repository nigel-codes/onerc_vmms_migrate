# Changelog

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
