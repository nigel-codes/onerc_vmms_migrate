"""Full migration orchestration, retained history and native dependency phases.

A rehearsal runs all phases in one destination transaction, so deployment and
event adapters can resolve the people created earlier, then rolls it all back.
It never writes attachment bytes or changes the source. A committed import
requires the same source/config/mapping rehearsal and the browser cutover gate.
"""
import datetime
import os
from collections import Counter

import frappe
from frappe.utils import today

from onerc_vmms_migrate import accounts, historical, load, run, workspace
from onerc_vmms_migrate.crosswalk import Crosswalks
from onerc_vmms_migrate.extract import extract
from onerc_vmms_migrate.history_load import Loader
from onerc_vmms_migrate.silence import Guard, SilenceBreached, silenced
from onerc_vmms_migrate.source import Source
from onerc_vmms_migrate import transform
from onerc_vmms_migrate.transaction import large_transaction
from onerc_vmms_migrate.policy import source_exclusions_accepted


@large_transaction
def prepare():
    from onerc_vmms_migrate.reference import prepare as prepare_references
    with Source() as source:
        snapshot=extract(source)
        if not snapshot.get('schemas'):
            frappe.throw('Take a fresh full-scope Bulk Export capture with both apps updated.')
        with silenced():
            result=prepare_references(snapshot,workspace.config())
    frappe.db.commit()
    directory=workspace.report_dir(run._run_id('reference-setup'))
    workspace.write_json(os.path.join(directory,'reference-setup.json'),result)
    print({'mapped':result['mapped'],'geography':result['geography'],'errors':len(result['errors'])})
    return result


@large_transaction
def execute(confirm=None, limit=None, progress=None):
    from onerc_vmms_migrate.file_transfer import assert_supported
    assert_supported()
    dry_run=confirm!='LOAD'
    conf=workspace.config()
    if not conf.get('preserve_passwords'):
        frappe.throw('Enable protected password preservation for this migration before rehearsing or importing.')
    run_id=run._run_id('full-rehearsal' if dry_run else 'full-import')
    report=workspace.report_dir(run_id)
    if not dry_run:
        from frappe.utils.scheduler import is_scheduler_disabled
        if not is_scheduler_disabled():
            frappe.throw('Disable the destination scheduler before importing.')
    frappe.set_user('Administrator')
    frappe.db.commit()
    result={'mode':'rehearsal' if dry_run else 'import','run_id':run_id,'failed':0,
            'rejected_before_load':0,'changed_since_migrated':0,'limited':bool(limit)}
    try:
        with Source(conf) as source, silenced():
            snapshot=extract(source)
            if not snapshot.get('schemas') or not snapshot.get('history'):
                frappe.throw('This capture has no complete VMMS/Buzz history. Update both apps and capture again.')
            if snapshot.get('capture_complete') is False and not dry_run and not conf.get('allow_missing_files'):
                frappe.throw('Source attachment bytes are missing. Review the report; use a complete live capture or explicitly approve attachment exceptions in settings.')
            from onerc_vmms_migrate import login_policy
            result['authentication_policy']=login_policy.plan(snapshot,conf)
            outbox=Guard()
            if progress:progress({'phase':'archive'})
            result['archive']=historical.archive(snapshot,run_id)
            if progress:progress({'phase':'accounts'})
            account_result=accounts.transfer(snapshot,conf,run_id,progress=progress)
            run._write_csv(report,'account-failures.csv',account_result.pop('failed'))
            excluded_accounts=account_result.pop('excluded')
            run._write_csv(report,'account-source-exclusions.csv',excluded_accounts)
            account_result['excluded']=len(excluded_accounts)
            run._write_csv(report,'staff-roles-for-review.csv',account_result.pop('roles_requiring_review'))
            # Count from the files as well as summaries; do not let a dropped
            # exception list make a partial rehearsal look successful.
            result['accounts']=account_result
            account_total=account_result['loaded']+account_result['existing']
            result['failed']+=account_result['total']-account_total-len(excluded_accounts)
            if progress:progress({'phase':'people'})
            crosswalks=Crosswalks.read()
            built=transform.build(snapshot,crosswalks,conf,datetime.date.fromisoformat(today()))
            run._report_plan(run_id,crosswalks,built)
            bundles=built['bundles'][:int(limit)] if limit else built['bundles']
            snapshot['excluded_native_identities']={(dt,name) for b in bundles if b['errors'] for dt,name in b['sources'] if dt!='User'}
            snapshot['excluded_native_identities'].update(('User',row['source_name']) for row in excluded_accounts)
            snapshot['excluded_native_identities'].update((dt,name) for b in bundles if b.get('volunteer_excluded') for dt,name in b['sources'] if dt=='Employee')
            excluded_volunteers=[{'person':b['key'],'reason':'Multiple source volunteer records share an account'} for b in bundles if b.get('volunteer_excluded')]
            run._write_csv(report,'volunteer-source-exclusions.csv',excluded_volunteers)
            volunteer_total=sum(any(dt=='Employee' for dt,_ in b['sources']) for b in bundles)
            result['volunteer_source_exclusions']=len(excluded_volunteers)
            result['volunteer_exclusions_accepted']=not excluded_volunteers or bool(conf.get('allow_source_exclusions') and len(excluded_volunteers)/max(1,volunteer_total)<=0.01)
            failures=[]
            loaded=existing=0
            for index,bundle in enumerate(bundles,1):
                if bundle['errors']:
                    result['rejected_before_load']+=1
                    continue
                done=load.already_migrated(bundle['key'])
                if done:
                    if done.source_hash!=bundle['source_hash']:
                        result['changed_since_migrated']+=1
                    existing+=1
                    continue
                frappe.db.savepoint('vmms_person')
                try:
                    written=load.load_bundle(bundle,source,conf,run_id,dry_run=dry_run)
                    # Retention and native conversion have separate accounting.
                    for dt,name in bundle['sources']:
                        target_dt,target_name=('User',written['user']) if dt=='User' else ('VMMS Volunteer',written['volunteer']) if dt=='Employee' else ('VMMS Member',written['member']) if dt=='VM Member' else (None,None)
                        if target_name:historical.mark(snapshot,dt,name,target_dt,target_name,run_id)
                    for membership in bundle['memberships']:
                        target=frappe.db.get_value('Legacy Migration Record',{'source_doctype':'VM Membership','source_name':membership['source_name'],'target_doctype':'VMMS Membership'},'target_name')
                        if target:historical.mark(snapshot,'VM Membership',membership['source_name'],'VMMS Membership',target,run_id)
                    loaded+=1
                except SilenceBreached:
                    raise
                except Exception as exc:
                    frappe.db.rollback(save_point='vmms_person')
                    frappe.clear_messages()
                    failures.append({'person':bundle['key'],'error':frappe.utils.strip_html(str(exc))})
                if progress and index%100==0:progress({'phase':'people','processed':index,'total':len(bundles)})
            result['people']={'total':len(built['bundles']),'loaded':loaded,'existing':existing,'failed':len(failures)}
            result['attempted']=len(bundles)
            result['failed']+=len(failures)
            run._write_csv(report,'people-failures.csv',failures)
            if progress:progress({'phase':'history'})
            history=Loader(snapshot,run_id,source,conf,dry_run=dry_run).run()
            result['history']={k:v for k,v in history.items() if k not in ('records','failures','exclusions','warnings')}
            result['failed']+=history['failed']
            run._write_csv(report,'history-converted.csv',history['records'])
            run._write_csv(report,'history-failures.csv',history['failures'])
            run._write_csv(report,'history-source-exclusions.csv',history['exclusions'])
            run._write_csv(report,'history-warnings.csv',history['warnings'])
            result['history_exclusions_accepted']=not history['excluded_count'] or bool(conf.get('allow_source_exclusions') and history['excluded_count']/max(1,history['converted']+history['excluded_count'])<=0.01)
            if progress:progress({'phase':'attachments'})
            result['attachments']=attachments(snapshot,source,run_id,dry_run=dry_run)
            # Missing bytes are visible even in a data-only local rehearsal.
            result['source_files_complete']=snapshot.get('capture_complete') is not False
            result['native_coverage']=coverage(snapshot)
            result['retained_by_doctype']={dt:meta.get('count',0) for dt,meta in snapshot['schemas'].items()}
            if progress:progress({'phase':'reconciliation'})
            from onerc_vmms_migrate.full_reconcile import verify
            result['reconciliation'],mismatches=verify(snapshot)
            run._write_csv(report,'rehearsal-reconciliation-mismatches.csv',mismatches)
            result['failed']+=len(mismatches)
            result['people_reconciliation']=run.reconcile()
            result['failed']+=result['people_reconciliation']['mismatches']
            outbox.check()
            result['source_exclusions_review_required']=bool(result['rejected_before_load'] or excluded_accounts)
            result['source_exclusions_accepted']=source_exclusions_accepted(
                result['rejected_before_load'],len(built['bundles']),len(excluded_accounts),
                account_result['total'],bool(conf.get('allow_source_exclusions')))
            result['passed']=bool(result['attempted']) and not any(result[k] for k in ('failed','changed_since_migrated','limited')) and result['source_exclusions_accepted'] and result['history_exclusions_accepted'] and result['volunteer_exclusions_accepted'] and (result['source_files_complete'] or bool(conf.get('allow_missing_files')))
            if dry_run:frappe.db.rollback()
            else:
                if result['passed']:login_policy.apply(result['authentication_policy'])
                frappe.db.commit()
    except Exception:
        frappe.db.rollback()
        raise
    workspace.write_json(os.path.join(report,'full-migration.json'),result)
    print({k:v for k,v in result.items() if k not in ('retained_by_doctype','native_coverage')})
    return result


def coverage(snapshot):
    """Never confuse retained archives with usable native records."""
    from onerc_vmms_migrate.historical import ARCHIVE
    counts=frappe.get_all(ARCHIVE,fields=['source_doctype','conversion_status',{'COUNT': '*', 'as': 'n'}],group_by='source_doctype,conversion_status',order_by=None)
    result={}
    for row in counts:
        result.setdefault(row.source_doctype,{})[row.conversion_status]=row.n
    return result


def attachments(snapshot,source,run_id,dry_run=True):
    """Restore bytes and rewrite native attachment fields, preserving ownership."""
    from onerc_vmms_migrate.bulk_source import sha256_file
    from onerc_vmms_migrate.history_model import History
    source_id=str(snapshot['source'].get('source_id') or snapshot['source']['db_name'])
    manifest=snapshot.get('all_file_manifest',snapshot.get('captured_file_manifest',{}))
    report={'available':0,'missing':0,'restored':0,'existing':0,'native_fields_updated':0}
    cache={}
    from onerc_vmms_migrate.file_transfer import required_privacy
    def restore(url,doctype,name,private=None):
        item=manifest.get(url)
        if not item or item.get('status')!='ready':return None
        private=required_privacy(doctype,bool(item.get('is_private')),private)
        identity=(url,doctype,name,private)
        if identity in cache:return cache[identity]
        for file in frappe.get_all('File',filters={'attached_to_doctype':doctype,'attached_to_name':name,
            'file_name':source.file_name(url),'is_private':int(private)},fields=['name','file_url']):
            path=frappe.get_doc('File',file.name).get_full_path()
            if os.path.isfile(path) and sha256_file(path)==item['sha256']:
                report['existing']+=1
                cache[identity]=file.file_url
                return file.file_url
        context={'source':source,'warnings':[],'dry_run':False,'conf':{}}
        new_url=load._copy_file(context,url,doctype,name,private=private)
        cache[identity]=new_url
        report['restored']+=1
        return new_url
    for item in manifest.values():
        url=item['source_url']
        path=source.file_path(url)
        if not path:
            report['missing']+=1
            continue
        source.verify_file(url,path)
        report['available']+=1
        if dry_run:continue
        for reference in item.get('references') or []:
            dt,name=reference.get('attached_to_doctype'),reference.get('attached_to_name')
            record_id=historical.archive_id(source_id,dt,name) if dt and name else None
            converted=frappe.db.get_value(historical.ARCHIVE,{'record_id':record_id,'conversion_status':'Converted'},['target_doctype','target_name'],as_dict=True) if record_id else None
            if converted:
                target_dt,target_name=converted.target_doctype,converted.target_name
            else:
                archive_name=frappe.db.get_value(historical.ARCHIVE,{'record_id':record_id},'name') if record_id else None
                file_name=reference.get('name')
                if not archive_name and file_name:
                    archive_name=frappe.db.get_value(historical.ARCHIVE,{'record_id':historical.archive_id(source_id,'File',file_name)},'name')
                if not archive_name:continue
                target_dt,target_name=historical.ARCHIVE,archive_name
            new_url=restore(url,target_dt,target_name)
            if reference.get('name'):
                native_file=frappe.db.get_value('File',{'attached_to_doctype':target_dt,'attached_to_name':target_name,'file_url':new_url},'name')
                if native_file:historical.mark(snapshot,'File',reference['name'],'File',native_file,run_id)
    # Embedded Attach fields can have no File.attached_to_* metadata at all.
    # Resolve them from the original schema, so banners, QR images and evidence
    # are restored even when the original upload was recorded as unattached.
    history=History(snapshot)
    for dt in history.schemas:
        attachment_fields=[f['fieldname'] for f in history.schemas.get(dt,{}).get('fields',[]) if f['fieldtype'] in ('Attach','Attach Image','Text Editor')]
        if not attachment_fields:continue
        for row in history.tables[dt].values():
            target=frappe.db.get_value(historical.ARCHIVE,{'record_id':historical.archive_id(source_id,dt,row['name']),'conversion_status':'Converted'},['target_doctype','target_name'],as_dict=True)
            if not target:continue
            meta=frappe.get_meta(target.target_doctype)
            for field in attachment_fields:
                url=row.get(field)
                if not url or not meta.has_field(field):continue
                source_field=next(f for f in history.schemas[dt]['fields'] if f['fieldname']==field)
                if source_field['fieldtype']=='Text Editor':
                    if dry_run:continue
                    from onerc_vmms_migrate.html_files import rewrite
                    current=frappe.db.get_value(target.target_doctype,target.target_name,field) or ''
                    new_html=rewrite(current,lambda old:restore(old,target.target_doctype,target.target_name))
                    if new_html!=current:
                        frappe.db.set_value(target.target_doctype,target.target_name,field,new_html,update_modified=False)
                        report['native_fields_updated']+=1
                    continue
                if url not in manifest or dry_run:continue
                new_url=restore(url,target.target_doctype,target.target_name)
                current=frappe.db.get_value(target.target_doctype,target.target_name,field)
                if new_url and current in (None,'',url):
                    frappe.db.set_value(target.target_doctype,target.target_name,field,new_url,update_modified=False)
                    report['native_fields_updated']+=1
    # User is deliberately excluded from generic history; avatars are captured
    # through the safe core user allowlist and still need their native field.
    if not dry_run:
        for row in snapshot['users'].values():
            url=row.get('user_image')
            if not url or url not in manifest:continue
            user=historical.converted(snapshot,'User',row['name'])
            if not user:continue
            current=frappe.db.get_value('User',user,'user_image')
            if current not in (None,'',url):continue
            new_url=restore(url,'User',user,private=False)
            if new_url:
                frappe.db.set_value('User',user,'user_image',new_url,update_modified=False)
                frappe.clear_document_cache('User',user)
                report['native_fields_updated']+=1
        frappe.cache.delete_key('user_info')
    for row in history.rows('Deployment Request Tool'):
        url=row.get('tor_attachment')
        if dry_run or not url or url not in manifest:continue
        deployment=historical.converted(snapshot,'Deployment Request Tool',row['name'])
        if not deployment:continue
        terms=frappe.db.get_value('VMMS Deployment',deployment,'terms_of_reference')
        if not terms or not frappe.db.get_value('VMMS Terms of Reference',terms,'legacy_unstructured_terms'):continue
        new_url=restore(url,'VMMS Terms of Reference',terms,True)
        if new_url:
            frappe.db.set_value('VMMS Terms of Reference',terms,'legacy_terms_attachment',new_url,update_modified=False)
            report['native_fields_updated']+=1
    return report
