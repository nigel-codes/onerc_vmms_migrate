"""Operational history after the people session: projects, tasks, terms of
reference, deployments and their requests, events, availability and time logs.

The production people session has no history phase, and the full-import batch
scope would re-run accounts and people. This runs the history alone, against
the selected capture, after the people are in:

1. archive — every captured row of the converted doctypes (and their child
   tables) into Legacy VMMS Document. The conversion ledger lives on these
   rows, so a re-run skips what is already done;
2. convert — the history adapters in dependency order, a page at a time,
   committing each page with its ledger marks;
3. files — attachments of the converted records only;
4. reconcile — every captured record converted, excluded or reported.

Preview runs all four inside one transaction and rolls it back. Import commits
page by page; a stopped run is simply started again. A record that fails is
reported and the run goes on: the operator reads history-not-imported.csv,
fixes the cause and runs Import again, which picks up only what is left.
Nothing is sent: everything runs silenced, and a page that would leave a
message behind stops the run before it commits.
"""

import os
import time
from collections import Counter

import frappe
from frappe.utils import now_datetime, strip_html

from onerc_vmms_migrate import helpdesk_load, historical, workspace

CONFIRMATION = "HISTORY"
PAGE = 500
METHODS = ("projects", "terms", "deployments", "events", "applications", "availability", "time_logs", "helpdesk")
# Source doctypes the adapters convert. Child tables of these come along in the archive.
CONVERTED = (
	"Project",
	"Task",
	"Personnel Terms of Reference",
	"Deployment Request Tool",
	"Personnel Deployment Request",
	"Event Category",
	"Event Host",
	"Event Venue",
	"Buzz Event",
	"Event Registration Question",
	"Event Ticket Type",
	"Event Booking",
	"Event Ticket",
	"Event Check In",
	"Event",
	"Job Applicant",
	"Shift Type",
	"Personnel Availability Schedule",
	"Timesheet",
	"Timesheet Detail",
)
# Archived as rows only: their child tables are people data the people session kept.
PEOPLE_OWNED = ("Job Applicant",)
# Read for other records, never converted themselves: Shift Types shape the
# availability slots, a Timesheet is carried by its Timesheet Detail rows.
NOT_CONVERTED = ("Shift Type", "Timesheet")
# Shown first in every summary: what the person asked to import.
CHAIN = ("Project", "Personnel Terms of Reference", "Deployment Request Tool", "Personnel Deployment Request")
# Helpdesk tickets and their threads (0.10.0). Apart from CONVERTED because
# Comment, Communication, ToDo and Version are whole tables in the capture:
# only their ticket rows are in scope (helpdesk_load.SCOPE, scoped_rows).
HELPDESK = helpdesk_load.CONVERTED


def preview(progress=None) -> dict:
	"""The whole import, rolled back. Writes the reports; changes nothing."""
	_assert_people_in()
	real_commit = frappe.db.commit
	frappe.db.commit = lambda *args, **kwargs: None
	try:
		result = _run("history-preview", live=False, progress=progress)
	finally:
		frappe.db.rollback()
		frappe.db.commit = real_commit
	return result


def apply(progress=None) -> dict:
	_assert_people_in()
	return _run("history-import", live=True, progress=progress)


def validate_request(confirmation, backup_confirmed) -> None:
	if confirmation != CONFIRMATION:
		frappe.throw(f"Type {CONFIRMATION} to import the history.")
	if not backup_confirmed:
		frappe.throw("Confirm that you have a current database backup.")
	_assert_people_in()
	_assert_quiet_site()
	capture = workspace.config().get("browser_capture")
	if not _previewed(capture):
		frappe.throw("Run Preview history with the selected capture first, and read its report.")


def _assert_quiet_site() -> None:
	"""The import runs while the cutover holds the scheduler and email. Once
	jobs run again, their own messages would mix with the import's and its
	outbox check would stop it part-way."""
	from frappe.utils.scheduler import is_scheduler_disabled

	from onerc_vmms_migrate.browser import state

	if not state().get("cutover") and not is_scheduler_disabled(verbose=False):
		frappe.throw("Import history before Finish cutover. Once the cutover is finished, "
			"switch the scheduler off (System Settings) for the import.")


def _run(kind, live, progress):
	from onerc_vmms_migrate.batches import build, excluded_identities
	from onerc_vmms_migrate.continuity import require_capture
	from onerc_vmms_migrate.extract import extract
	from onerc_vmms_migrate.history_load import Loader
	from onerc_vmms_migrate.silence import Guard, silenced
	from onerc_vmms_migrate.source import Source

	conf = workspace.config()
	run_id = _run_id(kind)
	directory = workspace.report_dir(run_id)
	started = time.monotonic()
	report = {"kind": kind, "started_at": str(now_datetime()), "capture": conf.get("browser_capture")}
	_report(progress, "reading the capture", 0, None)
	with Source(conf) as source, silenced():
		snapshot = extract(source)
		require_capture(snapshot)
		excluded_identities(snapshot, build(snapshot, conf))
		keep_imported_people(snapshot)
		guard = Guard()
		commit = _committer(guard, live)

		report["archive"] = archive(snapshot, run_id, commit, progress)
		loader = Loader(snapshot, run_id, source, conf, dry_run=not live)
		loader.guard = guard  # allows the ticket emails and assignments it writes, nothing else
		report["phase_errors"] = convert(loader, snapshot, live, commit, progress)
		_report(progress, "files", 0, None)
		report["files"] = files(snapshot, source, run_id, live)
		commit()
		guard.check()

		report["records"] = reconcile(snapshot, loader)
		report["statuses"] = statuses(snapshot)
		loader.field_drops = fields_not_imported(snapshot) + helpdesk_load.field_drops(snapshot)
		report["fields_not_imported"] = len(loader.field_drops)
	report["warnings"] = len(loader.warnings)
	report["exclusions"] = len(_unique(loader.excluded))
	report["failures"] = len(_unique(loader.failures))
	report["seconds"] = round(time.monotonic() - started)
	report["finished_at"] = str(now_datetime())
	_write(directory, report, loader)
	return {
		"report": os.path.basename(directory) + "/history-summary.json",
		"not_imported": report["failures"],
		"records": {dt: report["records"][dt] for dt in CHAIN if dt in report["records"]},
		"statuses": report["statuses"],
		"phase_errors": report["phase_errors"],
	}


def _committer(guard, live):
	def commit():
		guard.check()  # Before anything is kept: a page that queued a message stops here.
		if live:
			frappe.db.commit()

	return commit


# 1. Archive -----------------------------------------------------------------


def archived_doctypes(snapshot):
	"""The converted doctypes and every child table they own."""
	schemas = snapshot.get("schemas", {})
	doctypes = [dt for dt in CONVERTED + HELPDESK if dt in schemas] + helpdesk_load.archive_only(schemas)
	for dt in [dt for dt in doctypes if dt not in PEOPLE_OWNED]:
		for field in schemas[dt].get("fields", []):
			child = field.get("options")
			if field.get("fieldtype") in ("Table", "Table MultiSelect") and child in schemas and child not in doctypes:
				doctypes.append(child)
	return doctypes


def archive(snapshot, run_id, commit, progress):
	from onerc_vmms_migrate.history_model import History
	from onerc_vmms_migrate.legacy_payments import TYPES as evidence

	history = History(snapshot)
	totals = Counter()
	problems = []
	# The old M-Pesa records are archived too, never converted or reconciled:
	# archiving them keeps each one readable as a Legacy M-Pesa Payment. A page
	# of them that will not archive is reported; it does not stop the history.
	schemas = snapshot.get("schemas", {})
	for dt in archived_doctypes(snapshot) + [dt for dt in evidence if dt in schemas]:
		_report(progress, "archive", 0, None)
		rows = [{"doctype": dt, "record": row} for row in helpdesk_load.scoped_rows(history, dt)]
		for start in range(0, len(rows), 1000):
			page = rows[start : start + 1000]
			if dt in evidence:
				frappe.db.savepoint("vmms_history_evidence")
			try:
				done = historical.archive(snapshot, run_id, records=page)
			except Exception as exc:
				if dt not in evidence:
					raise
				frappe.db.rollback(save_point="vmms_history_evidence")
				totals["evidence_not_retained"] += len(page)
				problems.append(f"{dt}: {strip_html(str(exc))[:200]}")
				continue
			totals.update({"created": done["created"], "reused": done["reused"]})
			commit()
		totals[dt] = len(rows)
	return {**dict(totals), **({"evidence_problems": problems} if problems else {})}


# 2. Convert -----------------------------------------------------------------


def converted_index(snapshot):
	source_id = str(snapshot["source"].get("source_id") or snapshot["source"]["db_name"])
	rows = frappe.get_all(
		historical.ARCHIVE,
		filters={"source_id": source_id, "conversion_status": "Converted", "source_doctype": ["in", CONVERTED + HELPDESK]},
		fields=["source_doctype", "source_name", "target_name"],
	)
	return {(r.source_doctype, str(r.source_name)): r.target_name for r in rows}


def convert(loader, snapshot, live, commit, progress):
	"""Pages through the adapters. Each page replays them in dependency order:
	converted records are skipped through the ledger, failed ones through the
	run's own list, so every page only spends its allowance on new records."""
	from onerc_vmms_migrate.history_load import BatchBoundary
	from onerc_vmms_migrate.silence import SilenceBreached

	loader.naming.dry_run = not live
	loader.skip_identities = set()
	loader.validated = set()
	broken = {}
	page = PAGE if live else 10**9
	while True:
		loader.batch_limit, loader.batch_deadline = page, time.monotonic() + 3600
		loader.batch_attempted, loader.batch_progress = 0, None
		loader.converted_index = converted_index(snapshot)
		complete = True
		for name in METHODS:
			if name in broken:
				continue
			frappe.db.savepoint("vmms_history_method")
			try:
				getattr(loader, name)()
			except BatchBoundary:
				complete = False
				break
			except SilenceBreached:
				raise
			except Exception as exc:
				frappe.db.rollback(save_point="vmms_history_method")
				frappe.clear_messages()
				broken[name] = strip_html(str(exc))
				loader.failures.append({"source_doctype": name, "source_name": "__phase__", "error": broken[name]})
		loader.skip_identities.update(
			(row["source_doctype"], str(row["source_name"])) for row in loader.failures
		)
		commit()
		_report(progress, "convert", len(loader.converted_index), None)
		if complete:
			return broken


# 3. Files -------------------------------------------------------------------


def files(snapshot, source, run_id, live):
	"""Attachments of the converted records, and nothing of the people's:
	references to other doctypes, applicants' included (the people session
	placed those on profiles), are dropped before the copy."""
	from onerc_vmms_migrate import full_run
	from onerc_vmms_migrate.history_model import History

	parents = set(CONVERTED + HELPDESK) - set(PEOPLE_OWNED)
	history = History(snapshot)
	# A ticket's emails and comments only, not every Communication's file.
	in_scope = {dt: {str(r["name"]) for r in helpdesk_load.scoped_rows(history, dt)}
		for dt in helpdesk_load.SCOPE if dt in snapshot.get("schemas", {})}
	manifest = {}
	for url, item in snapshot.get("all_file_manifest", snapshot.get("captured_file_manifest", {})).items():
		references = [r for r in item.get("references") or [] if r.get("attached_to_doctype") in parents
			and str(r.get("attached_to_name")) in in_scope.get(r.get("attached_to_doctype"), (str(r.get("attached_to_name")),))]
		if references:
			manifest[url] = {**item, "references": references}
	rows = {}
	for dt in archived_doctypes(snapshot):
		if dt in PEOPLE_OWNED:
			continue
		for row in helpdesk_load.scoped_rows(history, dt):
			rows[f"{dt}\0{row['name']}"] = {"doctype": dt, "record": row}
	part = {**snapshot, "history": rows, "users": {}, "all_file_manifest": manifest}
	with helpdesk_load.quiet_attachment_comments():
		result = full_run.attachments(part, source, run_id, dry_run=not live, verify_manifest=True)
	result["files_considered"] = len(manifest)
	return result


# 4. Reconcile ---------------------------------------------------------------


def reconcile(snapshot, loader):
	"""Per source doctype: captured, converted, excluded and not imported."""
	from onerc_vmms_migrate.history_model import History

	history = History(snapshot)
	index = converted_index(snapshot)
	excluded = {(r["source_doctype"], str(r["source_name"])) for r in loader.excluded}
	excluded.update(loader.excluded_identities)
	failed = Counter(r["source_doctype"] for r in _unique(loader.failures) if r.get("source_name") != "__phase__")
	records = {}
	archive_only = helpdesk_load.archive_only(snapshot.get("schemas", {}))
	for dt in CONVERTED + HELPDESK + tuple(archive_only):
		if dt in NOT_CONVERTED:
			continue
		rows = helpdesk_load.scoped_rows(history, dt)
		if not rows:
			continue
		names = [str(row["name"]) for row in rows]
		if dt in archive_only:
			records[dt] = {"captured": len(names), "archived_only": len(names), "unaccounted": 0}
			continue
		done = sum((dt, name) in index for name in names)
		out = sum((dt, name) in excluded and (dt, name) not in index for name in names)
		counts = {"captured": len(names), "converted": done, "excluded": out, "not_imported": failed.get(dt, 0)}
		if dt == "Job Applicant":
			# Job and opportunity applications are left alone (decision, 15 Nov 2026).
			counts["left_alone"] = sum(
				not row.get("is_volunteer") and (dt, str(row["name"])) not in index
				and (dt, str(row["name"])) not in excluded for row in rows
			)
		counts["unaccounted"] = len(names) - sum(n for key, n in counts.items() if key != "captured")
		records[dt] = counts
	return records


def statuses(snapshot):
	"""What the imported chain looks like, for the people who decided it."""
	source_id = str(snapshot["source"].get("source_id") or snapshot["source"]["db_name"])

	def targets(source_doctype):
		return frappe.get_all(
			historical.ARCHIVE,
			filters={"source_id": source_id, "source_doctype": source_doctype, "conversion_status": "Converted"},
			pluck="target_name",
		)

	def count(doctype, field, names):
		if not names:
			return {}
		rows = frappe.get_all(doctype, filters={"name": ["in", names]}, fields=[field])
		return dict(Counter(str(row[field]) for row in rows))

	deployments = targets("Deployment Request Tool")
	assignments = targets("Personnel Deployment Request")
	terms = targets("Personnel Terms of Reference")
	return {
		"deployments": count("VMMS Deployment", "status", deployments),
		"coordinators": coordinators(deployments),
		"requests": count("VMMS Deployment Assignment", "status", assignments),
		"terms_active": count("VMMS Terms of Reference", "is_active", terms),
		"events": event_reach(targets("Buzz Event"), targets("Event Booking")),
		"helpdesk": helpdesk_load.statuses(targets),
	}


def coordinators(deployments):
	"""Who volunteers will see as the contact on the imported deployments."""
	if not deployments:
		return {}
	rows = frappe.get_all("VMMS Deployment", filters={"name": ["in", deployments]}, fields=["coordinator"])
	administrator = sum(row.coordinator in (None, "", "Administrator") for row in rows)
	return {"administrator": administrator, "a_person": len(rows) - administrator}


def event_reach(events, bookings):
	"""Whether the imported events reach the pages: published, placed, banners
	a guest can see, and bookings that belong to an account (Buzz lists a
	person's bookings by account, so an unclaimed one is invisible to them)."""
	if not events and not bookings:
		return {}
	out = {}
	if events:
		rows = frappe.get_all("Buzz Event", filters={"name": ["in", events]},
			fields=["is_published", "banner_image", "end_date", "start_date"]
			+ (["geo_node"] if frappe.get_meta("Buzz Event").has_field("geo_node") else []))
		today = str(frappe.utils.getdate())
		published = [row for row in rows if row.is_published]
		out.update({
			"events_published": len(published),
			"events_unpublished": len(rows) - len(published),
			"events_without_place": sum(not row.get("geo_node") for row in rows),
			"published_with_private_banner": sum(str(row.banner_image or "").startswith("/private/") for row in published),
			"published_still_running": sum(str(row.end_date or row.start_date or "") >= today for row in published),
		})
	if bookings:
		claimed = frappe.db.count("Event Booking", {"name": ["in", bookings], "user": ["is", "set"]})
		out.update({"bookings_claimed": claimed, "bookings_unclaimed": len(bookings) - claimed})
	return out


# Reports --------------------------------------------------------------------


def _write(directory, report, loader):
	from onerc_vmms_migrate import run

	workspace.write_json(os.path.join(directory, "history-summary.json"), report)
	run._write_csv(directory, "history-not-imported.csv", [_row(r) for r in _unique(loader.failures)])
	run._write_csv(directory, "history-warnings.csv", [_row(r) for r in loader.warnings])
	run._write_csv(directory, "history-excluded.csv", [_row(r) for r in _unique(loader.excluded)])
	helpdesk_load.write_reports(directory, loader)
	run._write_csv(directory, "history-fields-not-imported.csv", [_row(r) for r in getattr(loader, "field_drops", [])])


def fields_not_imported(snapshot):
	"""Old fields with values that the converted records do not carry. The
	archive keeps them; this names each one (see history_fields)."""
	from onerc_vmms_migrate.history_fields import destination_fields, field_drops
	from onerc_vmms_migrate.history_model import History

	return field_drops(History(snapshot), snapshot.get("schemas", {}), CONVERTED, destination_fields())


def _unique(rows):
	"""Each page replays the adapters, so an exclusion is met once per page."""
	seen = {}
	for row in rows:
		seen.setdefault((row.get("source_doctype"), str(row.get("source_name"))), row)
	return list(seen.values())


def _row(row):
	return {key: strip_html(str(value)) if value is not None else "" for key, value in row.items()}


# Guards ---------------------------------------------------------------------


def _assert_people_in() -> None:
	from onerc_vmms_migrate.batches import checkpoint

	batch = checkpoint()
	if batch and batch.get("status") != "finished":
		frappe.throw("Finish the people import before importing the history.")
	if not frappe.db.count("Legacy Migration Record"):
		frappe.throw("Import the people first: the history needs its volunteers and accounts.")


def _previewed(capture) -> bool:
	reports = os.path.dirname(workspace.path("reports", "placeholder"))
	if not capture or not os.path.isdir(reports):
		return False
	for name in os.listdir(reports):
		summary = os.path.join(reports, name, "history-summary.json")
		if name.endswith("-history-preview") and os.path.isfile(summary):
			import json

			with open(summary) as handle:
				if json.load(handle).get("capture") == capture:
					return True
	return False


def keep_imported_people(snapshot) -> None:
	"""Nobody the people session brought across is held back here.

	The exclusion set is rebuilt from today's Settings and crosswalks. If those
	differ from the people import's (field review switched off, a crosswalk row
	edited), people who are already in would lose their deployment requests,
	availability and tickets, and a live run marks those rows Excluded for good
	on this capture.
	"""
	imported = {
		(row.source_doctype, str(row.source_name))
		for row in frappe.get_all("Legacy Migration Record", fields=["source_doctype", "source_name"])
	}
	snapshot["excluded_native_identities"] = set(snapshot.get("excluded_native_identities") or ()) - imported


def _run_id(kind):
	return now_datetime().strftime("%Y%m%d-%H%M%S") + "-" + kind


def _report(progress, phase, processed, total) -> None:
	if progress:
		progress({"phase": phase, "processed": processed, "total": total})
