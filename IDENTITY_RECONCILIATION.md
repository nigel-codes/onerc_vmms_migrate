# Missing email and identity reconciliation

The approved missing-email format is `missingemail1@vmms.com`,
`missingemail2@vmms.com`, and so on. This is a receiver-only change. It needs no
exporter, VMMSX, onerc_vmms or onerc_core code changes and no new source capture.

Enable `assign_missing_emails: true` inside the destination's existing
`onerc_vmms_migrate` site configuration after installing the receiver update and
running its `after_install`/`after_migrate` hook. The default is off. Merge this
key into the existing configuration; do not replace the capture/login settings.

The receiver stores stable assignments in
`private/onerc_vmms_migrate/placeholder-emails.json` (mode 0600). Source site,
DocType and document name identify each assignment. Reordering records,
retries, extra source records and later captures do not renumber existing
aliases. Records join only through explicit source Employee/Member/Applicant or
User links. A linked record with a usable email keeps that identity instead of
getting a second placeholder. Source and existing destination emails are
reserved. Newly conflicting links or claims require reconciliation.

The protected snapshot is unchanged. Source fields, original volunteer/member
numbers and existing passwords are never rewritten to create aliases. No
password, approval or membership is invented. A placeholder profile may have
native volunteer/member affiliations when all their native validations pass;
its `Imported placeholder email` marker makes the correction requirement visible.
An occupied destination address is a conflict, never an unrelated profile/account adoption.

An existing source User with no usable login email also receives a stable alias.
The transfer retains that User's enabled/disabled state, username, social login
bindings and actual captured password hash when present. Only that original
source User's ledger mapping permits reuse on a retry. A record without a source
User does not receive a new login. Alias accounts cannot receive email-link
login or password reset messages; users without another preserved login method
need a verified real address before they can sign in. This limitation appears in
the reconciliation register. Missing mandatory account names remain validation
failures rather than being filled with an invented name.

The companion's Frappe Email Queue extension refuses delivery to registered
placeholder recipients, including forced sends and mixed recipient queues.
Ordinary email uses Frappe's existing transport. If the registry is missing or
invalid, sending to this reserved address pattern fails rather than releasing
mail. Retain the companion and registry while aliases remain in use. Verify and
set the real email before enabling a login; preserve the historical registry.
The marker is historical evidence and is not proof that a later email has been
verified. No alias activation/reset endpoint is provided by this change.

**Back up this private registry separately with the source capture and recipient
key. Standard site file backups do not include this directory.** The registry is
covered by the rehearsal/batch input fingerprint. Batch initialization prepares
it before freezing that fingerprint, so manual assignment changes invalidate
the previous rehearsal/resume inputs.

Plan reports include `identity-review.csv`, one row per issue and original
source record. It retains original name/email fields (including nulls), original
numbers/status, issue and conflict group, destination email, login availability,
and planned eligibility. Rows and people counts differ: issues can overlap.
Missing mandatory names, multiple volunteer/member identifiers on one email,
shared identification claims and orphan memberships are explicitly listed.
These reports contain personal data and remain under the protected site folder.

Final batch verification and ordinary reconciliation write
`identity-reconciliation.csv` with exact migration-ledger destination links.
To refresh that report separately:

```sh
bench --site TARGET execute onerc_vmms_migrate.run.identity_report
```

Ledger links are recorded mappings, not independent proof of current native
record validity. The existing native reconciliation still checks record values.
Reports do not label an archived record or mapping candidate as a usable native
account. This change reports missing names and conflicting public identities;
it does **not** bypass native mandatory names or unique profile/affiliation
constraints. Their historical compatibility handling remains pending.

The receiver handles missing emergency-contact relationships by retaining the
contact under an explicit `Not recorded` lookup. It creates that lookup inactive
when needed, so new registrations do not offer it, and does not change existing
society relationship entries. The missing field is reported for correction;
no actual relationship or contact consent is inferred. This resolved the
57-check native self-test on the clone. Passing the self-test or alias checks
does not certify the full source-data migration.
