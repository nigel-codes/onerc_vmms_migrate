"""Transfer every captured login, including people without a volunteer record."""
import hashlib

import frappe

from onerc_vmms_migrate import load, normalise, workspace
from onerc_vmms_migrate.history_model import canonical
from onerc_vmms_migrate.silence import SilenceBreached


def transfer(snapshot, conf, run_id, progress=None, stop=None):
    result = {'loaded': 0, 'existing': 0, 'failed': [], 'without_password': 0,
              'roles_requiring_review': [], 'excluded': [], 'total': len(snapshot['users'])}
    workspace.write_json(workspace.path('account-failures-in-progress.json'),[])
    processed = 0
    for index, row in enumerate(snapshot['users'].values(), 1):
        if processed and stop and stop():
            break
        processed += 1
        email = normalise.email(row.get('email')) or normalise.email(row['name'])
        if row['name'] in ('Administrator','Guest'):
            continue
        password = snapshot.get('password_hashes', {}).get(row['name'])
        roles = [r.get('role') for r in row.get('roles') or [] if r.get('role')]
        unusual = sorted(set(roles) - {'All','Guest','Volunteer','Vmms Guest','Employee','Employee Self Service',
            'LMS Student','Desk User','Website User','Buzz User'})
        if unusual:
            result['roles_requiring_review'].append({'source_user':row['name'],'roles':unusual})
        if not email:
            result['excluded'].append({'source_name':row['name'],'error':'No valid login email; original account retained in restricted history'})
            continue
        frappe.db.savepoint('vmms_account')
        try:
            existed = bool(frappe.db.exists('User',email))
            login = {'email':email,'source_name':row['name'],'enabled':int(bool(row.get('enabled'))),
                'password_hash':password,'social_logins':row.get('social_logins') or [],
                **{f:row.get(f) for f in ('username','mobile_no','login_after','login_before','restrict_ip','language','time_zone')}}
            context={'bundle':{'user':login,'profile':{'first_name':row.get('first_name') or email,
                'middle_name':row.get('middle_name'),'last_name':row.get('last_name') or ''}},
                'conf':conf,'ledger':[],'warnings':[]}
            target = load._user(context)
            from onerc_vmms_migrate.historical import mark
            mark(snapshot,'User',row['name'],'User',target,run_id)
            result['existing' if existed else 'loaded'] += 1
            result['without_password'] += int(not password)
            # Account rows have a separate key so they never make the people
            # loader mistake an account-only phase for a completed person.
            if not frappe.db.exists(load.LEDGER,{'person_key':'account:'+email}):
                frappe.get_doc({'doctype':load.LEDGER,'person_key':'account:'+email,
                    'source_doctype':'User','source_name':row['name'],'target_doctype':'User',
                    'target_name':target,'outcome':'adopted' if existed else 'created',
                    'source_hash':hashlib.sha256(canonical(row).encode()).hexdigest(),
                    'run_id':run_id}).insert(ignore_permissions=True)
        except SilenceBreached:
            raise
        except Exception as exc:
            frappe.db.rollback(save_point='vmms_account')
            frappe.clear_messages()
            result['failed'].append({'source_name':row['name'],'error':frappe.utils.strip_html(str(exc))})
            # Private checkpoint exposes repeatable source-validation issues
            # before a large import finishes; no personal values go to progress.
            workspace.write_json(workspace.path('account-failures-in-progress.json'),result['failed'])
        if progress and index % 100 == 0:
            progress({'phase':'accounts','processed':index,'total':result['total'],'loaded':result['loaded'],'existing':result['existing'],'failed':len(result['failed'])})
    if stop is not None:
        result['processed'] = processed
    return result
