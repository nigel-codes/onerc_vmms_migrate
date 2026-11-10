/* VMMS destination lookup preparation. Paste this whole file into the NEW site's Desk console.
 * Loading runs a READ-ONLY preview. To create missing lookup records:
 *   await vmmsLookupPrep.apply()
 * Download the result with vmmsLookupPrep.downloadReport(). Keep this tab open during apply.
 * Native permissions and validation apply. No existing records or mapping files are updated.
 * After apply finishes, use Generate mappings, then Plan migration in Migration Settings.
 * Scope: profile vocabularies only. Geography, fees, people, roles and payments are separate.
 */
(async () => {
  'use strict';
  const ORIGIN = 'https://kenyaredcross.jh.frappe.cloud';
  const CAPTURE = 'b09e06d9822849e8a1a70d550e882cb0';
  const API = 'onerc_vmms_migrate.browser.';
  const SPECS = [
    ['skill', 'VMMS Skill', ['skill_name'], 'skill_key'],
    ['education_level', 'VMMS Education Level', ['level_name'], 'level_key'],
    ['driving_licence_class', 'VMMS Driving Licence Class', ['class_name'], 'class_key'],
    ['identification_type', 'Identification Type', ['identification_type_name'], 'identification_type_key'],
    ['profession', 'Profession', ['profession', 'name']],
    ['licence_type', 'Personnel License Type', ['license_type', 'name']],
    ['disability', 'Disability', ['disability_name', 'disability']],
    ['language', 'Language', ['language_name'], 'language_code'],
    ['gender', 'Gender', ['gender']]
  ];
  const PLACEHOLDERS = new Set(['none', 'nil', 'null', 'n/a', 'na', 'no', 'unknown',
    'not applicable', 'not available', '-', '--', '.', '0']);
  const text = value => String(value ?? '').replace(/\s+/gu, ' ').trim();
  const key = value => text(value).toLowerCase();
  let running = false, stopped = false, report;

  function localGuard() {
    if (location.origin !== ORIGIN) throw new Error('Run this only on ' + ORIGIN);
    if (!window.frappe?.csrf_token || !frappe.session?.user || frappe.session.user === 'Guest')
      throw new Error('Open the destination Desk while signed in.');
    if (frappe.session.user !== 'Administrator' && !frappe.user_roles?.includes('System Manager'))
      throw new Error('A System Manager account is required.');
    if (stopped) throw new Error('Stopped by operator; completed lookup inserts remain intact.');
  }
  localGuard();
  if (window.vmmsLookupPrep?.isRunning()) throw new Error('The existing lookup preparation is still running.');

  async function request(method, args = {}) {
    localGuard();
    const response = await fetch('/api/method/' + method, {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-Frappe-CSRF-Token': frappe.csrf_token},
      body: JSON.stringify(args)
    });
    let data;
    try { data = await response.json(); } catch { throw new Error('Unexpected response from ' + method); }
    if (!response.ok || data.exc || data.exception) {
      const error = new Error((data.exc_type || 'Request failed') + ' at ' + method + ' (HTTP ' + response.status + ')');
      error.serverMessages = data._server_messages; throw error;
    }
    return data;
  }
  async function call(method, args) { return (await request(method, args)).message; }
  async function idleGuard() {
    const state = await call(API + 'status');
    if (state.busy || [state.batch, state.attachment_batch, state.date_repair].some(v => v?.status === 'ready'))
      throw new Error('Wait until migration, attachment and date-repair jobs are idle.');
    const settings = await call('frappe.client.get_value', {doctype: 'VMMS Migration Settings', fieldname: ['browser_capture']});
    if (settings?.browser_capture !== CAPTURE) throw new Error('The selected capture changed; review it before using this script.');
  }
  async function list(doctype, fields, filters, start = 0, size = 500) {
    return await call('frappe.client.get_list', {doctype, fields, filters, order_by: 'name asc', limit_start: start, limit_page_length: size});
  }
  async function allRows(doctype, fields) {
    const rows = [];
    for (let start = 0; ; start += 500) {
      const page = await list(doctype, fields, undefined, start);
      if (!Array.isArray(page)) throw new Error('Invalid lookup response: ' + doctype);
      rows.push(...page);
      if (page.length < 500) return rows;
      if (start >= 100000) throw new Error('Unexpected lookup size: ' + doctype);
    }
  }
  async function documentFor(spec, meta, label, value) {
    const [, doctype, , identity] = spec;
    const doc = {doctype, [label]: value};
    if (identity) {
      const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
      const hash = [...new Uint8Array(bytes)].map(n => n.toString(16).padStart(2, '0')).join('').slice(0, 20);
      doc[identity] = doctype === 'Language' ? 'x-vmms' + hash.replace(/[0-9]/g, n => 'ghijklmnop'[Number(n)]) : 'legacy_' + hash;
    }
    if (doctype === 'Language') doc.enabled = 0; // spoken-language entry, not a new UI locale
    return doc;
  }
  function invalidReason(meta, label, doc) {
    const field = meta.fields.find(f => f.fieldname === label);
    if (String(doc[label]).length > Number(field?.length || 140)) return 'Label exceeds destination length; retained for manual review.';
    if (/<\/?[a-z][^>]*>/iu.test(doc[label])) return 'HTML-like source text requires manual review.';
    const missing = meta.fields.filter(f => f.reqd && !text(f.default) && (doc[f.fieldname] == null || doc[f.fieldname] === ''));
    return missing.length ? 'Additional required fields: ' + missing.map(f => f.fieldname).join(', ') : '';
  }
  function show() {
    console.table(report.summary);
    console.info('Held items:', report.held.length, 'Failures:', report.failures.length,
      'Created:', report.created.length);
    console.info('Full details: vmmsLookupPrep.report. Download: vmmsLookupPrep.downloadReport()');
  }
  async function buildPlan() {
    await idleGuard();
    report = {site: ORIGIN, capture_id: CAPTURE, status: 'preview', started_utc: new Date().toISOString(),
      summary: [], held: [], failures: [], created: [], reused: [],
      separate_scope: ['geography/branch/residence', 'membership types and pricing', 'people and memberships', 'roles and permissions', 'payments']};
    const groups = [];
    for (const spec of SPECS) {
      const [kind, doctype, labels, identity] = spec;
      const mapping = await call(API + 'mappings', {kind});
      if (mapping.doctype !== doctype || !Array.isArray(mapping.rows)) throw new Error('Unexpected mapping schema: ' + kind);
      const summary = {kind, source_values: 0, mapped: 0, create: 0, reuse: 0, omitted: 0, held: 0, blocked: false};
      report.summary.push(summary);
      if (!mapping.rows.length) continue;
      const bundle = await request('frappe.desk.form.load.getdoctype', {doctype, with_parent: 1});
      const meta = bundle.docs?.find(d => d.name === doctype && d.doctype === 'DocType');
      if (!meta || !Array.isArray(meta.fields) || meta.istable || meta.issingle) throw new Error('Unexpected native DocType: ' + doctype);
      const label = labels.find(f => f === 'name' || meta.fields.some(v => v.fieldname === f));
      if (!label || (identity && !meta.fields.some(f => f.fieldname === identity))) throw new Error('Missing native lookup fields: ' + doctype);
      const fields = [...new Set(['name', label, identity].filter(Boolean))];
      const existing = await allRows(doctype, fields);
      const byName = new Map(existing.map(row => [key(row.name), row]));
      const byLabel = new Map();
      for (const row of existing) {
        const k = key(row[label]); if (!byLabel.has(k)) byLabel.set(k, []); byLabel.get(k).push(row);
      }
      const permission = await call('frappe.client.has_permission', {doctype, docname: '', perm_type: 'create'});
      const defaultTarget = text(mapping.rows.find(r => r.source_value === '*')?.target);
      const group = {kind, doctype, label, fields, actions: [], invalidMapping: false};
      groups.push(group);
      const hold = (value, reason) => { summary.held++; report.held.push({kind, source_value: value, reason}); };
      for (const row of mapping.rows) {
        if (row.source_value === '*') continue;
        summary.source_values++;
        const value = text(row.source_value), target = text(row.target) || defaultTarget;
        if (target === '-' || PLACEHOLDERS.has(key(value)) || !value) {summary.omitted++; continue;}
        if (target) {
          if (byName.has(key(target))) summary.mapped++;
          else {group.invalidMapping = true; summary.blocked = true; hold(value, 'Existing mapping target is absent: ' + target);}
          continue;
        }
        const matches = byLabel.get(key(value)) || [];
        if (matches.length > 1) { hold(value, 'Multiple destination records have this label.'); continue; }
        if (matches.length === 1) {
          group.actions.push({source: row.source_value, value, target: matches[0].name}); summary.reuse++; continue;
        }
        const doc = await documentFor(spec, meta, label, value);
        const identityValue = String(doc.name || doc[identity] || doc[label]);
        const reason = invalidReason(meta, label, doc);
        if (reason) { hold(value, reason); continue; }
        if (!permission?.has_permission) { hold(value, 'Native create permission is unavailable.'); continue; }
        if (byName.has(key(identityValue))) { hold(value, 'Proposed identifier already belongs to another label.'); continue; }
        group.actions.push({source: row.source_value, value, doc}); summary.create++;
        const planned = {name: identityValue, [label]: value};
        byName.set(key(identityValue), planned); byLabel.set(key(value), [planned]);
      }
    }
    if (!report.summary.some(row => row.source_values))
      throw new Error('Generate mappings for the selected capture before preparing its lookups.');
    show(); return groups;
  }
  async function currentMatch(group, value) {
    const rows = await list(group.doctype, group.fields, {[group.label]: value}, 0, 2);
    if (rows.length > 1) throw new Error('Destination label became ambiguous: ' + group.doctype);
    if (rows.length && key(rows[0][group.label]) !== key(value)) throw new Error('Destination collation matches a different label; review it manually.');
    return rows[0];
  }
  async function run(fn) {
    if (running) throw new Error('A lookup operation is already running.');
    running = true; stopped = false;
    try { return await fn(); }
    catch (error) {
      if (report) {report.status = stopped ? 'stopped' : 'failed'; report.failures.push({message: error.message, server_messages: error.serverMessages}); show();}
      throw error;
    } finally {running = false;}
  }
  window.vmmsLookupPrep = Object.freeze({
    get report() { return report; },
    isRunning: () => running,
    stop: () => {stopped = true;},
    preview: () => run(async () => {await buildPlan(); return report;}),
    apply: () => run(async () => {
      const groups = await buildPlan(); // refresh destination state before any insert
      report.status = 'applying';
      for (const group of groups) {
        if (group.invalidMapping) continue; // resolve existing invalid mappings before preparing this kind
        let processed = 0;
        for (const action of group.actions) {
          if (processed++ % 25 === 0) await idleGuard();
          localGuard();
          let found = await currentMatch(group, action.value);
          if (found) report.reused.push({kind: group.kind, source_value: action.source, target: found.name});
          else if (action.doc) {
            try {
              found = await call('frappe.client.insert', {doc: action.doc});
              if (!found?.name || key(found[group.label]) !== key(action.value)) throw new Error('Native insert returned an unexpected lookup record.');
              report.created.push({kind: group.kind, source_value: action.source, target: found.name});
            } catch (error) {
              // A POST can commit before its response is lost; verify before any retry.
              found = await currentMatch(group, action.value);
              if (!found) throw error;
              report.reused.push({kind: group.kind, source_value: action.source, target: found.name, recovered_after_error: true});
            }
          } else throw new Error('An existing/planned lookup disappeared; preview again.');
          if ((report.created.length + report.reused.length) % 25 === 0)
            console.info('Lookups processed:', report.created.length + report.reused.length);
          await new Promise(resolve => setTimeout(resolve, 100));
        }
        await idleGuard();
      }
      report.status = report.held.length ? 'applied_with_holds' : 'applied'; report.finished_utc = new Date().toISOString(); show();
      console.info('Lookup preparation finished. Review held items, then Generate mappings and Plan migration.');
      return report;
    }),
    downloadReport: () => {
      if (!report) throw new Error('Run a preview first.');
      const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], {type: 'application/json'}));
      const link = document.createElement('a'); link.href = url; link.download = 'vmms-lookup-preparation.json';
      link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  });
  console.info('VMMS lookup preparation loaded. Preview is read-only. Apply with: await vmmsLookupPrep.apply()');
  await window.vmmsLookupPrep.preview();
})();
