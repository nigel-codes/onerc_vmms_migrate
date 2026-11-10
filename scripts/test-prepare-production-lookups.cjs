const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto, createHash} = require('node:crypto');
const source = fs.readFileSync(__dirname + '/prepare-production-lookups.browser.js', 'utf8');
const capture = 'b09e06d9822849e8a1a70d550e882cb0';
const key = value => 'legacy_' + createHash('sha256').update(value).digest('hex').slice(0,20);
const types = {skill:['VMMS Skill','skill_name','skill_key'],education_level:['VMMS Education Level','level_name','level_key'],
  driving_licence_class:['VMMS Driving Licence Class','class_name','class_key'],identification_type:['Identification Type','identification_type_name','identification_type_key'],
  profession:['Profession','name'],licence_type:['Personnel License Type','name'],disability:['Disability','disability_name'],
  language:['Language','language_name','language_code'],gender:['Gender','gender']};

async function setup(options={}) {
  const maps=structuredClone(options.maps || {skill:[{source_value:'*',target:''},{source_value:'Recorded skill',target:''}]});
  const records=structuredClone(options.records || {}), calls=[], messages=[];
  const metas={};
  for(const [kind,[doctype,label,identity]] of Object.entries(types)) {
    metas[doctype]={doctype:'DocType',name:doctype,autoname:identity?'field:'+identity:label==='name'?'prompt':'field:'+label,
      fields:[...new Set([label,identity].filter(f=>f&&f!=='name'))].map(fieldname=>({fieldname,fieldtype:'Data',reqd:1}))};
    if(kind==='language')metas[doctype].fields.push({fieldname:'enabled',fieldtype:'Check',default:'1'});
  }
  if(options.meta)options.meta(metas);
  const match=(row,filters)=>!filters || Object.entries(filters).every(([k,v])=>String(row[k]??'').toLowerCase()===String(v).toLowerCase());
  const route=async(method,args)=>{
    if(method==='onerc_vmms_migrate.browser.status')return {message:{busy:typeof options.busy==='function'?options.busy(calls):Boolean(options.busy)}};
    if(method==='frappe.client.get_value')return {message:{browser_capture:options.capture || capture}};
    if(method==='onerc_vmms_migrate.browser.mappings')return {message:{doctype:types[args.kind][0],rows:structuredClone(maps[args.kind]||[])}};
    if(method==='frappe.desk.form.load.getdoctype')return {docs:[metas[args.doctype]]};
    if(method==='frappe.client.has_permission')return {message:{has_permission:options.noCreate!==args.doctype}};
    if(method==='frappe.client.get_list') {
      const rows=(records[args.doctype]||[]).filter(r=>match(r,args.filters));
      return {message:rows.slice(args.limit_start||0,(args.limit_start||0)+(args.limit_page_length||20)).map(r=>Object.fromEntries(args.fields.map(f=>[f,r[f]])))};
    }
    if(method==='frappe.client.insert') {
      if(options.insertError)throw new Error(options.insertError);
      const doc=structuredClone(args.doc), meta=metas[doc.doctype];
      doc.name=doc.name || doc[meta.autoname.slice(6)];
      assert.ok(doc.name);assert.equal(doc.flags,undefined);
      const table=records[doc.doctype] ||= [];
      if(table.some(r=>r.name===doc.name))throw new Error('DuplicateEntryError');
      table.push(doc);
      if(options.onInsert)options.onInsert(maps,records,doc);
      if(options.lostResponse){options.lostResponse=false;throw new Error('Connection lost after commit');}
      return {message:doc};
    }
    if(method==='onerc_vmms_migrate.browser.save_mappings') {
      assert.equal(args.rows.length,(maps[args.kind]||[]).length);
      maps[args.kind]=structuredClone(args.rows);return {message:null};
    }
    throw new Error('Unexpected API: '+method);
  };
  const context={location:{origin:options.origin || 'https://kenyaredcross.jh.frappe.cloud'},frappe:{session:{user:'Administrator'},user_roles:['System Manager'],csrf_token:'synthetic-csrf'},
    console:{log:(...v)=>messages.push(v),info:(...v)=>messages.push(v),error:(...v)=>messages.push(v),warn:(...v)=>messages.push(v),table:(...v)=>messages.push(v)},
    crypto:webcrypto,TextEncoder,URL,Blob,setTimeout:fn=>setTimeout(fn,0),clearTimeout,
    fetch:async(url,request)=>{
      const method=url.split('/api/method/')[1],args=JSON.parse(request.body);
      calls.push({method,args});
      assert.equal(request.credentials,'same-origin');assert.equal(request.headers['X-Frappe-CSRF-Token'],'synthetic-csrf');
      try {const body=await route(method,args);return {ok:true,status:200,json:async()=>body};}
      catch(error){return {ok:false,status:403,json:async()=>({exc_type:'PermissionError',exception:error.message})};}
    }};
  context.window=context;
  vm.createContext(context);
  let error;
  try {await vm.runInContext(source,context);}catch(exc){error=exc;}
  const writes=()=>calls.filter(c=>['frappe.client.insert','onerc_vmms_migrate.browser.save_mappings'].includes(c.method));
  return {context,api:context.vmmsLookupPrep,maps,records,calls,messages,writes,error};
}

test('loading previews missing vocabulary without writing',async()=>{
  const e=await setup();assert.ifError(e.error);assert.equal(e.writes().length,0);
  assert.equal(e.api.report.summary.find(r=>r.kind==='skill').create,1);
});
test('apply creates only allowlisted lookups and never changes mapping files',async()=>{
  const e=await setup();await e.api.apply();
  assert.equal(e.records['VMMS Skill'].length,1);assert.equal(e.records['VMMS Skill'][0].skill_key,key('Recorded skill'));
  assert.equal(e.maps.skill[1].target,'');
  assert.equal(e.calls.filter(c=>c.method.endsWith('.save_mappings')).length,0);
  assert.ok(e.writes().every(c=>c.method!=='frappe.client.insert'||c.args.doc.doctype==='VMMS Skill'));
});
test('retry is idempotent',async()=>{
  const e=await setup();await e.api.apply();await e.api.apply();
  assert.equal(e.calls.filter(c=>c.method==='frappe.client.insert').length,1);
  assert.equal(e.calls.filter(c=>c.method.endsWith('.save_mappings')).length,0);
});
test('existing inactive records are reused without changing their flags',async()=>{
  const e=await setup({records:{'VMMS Skill':[{name:'canonical',skill_key:'canonical',skill_name:'Recorded skill',is_active:0}]}});
  await e.api.apply();assert.equal(e.records['VMMS Skill'][0].is_active,0);assert.equal(e.maps.skill[1].target,'');
  assert.equal(e.calls.filter(c=>c.method==='frappe.client.insert').length,0);
});
test('explicit mappings and omissions are preserved',async()=>{
  const maps={skill:[{source_value:'*',target:'-'},{source_value:'Recorded skill',target:''},{source_value:'Other',target:'chosen'}]};
  const e=await setup({maps,records:{'VMMS Skill':[{name:'chosen',skill_key:'chosen',skill_name:'Other'}]}});
  await e.api.apply();assert.equal(e.writes().length,0);assert.deepEqual(e.maps,maps);
});
test('ambiguous labels and identity collisions are held',async()=>{
  for(const records of [
    {'VMMS Skill':[{name:'a',skill_key:'a',skill_name:'Recorded skill'},{name:'b',skill_key:'b',skill_name:'Recorded skill'}]},
    {'VMMS Skill':[{name:key('Recorded skill'),skill_key:key('Recorded skill'),skill_name:'Different label'}]}
  ]){
    const e=await setup({records});await e.api.apply();assert.equal(e.writes().length,0);assert.equal(e.api.report.held.length,1);
  }
});
test('invalid long or HTML values and unknown required fields are held',async()=>{
  const e=await setup({maps:{skill:[{source_value:'x'.repeat(141),target:''},{source_value:'<script>example</script>',target:''}]}});
  await e.api.apply();assert.equal(e.writes().length,0);assert.equal(e.api.report.held.length,2);
  const required=await setup({meta:m=>m['VMMS Skill'].fields.push({fieldname:'scope',fieldtype:'Link',reqd:1})});
  await required.api.apply();assert.equal(required.writes().length,0);assert.equal(required.api.report.held.length,1);
});
test('wrong site, changed capture and busy migration refuse before writes',async()=>{
  for(const options of [{origin:'https://onerc.redcross.or.ke'},{capture:'other-capture'},{busy:true}]){
    const e=await setup(options);assert.ok(e.error);assert.equal(e.writes().length,0);
  }
});
test('lack of native create permission is reported before insertion',async()=>{
  const e=await setup({noCreate:'VMMS Skill'});await e.api.apply();assert.equal(e.writes().length,0);assert.equal(e.api.report.held.length,1);
});
test('manual mappings changed during apply are preserved',async()=>{
  const e=await setup({records:{'VMMS Skill':[{name:'manual',skill_key:'manual',skill_name:'Manual choice'}]},
    onInsert:maps=>{maps.skill[1].target='manual';maps.skill[1].note='Human choice';}});
  await e.api.apply();assert.equal(e.maps.skill[1].target,'manual');assert.equal(e.maps.skill[1].note,'Human choice');
});
test('a lost insert response reuses the committed record instead of duplicating it',async()=>{
  const e=await setup({lostResponse:true});await e.api.apply();assert.equal(e.records['VMMS Skill'].length,1);assert.equal(e.maps.skill[1].target,'');
});
test('unexpected write failure stops and retains a failure report',async()=>{
  const e=await setup({insertError:'Permission denied'});
  await assert.rejects(e.api.apply(),/PermissionError/);assert.equal(e.api.report.status,'failed');
  assert.equal(e.api.report.failures.length,1);assert.equal(e.records['VMMS Skill'],undefined);
});
test('native Disability label and disabled private-use languages are populated correctly',async()=>{
  const e=await setup({maps:{disability:[{source_value:'Recorded disability',target:''}],language:[{source_value:'Recorded dialect',target:''}]}});
  await e.api.apply();assert.equal(e.records.Disability[0].disability_name,'Recorded disability');
  assert.equal(e.records.Language[0].enabled,0);assert.match(e.records.Language[0].language_code,/^x-vmms[a-z]+$/);
});
test('existing lookup pages beyond the first 500 records are searched',async()=>{
  const rows=Array.from({length:501},(_,i)=>({name:'item'+i,skill_key:'item'+i,skill_name:i===500?'Recorded skill':'Other '+i}));
  const e=await setup({records:{'VMMS Skill':rows}});await e.api.apply();
  assert.equal(e.calls.filter(c=>c.method==='frappe.client.insert').length,0);
  assert.ok(e.calls.some(c=>c.method==='frappe.client.get_list'&&c.args.limit_start===500));
});
test('an invalid existing mapping blocks that vocabulary without modifying it',async()=>{
  const maps={skill:[{source_value:'Old choice',target:'missing-target'},{source_value:'Recorded skill',target:''}]};
  const e=await setup({maps});await e.api.apply();assert.equal(e.writes().length,0);
  assert.equal(e.api.report.summary.find(r=>r.kind==='skill').blocked,true);assert.deepEqual(e.maps,maps);
});
test('stopping preserves completed inserts and a retry completes the remainder',async()=>{
  let e;
  const options={maps:{skill:[{source_value:'First',target:''},{source_value:'Second',target:''}]},onInsert:()=>{if(e)e.api.stop();}};
  e=await setup(options);
  await assert.rejects(e.api.apply(),/Stopped by operator/);
  assert.equal(e.api.report.status,'stopped');assert.equal(e.records['VMMS Skill'].length,1);
  options.onInsert=null;await e.api.apply();assert.equal(e.records['VMMS Skill'].length,2);
});
test('missing generated mapping values stop the script before writes',async()=>{
  const e=await setup({maps:{}});assert.match(e.error.message,/Generate mappings/);assert.equal(e.writes().length,0);
});
