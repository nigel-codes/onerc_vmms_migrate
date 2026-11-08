# Native mapping validation — 8 November 2026

- Receiver: 237 isolated unit tests passed, including native profile/child mapping, insurance/consent exclusions, zero/unknown preservation, effective education metadata, schema/value gates, source member-ID conflicts and complete profile scalar/child reconciliation.
- Exporter: 39 isolated unit tests passed, including effective Property Setters and capture-version transport.
- Undefined-name/syntax checks and git diff whitespace checks passed. A deliberate zero/answered mutation failed its regression test and was restored.
- A READ ONLY extraction of the local cloned source recovered all 3,253 education rows with captured User parents; a pure mapping preview evaluated 25,000 person bundles against current installed kenyaclone metadata and verified existing lookup records. No Frappe site was initialized, native records inserted, source rewritten, schema/seed run, external production contacted or payment replayed.
- The preview is not a native loader/controller/API/browser rehearsal. It reports remaining blockers and archive-only scope; it does not certify cutover. Existing SQLite fixture ResourceWarnings were visible and not suppressed.
- Only verified migration-app code and two migration crosswalk CSVs are included in this update. Both product apps remain outside its write scope. Worker reload, fresh version-2 capture and the next reviewed rehearsal are separate operational steps.

The dated validation below describes the earlier preparation, not the current mapping changes.

# Preparation validation — 4 November 2026

Prepared with Python 3.14 in isolated local checkouts. No site was initialized and no live/source/destination database, gateway, worker, scheduler, schema or numbering policy was changed by this preparation.

- Receiver: 197 isolated unit tests passed. The suite emitted SQLite ResourceWarnings from existing fixture paths; there were no test failures or disabled tests.
- Exporter: 35 isolated unit tests passed.
- Python undefined-name/syntax checks and git diff whitespace checks passed. Existing receiver configuration excludes unused-import findings; strict unused-import checks also passed on the new feature modules/tests.
- Added tests cover source audit/payment scope, physical-only columns, protected/retired/nested credentials, source field labels and naming metadata; historical payment fidelity, repeat-import integrity, projection reconciliation and immutability; original names, occupied IDs and shared member identity conflicts; stored counters/gaps; and fresh-capture import requirements.

The unit suites use isolated fixtures/mocked Frappe boundaries. No Frappe schema migration, site deployment, actual payment-history import, real authentication, user-visible legacy history/QR compatibility or rollback rehearsal was performed. PREPARATION_STATUS.md lists remaining work and deployment compatibility review.

Commands used:

```sh
python -B -m unittest discover -s onerc_vmms_migrate/tests -q
python -B -m unittest discover -s onerc_vmms_export/tests -q
```

Both commands were run with the corresponding isolated checkout first on PYTHONPATH and the existing bench environment's Python interpreter. No dependency installation or network transfer was required.
