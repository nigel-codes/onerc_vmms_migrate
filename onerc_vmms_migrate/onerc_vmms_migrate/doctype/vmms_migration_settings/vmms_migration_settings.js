const migration_api = 'onerc_vmms_migrate.browser.';
const mapping_kinds = ['branch', 'residence', 'membership_type', 'volunteer_status', 'gender', 'identification_type', 'supporting_document', 'disability', 'language', 'skill', 'profession', 'education_level', 'driving_licence_class', 'licence_type', 'question'];

frappe.ui.form.on('VMMS Migration Settings', {
 refresh(frm) {
  frm.get_field('instructions').$wrapper.html(`<div class="alert alert-info">
   <strong>Move VMMS data from the old site</strong><br>
   Choose Bulk Export and save the old site's HTTPS URL and dedicated migration credentials. Install VMMS Export on the old site first.
   Capture → prepare references → review mappings → plan → rehearse → import → reconcile.<br>
   Enable Preserve existing passwords and register this target’s login transfer public key on the old site.
   Full Bulk Export includes people, protected logins, deployment/event history and a restricted archive of the original business records. Reports distinguish native conversions from retained history.
   Both sites need hosting support to install apps; the target needs a running background worker.
  </div>`);
  const start = async (action, extra = {}) => {
   if (frm.is_dirty()) { frappe.msgprint(__('Save your settings first.')); return; }
   await migration_json(migration_api + 'start', {action, ...extra});
   migration_status(frm);
  };
  const add = (label, action, group) => frm.add_custom_button(__(label), () => start(action), __(group));
  frm.add_custom_button(__('Login transfer public key'), async () => {
   const {message: key} = await frappe.call({method: migration_api + 'login_transfer_public_key'});
   const dialog = new frappe.ui.Dialog({title: __('Register this key on the old site'), fields: [
    {fieldtype: 'HTML', options: '<p>Ask hosting to register this public key and fingerprint in the source export configuration. Keep the receiver private key and site encryption key on this target.</p>'},
    {fieldname: 'fingerprint', fieldtype: 'Data', label: __('Fingerprint'), read_only: 1, default: key.recipient_fingerprint},
    {fieldname: 'public_key', fieldtype: 'Long Text', label: __('Public key'), read_only: 1, default: key.public_key}
   ]}); dialog.show();
  }, __('1. Source'));
  frm.add_custom_button(__('Capture / resume old site'), () => {
   frappe.confirm(__('This reads records and downloads attachments from the old site. For a final capture, freeze changes on the old site first. Continue?'), () => start('capture'));
  }, __('1. Source'));
  frm.add_custom_button(__('Clear capture'), async () => {
   frappe.confirm(__('Clear the selected capture so you can capture again? Previous private capture files remain on the server. This is blocked after any records are migrated.'), async () => {
    await frappe.call({method: migration_api + 'clear_capture'}); frm.reload_doc();
   });
  }, __('1. Source'));
  add('Audit source login coverage', 'audit_logins', '1. Source');
  add('Check configuration', 'check', '2. Review');
  add('Run self-test', 'selftest', '2. Review');
  add('Profile source', 'profile', '2. Review');
  add('Prepare source lookups and geography', 'prepare', '2. Review');
  add('Generate mappings', 'crosswalks', '2. Review');
  frm.add_custom_button(__('Edit mappings'), () => mapping_dialog(frm), __('2. Review'));
  add('Plan migration', 'plan', '3. Validate');
  add('Rehearse full import', 'rehearsal', '3. Validate');
  frm.add_custom_button(__('Start batch test'), () => {
   const dialog = new frappe.ui.Dialog({title: __('Test migration in resumable batches'), fields: [
    {fieldtype: 'HTML', options: '<p><strong>Use a test destination.</strong> This creates and KEEPS destination accounts, Red Profiles, affiliations and history between batches. It pauses the scheduler and mutes email. It does not copy attachment bytes or authorize live cutover. Restore your backup or rebuild this test site when finished.</p><p>Each job handles up to the selected number of records, saves its checkpoint, then queues the next job. Validation failures pause the sequence for review.</p>'},
    {fieldname: 'test_destination', fieldtype: 'Check', label: __('This is a disposable test destination'), reqd: 1},
    {fieldname: 'backup_confirmed', fieldtype: 'Check', label: __('I have a current database and files backup'), reqd: 1},
    {fieldname: 'batch_size', fieldtype: 'Int', label: __('Records per batch (1–1000)'), default: 1000, reqd: 1},
    {fieldname: 'confirmation', fieldtype: 'Data', label: __('Type TEST to keep test records between batches'), reqd: 1}
   ], primary_action_label: __('Start batch test'), primary_action: async values => {
    await start('batch_test', values); dialog.hide();
   }}); dialog.show();
  }, __('3. Validate'));
  frm.add_custom_button(__('Pause batches'), async () => {
   const data = await migration_json('onerc_vmms_migrate.batch_browser.pause');
   frappe.show_alert({message: data.message, indicator: 'orange'}); migration_status(frm);
  }, __('3. Validate'));
  frm.add_custom_button(__('Resume / retry batches'), () => {
   const dialog = new frappe.ui.Dialog({title: __('Resume saved batch checkpoint'), fields: [
    {fieldtype: 'HTML', options: '<p>Completed batches remain in the destination. The last unfinished batch is retried using the migration ledger to avoid duplicates. Fix the reported errors first. A running job will not be interrupted.</p>'},
    {fieldname: 'batch_size', fieldtype: 'Int', label: __('Records per batch (1–1000)'), default: 1000, reqd: 1},
    {fieldname: 'accept_changes', fieldtype: 'Check', label: __('Test only: I reviewed code/settings/mapping changes since the previous batch'), description: __('A changed capture is always refused. Existing migrated values are never silently overwritten. Live imports must keep their original inputs.')}
   ], primary_action_label: __('Resume'), primary_action: async values => {
    if (frm.is_dirty()) { frappe.msgprint(__('Save your settings first.')); return; }
    await migration_json('onerc_vmms_migrate.batch_browser.resume', values);
    dialog.hide(); migration_status(frm);
   }}); dialog.show();
  }, __('3. Validate'));
  frm.add_custom_button(__('Import captured data'), async () => {
   const {message: state} = await frappe.call({method: migration_api + 'status'});
   const needs_email_test = !!state.rehearsal?.authentication_policy?.mail_delivery_test_required;
   const dialog = new frappe.ui.Dialog({title: __('Import captured data'), fields: [
    {fieldtype: 'HTML', options: '<p>The import creates live records. It pauses scheduled jobs and mutes email on this target until you finish cutover. Review all plan and rehearsal reports first. Recovery requires restoring your target backup.</p>'},
    {fieldname: 'batch_size', fieldtype: 'Int', label: __('Records per batch (1–1000)'), default: 1000, reqd: 1},
    {fieldname: 'backup_confirmed', fieldtype: 'Check', label: __('I have a current target database and files backup'), reqd: 1},
    {fieldname: 'source_frozen', fieldtype: 'Check', label: __('The old site was frozen before the selected capture and remains frozen'), reqd: 1},
    {fieldname: 'exclusions_reviewed', fieldtype: 'Check', label: __('I reviewed excluded records, warnings and login transfer results'), reqd: 1},
    {fieldname: 'email_login_confirmed', fieldtype: 'Check', label: __('I tested destination email-link sign-in delivery'), reqd: needs_email_test, hidden: !needs_email_test},
    {fieldname: 'confirmation', fieldtype: 'Data', label: __('Type LOAD to import'), reqd: 1}
   ], primary_action_label: __('Import'), primary_action: async values => {
    if (values.confirmation !== 'LOAD') { frappe.msgprint(__('Type LOAD exactly.')); return; }
    await start('import', values); dialog.hide();
   }}); dialog.show();
  }, __('4. Cutover'));
  add('Reconcile imported records', 'reconcile', '4. Cutover');
  frm.add_custom_button(__('Finish cutover'), () => frappe.confirm(__('Restore the target scheduler and email settings to their values before migration?'), async () => {
   await frappe.call({method: migration_api + 'finish_cutover'}); migration_status(frm);
  }), __('4. Cutover'));
  frm.add_custom_button(__('Restore services after failure'), () => {
   const recovery = new frappe.ui.Dialog({title: __('Restore target services'), fields: [
    {fieldtype: 'HTML', options: '<p>This restores the original scheduler and email settings even if migration is incomplete. It does not undo imported records. Review the failure reports and decide whether the target can reopen before proceeding.</p>'},
    {fieldname: 'confirmation', fieldtype: 'Data', label: __('Type RESTORE to resume services'), reqd: 1}
   ], primary_action_label: __('Restore services'), primary_action: async ({confirmation}) => {
    if (confirmation !== 'RESTORE') { frappe.msgprint(__('Type RESTORE exactly.')); return; }
    await frappe.call({method: migration_api + 'finish_cutover', args: {confirmation}});
    recovery.hide(); migration_status(frm);
   }}); recovery.show();
  }, __('4. Cutover'));
  frm.add_custom_button(__('Refresh progress'), () => migration_status(frm));
  migration_status(frm);
 }
});

async function migration_status(frm) {
 if (frm._migration_timer) clearTimeout(frm._migration_timer);
 const {message: state} = await frappe.call({method: migration_api + 'status'});
 if (cur_frm !== frm) return;
 const escape = value => frappe.utils.escape_html(String(value ?? ''));
 let html = `<h4>${__('Migration progress')}</h4><p>${escape(state.action || 'No job started')} · ${escape(state.status || 'Ready')}</p>`;
 if (state.busy) html += '<p>Queued or running. You can leave this page and return. If it stays queued, ask hosting support to check the long background worker.</p>';
 const progress = state.progress || state.transfer;
 if (progress && state.busy) html += `<pre>${escape(JSON.stringify(progress, null, 2))}</pre>`;
 if (state.progress_updated_at && state.busy) html += `<p>${__('Last progress update')}: ${escape(state.progress_updated_at)}</p>`;
 if (state.pending_bulk_capture && !state.busy) html += '<p>A bulk transfer can be resumed using Capture / resume old site.</p>';
 if (state.result?.complete === false) html += '<div class="alert alert-warning">The source snapshot has missing attachments. Review the missing-files report before importing.</div>';
 if (state.error) html += `<div class="alert alert-danger">${escape(state.error)}</div>`;
 if (state.batch) {
  const batch = state.batch;
  html += `<div class="alert alert-info"><strong>${batch.mode === 'test' ? __('Persistent batch test') : __('Batched import')}</strong><br>${escape(batch.phase)} · ${escape(batch.status)} · ${__('Committed batches')}: ${escape(batch.batch_number)}<br>${__('Completed in this phase')}: ${escape(batch.offset)}${batch.total != null ? ' / ' + escape(batch.total) : ''}<br>${__('Maximum records per job')}: ${escape(batch.batch_size)}</div>`;
  if (batch.status === 'blocked') html += '<p>Automatic batches stopped. Review the latest batch failure report, fix the cause, then use Resume / retry batches. Earlier committed batches remain intact.</p>';
  if (batch.mode === 'test' && batch.status === 'finished') html += '<p>Batch testing finished. Its records remain on this test site. This result does not authorize a live import.</p>';
 }
 if (state.cutover) html += '<div class="alert alert-warning">Cutover is active: scheduled jobs are paused and email is muted. Reconcile, then use Finish cutover to restore the original settings.</div>';
 if (state.result) html += `<pre style="max-height:360px;overflow:auto">${escape(JSON.stringify(state.result, null, 2))}</pre>`;
 if (state.rehearsal) html += `<p>Last full rehearsal: ${state.rehearsal.passed ? 'passed' : 'needs attention'}. Changes to the capture, mappings or settings require another rehearsal.</p>`;
 if (state.reports?.length) {
  html += '<h5>Reports</h5><ul>';
  for (const report of state.reports) {
   html += `<li>${escape(report.run)}: `;
   html += report.files.map(file => {
    const params = new URLSearchParams({run_id: report.run, filename: file});
    return `<a target="_blank" rel="noopener" href="/api/method/${migration_api}download_report?${escape(params.toString())}">${escape(file)}</a>`;
   }).join(' · ');
   html += '</li>';
  }
  html += '</ul>';
 }
 frm.get_field('migration_dashboard').$wrapper.html(html);
 if (state.busy || state.batch?.status === 'ready') frm._migration_timer = setTimeout(() => {
  if (cur_frm === frm) migration_status(frm);
 }, 4000);
 else if (state.action === 'capture' && state.status === 'finished' && state.result?.capture_id !== frm.doc.browser_capture) frm.reload_doc();
}

function mapping_dialog(frm) {
 const chooser = new frappe.ui.Dialog({title: __('Choose mapping type'), fields: [
  {fieldname: 'kind', fieldtype: 'Select', options: mapping_kinds, label: __('Mapping type'), reqd: 1}
 ], primary_action_label: __('Open mappings'), primary_action: async ({kind}) => {
  const {message: data} = await frappe.call({method: migration_api + 'mappings', args: {kind}});
  chooser.hide();
  if (!data.rows.length) { frappe.msgprint(__('Generate mappings first. This type may have no source values.')); return; }
  const dialog = new frappe.ui.Dialog({title: __('Review {0} mappings', [kind]), size: 'extra-large', fields: [
   {fieldtype: 'HTML', options: `<p>${frappe.utils.escape_html(data.about)}</p><p>Blank means unreviewed. Use - to deliberately omit a value. The * row supplies the default for values with a blank target. Targets must be existing record names.</p>`},
   {fieldname: 'rows', fieldtype: 'Table', cannot_add_rows: true, cannot_delete_rows: true, in_place_edit: true,
    data: data.rows, fields: [
     {fieldname: 'source_value', fieldtype: 'Data', label: __('Old value'), read_only: 1, in_list_view: 1, columns: 3},
     {fieldname: 'occurrences', fieldtype: 'Data', label: __('Count'), read_only: 1, in_list_view: 1, columns: 1},
     {fieldname: 'target', fieldtype: 'Data', label: __('Target record name'), in_list_view: 1, columns: 3},
     {fieldname: 'target_label', fieldtype: 'Data', label: __('Target label'), read_only: 1},
     {fieldname: 'note', fieldtype: 'Data', label: __('Note'), in_list_view: 1, columns: 3}
    ]},
   {fieldname: 'pick', fieldtype: 'Button', label: __('Choose a target'), click: () => {
    const fields = [{fieldname: 'source', fieldtype: 'Select', label: __('Old value'), options: data.rows.map(r => r.source_value), reqd: 1}];
    if (data.doctype) fields.push({fieldname: 'target', fieldtype: 'Link', label: __('Existing target'), options: data.doctype});
    fields.push({fieldname: 'special', fieldtype: 'Select', label: __('Or choose an action/status'), options: ['', ...data.choices]});
    const pick = new frappe.ui.Dialog({title: __('Choose a target'), fields, primary_action_label: __('Apply'), primary_action: values => {
     const row = dialog.fields_dict.rows.df.data.find(r => r.source_value === values.source);
     row.target = values.special || values.target || '';
     dialog.fields_dict.rows.grid.refresh(); pick.hide();
    }}); pick.show();
   }}
  ], primary_action_label: __('Save mappings'), primary_action: async () => {
   await migration_json(migration_api + 'save_mappings', {kind, rows: dialog.fields_dict.rows.df.data});
   dialog.hide(); frappe.show_alert({message: __('Mappings saved. Run the plan and rehearsal again.'), indicator: 'green'});
  }}); dialog.show();
 }}); chooser.show();
}


async function migration_json(method, args = {}) {
 const response = await fetch('/api/method/' + method, {
  method: 'POST', credentials: 'same-origin',
  headers: {'Content-Type': 'application/json', 'X-Frappe-CSRF-Token': frappe.csrf_token},
  body: JSON.stringify(args)
 });
 const data = await response.json();
 if (!response.ok || data.exc || data.exc_type) {
  let messages = [];
  try { messages = JSON.parse(data._server_messages || '[]').map(value => typeof value === 'string' ? JSON.parse(value).message : value.message); } catch {}
  const message = messages.join(' | ').replace(/<[^>]*>/g, '') || data.exc_type || 'HTTP ' + response.status;
  frappe.msgprint({title: __('Migration stopped'), message: frappe.utils.escape_html(message), indicator: 'red'});
  throw new Error(message);
 }
 return data.message;
}
