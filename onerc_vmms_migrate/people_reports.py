"""Private reports exposed by the existing System Manager download endpoint."""

from collections import Counter
from onerc_vmms_migrate.history_model import application_state
from onerc_vmms_migrate.file_recovery import external


def status_rows(snapshot, built):
	counts = Counter()
	for row in snapshot["applicants"].values():
		target = application_state(row)
		flag = (str(row.get("status")).lower() == "replied" and target == "In Review") or (
			row.get("status") == "Open" and int(row.get("docstatus") or 0) == 1 and target == "Draft"
		)
		counts[
			(
				"Job Applicant",
				row.get("status"),
				row.get("docstatus", 0),
				target,
				"Approval needed: existing mapping was not agreed" if flag else "Existing mapping; review",
			)
		] += 1
	for bundle in built["bundles"]:
		for row in bundle["memberships"]:
			counts[
				(
					"VM Membership",
					row.get("legacy_status"),
					"",
					row["membership_status"] + " / " + row["approval_state"],
					"Existing mapping; original status retained",
				)
			] += 1
	return [
		dict(
			zip(("source_doctype", "source_status", "source_docstatus", "target_state", "review"), key),
			count=count,
		)
		for key, count in sorted(counts.items(), key=lambda pair: str(pair[0]))
	]


def write(snapshot, built, run_id):
	from onerc_vmms_migrate import run, workspace

	directory = workspace.report_dir(run_id)
	run._write_csv(directory, "status_mapping.csv", status_rows(snapshot, built))
	run._write_csv(
		directory,
		"field-review.csv",
		[{"person": b["key"], **row} for b in built["bundles"] for row in b.get("_review_values", [])],
	)
	run._write_csv(directory, "not_migrated.csv", built["not_migrated"])
	run._write_csv(
		directory,
		"source-file-exceptions.csv",
		[
			{
				"source_url": url,
				"status": "external" if external(url) else item.get("status"),
				"reason": "External web link" if external(url) else item.get("reason") or item.get("errors"),
			}
			for url, item in snapshot.get("all_file_manifest", {}).items()
			if item.get("status") != "ready" or external(url)
		],
	)
	workspace.write_json(
		directory + "/people-scope.json",
		{
			"Version": "Included after people and their files",
			"Version_capture_available": "Version" in snapshot.get("schemas", {}),
			"bank_name": "Deferred; original retained",
			"Awards nominations": "Deferred; original retained if captured",
			"skills": "Excluded",
			"ward/location/sublocation": "Excluded",
		},
	)
