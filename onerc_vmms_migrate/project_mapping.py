"""Source Project relationships without guessed people or participation.

Built from the selected capture, including newer live-site records. Derived
missing-link mappings never change the preserved original source payload.
"""

from collections import defaultdict


def numbering_conflicts(naming, names):
    """Identify source IDs needing an explicit destination numbering policy."""
    import re
    from onerc_vmms_migrate.naming_continuity import match_number, prefix_pattern

    mode, patterns, rules = naming.configuration("Project")
    if mode == "autoincrement":
        return [str(name) for name in names if not re.fullmatch(r"[0-9]+", str(name))]
    patterns = list(patterns) + [
        (prefix_pattern(rule.get("prefix") or ""), int(rule.get("prefix_digits") or 0))
        for rule in rules if int(rule.get("prefix_digits") or 0) > 0
    ]
    return [str(name) for name in names
            if not any(match_number(pattern, digits, name) for pattern, digits in patterns)]


class ProjectLinks:
    def __init__(self, history):
        self.projects = {row["name"]: row for row in history.rows("Project")}
        self.terms = {row["name"]: row for row in history.rows("Personnel Terms of Reference")}
        self.tools = {row["name"]: row for row in history.rows("Deployment Request Tool")}
        self.assignments = history.rows("Personnel Deployment Request")
        self.term_contexts = defaultdict(set)
        for row in self.tools.values():
            if row.get("terms_of_reference") and row.get("project"):
                self.term_contexts[row["terms_of_reference"]].add(row["project"])
        for row in self.assignments:
            tool = self.tools.get(row.get("deployment"), {})
            terms = row.get("terms_of_reference") or tool.get("terms_of_reference")
            if terms and row.get("project"):
                self.term_contexts[terms].add(row["project"])

    def project(self, name):
        if name and name not in self.projects:
            raise ValueError("Missing source Project: " + str(name))
        return name or None

    def terms_project(self, name):
        row = self.terms.get(name)
        if row is None:
            raise ValueError("Missing source TOR: " + str(name))
        if row.get("project"):
            return self.project(row["project"])
        candidates = self.term_contexts[name]
        if len(candidates) > 1:
            raise ValueError("TOR " + str(name) + " has conflicting Project references: "
                             + ", ".join(sorted(candidates)))
        return self.project(next(iter(candidates), None))

    @staticmethod
    def agree(context, *projects):
        values = {value for value in projects if value}
        if len(values) > 1:
            raise ValueError("Project conflict in " + context + ": " + ", ".join(sorted(values)))
        return next(iter(values), None)

    def tool_project(self, row):
        project = self.project(row.get("project"))
        terms_project = self.terms_project(row["terms_of_reference"]) if row.get("terms_of_reference") else None
        return self.agree("deployment " + row["name"], project, terms_project)

    def assignment_project(self, row):
        tool = self.tools.get(row.get("deployment"), {})
        tool_project = self.tool_project(tool) if tool else None
        terms = row.get("terms_of_reference") or tool.get("terms_of_reference")
        offered_project = self.terms_project(terms) if terms else None
        return self.agree("assignment " + row["name"],
                          self.project(row.get("project")), tool_project, offered_project)

    def report(self):
        """Review every source link before a native graph is imported."""
        result = {"projects": len(self.projects), "terms": [], "deployments": [],
                  "assignments": [], "problems": []}
        for kind, rows, resolve in (
            ("terms", self.terms.values(), lambda row: self.terms_project(row["name"])),
            ("deployments", self.tools.values(), self.tool_project),
            ("assignments", self.assignments, self.assignment_project),
        ):
            for row in rows:
                try:
                    project = resolve(row)
                    if kind == "assignments" and row.get("deployment") not in self.tools and not row.get("terms_of_reference"):
                        raise ValueError("Missing source deployment and structured TOR; retain original request and file evidence")
                    result[kind].append({"source_name": row["name"], "project": project,
                                         "derived_project": bool(project and not row.get("project"))})
                except ValueError as exc:
                    result["problems"].append({"kind": kind, "source_name": row["name"], "problem": str(exc)})
        return result


def preview():
    """Review the selected capture on the target; never load business records.

    Used by the existing System Manager long-worker controller as project_plan.
    Full mappings and exceptions are private report files; the result is a summary.
    """
    from pathlib import Path
    import frappe
    from vmmsx.deployment.services.project import default_company
    from onerc_vmms_migrate import browser, run, workspace
    from onerc_vmms_migrate.continuity import require_capture
    from onerc_vmms_migrate.extract import extract
    from onerc_vmms_migrate.history_load import Loader
    from onerc_vmms_migrate.source import Source

    browser.authorize()
    conf = workspace.config()
    run_id = run._run_id("project-plan")
    with Source(conf) as source:
        snapshot = extract(source)
        require_capture(snapshot)
        if not snapshot.get("source", {}).get("continuity", {}).get("naming"):
            raise ValueError("A fresh capture with source naming metadata is required.")
        if not frappe.get_meta("Project").has_field("vmms_geo_node"):
            raise ValueError("VMMSX Project geography field is missing; complete destination setup first")
        company = default_company()
        if not company or not frappe.db.exists("Company", company):
            raise ValueError("Set the destination legal Company before reviewing Projects")
        loader = Loader(snapshot, run_id, source, conf, dry_run=True)
        review = loader.project_context().report()
        unsupported_names = set(numbering_conflicts(loader.naming, loader.project_context().projects))
        review["project_mappings"] = []
        for row in loader.project_context().projects.values():
            try:
                if row["name"] in unsupported_names:
                    raise ValueError("Project ID does not match destination numbering policy: " + row["name"])
                geo = loader.geo(row.get("company"))
                if not frappe.db.exists("Geo Node", geo):
                    raise ValueError("Mapped Geo Node is missing: " + str(geo))
                mapped = loader.target("Project", row["name"], required=False)
                if mapped:
                    loader.validate_project(row, mapped)
                elif frappe.db.exists("Project", row["name"]):
                    raise ValueError("Project ID is occupied without this source mapping: " + row["name"])
                review["project_mappings"].append({
                    "source_name": row["name"], "target_name": mapped or row["name"],
                    "source_company": row.get("company"), "company": company, "geo_node": geo,
                })
            except ValueError as exc:
                review["problems"].append({"kind": "projects", "source_name": row["name"], "problem": str(exc)})
        loader.naming.reserve("Project", "Project",
            list(loader.project_context().projects) + frappe.get_all("Project", pluck="name"))
        naming = loader.naming.report()
        # Reports include an already-higher target counter, not just imported IDs.
        for series, floor in naming["series_floors"].items():
            naming["series_floors"][series] = max(
                floor, int(frappe.db.get_value("Series", series, "current", order_by="name") or 0))
        for rule, floor in naming["naming_rule_floors"].items():
            naming["naming_rule_floors"][rule] = max(
                floor, int(frappe.db.get_value("Document Naming Rule", rule, "counter") or 0))
        source_info = snapshot["source"]
        review.update(source_id=source_info.get("source_id") or source_info.get("db_name"),
                      capture_id=source_info.get("capture_id") or source_info.get("snapshot_id"),
                      run_id=run_id, mode="review_only", numbering=naming)
    directory = Path(workspace.report_dir(run_id))
    workspace.write_json(str(directory / "project-mapping-review.json"), review)
    run._write_csv(str(directory), "project-mapping-problems.csv", review["problems"])
    return {
        "run_id": run_id, "mode": "review_only", "capture_id": review["capture_id"],
        "source_id": review["source_id"], "projects": review["projects"],
        "project_mappings": len(review["project_mappings"]),
        "source_link_problems": len(review["problems"]), "numbering": naming,
        "reports": ["project-mapping-review.json", "project-mapping-problems.csv"],
        "note": "No business records or counters changed. This reviews mappings, not a completed import.",
    }
