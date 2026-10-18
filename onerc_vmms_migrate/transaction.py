"""Keep large rehearsals atomic; never let Frappe auto-commit part of one."""
from functools import wraps
import frappe


def large_transaction(function):
    @wraps(function)
    def wrapped(*args,**kwargs):
        database=frappe.db
        previous_limit=database.MAX_WRITES_PER_TRANSACTION
        previous_config=frappe.conf.get('max_writes_per_transaction')
        previous_auto=database.auto_commit_on_many_writes
        database.MAX_WRITES_PER_TRANSACTION=5_000_000
        frappe.conf.max_writes_per_transaction=5_000_000
        database.auto_commit_on_many_writes=0
        try:return function(*args,**kwargs)
        finally:
            database.MAX_WRITES_PER_TRANSACTION=previous_limit
            database.auto_commit_on_many_writes=previous_auto
            if previous_config is None:frappe.conf.pop('max_writes_per_transaction',None)
            else:frappe.conf.max_writes_per_transaction=previous_config
    return wrapped
