"""Operational history adapters. Originals remain in the restricted archive.

Historical inserts preserve dates and docstatus without firing modern business
automation. Unresolvable dependencies are reported per record and retained;
they never become fabricated approvals, attendance, payments or contracts.
"""
import datetime
import time
from collections import Counter

import frappe

from onerc_vmms_migrate import historical, normalise, presentation
from onerc_vmms_migrate.crosswalk import Crosswalks, UNMAPPED, DROP
from onerc_vmms_migrate.history_model import History, assignment_state, application_state, key, split_name


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
        override = (self.conf.get('history_branch_overrides') or {}).get(company)
        if override:
            if not frappe.db.exists('Geo Node', override):
                raise ValueError('History Geo Node does not exist: '+str(override))
            return override
        target=self.crosswalks.lookup('branch',company)
        if not target or target is UNMAPPED or target == DROP:
            raise ValueError('Unresolved source branch: '+str(company))
        return target

    def one(self, source_dt, row, target_dt, build, name=None):
        if (source_dt,row['name']) in self.excluded_identities:
            reason = getattr(self, 'excluded_reasons', {}).get((source_dt, str(row['name']))) or PERSON_EXCLUSION+'Source identity requires review; original record retained'
            self.excluded.append({'source_doctype':source_dt,'source_name':row['name'],'reason':reason})
            return None
        batched = getattr(self, 'batch_limit', None) is not None
        identity = (source_dt, str(row['name']))
        old = self.converted_index.get(identity) if batched else historical.converted(self.snapshot,source_dt,row['name'])
        if old:
            self.targets[(source_dt,row['name'])]=old
            self.results.append({'source_doctype':source_dt,'source_name':row['name'],'target_doctype':target_dt,'target_name':old,'outcome':'existing'})
            return old
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
                doc=historical.insert(target_dt,values,name=name)
            historical.mark(self.snapshot,source_dt,row['name'],target_dt,doc.name,self.run_id)
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
                    continue
            if field.fieldtype in ('Attach','Attach Image') and value and str(value).startswith(('/files/','/private/files/')):
                # The general attachment phase restores these with real owners.
                continue
            values[name]=value
        values['docstatus']=int(row.get('docstatus') or 0)
        return values

    def projects(self):
        for row in self.history.rows('Project'):
            def build(r):
                values=self.copy_values('Project',r,omit=('company','cost_center','customer','sales_order','project_template','project_type','users'))
                if frappe.get_meta('Project').has_field('vmms_geo_node'):
                    values['vmms_geo_node']=self.geo(r.get('company'))
                # ERP financial accounts and customer IDs remain history; no
                # chart of accounts is manufactured to satisfy an optional link.
                return values
            self.one('Project',row,'Project',build,name=row['name'])
        # ERP tasks are project work items; no volunteer is guessed for them.
        pending=self.history.rows('Task')
        while pending:
            ready=[r for r in pending if not r.get('parent_task') or self.target('Task',r['parent_task'],required=False)]
            if not ready:ready=pending[:1]
            for row in ready:
                def build(r):
                    values=self.copy_values('Task',r,omit=('company','department','issue','depends_on','type','parent_task','lft','rgt','old_parent'))
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

    def terms(self):
        child_map={'stakeholders':('TOR Stakeholders','stakeholder'), 'objectives':('TOR Objectives','objective'),
            'expected_outputs':('TOR Outputs','output'), 'approach_methods':('TOR Approach Methods','approach'),
            'itinerary':('TOR Itinerary','activity'), 'resources':('TOR Resources','resource')}
        for row in self.history.rows('Personnel Terms of Reference'):
            def build(r):
                values={'tor_name':r.get('title_of_mission'),'tor_key':'legacy_'+key('tor',r['name'])[:24],
                    'geo_scope':self.geo(r.get('company')),'project':self.target('Project',r.get('project'),required=False),
                    'expected_start_date':r.get('expected_start_date'),'expected_end_date':r.get('expected_end_date'),
                    'mission_background':r.get('mission_background'),'approval_mode':'direct',
                    'docstatus':int(r.get('docstatus') or 0),'approval_state':{'Approved':'Approved','Draft':'Draft','Pending Approval':'Submitted'}.get(r.get('workflow_state'),'Draft'),
                    'notes':r.get('notes') or ''}
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
                            new['methodology']=frappe.db.get_value('VMMS TOR Methodology',{'methodology_name':child['methodology']})
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

    def deployments(self):
        for row in self.history.rows('Deployment Request Tool'):
            def build(r):
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
                            'expected_start_date':r.get('expected_start_date'),'expected_end_date':r.get('expected_end_date'),
                            'approval_mode':'direct','approval_state':'Draft','legacy_unstructured_terms':1,
                            'legacy_terms_url':r.get('tor_url'),
                            'notes':presentation.FILE_TERMS_NOTE}).name
                else:
                    raise ValueError('Legacy deployment has neither structured terms nor an original terms document')
                coordinator=self.target('User',r.get('owner'))
                return {'terms_of_reference':terms,'geo_node':self.geo(r.get('company')),
                    'coordinator':coordinator,'status':'Planned','volunteers_required':r.get('number_of_volunteers_required'),
                    'planned_start':r.get('expected_start_date'),'planned_end':r.get('expected_end_date'),
                    'start_date':normalise.date(r.get('expected_start_date')),'end_date':normalise.date(r.get('expected_end_date')),
                    'notes':(r.get('notes') or '')+presentation.DEPLOYMENT_SUFFIX}
            self.one('Deployment Request Tool',row,'VMMS Deployment',build)
        for row in self.history.rows('Personnel Deployment Request'):
            def build(r):
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
                        historical.insert('VMMS Deployment',{'terms_of_reference':terms,
                            'geo_node':self.geo(original['company']),
                            'coordinator':self.target('User',r.get('owner')),'status':'Planned',
                            'volunteers_required':1,'planned_start':r.get('expected_start_date'),
                            'planned_end':r.get('expected_end_date'),
                            'notes':presentation.ASSIGNMENT_NOTE},name=name)
                    deployment=name
                d=frappe.get_doc('VMMS Deployment',deployment)
                terms=self.target('Personnel Terms of Reference',r.get('terms_of_reference'),'VMMS Terms of Reference',required=False) or d.terms_of_reference
                return {'deployment':deployment,'volunteer':volunteer,'terms_of_reference':terms,
                    'geo_node':d.geo_node,'status':assignment_state(r),'role':'member',
                    'start_date':normalise.date(r.get('expected_start_date')),'end_date':normalise.date(r.get('expected_end_date')),
                    'invited_on':r.get('creation'),'invited_by':self.target('User',r.get('owner'),required=False),
                    'notes':r.get('notes')}
            self.one('Personnel Deployment Request',row,'VMMS Deployment Assignment',build)

    def events(self):
        if self.history.rows('Buzz Event') and 'buzz' not in frappe.get_installed_apps():
            raise ValueError('Install Buzz on the destination before rehearsing the full event migration.')
        for dt in ('Event Category','Event Host','Event Venue'):
            for row in self.history.rows(dt):
                self.one(dt,row,dt,lambda r,dt=dt:self.copy_values(dt,r),name=row['name'])
        for row in self.history.rows('Buzz Event'):
            def build(r):
                values=self.copy_values('Buzz Event',r,omit=('ticket_email_template','ticket_print_format','default_ticket_type'))
                if r.get('event_access') not in (None,'','Public'):
                    values['is_published']=0  # Never turn a restricted source event into a public event.
                values.update(send_booking_confirmation_email=0,send_ticket_email=0,auto_send_pitch_deck=0)
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
                values=self.copy_values('Event Booking',r,omit=('amended_from','coupon_code','payment_status','status'))
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
                values=self.copy_values('Event Ticket',r,omit=('qr_code','amended_from'))
                values['first_name'],values['last_name']=split_name(r.get('attendee_name'))
                if r.get('booking'):values['booking']=self.target('Event Booking',r['booking'])
                return values
            # Ticket identity is the QR payload: preserve the original ticket name.
            self.one('Event Ticket',row,'Event Ticket',build,name=row['name'])
        for dt in ('Event Check In','Event'):
            for row in self.history.rows(dt):
                self.one(dt,row,dt,lambda r,dt=dt:self.copy_values(dt,r),name=row['name'])

    def applications(self):
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
                    'applied_on':normalise.date(r.get('creation')),'approval_state':state}
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
            'exclusions':self.excluded,'warnings':self.warnings}


    def run_batch(self, limit, deadline, progress=None):
        """Replay dependency order using the committed conversion index.

        Existing identities do not consume the batch allowance. A boundary is
        checked before inserting the next new record; genuine exceptions still
        escape and roll back the batch. No cursor depends on changing Task order.
        """
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
            'exclusions':self.excluded,'warnings':self.warnings}
