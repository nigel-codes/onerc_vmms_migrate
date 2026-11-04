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
