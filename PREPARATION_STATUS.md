# Current mapping preparation — 8 November 2026

Receiver 0.6.0 / exporter 0.4.0 add native person mappings and stricter field/value reporting. See README.md and VALIDATION.md for the new contract and evidence. A fresh profile_mapping_version=2 capture is required; updating code does not repair the old captured payload. Crosswalk updates use 17 verified exact profession records and the approved insurance omission. No new native reference records are created by preparing those CSVs.

The remaining application-history/routing, geography, identity, payment-allocation, file and whole-module obligations from the deep audit remain open. Existing native fields are distinguished from unsupported values or missing operational adapters. The detailed site-specific evidence is stored outside app repositories.

The following section is the dated 4 November preparation record.

# Migration continuity preparation — 4 November 2026

Code prepared in onerc_vmms_migrate 0.5.0 and companion onerc_vmms_export 0.3.0. Neither vmmsx nor onerc_vmms source is part of this change. No site migration, installation, capture, import, payment request or numbering-setting update was run while preparing this code.

See VALIDATION.md for the 197 receiver / 35 exporter unit-test results and the limits of that verification.

## Implemented

- Export Navari STK/C2B/B2C records, full noncredential User history, Version, activity/access/workflow/deletion logs and financial/permission dependencies previously outside the business scope.
- Preserve physical columns missing from current DocType metadata. Record SQL column types, protected omissions, unparsed-history exceptions, all uncaptured table obligations and source naming-policy evidence in the capture's continuity manifest. Uncaptured tables and sealed backups remain explicit requirements; the manifest never claims a lossless cutover has been proved.
- Protect known structured credential fields, including nested Version/deletion changes. Original protected payloads require the complete sealed source backup; ordinary exports are not a substitute for that backup.
- Add persistent manager-readable Legacy M-Pesa Payment and Legacy Payment Allocation DocTypes. Parent evidence records preserve exact original state, receipt/reference, amount as decimal text, currency absence, raw payload/hash, source actors and dates. Original evidence cannot be edited; resolved linkage fields remain a separate review concern.
- Populate immutable historical payment projections during the transactional archive phase, including resumable batches. Reimport reuses the source-version identity and verifies its retained body. No gateway/accounting/membership processor is called. Allocations are deliberately left unresolved until source evidence and review establish them.
- Reconcile payment projections, and archive full User history once instead of appending a second partial operational User representation.
- Preserve original volunteer/member/membership numbers during native creation. Existing records with different numbers, occupied identifiers and multiple source member IDs require explicit review. Historical fee/joining/currency/member-number fields are populated where the destination schema supports them.
- Provide a read-only naming continuation preview using source Document Naming Rules, stored series and existing source/destination suffixes. Preserve gaps and source rule counter semantics. Conditional/multiple rules require explicit review; no rule or counter is applied automatically.
- Require a fresh continuity capture in full-import/rehearsal preflight. Old captures remain available for inspection; upgrading code cannot silently turn their omitted history into a complete capture.

## Deployment and capture boundary

The apps must be reviewed and installed/upgraded on a disposable rehearsal destination before any real import. Existing captures do not acquire missing records or naming metadata by upgrading code. Update both apps and take a fresh capture. Retain older snapshots/checkpoints; changed capture/code requirements must not be used to restart or overwrite an existing live import silently.

The selected future destination is kenyaredcross.localhost, which already has data. This preparation does not authorize resetting it. The migration companion must remain installed while it owns retained historical evidence; removing it can remove its DocTypes/history. Its existing packaging metadata advertises a Frappe 16 compatibility range; the actual Frappe 17 development destination requires a separate runtime/deployment compatibility review before installation.

After a fresh reviewed capture is configured, a System Manager can request the read-only naming preview through `onerc_vmms_migrate.naming_plan.preview`. It reads the captured policy and destination names/counters and returns a plan with applied=false. Applying that policy is a later reviewed operation.

## Still pending before a lossless switch

- Resolve shared/missing person identities, orphan member links, national/HQ geography and existing destination adoption/credential/authority decisions.
- Resolve or explicitly retain as unallocated the historical completed receipts without membership/accounting evidence; determine old pending-request/callback cutover handling.
- Review all unmatched fields/features and complete native registration/child/geography mappings. This release does not claim full operational parity or migrate all other installed applications.
- Complete the source-schema-aware audit display, combined legacy/new payment history, allocation-review workflow and legacy card/QR compatibility.
- Recover/verify missing and external files; test byte integrity, owner links, privacy and actual logins/permissions.
- Verify the sealed full source backup, actual schema migrations and source/destination compatibility; rehearse against copies of both populated sites, reconcile every preservation obligation and prove rollback.

No runtime database/schema rehearsal or browser proof is implied by isolated unit tests. The mapping package and review register prepared on 3 November remain the source for unresolved business decisions.
