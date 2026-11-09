# People first: deployment and operating notes

Prepared 9 November 2026. This is a staged data preload, not a completed site switch.

## Fixes verified locally

The migration companion accepts recorded blank names without fabricating replacements. Its narrowly scoped User/Profile compatibility preserves ordinary registration requirements. Unsupported phone, social URL, dependant, long education subject and reversed end-date values are retained in the profile's **Information to review** field and private reports rather than rejecting the entire person.

Native User inserts and role updates enqueue derived Contact jobs. The local default queue reached its limit and later people rolled back with `QueueOverloaded`. During the migration's existing silenced context, only the native `create_contact` job now runs synchronously through Frappe's supported `now=True` option. Native Contact validation still runs in the person transaction and rolls back with it. Unrelated jobs retain their normal scheduling. No shared queue is purged or enlarged.

Verification: 290 receiver unit tests and 39 exporter unit tests passed. The native clone self-test passed 57 checks. Additional native probes created User, Contact, Profile and Volunteer records with the default queue deliberately unavailable, including fully blank names and reversed training dates; their synthetic records all rolled back. Existing queued Contacts may be processed concurrently; observed additions were checked against real imported User provenance.

## Deploy the companion apps

Deploy the tested receiver revision from `nigel-codes/onerc_vmms_migrate` (`develop`) and exporter revision from `nigel-codes/onerc_vmms_export` (`main`). A Git push does not establish that a Frappe Cloud bench has deployed it. Confirm the actual deployed hashes and run the app's site migration on the appropriate source/destination benches. Install the exporter on the old site and receiver on the new VMMSX site; do not replace either business app's source code.

The destination URL, hosting access, Frappe/core/VMMSX versions and installed apps must be checked on that destination. A passing local test does not prove a different hosted schema is ready. Back up the existing hosted destination before imports and preserve private capture, placeholder registry and login-transfer key material separately from ordinary file backups.

Configure an immutable capture with profile mapping version 2, source provenance, and destination-bound sealed login transfer. The local restored backup is not a fresh view of an old production site that is still accepting changes. The source path on this workstation cannot be used as a Frappe Cloud filesystem path. Use an explicitly transferred, verified capture or the source export transport. No source credentials or private capture are included in either Git repository.

Configure the approved review policy (`import_with_review`, placeholder-email assignment, county/subcounty residence policy) and validate crosswalks against the destination's actual Geo Nodes and other native lookups. Copying code does not transfer those private mappings or guarantee identical Geo Node IDs. Preserve and reuse the correct source-scoped placeholder registry on every retry.

## People-only entry point

The existing people loader now has an explicit `defer_files=True` option:

```python
onerc_vmms_migrate.run.load(confirm="LOAD", defer_files=True)
```

This is a trusted bench-execute entry point, not a new browser action. It still requires the destination scheduler to be disabled. Keep outbound notifications muted and the destination closed to normal use during this preload. Run the native self-test on the destination and first verify a small bounded load with `limit`, then run the full reviewed people scope in a durable hosted worker or supervised shell with sufficient runtime. Do not run two importers concurrently.

It imports eligible people, their supported accounts, profiles, volunteers, members and memberships. It does not automatically import all account-only users, staff authority/profile definitions, every historical application or all other modules. Original public numbers and captured credentials are retained where source ownership is unambiguous. Shared volunteer/member identities remain explicitly reported for reconciliation rather than arbitrarily merged.

With `defer_files=True`, the person transaction does not read/copy profile attachment bytes. Original attachment references remain in the immutable capture, and attachment exceptions are reported separately. The summary explicitly says attachments were deferred and full migration qualification is false. It is permissible to preload people when attachment bytes are incomplete because this option does not attempt to copy those bytes; the default loader and full-import file gates remain intact.

Each successful person transaction commits its migration ledger. Restarting skips those committed people and reports source changes; it does not update changed existing people automatically. A retry must use the reviewed capture, registry and mappings. `local_people=True` remains restricted to the local clone and must not be used on production.

## Following phases and final switch

1. Read back the people identifiers, account states, available credential hashes, affiliations, memberships and exception reports on the hosted destination.
2. Run a separately reviewed attachment backfill with verified bytes, privacy and owner/field links. Rerunning the people loader alone skips existing people and does not backfill their files. Existing trial attachment controls require their own successful persistent test and are not automatically qualified by a people-only load.
3. Complete the remaining account/staff access, application/correspondence, audit, historical payment, naming continuation and deferred project/deployment/TOR phases. Current native membership facts are distinct from complete historical M-Pesa/accounting restoration; the people preload does not post GL entries or retry old gateway requests.
4. Freeze old-site writes for the final switch and reconcile changes made after the preload's snapshot. Automatic update of changed already-imported people is not implemented by this loader; an approved delta/reconciliation procedure is still required. Do not enable the new site for general use and call the migration complete while those changes are unaccounted for.

At preparation time the local clone retained 790 profiles, 742 volunteers, 37 members and 73 memberships. Its original pass was stopped after the queue overflow; these committed records were preserved. The private failure ledger distinguishes queue failures from source-value corrections. This is evidence of successful native transfer and an identified runtime blocker, not evidence of a completed full migration or a production run.
