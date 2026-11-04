"""Review-only naming continuation. This module never updates a rule/counter."""
import re

MAPPINGS = {'Employee': ('VMMS Volunteer', 'employees'),
            'VM Member': ('VMMS Member', 'members'),
            'VM Membership': ('VMMS Membership', 'memberships')}


def suffixes(names, prefix):
    result = {}
    for name in names:
        name = str(name)
        match = re.fullmatch(r'(.*?)(\d+)', name)
        if match and name.startswith(prefix):
            key, number = match.groups()
            result[key] = max(result.get(key, 0), int(number))
    return result


def build(snapshot, target_names=None, target_series=None, target_rules=None):
    naming = snapshot.get('source', {}).get('continuity', {}).get('naming')
    if not naming:
        raise ValueError('A fresh continuity capture with source naming rules and counters is required.')
    target_names = target_names or {}
    series = {str(r['name']): int(r['current']) for r in naming.get('series', [])}
    for row in target_series or []:
        series[str(row['name'])] = max(series.get(str(row['name']), 0), int(row['current']))
    rules, patterns, retained_series = [], [], {}
    for old, (new, dataset) in MAPPINGS.items():
        records = list(snapshot.get(dataset, {}).values())
        active = [r for r in naming.get('rules', []) if r.get('document_type') == old
                  and str(r.get('disabled') or 0).lower() in ('0', 'false')]
        if len(active) > 1 or any(r.get('conditions') for r in active):
            raise ValueError('Multiple or conditional source naming rules need explicit review.')
        if active:
            source = active[0]
            prefix = source['prefix'].split('.')[0]
            existing = suffixes([r['name'] for r in records] + list(target_names.get(new, [])), prefix)
            counter = max([int(source['counter']), *existing.values()])
            for row in target_rules or []:
                if row.get('document_type') == new and row.get('prefix') == source['prefix']:
                    counter = max(counter, int(row['counter']))
            rules.append({'document_type': new, 'prefix': source['prefix'],
                          'prefix_digits': int(source['prefix_digits']), 'counter': counter,
                          'counter_policy': 'Preserve source rule behavior; no inferred annual reset.'})
            continue
        field = next((dict(f) for f in naming.get('fields', []) if f.get('parent') == old), {})
        for prop in naming.get('properties', []):
            if prop.get('doc_type') == old and prop.get('field_name') == 'naming_series':
                field[prop['property']] = prop['value']
        pattern = field.get('default') or (field.get('options') or '').split('\n')[0]
        if not pattern:
            raise ValueError(f'Missing source naming-series pattern for {old}; it was not guessed.')
        prefix = pattern.split('.')[0]
        counters = suffixes([r['name'] for r in records] + list(target_names.get(new, [])), prefix)
        for key, value in series.items():
            if key.startswith(prefix):
                counters[key] = max(counters.get(key, 0), value)
        retained_series.update(counters)
        patterns.append({'document_type': new, 'naming_series': pattern})
    return {'applied': False, 'requires_review': True, 'rules': rules, 'patterns': patterns,
            'series': [{'name': key, 'current': value} for key, value in sorted(retained_series.items())],
            'existing_destination_rules': target_rules or [],
            'note': 'Import exact names first; review and synchronize counters on a disposable destination before enabling registration.'}


def preview():
    import frappe
    from onerc_vmms_migrate.extract import extract
    from onerc_vmms_migrate.source import Source
    frappe.only_for('System Manager')
    with Source() as source:
        snapshot = extract(source)
        names = {new: frappe.get_all(new, pluck='name') for new, _ in MAPPINGS.values()}
        counters = frappe.db.sql('SELECT name,current FROM `tabSeries`', as_dict=True)
        rules = frappe.get_all('Document Naming Rule', fields=['document_type', 'prefix', 'counter', 'prefix_digits'])
        return build(snapshot, names, counters, rules)
