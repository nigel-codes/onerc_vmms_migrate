"""Operational history adapters. Originals remain in the restricted archive.

Historical inserts preserve dates and docstatus without firing modern business
automation. Unresolvable dependencies are reported per record and retained;
they never become fabricated approvals, attendance, payments or contracts.
"""
import datetime
import re
import time
from collections import Counter

import frappe

from onerc_vmms_migrate import historical, normalise, presentation
from onerc_vmms_migrate.crosswalk import Crosswalks, UNMAPPED, DROP
from onerc_vmms_migrate.history_fields import OMIT
from onerc_vmms_migrate.history_model import History, assignment_state, application_state, key, split_name
from onerc_vmms_migrate.naming_continuity import NamingContinuity


# Records that keep their old creator as owner. A deployment's owner is who
# hears a volunteer's answer (vmmsx invitation.askers) and whose "mine" list
# shows it; an event's is who made it.
OWNER_DOCTYPES = {"Project", "Task", "VMMS Terms of Reference", "VMMS Deployment", "Buzz Event",
                  # A ticket's raiser, an email's sender, a comment's author, an assigner.
                  "HD Ticket", "Communication", "Comment", "HD Ticket Comment", "ToDo"}
COORDINATOR_ROLE = "County Coordinator"


class BatchBoundary(Exception):
    """Stop before the next record without rolling back completed records."""


class SourceIdentityExcluded(ValueError):
    pass


PERSON_EXCLUSION = '[approved-person-exclusion] '


def history_exclusion_counts(history):
    """Count approved person fan-out separately; unclassified old notes stay gaps."""
    person = sum(str(row.get('reason') or '').startswith(PERSON_EXCLUSION)
                 for row in history['exclusions'])
    return {'approved_person': person, 'history_gap': len(history['exclusions']) - person}


def history_exclusions_accepted(history, approved):
    counts = history_exclusion_counts(history)
    total_excluded = counts['approved_person'] + counts['history_gap']
    return not total_excluded or bool(approved and
        counts['history_gap'] / max(1, history['converted'] + total_excluded) <= 0.01)


class Loader:
    def __init__(self, snapshot, run_id, source, conf, dry_run=False):
        self.snapshot, self.run_id, self.source, self.conf = snapshot, run_id, source, conf
        self.history = History(snapshot)
        self.crosswalks = Crosswalks.read()
        self.targets = {}
        self.results, self.failures, self.excluded, self.warnings = [], [], [], []
        self.excluded_identities=set(snapshot.get('excluded_native_identities',[]))
        self.excluded_reasons = {}
        source_id = str(snapshot['source'].get('source_id') or snapshot['source']['db_name'])
        capture = snapshot['source'].get('capture_id') or snapshot['source'].get('snapshot_id')
        for row in frappe.get_all(historical.ARCHIVE,
                filters={'source_id': source_id, 'snapshot_id': capture, 'conversion_status': 'Excluded'},
                fields=['source_doctype', 'source_name', 'conversion_note']):
            identity = (row.source_doctype, str(row.source_name))
            self.excluded_identities.add(identity)
            self.excluded_reasons[identity] = row.conversion_note
        self.dry_run = dry_run
        self.naming = NamingContinuity(snapshot, dry_run=dry_run)
        self.by_source = {}
        for row in frappe.get_all('Legacy Migration Record',fields=['source_doctype','source_name','target_doctype','target_name']):
            self.by_source[(row.source_doctype,row.source_name,row.target_doctype)] = row.target_name

    def target(self, doctype, name, target_dt=None, required=True):
        if not name:
            if required:raise ValueError('Missing legacy '+doctype+' reference')
            return None
        name=str(name)
        if (doctype,name) in self.excluded_identities:
            if required:
                reason = getattr(self, 'excluded_reasons', {}).get((doctype, name))
                raise SourceIdentityExcluded(reason or PERSON_EXCLUSION+'Dependent source identity requires review; original record retained')
            return None
        target=self.targets.get((doctype,name)) or self.by_source.get((doctype,name,target_dt or doctype))
        if not target:
            target=historical.converted(self.snapshot,doctype,name)
        if not target and doctype in ('User','Currency','Country') and frappe.db.exists(doctype,name):
            target=name
        if not target and required:
            if self.conf.get('allow_missing_history_dependencies') and doctype in ('User', 'Employee', 'Personnel Terms of Reference'):
                if doctype in ('User', 'Employee'):
                    source = self.snapshot.get('users' if doctype == 'User' else 'employees')
                    present = source is None or name in source
                else:
                    present = self.history.get(doctype, name)
                if not present:
                    raise SourceIdentityExcluded('Required '+doctype+' is absent from the captured source: '+name+'; original dependent record retained in archive')
            raise ValueError('Unresolved '+doctype+' reference: '+str(name))
        return target

    def geo(self, company):
        override = (self.conf.get('history_branch_overrides') or {}).get(company) if normalise.key(company) != 'krcs - hq' else None
        if override:
            if not frappe.db.exists('Geo Node', override):
                raise ValueError('History Geo Node does not exist: '+str(override))
            return override
        target=self.crosswalks.lookup('branch',company)
        if not target or target is UNMAPPED or target == DROP:
            raise ValueError('Unresolved source branch: '+str(company))
        return target

    def one(self, source_dt, row, target_dt, build, name=None, *, validate_existing=None):
        if (source_dt,row['name']) in self.excluded_identities:
            reason = getattr(self, 'excluded_reasons', {}).get((source_dt, str(row['name']))) or PERSON_EXCLUSION+'Source identity requires review; original record retained'
            self.excluded.append({'source_doctype':source_dt,'source_name':row['name'],'reason':reason})
            return None
        batched = getattr(self, 'batch_limit', None) is not None
        identity = (source_dt, str(row['name']))
        old = self.converted_index.get(identity) if batched else historical.converted(self.snapshot,source_dt,row['name'])
        if old:
            validated = getattr(self, 'validated', None)
            if validate_existing and (validated is None or identity not in validated):
                if validated is not None:
                    validated.add(identity)  # once per run, not on every page replay
                try:
                    validate_existing(row, old)
                except Exception as exc:
                    self.failures.append({'source_doctype':source_dt,'source_name':row['name'],
                        'target_doctype':target_dt,'error':frappe.utils.strip_html(str(exc))})
                    return None
            if name is not None:
                self.naming.reserve(source_dt, target_dt, [old])
            self.targets[(source_dt,row['name'])]=old
            self.results.append({'source_doctype':source_dt,'source_name':row['name'],'target_doctype':target_dt,'target_name':old,'outcome':'existing'})
            return old
        if identity in getattr(self, 'skip_identities', ()):
            # Failed earlier in this run and already reported; a later page
            # must not spend its allowance on the same record again.
            return None
        if batched:
            if self.batch_attempted >= self.batch_limit or (self.batch_attempted and time.monotonic() >= self.batch_deadline):
                raise BatchBoundary()
            self.batch_attempted += 1
            if self.batch_progress and self.batch_attempted % 25 == 0:
                self.batch_progress({'phase':'history','processed':len(self.results),
                                     'loaded':self.batch_attempted,'failed':len(self.failures)})
        frappe.db.savepoint('vmms_history_record')
        try:
            values=build(row)
            values.setdefault('creation',row.get('creation'))
            values.setdefault('modified',row.get('modified'))
            doc = self.reuse_event_category(values, name) if source_dt == target_dt == 'Event Category' else None
            outcome = 'existing' if doc is not None else 'created'
            if doc is None:
                owner = self.source_owner(source_dt, row) if target_dt in OWNER_DOCTYPES else None
                doc=historical.insert(target_dt,values,name=name,owner=owner)
            historical.mark(self.snapshot,source_dt,row['name'],target_dt,doc.name,self.run_id)
            if name is not None:
                self.naming.reserve(source_dt, target_dt, [doc.name])
            if batched:
                self.converted_index[identity] = doc.name
            self.targets[(source_dt,row['name'])]=doc.name
            self.results.append({'source_doctype':source_dt,'source_name':row['name'],'target_doctype':target_dt,'target_name':doc.name,'outcome':outcome})
            return doc.name
        except SourceIdentityExcluded as exc:
            frappe.db.rollback(save_point='vmms_history_record')
            frappe.clear_messages()
            # Commit an exclusion disposition with this batch's checkpoint.
            # Retain the original payload; retries must not spend every batch
            # reattempting the same intentionally unconverted dependencies.
            historical.exclude(self.snapshot, source_dt, row['name'], str(exc), self.run_id)
            self.excluded_identities.add(identity)
            if not hasattr(self, 'excluded_reasons'):
                self.excluded_reasons = {}
            self.excluded_reasons[identity] = str(exc)
            self.excluded.append({'source_doctype':source_dt,'source_name':row['name'],'reason':str(exc)})
            return None
        except Exception as exc:
            frappe.db.rollback(save_point='vmms_history_record')
            frappe.clear_messages()
            self.failures.append({'source_doctype':source_dt,'source_name':row['name'],
                'target_doctype':target_dt,'error':frappe.utils.strip_html(str(exc))})
            return None

    def reuse_event_category(self, values, name):
        """Buzz installs these vocabulary records before migration begins.

        Link by their standard names, preserving destination presentation and
        reporting source differences. This exception never applies to hosts,
        venues, people, transactions or custom category-name collisions.
        """
        if name not in ('Conferences', 'Local', 'Meetups', 'Webinars', 'Zoom Meeting'):
            return None
        existing = frappe.db.exists('Event Category', name)
        if not existing:
            return None
        doc = frappe.get_doc('Event Category', existing)
        if int(values.get('docstatus') or 0) or int(doc.get('docstatus') or 0):
            raise ValueError('Seeded Event Category has an unexpected document status: '+str(name))
        differences = sorted(field for field, value in values.items()
            if field not in ('creation', 'modified', 'docstatus')
            and (value if value is not None else '') != (doc.get(field) if doc.get(field) is not None else ''))
        self.warnings.append({'source_doctype': 'Event Category', 'source_name': name,
            'target_name': doc.name, 'different_fields': ', '.join(differences),
            'warning': 'Reused standard Buzz category; destination values unchanged, original source values retained in archive'})
        return doc

    def copy_values(self, doctype, row, omit=()):
        """Native same-doctype scalar fields, with every unsupported value archived."""
        meta=frappe.get_meta(doctype)
        values={}
        for field in meta.fields:
            name=field.fieldname
            if name in omit or name not in row or field.fieldtype in ('Password','Table','Table MultiSelect','Section Break','Column Break','HTML','Button','Tab Break'):
                continue
            value=row[name]
            if field.fieldtype=='Link' and value:
                mapped=self.target(field.options,value,required=False)
                if mapped:value=mapped
                elif not frappe.db.exists(field.options,value):
                    if field.reqd:raise ValueError('Unresolved required '+field.options+' link')
                    self.warn(doctype, row, f"Unresolved optional {field.options} link in {name}: {value}")
                    continue
            if field.fieldtype in ('Attach','Attach Image') and value and str(value).startswith(('/files/','/private/files/')):
                # The general attachment phase restores these with real owners.
                continue
            values[name]=value
        values['docstatus']=int(row.get('docstatus') or 0)
        return values

    def warn(self, doctype, row, message):
        self.warnings.append({"source_doctype": doctype, "source_name": row["name"], "warning": message})

    def source_owner(self, doctype, row):
        owner = self.target("User", row.get("owner"), required=False)
        if not owner or not frappe.db.exists("User", owner):
            self.warn(doctype, row, f"Source owner could not be resolved ({row.get('owner') or 'blank'}); used Administrator")
            return None
        return owner

    def project_link(self, doctype, row):
        source = row.get("project")
        if doctype == 'Personnel Terms of Reference':
            source = self.project_context().terms_project(row['name'])
        elif doctype == 'Deployment Request Tool':
            source = self.project_context().tool_project(row)
        if source and not row.get('project'):
            self.warn(doctype, row, 'Missing Project resolved from consistent captured deployment references: '+source)
        return self.target("Project", source) if source else None

    def project_context(self):
        if not hasattr(self, '_project_context'):
            from onerc_vmms_migrate.project_mapping import ProjectLinks
            self._project_context = ProjectLinks(self.history)
        return self._project_context

    def check_terms_project(self, source_project, terms):
        if source_project:
            expected = self.target('Project', source_project)
            actual = frappe.db.get_value('VMMS Terms of Reference', terms, 'project')
            if actual != expected:
                raise ValueError('Native TOR Project does not match recorded participation: '
                                 +str(terms)+' expected '+str(expected)+', found '+str(actual))

    def validate_project(self, row, name):
        from vmmsx.deployment.services.project import default_company
        document = frappe.get_doc('Project', name)
        if document.company != default_company() or document.vmms_geo_node != self.geo(row.get('company')):
            raise ValueError('Existing Project placement changed; review its Company/Geo mapping: '+name)

    def validate_deployment(self, row, name):
        document = frappe.get_doc('VMMS Deployment', name)
        if row.get('terms_of_reference'):
            offered = self.target('Personnel Terms of Reference', row['terms_of_reference'], 'VMMS Terms of Reference')
            if document.terms_of_reference != offered:
                raise ValueError('Existing deployment points to a different source TOR: '+name)
        self.check_terms_project(self.project_context().tool_project(row), document.terms_of_reference)

    def validate_assignment(self, row, name):
        document = frappe.get_doc('VMMS Deployment Assignment', name)
        volunteer = self.target('Employee', row.get('employee'), 'VMMS Volunteer')
        if document.volunteer != volunteer:
            raise ValueError('Existing assignment belongs to a different volunteer: '+name)
        parent = self.target('Deployment Request Tool', row.get('deployment'), 'VMMS Deployment', required=False)
        if parent and document.deployment != parent:
            raise ValueError('Existing assignment points to a different deployment: '+name)
        if not parent:
            if not row.get('terms_of_reference'):
                raise ValueError('Orphan assignment has no recorded source TOR: '+name)
            derived = 'legacy-assignment-'+key('assignment',row['name'])[:24]
            if document.deployment != derived:
                raise ValueError('Existing assignment points to a different derived deployment: '+name)
        if row.get('terms_of_reference'):
            offered = self.target('Personnel Terms of Reference', row['terms_of_reference'], 'VMMS Terms of Reference')
            if document.terms_of_reference != offered:
                raise ValueError('Existing assignment has a different offered TOR: '+name)
        deployment = frappe.get_doc('VMMS Deployment', document.deployment)
        if document.geo_node != deployment.geo_node:
            raise ValueError('Existing assignment geography differs from its deployment: '+name)
        if not parent and deployment.terms_of_reference != document.terms_of_reference:
            raise ValueError('Existing derived deployment has a different offered TOR: '+name)
        if not row.get('terms_of_reference') and document.terms_of_reference != deployment.terms_of_reference:
            raise ValueError('Existing assignment has a different inherited TOR: '+name)
        project = self.project_context().assignment_project(row)
        self.check_terms_project(project, deployment.terms_of_reference)
        self.check_terms_project(project, document.terms_of_reference)
        if document.status not in (assignment_state(row), self.assignment_status(row, deployment.status)):
            raise ValueError('Source/destination assignment response changed; review before updating: '+name)

    def project_department(self, doctype, row, company, abbreviation):
        source = normalise.text(row.get("department"))
        if not source:
            return None
        label = source.rsplit(" - ", 1)[0]
        if abbreviation and company:
            target = frappe.db.exists("Department", {"name": f"{label} - {abbreviation}", "company": company})
            if target:
                return target
        self.warn(doctype, row, f"Unresolved optional Department link in department: {source}")
        return None

    def projects(self):
        from vmmsx.deployment.services.project import default_company

        company = default_company()
        abbreviation = frappe.db.get_value("Company", company, "abbr") if company else None
        project_meta = frappe.get_meta("Project")
        projects = self.history.rows('Project')
        if projects:
            if not project_meta.has_field('vmms_geo_node'):
                raise ValueError('VMMSX Project geography field is missing; complete destination setup first')
            if not company or not frappe.db.exists('Company', company):
                raise ValueError('Set the destination legal Company before importing Projects')
            from onerc_vmms_migrate.project_mapping import numbering_conflicts
            unsupported_names = numbering_conflicts(self.naming, (r['name'] for r in projects))
            if unsupported_names:
                raise ValueError('Review the destination Project numbering policy before import: '
                                 +', '.join(unsupported_names[:5]))
            # Reserve the fresh capture's entire range before the first insert.
            # Existing target names may be ahead of a stale target counter.
            self.naming.reserve('Project', 'Project',
                [r['name'] for r in projects]+frappe.get_all('Project', pluck='name'))
            self.project_types(projects)
        for row in projects:
            def build(r):
                dates = ('expected_start_date', 'expected_end_date', 'actual_start_date', 'actual_end_date')
                values=self.copy_values('Project',r,omit=OMIT['Project'])
                if project_meta.has_field('vmms_geo_node'):
                    values['vmms_geo_node']=self.geo(r.get('company'))
                if company:
                    values['company'] = company
                if project_meta.has_field('department'):
                    values['department'] = self.project_department("Project", r, company, abbreviation)
                for field in dates:
                    if project_meta.has_field(field) and field in r:
                        values[field] = normalise.date(r[field])
                # ERP financial accounts and customer IDs remain history; no
                # chart of accounts is manufactured to satisfy an optional link.
                return values
            self.one('Project',row,'Project',build,name=row['name'],validate_existing=self.validate_project)
        # ERP tasks are project work items; no volunteer is guessed for them.
        pending=self.history.rows('Task')
        task_meta = frappe.get_meta("Task")
        while pending:
            ready=[r for r in pending if not r.get('parent_task') or self.target('Task',r['parent_task'],required=False)]
            if not ready:ready=pending[:1]
            for row in ready:
                def build(r):
                    values=self.copy_values('Task',r,omit=OMIT['Task'])
                    if task_meta.has_field('department') and r.get('department'):
                        values['department'] = self.project_department("Task", r, company, abbreviation)
                    if r.get('project'):values['project']=self.target('Project',r['project'])
                    if r.get('parent_task'):values['parent_task']=self.target('Task',r['parent_task'])
                    return values
                self.one('Task',row,'Task',build,name=row['name'])
                pending.remove(row)
        if self.history.rows('Task'):
            from onerc_vmms_migrate.trees import boundaries
            values=boundaries(frappe.get_all('Task',fields=['name','parent_task']),'parent_task')
            frappe.db.bulk_update('Task',values,update_modified=False)

    def review_phone(self, source_dt, source_name, values, field):
        from onerc_vmms_migrate.load import _check_phone
        context = {'conf': self.conf, 'warnings': []}
        _check_phone(context, values, field, field, society=False)
        for row in context.get('phone_review', []):
            self.warnings.append({'source_doctype': source_dt, 'source_name': source_name, **row,
                'warning': 'Invalid historical phone omitted; original retained in source archive'})

    def resource_unit(self, unit, parent):
        if not unit:
            return None
        existing = frappe.db.exists('UOM', unit)
        if existing:
            return existing
        # A recorded label is vocabulary, not a conversion factor. Do not map
        # unfamiliar labels to another unit or invent stock conversions.
        doc = frappe.get_doc({'doctype': 'UOM', 'uom_name': unit}).insert(ignore_permissions=True)
        self.warnings.append({'source_doctype': 'Personnel Terms of Reference', 'source_name': parent,
            'warning': 'Created recorded legacy UOM label: '+str(unit)+'; no conversion inferred'})
        return doc.name

    def methodology(self, label, parent):
        """The old TOR typed its method as free text; vmmsx keeps a register.
        A label the register lacks is added exactly as written, switched off,
        so the old TOR keeps its wording and new TORs are not offered it."""
        label = str(label).strip()
        existing = frappe.db.get_value('VMMS TOR Methodology', {'methodology_name': label})
        if existing:
            return existing
        slug = re.sub(r'[^a-z0-9]+', '_', label.lower()).strip('_')[:100] or 'method'
        name = slug if not frappe.db.exists('VMMS TOR Methodology', slug) else slug[:90]+'_'+key('method', label)[:8]
        frappe.get_doc({'doctype': 'VMMS TOR Methodology', 'methodology_key': name,
            'methodology_name': label, 'is_active': 0}).insert(ignore_permissions=True)
        self.warnings.append({'source_doctype': 'Personnel Terms of Reference', 'source_name': parent,
            'warning': 'Added the old methodology "'+label+'" to the register, switched off'})
        return name

    def project_types(self, projects):
        """Old Project Types the destination lacks are added with their old names."""
        for kind in sorted({str(r['project_type']) for r in projects if r.get('project_type')}):
            if not frappe.db.exists('Project Type', kind):
                frappe.get_doc({'doctype': 'Project Type', 'project_type': kind}).insert(ignore_permissions=True)
                self.warnings.append({'source_doctype': 'Project', 'source_name': kind,
                    'warning': 'Added the old Project Type "'+kind+'"'})

    @staticmethod
    def deployment_status(start, end):
        """Where an old deployment stands today: ended ones are Completed,
        running ones Active, the rest Planned."""
        from frappe.utils import getdate
        today = getdate()
        if end and getdate(end) < today:
            return 'Completed'
        if start and getdate(start) <= today:
            return 'Active'
        return 'Planned'

    @staticmethod
    def assignment_status(row, deployment_status):
        """An ask nobody answered before its deployment finished has expired
        (decision, 15 Nov 2026). Every answer that was given is kept."""
        state = assignment_state(row)
        if state == 'Pending' and deployment_status in ('Completed', 'Closed Out', 'Cancelled'):
            return 'Expired'
        return state

    def coordinator(self, doctype, row, geo_node=None):
        """The old tool's creator. When that account was not imported, the
        county's one active County Coordinator (whom volunteers then see as
        their contact); with none or several there, Administrator. Either
        fallback is reported, so the deployment and its volunteers still come
        across. A tool the old Administrator account made is treated the same
        way: Administrator is nobody a volunteer can contact."""
        user = self.target('User', row.get('owner'), required=False)
        if user and user not in ('Administrator', 'Guest'):
            return user
        who = ('Coordinator '+str(row.get('owner'))+' was not imported' if not user
               else 'Made by the old '+user+' account, which is not a person to contact')
        held = self.county_coordinators(geo_node)
        if len(held) == 1:
            self.warnings.append({'source_doctype': doctype, 'source_name': row.get('name'),
                'warning': who+'; county coordinator '+held[0]+' recorded'})
            return held[0]
        found = 'no County Coordinator' if not held else str(len(held))+' County Coordinators'
        self.warnings.append({'source_doctype': doctype, 'source_name': row.get('name'),
            'warning': who+' and its place has '+found+'; Administrator recorded'})
        return 'Administrator'

    def county_coordinators(self, geo_node):
        """Active holders of the deployment scope role at exactly this place,
        through vmmsx's own wrapper over core (no walking up, no subtree)."""
        if not geo_node:
            return []
        cache = self.__dict__.setdefault('_county_coordinators', {})
        if geo_node not in cache:
            from vmmsx.approvals.services.routing import holders_exactly_at
            role = (frappe.db.get_single_value('National Society Settings', 'vmms_deployment_scope_role')
                    or COORDINATOR_ROLE)
            people = holders_exactly_at(geo_node, role)
            cache[geo_node] = sorted({p for p in people if p and p not in ('Administrator', 'Guest')
                                      and frappe.db.get_value('User', p, 'enabled')})
        return cache[geo_node]

    def terms(self):
        child_map={'stakeholders':('TOR Stakeholders','stakeholder'), 'objectives':('TOR Objectives','objective'),
            'expected_outputs':('TOR Outputs','output'), 'approach_methods':('TOR Approach Methods','approach'),
            'itinerary':('TOR Itinerary','activity'), 'resources':('TOR Resources','resource')}
        for row in self.history.rows('Personnel Terms of Reference'):
            def build(r):
                values={'tor_name':r.get('title_of_mission'),'tor_key':'legacy_'+key('tor',r['name'])[:24],
                    'geo_scope':self.geo(r.get('company')),'project':self.project_link('Personnel Terms of Reference', r),
                    'expected_start_date':r.get('expected_start_date'),'expected_end_date':r.get('expected_end_date'),
                    'mission_background':r.get('mission_background'),'approval_mode':'direct',
                    'docstatus':int(r.get('docstatus') or 0),'approval_state':{'Approved':'Approved','Draft':'Draft','Pending Approval':'Submitted'}.get(r.get('workflow_state'),'Draft'),
                    'notes':r.get('notes') or '',
                    # Old terms stay on their deployments and in history but are
                    # not offered for new deployments; staff can switch one on.
                    'is_active':0}
                for field,(child_dt,required_field) in child_map.items():
                    target_child=frappe.get_meta('VMMS Terms of Reference').get_field(field).options
                    target_meta=frappe.get_meta(target_child)
                    target_fields={f.fieldname for f in target_meta.fields}
                    children=[]
                    for child in self.history.children('Personnel Terms of Reference',r['name'],field):
                        new={k:v for k,v in child.items() if k in target_fields}
                        # Explicit source-to-target vocabulary, never a guessed fact.
                        for old,new_field in {'stakeholder_name':'stakeholder','objectives':'objective','outputs':'output','approach_methods':'approach','date':'activity_date','time':'activity_time','activity_description':'activity','resource_name':'resource','qty':'quantity','rate':'unit_cost'}.items():
                            if old in child and new_field in target_fields and not new.get(new_field):new[new_field]=child[old]
                        if not new.get(required_field):
                            candidates=[v for k,v in child.items() if k not in ('name','parent','parenttype','parentfield','owner','modified_by','creation','modified','idx','docstatus') and isinstance(v,str) and v]
                            if len(candidates)==1:new[required_field]=candidates[0]
                        if field=='approach_methods' and child.get('methodology'):
                            new['methodology']=self.methodology(child['methodology'], r['name'])
                        if field=='resources':
                            new['needed_on']=child.get('date')
                            new['unit_cost']=child.get('cost_per_day')
                            if new.get('unit'):
                                new['unit']=self.resource_unit(new['unit'], r['name'])
                        for target_field in target_meta.fields:
                            if target_field.fieldtype=='Data' and target_field.options=='Phone':
                                self.review_phone('Personnel Terms of Reference', r['name'], new, target_field.fieldname)
                        children.append(new)
                    values[field]=children
                return values
            self.one('Personnel Terms of Reference',row,'VMMS Terms of Reference',build)

    def deployment_title(self, r, terms):
        """The old tool's own title, as it was written.

        A deployment's title is required (vmmsx, Nov 2026). Legacy titles are
        kept as they are — two old tools may share one, and the import does not
        run the rule that open deployments must differ. Only a tool with no
        title is given one, the way vmmsx titles a deployment nobody named: its
        terms and the month it starts.
        """
        if (r.get('title') or '').strip():
            return r['title']
        try:
            from vmmsx.deployment.services.deployment import default_title
        except ImportError:
            # A vmmsx from before deployment titles has no title field either.
            return r.get('name') or ''
        return default_title(frappe._dict(terms_of_reference=terms, name='', geo_node=self.geo(r.get('company')),
            planned_start=r.get('expected_start_date'),
            start_date=normalise.date(r.get('expected_start_date'))))

    def deployments(self):
        for row in self.history.rows('Deployment Request Tool'):
            def build(r):
                source_project = self.project_context().tool_project(r)
                if r.get('terms_of_reference'):
                    terms=self.target('Personnel Terms of Reference',r['terms_of_reference'],'VMMS Terms of Reference')
                elif r.get('tor_attachment') or r.get('tor_url'):
                    # The legacy tool supplied a file/URL rather than a structured
                    # TOR record. Retain that evidence in an inactive draft, with
                    # no invented approval, wording, or certification requirements.
                    tor_key='legacy_file_'+key('Deployment Request Tool',r['name'])[:24]
                    terms=frappe.db.get_value('VMMS Terms of Reference',{'tor_key':tor_key})
                    if not terms:
                        terms=historical.insert('VMMS Terms of Reference',{
                            'tor_key':tor_key,'tor_name':r.get('title') or r['name'],
                            'geo_scope':self.geo(r.get('company')),'is_active':0,'docstatus':0,
                            'project':self.project_link('Deployment Request Tool', r),
                            'expected_start_date':r.get('expected_start_date'),'expected_end_date':r.get('expected_end_date'),
                            'approval_mode':'direct','approval_state':'Draft','legacy_unstructured_terms':1,
                            'legacy_terms_url':r.get('tor_url'),
                            'notes':presentation.FILE_TERMS_NOTE},owner=self.source_owner('Deployment Request Tool', r)).name
                else:
                    raise ValueError('Legacy deployment has neither structured terms nor an original terms document')
                self.check_terms_project(source_project, terms)
                place = self.geo(r.get('company'))
                return {'title':self.deployment_title(r,terms),
                    'terms_of_reference':terms,'geo_node':place,
                    'coordinator':self.coordinator('Deployment Request Tool', r, place),
                    'status':self.deployment_status(r.get('expected_start_date'), r.get('expected_end_date')),
                    'site_name':(r.get('location') or '').strip() or None,
                    'requires_contract':int(bool(int(r.get('require_contract_before_deployment') or 0))),
                    'volunteers_required':r.get('number_of_volunteers_required'),
                    'planned_start':r.get('expected_start_date'),'planned_end':r.get('expected_end_date'),
                    'start_date':normalise.date(r.get('expected_start_date')),'end_date':normalise.date(r.get('expected_end_date')),
                    # The old notes as written. The status comes from the dates,
                    # so nothing is added about it.
                    'notes':r.get('notes')}
            self.one('Deployment Request Tool',row,'VMMS Deployment',build,
                     validate_existing=self.validate_deployment)
        for row in self.history.rows('Personnel Deployment Request'):
            def build(r):
                source_project = self.project_context().assignment_project(r)
                volunteer=self.target('Employee',r.get('employee'),'VMMS Volunteer')
                deployment=self.target('Deployment Request Tool',r.get('deployment'),'VMMS Deployment',required=False)
                if not deployment:
                    # Some legacy assignments were standalone or their parent
                    # request was deleted. Retain a derived container only when
                    # the assignment's own recorded terms establish the scope.
                    if not r.get('terms_of_reference') and self.conf.get('allow_missing_history_dependencies'):
                        raise SourceIdentityExcluded('Standalone assignment has no usable deployment or structured TOR; original record and file references retained in archive')
                    terms=self.target('Personnel Terms of Reference',r.get('terms_of_reference'),'VMMS Terms of Reference')
                    original=self.history.get('Personnel Terms of Reference',r.get('terms_of_reference'))
                    if not original or not original.get('company'):
                        raise ValueError('Standalone assignment has no recorded deployment scope')
                    name='legacy-assignment-'+key('assignment',r['name'])[:24]
                    if not frappe.db.exists('VMMS Deployment',name):
                        title_source = {**r, 'company': original['company'],
                                        'title': original.get('title_of_mission')}
                        place = self.geo(original['company'])
                        historical.insert('VMMS Deployment',{'terms_of_reference':terms,
                            'title': self.deployment_title(title_source, terms),
                            'geo_node':place,
                            'coordinator':self.coordinator('Personnel Deployment Request', r, place),
                            'status':self.deployment_status(r.get('expected_start_date'), r.get('expected_end_date')),
                            'volunteers_required':1,'planned_start':r.get('expected_start_date'),
                            'planned_end':r.get('expected_end_date'),
                            'notes':presentation.ASSIGNMENT_NOTE},name=name)
                    deployment=name
                d=frappe.get_doc('VMMS Deployment',deployment)
                terms=(self.target('Personnel Terms of Reference',r['terms_of_reference'],'VMMS Terms of Reference')
                       if r.get('terms_of_reference') else d.terms_of_reference)
                self.check_terms_project(source_project, d.terms_of_reference)
                self.check_terms_project(source_project, terms)
                return {'deployment':deployment,'volunteer':volunteer,'terms_of_reference':terms,
                    'geo_node':d.geo_node,'status':self.assignment_status(r, d.status),'role':'member',
                    'start_date':normalise.date(r.get('expected_start_date')),'end_date':normalise.date(r.get('expected_end_date')),
                    'invited_on':r.get('creation'),'invited_by':self.target('User',r.get('owner'),required=False),
                    'notes':r.get('notes')}
            self.one('Personnel Deployment Request',row,'VMMS Deployment Assignment',build,
                     validate_existing=self.validate_assignment)

    def event_times(self, r, values):
        """The time zone label Buzz derives on save (the import does not save
        through Buzz), and a warning for times Buzz would refuse on an edit.
        The recorded dates and times themselves are kept as they are."""
        from frappe.utils import get_datetime, time_diff_in_seconds
        if values.get('time_zone') and values.get('start_date') and values.get('start_time'):
            try:
                from buzz.utils import get_time_zone_label
                values['time_zone_label'] = get_time_zone_label(
                    values['time_zone'], get_datetime(f"{values['start_date']} {values['start_time']}"))
            except Exception as exc:
                self.warn('Buzz Event', r, 'Time zone label not derived: '+str(exc))
        start, end = values.get('start_time'), values.get('end_time')
        same_day = not values.get('end_date') or str(values.get('end_date')) == str(values.get('start_date'))
        if same_day and start and end:
            try:
                backwards = time_diff_in_seconds(str(end), str(start)) <= 0
            except Exception:
                backwards = False
            if backwards:
                self.warn('Buzz Event', r, 'End time '+str(end)+' is not after start time '+str(start)
                          +' on the same day; kept as recorded. Buzz asks for a later end time when the event is next edited.')

    def events(self):
        if self.history.rows('Buzz Event') and 'buzz' not in frappe.get_installed_apps():
            raise ValueError('Install Buzz on the destination before rehearsing the full event migration.')
        for dt in ('Event Category','Event Host','Event Venue'):
            for row in self.history.rows(dt):
                self.one(dt,row,dt,lambda r,dt=dt:self.copy_values(dt,r),name=row['name'])
        for row in self.history.rows('Buzz Event'):
            def build(r):
                values=self.copy_values('Buzz Event',r,omit=OMIT['Buzz Event'])
                if r.get('company'):
                    if frappe.get_meta('Buzz Event').has_field('geo_node'):
                        try:
                            values['geo_node']=self.geo(r['company'])
                        except ValueError as exc:
                            self.warn('Buzz Event', r, str(exc))
                    else:
                        self.warn('Buzz Event', r, 'Buzz Event has no geo_node field (run the vmmsx migrate); '
                                  'branch '+str(r['company'])+' kept in the archive only')
                if r.get('event_access') not in (None,'','Public'):
                    values['is_published']=0  # Never turn a restricted source event into a public event.
                    if int(r.get('is_published') or 0):
                        self.warn('Buzz Event', r, 'Old event access "'+str(r['event_access'])+'" has no equivalent in Buzz; '
                                  'the published event came in unpublished')
                values.update(send_booking_confirmation_email=0,send_ticket_email=0,auto_send_pitch_deck=0)
                self.event_times(r, values)
                return values
            self.one('Buzz Event',row,'Buzz Event',build,name=row['name'])
        for row in self.history.rows('Event Registration Question'):
            def build_question(r):
                kind={'Text':'Small Text','MultiSelect':'Multi Select','Yes/No':'Check'}.get(r.get('question_type'),r.get('question_type') or 'Small Text')
                return {'event':self.target('Buzz Event',r.get('parent')),
                    'label':r.get('question'),'fieldname':r.get('question_id') or 'legacy_'+key('question',r['name'])[:20],
                    'fieldtype':kind,'options':r.get('options'),'enabled':0,
                    'mandatory':int(bool(r.get('is_required'))),'applied_to':'Booking','order':r.get('idx') or 1}
            self.one('Event Registration Question',row,'Buzz Custom Field',build_question)
        for row in self.history.rows('Event Ticket Type'):
            self.one('Event Ticket Type',row,'Event Ticket Type',lambda r:self.copy_values('Event Ticket Type',r),name=row['name'])
        invoices_by_booking={}
        for invoice in self.history.rows('Sales Invoice'):
            if invoice.get('event_booking'):
                invoices_by_booking.setdefault(invoice['event_booking'],[]).append(invoice)
        for invoice in self.history.rows('Sales Invoice'):
            if invoice.get('is_return') and invoice.get('return_against') and not invoice.get('event_booking'):
                original=self.history.get('Sales Invoice',invoice['return_against']) or {}
                if original.get('event_booking'):
                    invoices_by_booking.setdefault(original['event_booking'],[]).append(invoice)
        for row in self.history.rows('Event Booking'):
            def build(r):
                values=self.copy_values('Event Booking',r,omit=OMIT['Event Booking'])
                attendees=[]
                for child in self.history.children('Event Booking',r['name'],'attendees'):
                    a={f:child.get(f) for f in ('full_name','email','ticket_type','currency','amount','add_on_total','number_of_add_ons')}
                    a['first_name'],a['last_name']=split_name(child.get('full_name'))
                    a['ticket_type']=self.target('Event Ticket Type',child.get('ticket_type'))
                    attendees.append(a)
                values['attendees']=attendees
                values['additional_fields']=[{'fieldname':c.get('question_id') or 'legacy_'+key('response',c['name'])[:20],
                    'label':c.get('question'),'value':c.get('response'),'fieldtype':'Small Text'}
                    for c in self.history.children('Event Booking',r['name'],'responses')]
                user=self.target('User',r.get('user'),required=False)
                if not user:
                    contact=normalise.email(r.get('primary_contact'))
                    user=self.target('User',contact,required=False) if contact else None
                values['user']=user
                values['legacy_primary_contact']=r.get('primary_contact')
                values['legacy_unclaimed_booking']=int(not user)
                # Submitted is not proof of payment. Preserve amounts/docstatus,
                # but use the new Unpaid state unless a source payment fact exists.
                from onerc_vmms_migrate.event_payments import state as payment_state
                values['payment_status']=payment_state(r,invoices_by_booking.get(r['name'],[]))
                values['status']=r.get('status') if r.get('status') in ('Confirmed','Approval Pending','Approved','Rejected') else 'Confirmed' if int(r.get('docstatus') or 0)==1 else 'Approval Pending'
                return values
            self.one('Event Booking',row,'Event Booking',build,name=row['name'])
        for row in self.history.rows('Event Ticket'):
            def build(r):
                values=self.copy_values('Event Ticket',r,omit=OMIT['Event Ticket'])
                values['first_name'],values['last_name']=split_name(r.get('attendee_name'))
                if r.get('booking'):values['booking']=self.target('Event Booking',r['booking'])
                return values
            # Ticket identity is the QR payload: preserve the original ticket name.
            self.one('Event Ticket',row,'Event Ticket',build,name=row['name'])
        for dt in ('Event Check In','Event'):
            for row in self.history.rows(dt):
                self.one(dt,row,dt,lambda r,dt=dt:self.copy_values(dt,r),name=row['name'])

    def applications(self):
        from onerc_vmms_migrate.people_applications import decisions
        for row in self.history.rows('Job Applicant'):
            if not row.get('is_volunteer'):continue
            existing=self.by_source.get(('Job Applicant',row['name'],'VMMS Volunteer Application'))
            if existing:
                if getattr(self,'batch_limit',None) is not None and ('Job Applicant',str(row['name'])) in self.converted_index:
                    continue
                historical.mark(self.snapshot,'Job Applicant',row['name'],'VMMS Volunteer Application',existing,self.run_id)
                continue
            def build(r):
                email=normalise.email(r.get('email_id'))
                profile=frappe.db.get_value('Red Profile',{'email':email}) if email else None
                if not profile:raise ValueError('Application has no migrated person profile')
                state=application_state(r)
                # Accepted alone does not prove an active volunteer Employee was
                # created. Keep that distinction in the original source record.
                # Insert historical state directly; no auto-accept hook runs.
                return {'red_profile':profile,'geo_node':self.geo(r.get('company')),
                    'applied_on':normalise.date(r.get('creation')),'approval_state':state,
                    'legacy_orphan_application':int(state=='Approved'),
                    'approval_decisions':decisions(r)}
            self.one('Job Applicant',row,'VMMS Volunteer Application',build)

    def availability(self):
        from onerc_vmms_migrate.availability_model import windows,day
        from onerc_vmms_migrate.reference import ensure,stable_key
        shift_map={}
        for row in self.history.rows('Shift Type'):
            parts=windows(row.get('start_time'),row.get('end_time'))
            if len(parts)>1:
                self.warnings.append({'source_doctype':'Shift Type','source_name':row['name'],
                    'warning':'Overnight availability expanded onto consecutive days; evening ends at 23:59:59. Exact original times retained in source archive and slot descriptions.'})
            shift_map[row['name']]=[]
            for offset,start,end in parts:
                label='Legacy VMMS: '+row['name']+' ('+str(start or 'unspecified')+'–'+str(end or 'unspecified')+')'
                if len(label)>140:label=label[:110]+' '+stable_key(label)
                description='Original recorded shift: '+str(row.get('start_time'))+' to '+str(row.get('end_time'))+'.'
                if len(parts)>1:
                    description+=' Overnight HRMS shift expanded onto its start day and following day. The evening boundary uses 23:59:59; original exact times are retained above.'
                target=ensure('VMMS Availability Slot','slot_name',label,'slot_key',
                    {'start_time':start,'end_time':end,'is_active':int(bool(start and end)),'description':description})
                shift_map[row['name']].append((offset,target))
        unspecified=ensure('VMMS Availability Slot','slot_name','Legacy availability (time not recorded)','slot_key',{'is_active':0})
        # Native availability is one current schedule per volunteer. Keep each
        # preceding dated schedule in the immutable historical archive.
        latest={}
        for row in self.history.rows('Personnel Availability Schedule'):
            employee=row.get('employee')
            previous=latest.get(employee)
            if previous is None or (str(row.get('start_date') or ''),str(row.get('creation') or ''),row['name']) > (str(previous.get('start_date') or ''),str(previous.get('creation') or ''),previous['name']):
                latest[employee]=row
        for row in latest.values():
            def build(r):
                days=[]
                for child in self.history.children('Personnel Availability Schedule',r['name'],'schedules'):
                    slots=shift_map.get(child.get('shift_type')) if child.get('shift_type') else [(0,unspecified)]
                    if not slots:raise ValueError('Availability day references an unknown shift')
                    for offset,slot in slots:
                        item={'day':day(child.get('day'),offset),'availability_slot':slot}
                        if item not in days:days.append(item)
                return {'volunteer':self.target('Employee',r.get('employee'),'VMMS Volunteer'),
                    'available_on_holidays':r.get('available_on_holidays'),
                    'valid_from':r.get('start_date'),'valid_to':r.get('end_date'),'available_days':days}
            self.one('Personnel Availability Schedule',row,'VMMS Availability Schedule',build,
                name=self.target('Employee',row.get('employee'),'VMMS Volunteer',required=False))

    def time_logs(self):
        for row in self.history.rows('Timesheet'):
            for child in self.history.children('Timesheet',row['name'],'time_logs'):
                if int(row.get('docstatus') or 0)!=1:continue
                def build(r,parent=row):
                    volunteer=self.target('Employee',parent.get('employee'),'VMMS Volunteer')
                    geo=frappe.db.get_value('VMMS Volunteer',volunteer,'home_geo_node')
                    return {'volunteer':volunteer,'geo_node':geo,'log_type':'general',
                        'activity_date':normalise.date(r.get('from_time')),'hours':r.get('hours'),
                        'notes':r.get('description') or r.get('activity_type')}
                self.one('Timesheet Detail',child,'VMMS Time Log',build)

    def helpdesk(self):
        """Helpdesk tickets and their threads: see helpdesk_load."""
        from onerc_vmms_migrate import helpdesk_load
        helpdesk_load.run(self)

    def run(self):
        from onerc_vmms_migrate import workspace
        for method in (self.projects,self.terms,self.deployments,self.events,self.applications,self.availability,self.time_logs):
            frappe.db.savepoint('vmms_history_phase')
            before=len(self.results)
            previous_targets=dict(self.targets)
            try:
                method()
            except Exception as exc:
                frappe.db.rollback(save_point='vmms_history_phase')
                frappe.clear_messages()
                del self.results[before:]
                self.targets=previous_targets
                self.failures.append({'source_doctype':method.__name__,'source_name':'__phase__',
                    'error':frappe.utils.strip_html(str(exc))})
            workspace.write_json(workspace.path('history-progress.json'),{
                'run_id':self.run_id,'phase':method.__name__,'converted':len(self.results),
                'failures':self.failures,'exclusions':self.excluded,'warnings':self.warnings})
        return {'converted':len(self.results),'failed':len(self.failures),
            'by_source':dict(Counter(r['source_doctype'] for r in self.results)),
            'failures':self.failures,'records':self.results,'excluded_count':len(self.excluded),
            'excluded_by_source':dict(Counter(r['source_doctype'] for r in self.excluded)),
            'exclusions':self.excluded,'warnings':self.warnings,'naming':self.naming.report()}


    def run_batch(self, limit, deadline, progress=None):
        """Replay dependency order using the committed conversion index.

        Existing identities do not consume the batch allowance. A boundary is
        checked before inserting the next new record; genuine exceptions still
        escape and roll back the batch. No cursor depends on changing Task order.
        """
        # Both live and test batches retain records on the destination.
        self.naming.dry_run = False
        self.batch_limit, self.batch_deadline = limit, deadline
        self.batch_attempted, self.batch_progress = 0, progress
        source_id = str(self.snapshot['source'].get('source_id') or self.snapshot['source']['db_name'])
        rows = frappe.get_all(historical.ARCHIVE, filters={'source_id':source_id,'conversion_status':'Converted'},
                              fields=['source_doctype','source_name','target_name'])
        self.converted_index = {(r.source_doctype,str(r.source_name)):r.target_name for r in rows}
        complete = True
        try:
            for method in (self.projects,self.terms,self.deployments,self.events,self.applications,self.availability,self.time_logs):
                method()
        except BatchBoundary:
            complete = False
        return {'converted':len(self.results),'failed':len(self.failures),
            'attempted':self.batch_attempted,'complete':complete,
            'by_source':dict(Counter(r['source_doctype'] for r in self.results)),
            'failures':self.failures,'records':self.results,'excluded_count':len(self.excluded),
            'excluded_by_source':dict(Counter(r['source_doctype'] for r in self.excluded)),
            'exclusions':self.excluded,'warnings':self.warnings,'naming':self.naming.report()}
