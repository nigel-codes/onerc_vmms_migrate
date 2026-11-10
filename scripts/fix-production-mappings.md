# Fix the reviewed production mappings

Destination: **https://kenyaredcross.jh.frappe.cloud**. Capture: **b09e06d9822849e8a1a70d550e882cb0**.

This browser tool addresses the 84 source values in the supplied 10 November 2026 `crosswalk_problems.csv`. It uses the destination's existing records and saves mapping files through the migration app's authenticated API. It needs no backend deployment and makes no changes to VMMSX, onerc_core or onerc_vmms product code. It does not import people, create geography or create more lookup records.

## Use

1. Keep migration jobs and mapping editors idle. Open the new site's Desk as Administrator or a System Manager.
2. Open `vmms-fix-production-mappings.js` from the Desktop. Copy the entire file into the browser developer console and run it. Loading performs a **read-only preview**.
3. Inspect `vmmsMappingFix.report.proposed` and `vmmsMappingFix.report.held`. Geographic proposals include the destination level and ancestry. Existing explicit targets and omission defaults are preserved.
4. Save the proposed mappings:

   ```javascript
   await vmmsMappingFix.apply()
   vmmsMappingFix.downloadReport()
   ```

5. In Migration Settings, run **Plan migration** using this same capture. There is no need to generate mappings again first. Download its new `plan.json` and any remaining `crosswalk_problems.csv` / `rejects.csv`.

Apply rebuilds its plan from current destination data and refreshes each mapping file before saving. It fills still-blank targets only. Do not edit mappings concurrently: the server API has no compare-and-swap operation or operator-held lock. Each file save is independent. A failure keeps completed saves; apply can resume. `vmmsMappingFix.stop()` stops before the next request. If a response is lost, the script reads back the mapping file before deciding whether saving failed.

## Rules implemented

- Branches match **county-level** Geo Nodes, even where a subcounty shares the name.
- **KRCS - HQ** and **krcs branch test** use **Nairobi county**, as explicitly approved.
- Curly apostrophes, apostrophes, spaces, hyphens and capitalization are treated as geographic spelling differences. Thus `Murang’a` can match `Murang'a`, and `HOMABAY` can match `Homa Bay`. Different historical names or boundaries are not inferred.
- Residence mappings match their recorded **county** or **subcounty** level. A subcounty must have a county ancestor, and the destination tree must lead to a national node. Missing, ambiguous or invalid trees are held. The script does not turn an unresolved subcounty into a county or insert an omission. The existing importer can use that person's separately recorded mapped county.
- Education, skill, identification and disability use a **unique existing label**, with the actual stored destination ID. This avoids the generator treating an input spelling and the stored lowercase ID as different candidates.
- Only the 84 reviewed source values are considered. Other mapping kinds, already-filled targets, people, native lookup records, pricing, roles and payments are untouched.

## What the supplied reports actually show

`plan.json` reports **25,772 people**, **7,997 ready**, **17,775 rejected**, **631 members ready**, **994 memberships ready** and **5,898 volunteers ready**. These are planning counts, not imported records. Counts for volunteers, members and memberships measure different things and should not be added to the person count.

The detailed rejection file confirms **17,772 distinct people have only unmapped-value errors**. The remaining **three** have multiple source member numbers sharing one profile. Fixing all blocking mappings should remove the current mapping errors; a new plan and native validation must confirm the resulting eligibility. People mode separately defers shared volunteer identities, so a plan-ready count does not guarantee every person is importable in that mode.

| Mapping kind | Unmapped source values | Distinct people with this rejection |
| --- | ---: | ---: |
| Branch | 28 | 15,314 |
| Education level | 5 | 2,926 |
| Identification type | 1 | 208 |
| Skill | 4 | 22 |
| Disability | 1 | 9 |
| Residence | 45 | No residence rejection; warning/fallback behavior |

Person counts overlap. The **39 blocking mapping values** and **45 residence values** total **84**. The earlier generator display of 1,791 is not the current count: the writer can suggest targets in CSV output without changing the in-memory object used for that immediate count. The supplied plan and detailed CSVs reload the written files.

The preceding lookup preparation succeeded: **1,707 native lookup records created**, **11 existing labels reused**, zero held and zero failures. Those 11 reused labels are precisely the five education, four skill, Passport and Low Vision values still requiring explicit mapping here.

`mapping_issues.csv` contains **zero field/schema errors** and **2,044 review rows**: **2,016 child-link references to inactive driving classes** and **28 missing last-name entries**. These counts are rows, not distinct people. This tool does not activate driving classes or invent surnames. The existing blank-name policy remains in force.

`identity-review.csv` contains **754 review rows**, not 754 proven failed accounts: 516 conflicting identifications, 80 shared volunteer emails, 63 missing names, 47 missing source members, 31 placeholder emails, 11 missing login emails and six shared member email rows. Its status labels were generated while many people were still mapping-blocked. Do not use those status totals as a post-fix account reconciliation. The three currently rejected member identity groups remain deferred; preserving both exact member numbers requires a separate reviewed identity decision.

There are **60,240 warning rows**, largely repeated messages about absent emergency contacts, additional old roles requiring deliberate transfer, archived motivation text and untyped identification numbers. They are not 60,240 rejection errors. Other warnings flag raw profile values and geography fallback. Role/profile transfer and authority must be checked separately before calling the result a usable clone.

## Remaining qualification after mapping repair

This is a mapping repair, not a declaration that the full clone is complete. The existing obligations remain: confirm captured `source.profile_mapping_version == 2` and deployed exporter code; validate native people inserts; retain identity deferrals/review; verify account state, hashes and roles; complete attachment exceptions and attachment linking; check history and payments in their later phases; reconcile a final delta because the old production site remains live. No hosted people import has been confirmed by the agent.

## Validation

Tests run with `node --test --test-isolation=none scripts/test-fix-production-mappings.cjs`. They cover level disambiguation, approved Nairobi overrides, spelling, ancestry, lowercase native IDs, native disability fields, ambiguity, preserving manual/default choices, intervening edits, site/capture/job guards, partial failure and resume, lost responses, read-back verification and pagination. These are isolated Node VM/API-contract tests, not an agent-executed production browser test.
