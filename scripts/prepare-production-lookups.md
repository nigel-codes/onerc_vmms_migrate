# Prepare missing production profile lookups from the browser

This script uses the destination's generated migration mappings as its source of vocabulary values. It does not copy the old local site's people or assume that local lookup identifiers exist on production. No application deployment is needed to use the script.

The script is pinned to `https://kenyaredcross.jh.frappe.cloud` and capture `b09e06d9822849e8a1a70d550e882cb0`. A different site or selected capture is refused. Generate mappings for that capture before using it. Use a System Manager account on the new site's Desk and keep other migration, attachment, date-repair and mapping work idle while it runs.

## Run

1. Open the **new site's** VMMS Migration Settings and your browser's developer console.
2. Paste the entire `prepare-production-lookups.browser.js` file. Loading runs a **read-only preview** and prints counts by vocabulary. It does not create records.
3. Review `vmmsLookupPrep.report.proposed` for each value that would be created or reused, and `vmmsLookupPrep.report.held` for exceptions. A `blocked` vocabulary needs its invalid existing mappings reviewed first.
4. Add the supported missing reference records with:

   ```js
   await vmmsLookupPrep.apply()
   ```

5. Keep this page open until apply finishes. It prints progress every 25 lookups. Download the result with:

   ```js
   vmmsLookupPrep.downloadReport()
   ```

6. In Migration Settings, run **Generate mappings**, then **Plan migration**. Use the fresh plan and its reports to determine the actual remaining person blockers. The generator's immediate count may precede its newly written suggestions.

To stop between requests:

```js
vmmsLookupPrep.stop()
```

Completed inserts remain committed. Rerunning `apply()` refreshes the preview and reuses existing records. A failed/lost HTTP response is checked against the server before another creation is considered. There is no bulk rollback or automated deletion.

## Scope and rules

Supported vocabularies: skills, education levels, driving licence classes, identification types, professions, personnel licence types, disabilities, spoken languages and gender.

Source values are read through the installed migration app's mapping API. Existing labels and identities are checked against the destination's actual metadata and paginated lookup lists. An unambiguous existing label is reused. Existing values, active/inactive flags, explicit mappings and omissions are not changed. New keys follow the companion's deterministic lookup-key scheme. Unknown spoken languages receive disabled private-use codes, rather than being enabled as interface locales.

Inserts use Frappe's normal client API, native permissions and validation. No permission bypass flags are sent. The script holds ambiguous labels, occupied identifiers with different labels, overlong or HTML-like text, contact/URL/long-identifier-like text, missing additional mandatory fields, missing create permission and invalid existing mapping targets. The contact-text check helps prevent private free-form profile text becoming a shared selection; it does not establish that every remaining label is appropriate, so review the proposed values. It stops on unexpected write failures. Held items remain in the captured source and require review; they are not silently marked resolved.

It does not create geography, membership types/pricing, accounts, profiles, volunteers, members, roles or payments. Geography should come from the approved canonical Kenya seed. Membership type and pricing setup must use reviewed current source facts. It does not edit application code, site configuration, source data or mapping files, and does not start an import or assert final migration readiness.

The script checks migration idleness before preview, between groups of up to 25 lookups, and at each vocabulary boundary. This is not a server-held exclusive lock: keep other operators from editing the same lookup/mapping setup or starting jobs while it runs. Do not close/navigate away from the page during apply; the work runs in the browser, not an unattended background worker.

## Validation

Run the isolated API-contract tests with Node.js 24:

```sh
node --test --test-isolation=none scripts/test-prepare-production-lookups.cjs
```

The tests cover preview without writes, insertion scope, idempotent retry, inactive record preservation, explicit mappings/defaults, ambiguity, identifier collisions, invalid labels, additional mandatory fields, site/capture/job guards, permissions, concurrent manual mapping preservation, lost responses, failure reporting, language/disability fields, pagination and stopping/resuming. The script has not been executed against production. Actual source vocabulary completeness remains a mapping-plan review task.
