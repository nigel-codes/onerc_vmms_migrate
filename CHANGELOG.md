# Changelog

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
