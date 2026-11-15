# Project mapping and numbering — receiver 0.8.0

Prepared on 15 November 2026. This release prepares Project mappings and guards
their volunteer participation links. It does not run an import, change either
product app or implement the remaining operational migration.

## Approved scope

- Preserve source Project document IDs and supported Project fields.
- Map the source branch Company through approved geography mappings into
  vmms_geo_node; use the destination legal Company in Project.company.
- Protect Project numbering from reserved/deleted source numbers and higher
  destination IDs/counters.
- Preserve the Project/TOR/Deployment/Assignment/Volunteer chain and exact
  offered TOR; report unsupported or contradictory source facts.
- No Project collaborator import. Task improvements and financial/customer/
  template/cost-centre links remain outside this slice. Existing Task code
  is unchanged.

## The source is still live

The local copy is an old snapshot, never the final inventory or numbering floor.
Every review/import reads its selected capture; new Projects and source counter
values in a later capture are included dynamically. Nothing hardcodes a Project
count or the old counter of 118.

Before the operational transfer, take a fresh capture after resolving any active
date-repair job. Keep the finished people checkpoint and original capture as
evidence. Import genuinely new account/volunteer dependencies before their
operational records; this release does not overwrite changed existing people.
Records created after a capture require a later manual catch-up. Freeze source
writes for the final capture and final reconciliation before switching sites.

## Project placement and numbering

Project IDs retain their exact source names. A missing native geography field
or unresolved destination legal Company is a setup error, not a guessed branch.
An explicitly recorded Project link must resolve, instead of being silently
omitted.

The import reserves the range represented by all captured Project IDs and
existing destination IDs before creating the first Project. Applicable source
series/rule counters include reserved/deleted numbers. Counter updates use
GREATEST so an already-higher destination counter cannot be lowered.

Source IDs inconsistent with the destination's naming policy are reported for
review. Updating naming policy is not silently guessed. Repeating the import
reserves the current capture's range again. A review calculates floors without
writing series, rule or numeric sequence values.

## Volunteer participation

VMMSX reaches a Project through its TOR. These guards check the source graph and
the actual native TOR.project relationship:

- Keep an explicit source TOR Project unchanged.
- Derive a missing TOR Project only when its recorded tool and individual-request
  contexts agree. Include requests inheriting their parent's TOR.
- Reject conflicting Project contexts and explicit missing TOR references.
  Never replace a nonblank missing offered TOR with the deployment's TOR.
- Keep an assignment's exact offered TOR even when the deployment names newer
  terms, provided their Project context is compatible.
- On retry, check the actual volunteer, original/derived deployment identity,
  offered TOR, Project, assignment/deployment geography and recorded response.
  A changed response is reported for reviewed catch-up, not silently overwritten.
- Derived orphan deployment containers include the required native title.

Pending remains an invitation; Accepted stays Accepted; rejected/cancelled
responses retain their established mapping. No attendance, hours, contracts,
approval decisions or organisational Deployment Requests are invented.

Projects alone do not expose participation: their native TOR, Deployment and
Assignment records must also be imported. Original file download, historical
TOR/deployment actionability and full staff Geo authority remain separate
qualification gates. A creator identity is preserved when resolvable; source
owner fallbacks remain visible in existing history warnings.

## Read-only Project review on Frappe Cloud

After deploying this receiver, sign in as a System Manager on the destination.
The existing long-worker controller accepts:

~~~javascript
frappe.call({
  method: "onerc_vmms_migrate.browser.start",
  args: { action: "project_plan" }
});
~~~

The finished people checkpoint is preserved. The normal migration lock prevents
concurrent workers. No source-freeze or LOAD confirmation is claimed by this
read-only action; it cannot authorize an import.

Watch Migration Settings progress/status and download the private reports:

- project-mapping-review.json — source/capture IDs, exact Project placement,
  derived TOR links, participation contexts, problems and numbering floors.
- project-mapping-problems.csv — named missing/conflicting source relationships,
  occupied target IDs, placement conflicts and unsupported numbering.

The summary is review_only. Source-link counts are not native-import counts or
proof of user access. Zero problems does not replace a native rehearsal of the
whole operational graph. Existing converted Projects are checked against their
mapping; occupied IDs without a matching source ledger are not adopted silently.

## Validation and remaining execution

Regression tests cover dynamic newer source counters, higher target IDs/counters,
dry-run nonmutation, Project geography, stable source IDs, missing/conflicting/
inherited TOR contexts, exact terms, retry ownership/parent/response/geography,
target collisions and the protected review action after completed people.

An older local read-only replay covered 83 Projects, 58 TORs, 109 tool Project
links and 4,268 individual-request Project contexts. Twelve requests have explicit
exceptions: nine lack a usable source parent/structured TOR, two reference missing
TOR records and one has contradictory Project context. One blank TOR Project can
be recovered from a consistent recorded tool link.

Those counts are historical test evidence. Production must regenerate the
reports from its fresh capture. No local or hosted business records, counters,
files, roles or settings were changed to obtain this replay.

Before committed operations, qualify the exact deployed schema on a disposable
target clone: original creator and assignee access, correct Project context and
TOR download, genuine next-Project creation above all floors, no duplicates or
notifications on retry, and independent field/reference reconciliation.
