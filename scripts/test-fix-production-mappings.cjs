const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const sourcePath = __dirname + '/fix-production-mappings.browser.js';
const types = {branch:'Geo Node', residence:'Geo Node', identification_type:'Identification Type',
  disability:'Disability', skill:'VMMS Skill', education_level:'VMMS Education Level'};
const geo = [
  {name:'root',geo_node_name:'Kenya',geo_level:'national',parent_geo_node:''},
  {name:'nairobi',geo_node_name:'Nairobi',geo_level:'county',parent_geo_node:'root'},
  {name:'county',geo_node_name:'Kiambu',geo_level:'county',parent_geo_node:'root'},
  {name:'subcounty',geo_node_name:'Kiambu',geo_level:'sub_county',parent_geo_node:'county'},
  {name:'muranga',geo_node_name:"Murang'a",geo_level:'county',parent_geo_node:'root'}
];
async function setup(options = {}) {
  const maps = structuredClone(options.maps || {branch:[{source_value:'*',target:''},{source_value:'Kiambu',target:''}]}), calls=[];
  const records = structuredClone({'Geo Node':geo,...options.records});
  let saves=0;
  const context = {location:{origin:options.origin || 'https://kenyaredcross.jh.frappe.cloud'},
    frappe:{session:{user:'Administrator'},user_roles:['System Manager'],csrf_token:'synthetic-csrf'},
    console:{info(){},table(){},warn(){}}, URL,Blob,setTimeout,clearTimeout,
    fetch:async(url,request) => {
      const method=url.split('/api/method/')[1], args=JSON.parse(request.body);
      calls.push({method,args});
      assert.equal(request.credentials,'same-origin');assert.equal(request.headers['X-Frappe-CSRF-Token'],'synthetic-csrf');
      let message;
      try {
        if(method.endsWith('.status')) message={busy:!!options.busy,...options.status};
        else if(method==='frappe.client.get_value') message={browser_capture:options.capture || 'b09e06d9822849e8a1a70d550e882cb0'};
        else if(method.endsWith('.mappings')) {
          if(options.beforeRead) options.beforeRead(maps,args.kind,calls);
          message={doctype:types[args.kind],rows:structuredClone(maps[args.kind] || [])};
        } else if(method==='frappe.client.get_list') {
          const rows=records[args.doctype] || [];
          message=rows.slice(args.limit_start,args.limit_start+args.limit_page_length);
        } else if(method.endsWith('.save_mappings')) {
          saves++;
          if(options.failSave===saves) throw new Error('Synthetic failed save');
          maps[args.kind]=structuredClone(args.rows);
          if(options.afterSave)options.afterSave(maps,args.kind);
          if(options.lostResponse===saves)throw new Error('Response lost after commit');
          message=null;
        } else throw new Error('Unexpected API: '+method);
        return {ok:true,status:200,json:async()=>({message})};
      } catch(error) {return {ok:false,status:409,json:async()=>({exception:error.message,exc_type:'TestError'})};}
    }};
  context.window=context;vm.createContext(context);
  let error;
  try {await vm.runInContext(fs.readFileSync(sourcePath,'utf8'),context);}catch(e){error=e;}
  return {context,api:context.vmmsMappingFix,maps,calls,error,records,
    writes:()=>calls.filter(c=>c.method.endsWith('.save_mappings'))};
}

test('load previews without writing and selects the county rather than its homonymous subcounty',async()=>{
  const e=await setup();assert.ifError(e.error);assert.equal(e.writes().length,0);
  assert.equal(e.api.report.proposed.find(p=>p.kind==='branch').target,'county');
});
test('apply saves approved mappings only and repeated apply is idempotent',async()=>{
  const e=await setup();await e.api.apply();await e.api.apply();
  assert.equal(e.maps.branch[1].target,'county');assert.equal(e.writes().length,1);
  assert.ok(e.calls.every(c=>!c.method.includes('insert') && !c.method.includes('set_value')));
});
test('HQ and test branches use Nairobi; curly Muranga spelling uses its county',async()=>{
  const e=await setup({maps:{branch:['KRCS - HQ','krcs branch test','Murang’a'].map(source_value=>({source_value,target:''}))}});
  await e.api.apply();assert.deepEqual(e.maps.branch.map(r=>r.target),['nairobi','nairobi','muranga']);
});
test('residence level, formatting and county ancestry are respected',async()=>{
  const e=await setup({maps:{residence:['sub_county:Kiambu','county:Kiambu','sub_county:Lang\'ata'].map(source_value=>({source_value,target:''}))},
    records:{'Geo Node':[...geo,{name:'langata',geo_node_name:'Langata',geo_level:'sub_county',parent_geo_node:'nairobi'}]}});
  await e.api.apply();assert.deepEqual(e.maps.residence.map(r=>r.target),['subcounty','county','langata']);
});
test('uncertain historical residence labels remain blank; the script does not omit them or invent geography',async()=>{
  const e=await setup({maps:{residence:[{source_value:'sub_county:MURANGA NORTH',target:''}]}});
  await e.api.apply();assert.equal(e.writes().length,0);assert.equal(e.api.report.held.length,1);
});
test('the actual cloud lookup IDs are reused when a separate document name collides with the label',async()=>{
  const e=await setup({maps:{education_level:[{source_value:'Undergraduate',target:''}]},records:{'VMMS Education Level':[
    {name:'Undergraduate',level_name:'Unrelated label'}, {name:'undergraduate',level_name:'Undergraduate'}]}});
  await e.api.apply();assert.equal(e.maps.education_level[0].target,'undergraduate');
});
test('native disability label and passport source aliases match existing records',async()=>{
  const e=await setup({maps:{disability:[{source_value:'Low Vision',target:''}],identification_type:[{source_value:'Passport',target:''}]},
    records:{Disability:[{name:'Low vision',disability_name:'Low vision'}],'Identification Type':[{name:'passport',identification_type_name:'Passport'}]}});
  await e.api.apply();assert.equal(e.maps.disability[0].target,'Low vision');assert.equal(e.maps.identification_type[0].target,'passport');
});
test('duplicate county or lookup labels are held rather than chosen arbitrarily',async()=>{
  const e=await setup({records:{'Geo Node':[...geo,{name:'other',geo_node_name:'Kiambu',geo_level:'county',parent_geo_node:'root'}]}});
  await e.api.apply();assert.equal(e.writes().length,0);assert.equal(e.api.report.held.length,1);
  const lookup=await setup({maps:{skill:[{source_value:'Driving',target:''}]},records:{'VMMS Skill':[{name:'a',skill_name:'Driving'},{name:'b',skill_name:'Driving'}]}});
  await lookup.api.apply();assert.equal(lookup.writes().length,0);assert.equal(lookup.api.report.held.length,1);
});
test('malformed subcounty ancestry is held',async()=>{
  const e=await setup({maps:{residence:[{source_value:'sub_county:Kiambu',target:''}]},
    records:{'Geo Node':geo.map(r=>r.name==='subcounty'?{...r,parent_geo_node:'missing'}:r)}});
  await e.api.apply();assert.equal(e.writes().length,0);assert.equal(e.api.report.held.length,1);
});
test('existing manual selections, omission defaults, notes and out-of-scope values are preserved',async()=>{
  const maps={branch:[{source_value:'*',target:'-'},{source_value:'Kiambu',target:'county',note:'Human decision'},
    {source_value:'Garissa',target:''},{source_value:'New unreviewed place',target:''}]};
  const e=await setup({maps});await e.api.apply();assert.deepEqual(e.maps,maps);assert.equal(e.writes().length,0);
});
test('edits made after the preview are preserved',async()=>{
  const e=await setup();e.maps.branch[1].target='nairobi';e.maps.branch[1].note='Manual override';
  await e.api.apply();assert.equal(e.writes().length,0);assert.equal(e.maps.branch[1].note,'Manual override');
});
test('edits made between apply planning and the final read are preserved',async()=>{
  let reads=0;
  const e=await setup({beforeRead:(maps,kind)=>{if(kind==='branch' && ++reads===3){maps.branch[1].target='nairobi';maps.branch[1].note='Latest decision';}}});
  await e.api.apply();assert.equal(e.writes().length,0);assert.equal(e.maps.branch[1].target,'nairobi');
});
test('wrong origin, capture, busy jobs and ready batches stop before mapping writes',async()=>{
  for(const options of [{origin:'https://onerc.redcross.or.ke'},{capture:'different'},{busy:true},
    {status:{batch:{status:'ready'}}},{status:{attachment_batch:{status:'ready'}}},{status:{date_repair:{status:'ready'}}}]) {
    const e=await setup(options);assert.ok(e.error);assert.equal(e.writes().length,0);
  }
});
test('a partial failure retains completed kinds and can resume without rewriting them',async()=>{
  const e=await setup({maps:{branch:[{source_value:'Kiambu',target:''}],education_level:[{source_value:'Undergraduate',target:''}]},
    records:{'VMMS Education Level':[{name:'undergraduate',level_name:'Undergraduate'}]},failSave:2});
  await assert.rejects(e.api.apply());assert.equal(e.api.report.status,'failed');assert.equal(e.maps.branch[0].target,'county');
  await e.api.apply();assert.equal(e.maps.education_level[0].target,'undergraduate');assert.equal(e.writes().filter(c=>c.args.kind==='branch').length,1);
});
test('a lost save response verifies the committed mappings without duplicating the save',async()=>{
  const e=await setup({lostResponse:1});await e.api.apply();assert.equal(e.maps.branch[1].target,'county');
  assert.equal(e.writes().length,1);assert.equal(e.api.report.status,'applied');
});
test('mapping changes are read back and an unexpected save result is reported',async()=>{
  const e=await setup({afterSave:maps=>{maps.branch[1].target='nairobi';}});
  await assert.rejects(e.api.apply(),/verify|changed/i);assert.equal(e.api.report.status,'failed');
});
test('lookup pagination does not miss an exact match beyond the first page',async()=>{
  const e=await setup({maps:{skill:[{source_value:'Driving',target:''}]},records:{'VMMS Skill':[
    ...Array.from({length:500},(_,i)=>({name:'other'+i,skill_name:'Other '+i})),{name:'driving',skill_name:'Driving'}]}});
  await e.api.apply();assert.equal(e.maps.skill[0].target,'driving');
});
