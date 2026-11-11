/* VMMS mapping repair for the reviewed 10 November 2026 report.
 * Paste into the NEW site's Desk browser console. Loading previews only.
 * Apply: await vmmsMappingFix.apply(); export: vmmsMappingFix.downloadReport()
 * Updates mapping files only, using existing destination records. Keep other
 * migration jobs and mapping editors idle. No records or product code change.
 */
(async () => {
  'use strict';
  const ORIGIN = 'https://kenyaredcross.jh.frappe.cloud';
  const CAPTURE = 'b09e06d9822849e8a1a70d550e882cb0';
  const API = 'onerc_vmms_migrate.browser.';
  const EXPECTED = {
  "branch": [
    "Tana River",
    "Kitui",
    "Kiambu",
    "Garissa",
    "Mombasa",
    "Kwale",
    "Embu",
    "Baringo",
    "Kilifi",
    "Lamu",
    "Busia",
    "Marsabit",
    "Isiolo",
    "krcs branch test",
    "Machakos",
    "Migori",
    "Homa Bay",
    "Nakuru",
    "Makueni",
    "Vihiga",
    "Murang’a",
    "Kericho",
    "Siaya",
    "Nyamira",
    "Bomet",
    "West Pokot",
    "Kirinyaga",
    "KRCS - HQ"
  ],
  "residence": [
    "sub_county:Mt Elgon",
    "sub_county:Lang'ata",
    "sub_county:ISIOLO",
    "county:Isiolo",
    "sub_county:MACHAKOS",
    "county:Machakos",
    "sub_county:Makueni",
    "county:Makueni",
    "sub_county:EMBU",
    "county:Embu",
    "sub_county:KILIFI",
    "county:Kilifi",
    "county:Kitui",
    "sub_county:IMENTI NORTH",
    "sub_county:Kiambu",
    "county:Kiambu",
    "sub_county:TANA RIVER",
    "county:Tana River",
    "sub_county:WEST POKOT",
    "county:West Pokot",
    "sub_county:HOMABAY",
    "county:Homa Bay",
    "sub_county:IMENTI SOUTH",
    "county:Bomet",
    "sub_county:Olkalou",
    "sub_county:Maragua",
    "sub_county:NAKURU",
    "county:Nakuru",
    "sub_county:Imenti Central",
    "sub_county:MOMBASA",
    "county:Mombasa",
    "sub_county:KITUI",
    "sub_county:NYAMIRA",
    "county:Nyamira",
    "county:Garissa",
    "sub_county:Vihiga",
    "county:Vihiga",
    "sub_county:SIAYA",
    "county:Siaya",
    "sub_county:BOMET",
    "sub_county:MURANGA SOUTH",
    "sub_county:Homa Bay",
    "sub_county:BUSIA",
    "county:Busia",
    "sub_county:MURANGA NORTH"
  ],
  "identification_type": [
    "Passport"
  ],
  "disability": [
    "Low Vision"
  ],
  "skill": [
    "Driving",
    "Translation",
    "IT",
    "Counselling"
  ],
  "education_level": [
    "Undergraduate",
    "Others",
    "Graduate",
    "Postgraduate",
    "Primary"
  ]
};
  const SPECS = {
    branch: ['Geo Node', 'geo_node_name'], residence: ['Geo Node', 'geo_node_name'],
    identification_type: ['Identification Type', 'identification_type_name'],
    disability: ['Disability', 'disability_name'], skill: ['VMMS Skill', 'skill_name'],
    education_level: ['VMMS Education Level', 'level_name']
  };
  const text = v => String(v ?? '').replace(/\s+/gu, ' ').trim();
  const key = v => text(v).normalize('NFC').toLowerCase();
  // Geographic spelling only: punctuation/spacing, never a historical boundary alias.
  const geoKey = v => key(v).replace(/[’'\s-]/gu, '');
  const level = v => key(v).replace(/[\s_-]/gu, '');
  let running = false, stopped = false, report;

  function guard() {
    if (location.origin !== ORIGIN) throw new Error('Run this only on ' + ORIGIN);
    if (!window.frappe?.csrf_token || !frappe.session?.user || frappe.session.user === 'Guest')
      throw new Error('Sign in to the destination Desk.');
    if (frappe.session.user !== 'Administrator' && !frappe.user_roles?.includes('System Manager'))
      throw new Error('A System Manager account is required.');
    if (stopped) throw new Error('Stopped; completed mapping saves remain intact.');
  }
  guard();
  if (window.vmmsMappingFix?.isRunning()) throw new Error('A mapping repair is already running.');

  async function call(method, args = {}) {
    guard();
    const response = await fetch('/api/method/' + method, {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type':'application/json', 'X-Frappe-CSRF-Token':frappe.csrf_token},
      body: JSON.stringify(args)
    });
    const data = await response.json();
    if (!response.ok || data.exc || data.exception) {
      const error = new Error((data.exc_type || 'Request failed') + ' at ' + method + ' (HTTP ' + response.status + ')');
      error.serverMessages = data._server_messages;
      throw error;
    }
    return data.message;
  }
  async function idle() {
    const state = await call(API + 'status');
    if (state.busy || [state.batch,state.attachment_batch,state.date_repair].some(v => v?.status === 'ready'))
      throw new Error('Wait until migration, attachment and date-repair jobs are idle.');
    const settings = await call('frappe.client.get_value', {doctype:'VMMS Migration Settings',fieldname:['browser_capture']});
    if (settings?.browser_capture !== CAPTURE) throw new Error('The selected capture changed.');
  }
  async function allRows(doctype, fields) {
    const result = [];
    for (let start = 0; start <= 100000; start += 500) {
      const rows = await call('frappe.client.get_list', {doctype,fields,order_by:'name asc',limit_start:start,limit_page_length:500});
      if (!Array.isArray(rows)) throw new Error('Invalid lookup response: ' + doctype);
      result.push(...rows);
      if (rows.length < 500) return result;
    }
    throw new Error('Unexpected lookup size: ' + doctype);
  }
  async function mapping(kind) {
    const m = await call(API + 'mappings', {kind});
    if (m.doctype !== SPECS[kind][0] || !Array.isArray(m.rows)) throw new Error('Unexpected mapping schema: ' + kind);
    const seen = new Set();
    for (const row of m.rows) {
      if (!text(row.source_value) || seen.has(key(row.source_value))) throw new Error('Invalid mapping sources: ' + kind);
      seen.add(key(row.source_value));
    }
    return m;
  }
  function ancestry(node, byName) {
    const path = [], seen = new Set();
    let countySeen = false;
    for (let current = node; current; current = byName.get(current.parent_geo_node)) {
      if (seen.has(current.name) || path.length >= 20) return {error:'Geographic ancestry is cyclic or too deep.'};
      seen.add(current.name); path.push(current.geo_node_name);
      countySeen ||= level(current.geo_level) === 'county';
      if (current.parent_geo_node && !byName.has(current.parent_geo_node)) return {error:'A geographic parent is absent.'};
      if (!current.parent_geo_node && level(current.geo_level) !== 'national') return {error:'Geography does not lead to a national node.'};
    }
    if (level(node.geo_level) === 'subcounty' && !countySeen) return {error:'Subcounty has no county ancestor.'};
    return {path:path.reverse().join(' > ')};
  }
  function resolve(kind, source, rows) {
    let matches, evidence;
    if (kind === 'branch' || kind === 'residence') {
      let wantedLevel = 'county', label = source;
      if (kind === 'branch' && ['krcs - hq','krcs branch test'].includes(key(source))) {
        label = 'Nairobi'; evidence = 'Explicitly approved Nairobi county assignment.';
      } else if (kind === 'residence') {
        const colon = source.indexOf(':');
        wantedLevel = level(source.slice(0,colon)); label = source.slice(colon+1);
        if (colon < 0 || !['county','subcounty'].includes(wantedLevel)) return {error:'Outside the approved county/subcounty scope.'};
      }
      matches = rows.filter(r => level(r.geo_level) === wantedLevel && geoKey(r.geo_node_name) === geoKey(label));
      evidence ||= 'Same geographic name at the recorded ' + wantedLevel + ' level.';
    } else {
      // Labels identify meaning; a different record whose ID resembles the source
      // is not evidence of a match. Multiple matching labels still require review.
      matches = rows.filter(r => key(r[SPECS[kind][1]]) === key(source));
      evidence = 'Unique existing destination label.';
    }
    if (matches.length !== 1) return {error:matches.length ? 'Multiple destination labels match at this level.' : 'No matching existing destination label at this level.'};
    const match = matches[0];
    const chain = match.geo_level ? ancestry(match,new Map(rows.map(r => [r.name,r]))) : {};
    if (chain.error) return chain;
    return {target:match.name,target_label:match[SPECS[kind][1]],geo_level:match.geo_level || '',path:chain.path || '',evidence};
  }
  function show() {
    console.table(report.summary);
    console.info('Proposed mappings:',report.proposed.length,'Held:',report.held.length,'Saved:',report.saved.length);
    console.info('Details: vmmsMappingFix.report; export: vmmsMappingFix.downloadReport()');
  }
  async function build() {
    await idle();
    report = {site:ORIGIN,capture_id:CAPTURE,status:'preview',started_utc:new Date().toISOString(),
      summary:[],proposed:[],held:[],saved:[],failures:[]};
    const plans = [], records = new Map();
    for (const [kind,values] of Object.entries(EXPECTED)) {
      const m = await mapping(kind), [doctype,label] = SPECS[kind];
      const summary = {kind,report_values:values.length,already_mapped:0,proposed:0,held:0,absent:0};
      report.summary.push(summary);
      if (!records.has(doctype)) records.set(doctype,await allRows(doctype,
        doctype === 'Geo Node' ? ['name',label,'geo_level','parent_geo_node'] : ['name',label]));
      const rows = records.get(doctype), actions = [];
      const bySource = new Map(m.rows.map(r => [key(r.source_value),r]));
      const defaultTarget = text(bySource.get('*')?.target);
      for (const source of values) {
        const row = bySource.get(key(source));
        if (!row) {summary.absent++; continue;}
        if (text(row.target) || defaultTarget) {summary.already_mapped++; continue;}
        const result = resolve(kind,text(row.source_value),rows);
        if (result.error) {summary.held++;report.held.push({kind,source_value:row.source_value,reason:result.error});continue;}
        const action = {kind,source_value:row.source_value,...result};
        actions.push(action);report.proposed.push(action);summary.proposed++;
      }
      plans.push({kind,actions});
    }
    if (!report.summary.some(r => r.report_values > r.absent)) throw new Error('The reviewed source values are absent. Select the correct capture and generated mappings.');
    show();return plans;
  }
  async function run(fn) {
    if (running) throw new Error('A mapping operation is already running.');
    running = true;stopped = false;
    try {return await fn();}
    catch(error) {
      if (report) {report.status=stopped?'stopped':'failed';report.failures.push({message:error.message,server_messages:error.serverMessages});show();}
      throw error;
    } finally {running=false;}
  }
  window.vmmsMappingFix = Object.freeze({
    get report() {return report;},
    isRunning:() => running, stop:() => {stopped=true;},
    preview:() => run(async () => {await build();return report;}),
    apply:() => run(async () => {
      const plans = await build();report.status='applying';
      for (const {kind,actions} of plans) {
        if (!actions.length) continue;
        await idle();
        const fresh = await mapping(kind);
        if (text(fresh.rows.find(r => key(r.source_value)==='*')?.target)) continue;
        const latest = new Map(fresh.rows.map(r => [key(r.source_value),r]));
        const changes = [];
        for (const action of actions) {
          const row = latest.get(key(action.source_value));
          if (!row || text(row.target)) continue; // preserve intervening human choices
          row.target=action.target;
          row.note=[text(row.note),'Reviewed mapping: '+action.evidence].filter(Boolean).join(' ');
          changes.push(action);
        }
        if (!changes.length) continue;
        guard();
        let saveError;
        try {await call(API+'save_mappings',{kind,rows:fresh.rows});}catch(error){saveError=error;}
        const verified = await mapping(kind);
        const targets = new Map(verified.rows.map(r => [key(r.source_value),text(r.target)]));
        const committed = changes.filter(a => targets.get(key(a.source_value)) === a.target);
        report.saved.push(...committed);
        if (committed.length !== changes.length) throw saveError || new Error('Mapping save could not be verified; mappings changed. Review '+kind+'.');
      }
      report.status=report.held.length?'applied_with_holds':'applied';
      report.finished_utc=new Date().toISOString();show();
      console.info('Mappings saved. Run Plan migration next; keep this capture and do not regenerate mappings first.');
      return report;
    }),
    downloadReport:() => {
      if (!report) throw new Error('Run preview first.');
      const url=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:'application/json'}));
      const link=document.createElement('a');link.href=url;link.download='vmms-mapping-fix-report.json';
      link.click();setTimeout(() => URL.revokeObjectURL(url),1000);
    }
  });
  console.info('Mapping repair loaded; preview is read-only. Apply with: await vmmsMappingFix.apply()');
  await window.vmmsMappingFix.preview();
})();
