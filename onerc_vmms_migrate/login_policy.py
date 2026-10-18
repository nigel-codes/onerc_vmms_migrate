"""Preserve recorded email-link login without disabling destination MFA."""
import frappe


def decision(source,destination,restore=True):
    if 'login_with_email_link' not in source:
        raise ValueError('Capture the source again with the updated exporter so its login methods are recorded.')
    if source.get('enable_two_factor_auth'):
        raise ValueError('The source uses two-factor authentication. Its enrollment secrets are not part of this migration; review authentication continuity before cutover.')
    wanted=bool(source.get('login_with_email_link'))
    present=bool(destination.get('login_with_email_link'))
    enable=wanted and not present
    if enable and not restore:
        raise ValueError('The source uses email-link login. Preserve that method to avoid password resets for accounts without a local password.')
    if enable and destination.get('enable_two_factor_auth'):
        raise ValueError('Destination MFA is enabled. Review email-link login explicitly; migration will not bypass its existing authentication policy.')
    def positive(value,default):
        try:return max(1,int(value or default))
        except (ValueError,TypeError):return default
    return {'source_email_link_login':wanted,'destination_email_link_login':present,'enable_after_import':enable,
        'login_with_email_link_expiry':min(positive(source.get('login_with_email_link_expiry'),10),positive(destination.get('login_with_email_link_expiry'),10)),
        'rate_limit_email_link_login':min(positive(source.get('rate_limit_email_link_login'),5),positive(destination.get('rate_limit_email_link_login'),5)),
        'mail_delivery_test_required':wanted}


def plan(snapshot,conf):
    settings=frappe.get_single('System Settings')
    try:
        return decision(snapshot.get('source',{}).get('authentication_policy') or {},settings.as_dict(),
            bool(conf.get('restore_email_link_login',True)))
    except ValueError as exc:
        frappe.throw(str(exc))


def apply(planned):
    if not planned['enable_after_import']:return
    settings=frappe.get_single('System Settings')
    if settings.enable_two_factor_auth and not settings.login_with_email_link:
        frappe.throw('Destination MFA changed during migration; email-link sign-in was not enabled.')
    frappe.db.after_rollback.add(frappe.clear_cache)
    settings.login_with_email_link=1
    for field in ('login_with_email_link_expiry','rate_limit_email_link_login'):
        if settings.meta.has_field(field):settings.set(field,planned[field])
    settings.save(ignore_permissions=True)
    frappe.db.after_commit.add(frappe.clear_cache)
