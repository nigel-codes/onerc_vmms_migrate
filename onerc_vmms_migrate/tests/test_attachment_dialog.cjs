// Run with node onerc_vmms_migrate/tests/test_attachment_dialog.cjs.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const buttons = new Map(), requests = [];
let handlers, dialog;
const context = {
 __: value => value, cur_frm: null,
 frappe: {
  csrf_token: 'test',
  ui: {form: {on: (_name, value) => {handlers = value;}},
   Dialog: class {constructor(options) {dialog = options;} show() {} hide() {}}},
  call: async () => ({message: {attachment_batch: {status: 'review', plan_digest: 'reviewed'}, date_repair: {status: 'review', plan_digest: 'dates-reviewed'}}})
 },
 fetch: async (_url, options) => {
  requests.push(JSON.parse(options.body));
  return {ok: true, json: async () => ({message: {}})};
 }
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,
 '../onerc_vmms_migrate/doctype/vmms_migration_settings/vmms_migration_settings.js'), 'utf8'), context);
const frm = {is_dirty: () => false, get_field: () => ({$wrapper: {html() {}}}),
 add_custom_button: (label, action) => buttons.set(label, action)};
(async () => {
 handlers.refresh(frm);
 await buttons.get('Copy reviewed trial files')();
 for (const checked of [1, true, 0, false, undefined, '1', 'false']) {
  await dialog.primary_action({confirmation: 'COPY FILES', batch_size: 100,
   backup_confirmed: checked, review_confirmed: checked, test_destination: checked});
  const request = requests.at(-1);
  for (const field of ['backup_confirmed', 'review_confirmed', 'test_destination']) {
   assert.equal(request[field], checked === 1 || checked === true);
  }
  assert.equal(request.plan_digest, 'reviewed');
 }
 buttons.get('Resume attachment copy')();
 await dialog.primary_action({batch_size: 100, accept_code_changes: 1});
 assert.equal(requests.at(-1).accept_code_changes, true);
 await dialog.primary_action({batch_size: 100, accept_code_changes: 0});
 assert.equal(requests.at(-1).accept_code_changes, false);
 await buttons.get('Repair reviewed dates')();
 await dialog.primary_action({backup_confirmed: 1, confirmation: 'REPAIR DATES'});
 assert.equal(requests.at(-1).backup_confirmed, true);
 assert.equal(requests.at(-1).plan_digest, 'dates-reviewed');
 console.log('PASS: attachment dialog serializes checked numeric controls as strict booleans.');
})().catch(error => {console.error(error); process.exitCode = 1;});
