"""Create source vocabularies and geography without guessing identity mappings."""
import hashlib

import frappe

from onerc_vmms_migrate import normalise, workspace
from onerc_vmms_migrate.crosswalk import Crosswalks, KINDS, VOLUNTEER_STATUS_DEFAULTS
from onerc_vmms_migrate.history_model import History


def stable_key(value):
    return 'legacy_' + hashlib.sha256(str(value).encode()).hexdigest()[:20]


def ensure(doctype, label_field, value, key_field=None, extra=None):
    value = str(value or '').strip()
    if not value:
        return None
    if not frappe.get_meta(doctype).has_field(label_field):
        label_field = 'name'
    existing=frappe.db.exists(doctype,value)
    if existing:
        return existing
    names = frappe.get_all(doctype, filters={label_field: value}, pluck='name', limit=2)
    if len(names) == 1:
        return names[0]
    if len(names) > 1:
        raise ValueError(f'Ambiguous {doctype} label; map it explicitly.')
    data = {'doctype': doctype, label_field: value, **(extra or {})}
    if key_field:
        data[key_field] = stable_key(value)
    return frappe.get_doc(data).insert(ignore_permissions=True).name


def prepare(snapshot, conf=None):
    """Called explicitly before rehearsal. Every supplied mapping takes priority."""
    history = History(snapshot)
    cw = Crosswalks.read()
    report = {'mapped': {}, 'errors': [], 'geography': {}}
    attempted = set()
    def map_value(kind, value, target, correct_own_level=False):
        if not value or not target:
            return
        k = normalise.key(value)
        rows = cw.entries.setdefault(kind, {})
        previous=rows.get(k,{}).get('target')
        own_level_error=bool(correct_own_level and previous and kind=='branch'
            and frappe.db.get_value('Geo Node',previous,'geo_code')=='vmms-'+stable_key('Company\0'+str(value))
            and frappe.db.get_value('Geo Node',previous,'geo_level')!=frappe.db.get_value('Geo Node',target,'geo_level'))
        if not previous or own_level_error:
            rows[k] = {'source_value': value, 'target': target,
                       'note': 'Imported from the source vocabulary; original record retained.'}
            report['mapped'][kind] = report['mapped'].get(kind, 0) + 1
    def lookup(kind, value, dt, label, key_field=None, extra=None):
        if not normalise.text(value) or normalise.key(value) in {normalise.key(v) for v in workspace.DEFAULTS['placeholder_values']}:
            return
        if cw.target(kind,normalise.key(value)):
            return
        token = (kind, normalise.key(value))
        if token in attempted:
            return
        attempted.add(token)
        if dt == 'Language':
            # These are spoken-language labels, not necessarily UI locales.
            # Retain unknown dialects under an explicit private-use code.
            code = 'x-vmms' + hashlib.sha256(str(value).encode()).hexdigest()[:16].translate(str.maketrans('0123456789', 'ghijklmnop'))
            extra = {'language_code': code, 'enabled': 0}
        frappe.db.savepoint("vmms_reference")
        try:
            map_value(kind, value, ensure(dt, label, value, key_field, extra))
        except Exception as exc:
            frappe.db.rollback(save_point='vmms_reference')
            report['errors'].append({'kind': kind, 'value': value, 'error': str(exc)})
    for old, new in VOLUNTEER_STATUS_DEFAULTS.items():
        map_value('volunteer_status', old, new)
    for field, kind, dt, label, key_field in (
        ('profession','profession','Profession','profession',None),
        ('gender','gender','Gender','gender',None),
        ('identification_type','identification_type','Identification Type','identification_type_name','identification_type_key')):
        for group in ('users','applicants'):
            for row in snapshot[group].values():
                lookup(kind, row.get(field), dt, label, key_field)
    # Preserve unknown ID vocabularies as their own types rather than dropping IDs.
    map_value('identification_type', '__passport_number__', frappe.db.get_value('Identification Type', {'identification_type_name':'Passport'}))
    specs = (
        ('languages',('language','language_name'),'language','Language','language_name',None),
        ('disabilities',('disability','disability_name'),'disability','Disability','disability_name',None),
        ('education',('level','education_level','degree_type'),'education_level','VMMS Education Level','level_name','level_key'),
        ('driving_licence',('licence_class','class'),'driving_licence_class','VMMS Driving Licence Class','class_name','class_key'),
        ('licences',('license_type','licence_type'),'licence_type','Personnel License Type','license_type',None),
        ('additional_skills',('additional_skill','skill','skill_name'),'skill','VMMS Skill','skill_name','skill_key'),
        ('skill',('skill','skill_name'),'skill','VMMS Skill','skill_name','skill_key'),
    )
    for group in ('users','applicants','employees'):
        for row in snapshot[group].values():
            for field, sources, kind, dt, label, key_field in specs:
                for child in row.get(field) or []:
                    value = next((child.get(f) for f in sources if child.get(f)), None)
                    lookup(kind, value, dt, label, key_field)
    for row in snapshot['membership_types'].values():
        cycle = row.get('billing_cycle')
        if cycle not in ('One Off','Monthly','Yearly'):
            report['errors'].append({'kind':'membership_type','value':row['name'],'error':'Unknown billing cycle'})
            continue
        target = ensure('VMMS Membership Type','membership_type_name',row.get('membership_type') or row['name'],
            'membership_type_key', {'fee_amount':row.get('amount') or 0,'fee_currency':row.get('currency'),
             'is_lifetime':int(cycle=='One Off'),'duration_days':30 if cycle=='Monthly' else 365,
             'approval_mode':'routed'})
        map_value('membership_type',row['name'],target)
    # Supporting scans stay private on the profile. Their original types survive
    # in the archive; a scan alone is not proof of an ID number/type.
    for group in ('users','applicants'):
        for row in snapshot[group].values():
            for child in row.get('supporting_documents') or []:
                map_value('supporting_document', child.get('document_type') or child.get('type'), 'file')
    from onerc_vmms_migrate.transform import QUESTION_FIELDS
    for field in QUESTION_FIELDS:
        label = 'Legacy: ' + field.replace('_',' ')
        existing = frappe.db.get_value('VMMS Application Question', {'asked_on':'VMMS Volunteer Application','question_label':label})
        target = existing or frappe.get_doc({'doctype':'VMMS Application Question',
            'asked_on':'VMMS Volunteer Application','question_label':label,
            'field_type':'Small Text','is_active':0,'is_required':0}).insert(ignore_permissions=True).name
        map_value('question',field,target)
    for row in history.rows('TOR Methodology'):
        ensure('VMMS TOR Methodology','methodology_name',row['name'],'methodology_key')
    from onerc_vmms_migrate.trees import batched_trees
    with batched_trees():
        geography(history, map_value, report)
    # Store the database's canonical document spelling, including case.
    for kind,entries in cw.entries.items():
        dt=KINDS[kind].get('doctype')
        if not dt:continue
        for entry in entries.values():
            target=entry.get('target')
            if target and target!='-':
                actual=frappe.db.exists(dt,target)
                if actual:entry['target']=actual
    # Include entries even when transform has not visited them yet.
    for kind, rows in cw.entries.items():
        for k, row in rows.items():
            cw.display[(kind,k)] = row['source_value']
    cw.write(suggest=False)
    return report


def geography(history, map_value, report):
    levels = [('national',1),('region',2),('county',3),('sub_county',4),('ward',5),('administrative_location',6),('sub_location',7)]
    for level, order in levels:
        if not frappe.db.exists('Geo Level',level):
            frappe.get_doc({'doctype':'Geo Level','geo_level_key':level,'geo_level_name':level.replace('_',' ').title(),
                'geo_level_order':order,'requires_parent':int(order>1)}).insert(ignore_permissions=True)
    def node(dt, row, label, level, parent):
        code = 'vmms-' + stable_key(dt+'\0'+row['name'])
        found = frappe.db.get_value('Geo Node', {'geo_code':code})
        if not found:
            # A name is only reusable under the same known parent and level.
            matches = frappe.get_all('Geo Node',filters={'geo_node_name':label,'geo_level':level,
                'parent_geo_node':parent or ('is','not set')},pluck='name',limit=2)
            if len(matches)==1:
                found=matches[0]
            elif matches:
                raise ValueError('Ambiguous geographic siblings: '+label)
            else:
                siblings=frappe.get_all('Geo Node', filters={'geo_node_name':label,
                    'parent_geo_node':parent or ('is','not set')}, pluck='name', limit=1)
                if siblings:
                    label += ' (' + level.replace('_', ' ').title() + ')'
                frappe.db.savepoint('vmms_geo_node')
                try:
                    found=frappe.get_doc({'doctype':'Geo Node','geo_node_name':label,'geo_level':level,
                        'parent_geo_node':parent,'geo_code':code,'is_group':int(level!='sub_location')}).insert(ignore_permissions=True).name
                except Exception:
                    frappe.db.rollback(save_point='vmms_geo_node')
                    raise
        return found
    companies={r['name']:r for r in history.rows('Company')}
    company_nodes={}
    counties={normalise.key(value) for r in history.rows('County') for value in (r['name'],r.get('county_name')) if value}
    pending=dict(companies)
    while pending:
        progress=False
        for name,row in list(pending.items()):
            parent=row.get('parent_company')
            if parent and parent not in company_nodes:
                if parent not in companies:
                    report['errors'].append({'kind':'branch','value':name,'error':'Missing company parent'})
                    del pending[name]
                continue
            kind=row.get('organisation_type')
            level={'Branch':'county','Region':'region','Company':'national'}.get(kind)
            if normalise.key(name) in counties:
                level='county'
            elif level=='national' and parent:
                parent_level=frappe.db.get_value('Geo Node',company_nodes[parent],'geo_level')
                level={'national':'region','region':'county'}.get(parent_level,level)
            if not level:
                report['errors'].append({'kind':'branch','value':name,'error':'Unknown organisation type'})
                del pending[name]
                continue
            try:
                effective_parent=company_nodes.get(parent)
                if level=='county' and not parent:
                    effective_parent=company_nodes.get('Kenya Red Cross Society')
                company_nodes[name]=node('Company',row,row.get('company_name') or name,level,effective_parent)
                map_value('branch',name,company_nodes[name])
            except Exception as exc:
                report['errors'].append({'kind':'branch','value':name,'error':str(exc)})
            del pending[name];progress=True
        if not progress:
            report['errors'].extend({'kind':'branch','value':n,'error':'Company hierarchy cycle or unresolved parent'} for n in pending)
            break
    roots=[n for n in company_nodes.values() if frappe.db.get_value('Geo Node',n,'geo_level')=='national']
    primary=company_nodes.get('Kenya Red Cross Society') or (roots[0] if len(roots)==1 else None)
    maps={}
    for dt,field,label,level,parent_dt,parent_field in (
        ('County','county','county_name','county',None,None),
        ('Sub County','sub_county','sub_county_name','sub_county','County','county'),
        ('Ward','ward',None,'ward','Sub County','sub_county'),
        ('Administrative Location','administrative_location','location_name','administrative_location','Sub County','sub_county'),
        ('Sub Location','sub_location','sub_location_name','sub_location','Administrative Location','location')):
        maps[dt]={}
        for row in history.rows(dt):
            try:
                parent=maps.get(parent_dt,{}).get(row.get(parent_field)) if parent_dt else primary
                # County branches and civil counties describe the same county only
                # on an exact, unambiguous source name, never a fuzzy match.
                found=company_nodes.get(row['name']) if dt=='County' else None
                if found and frappe.db.get_value('Geo Node',found,'geo_level')!='county':found=None
                if not parent and not found:
                    raise ValueError('Source geographic parent is absent; map explicitly')
                target=found or node(dt,row,row.get(label) or row['name'],level,parent)
                maps[dt][row['name']]=target
                map_value('residence',field+':'+row['name'],target)
            except Exception as exc:
                report['errors'].append({'kind':'residence','value':field+':'+row['name'],'error':str(exc)})
    # Exact county identities are authoritative when the old Company master
    # kept its default organisation_type='Company'. Never alter a user-selected
    # mapping or an existing destination geography record.
    for company in companies:
        matches={maps['County'][r['name']] for r in history.rows('County')
            if r['name'] in maps['County'] and normalise.key(company) in
            {normalise.key(r['name']),normalise.key(r.get('county_name'))}}
        if len(matches)==1:map_value('branch',company,matches.pop(),correct_own_level=True)
    report['geography']={'companies':len(company_nodes),**{dt:len(rows) for dt,rows in maps.items()}}
