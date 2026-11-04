"""Preserve public record numbers without silent adoption or renumbering."""


def original_name(names):
    names = [str(name) for name in names if name is not None and str(name)]
    if len(names) != 1:
        raise ValueError('A native identity needs exactly one original source identifier; review shared identities first.')
    return names[0]


def assert_same_name(target, source_names):
    if str(target) != original_name(source_names):
        raise ValueError('The existing destination record has a different original identifier; review adoption without renumbering.')


def create(doctype, profile, source_names, values):
    import frappe
    name = original_name(source_names)
    if frappe.db.exists(doctype, name):
        raise ValueError(f'{doctype} {name} already exists; resolve its identity before import.')
    doc = frappe.get_doc({'doctype': doctype, 'red_profile': profile, **values})
    doc.insert(ignore_permissions=True, set_name=name)
    return doc
