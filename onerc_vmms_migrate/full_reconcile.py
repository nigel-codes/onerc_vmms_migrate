"""Reconcile retained data, native references and preserved login credentials."""
import hmac
import os

import frappe

from onerc_vmms_migrate import historical, normalise, run, workspace
from onerc_vmms_migrate.source import Source


def verify(snapshot):
    source_id=str(snapshot['source'].get('source_id') or snapshot['source']['db_name'])
    rows=frappe.db.sql('''SELECT record_id,source_doctype,source_name,source_hash,conversion_status,
        target_doctype,target_name,SHA2(source_payload,256) AS payload_hash
        FROM `tabLegacy VMMS Document` WHERE source_id=%s''',(source_id,),as_dict=True)
    retained={r.record_id:r for r in rows}
    failures=[]
    from itertools import chain
    def expected_records():
        return chain(snapshot['history'].values(),({'doctype':'User','record':u} for u in snapshot['users'].values()))
    total=len(snapshot['history'])+len(snapshot['users'])
    import hashlib
    from onerc_vmms_migrate.history_model import canonical
    for item in expected_records():
        record=item['record']
        record_id=historical.archive_id(source_id,item['doctype'],record['name'])
        stored=retained.get(record_id)
        if not stored:
            failures.append({'source_doctype':item['doctype'],'source_name':record['name'],'problem':'Missing retained source record'})
    hashes={(r.record_id,r.source_hash) for r in rows if r.source_hash==r.payload_hash}
    for item in expected_records():
        record=item['record'];record_id=historical.archive_id(source_id,item['doctype'],record['name'])
        digest=hashlib.sha256(canonical(record).encode()).hexdigest()
        if (record_id,digest) not in hashes:
            failures.append({'source_doctype':item['doctype'],'source_name':record['name'],'problem':'Missing or changed retained source version'})
    names={}
    for dt in {r.target_doctype for r in rows if r.target_doctype}:
        names[dt]=set(frappe.get_all(dt,pluck='name')) if frappe.db.exists('DocType',dt) else set()
    for row in rows:
        if row.conversion_status=='Converted' and row.target_name not in names.get(row.target_doctype,set()):
            failures.append({'source_doctype':row.source_doctype,'source_name':row.source_name,'problem':'Native target is missing'})
    # Authentication hashes are compared only in memory and never enter reports.
    auth={r[0]:r[1] for r in frappe.db.sql("SELECT name,password FROM __Auth WHERE doctype='User' AND fieldname='password' AND encrypted=0")}
    passwords_checked=0
    passwords_without_native_account=0
    for name,password in snapshot.get('password_hashes',{}).items():
        user=snapshot['users'][name]
        email=normalise.email(user.get('email')) or normalise.email(name)
        if not email:
            passwords_without_native_account+=1
            continue
        if email not in auth or not hmac.compare_digest(password,str(auth[email])):
            failures.append({'source_doctype':'User','source_name':name,'problem':'Password continuity mismatch'})
        else:passwords_checked+=1
    from onerc_vmms_migrate.full_run import coverage
    summary={'retained_records_expected':total,'retained_records_verified':total-sum(1 for f in failures if f['problem']=='Missing or changed retained source version'),
        'passwords_checked':passwords_checked,'passwords_without_native_account':passwords_without_native_account,'mismatches':len(failures),'native_coverage':coverage(snapshot),
        'people_checked':frappe.db.count('Legacy Migration Record',{'person_key':('not like','account:%')}),
        'accounts_expected':len(snapshot['users'])}
    return summary, failures


def execute():
    with Source() as source:
        summary,failures=verify(source.data)
        directory=workspace.report_dir(run._run_id('full-reconcile'))
        run._write_csv(directory,'mismatches.csv',failures)
        workspace.write_json(os.path.join(directory,'full-reconcile.json'),summary)
        people=run.reconcile()
        summary['people_field_mismatches']=people['mismatches']
        summary['mismatches']+=people['mismatches']
        workspace.write_json(os.path.join(directory,'full-reconcile.json'),summary)
        print({k:v for k,v in summary.items() if k!='native_coverage'})
        return summary
