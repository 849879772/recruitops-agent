const { test } = require('node:test');
const assert = require('node:assert/strict');
const { websiteUrl, isolatedApi, parseCommand, trustedSender, securePreferences, isPrivateHost } = require('../dist/policy');

test('public HTTPS only; production, files, credentials and local networks fail closed', () => {
  for (const url of ['http://example.com', 'file:///C:/secret', 'javascript:alert(1)', 'https://localhost', 'http://127.0.0.1:8012', 'https://127.1', 'https://0x7f000001', 'https://10.0.0.2', 'https://172.16.1.1', 'https://192.168.1.1', 'https://[::1]', 'https://example.com:5433', 'https://user:password@example.com']) assert.throws(() => websiteUrl(url), url);
  assert.equal(websiteUrl('https://careers.example.com/jobs'), 'https://careers.example.com/jobs');
  assert.throws(() => websiteUrl('http://127.0.0.1:8012', 'http://127.0.0.1:8012'));
});
test('API is opt-in and cannot use production or ambient hostnames', () => {
  assert.equal(isolatedApi(), undefined);
  assert.equal(isolatedApi('http://127.0.0.1:49151'), 'http://127.0.0.1:49151');
  for (const url of ['http://127.0.0.1:8012', 'http://127.0.0.1:5433', 'http://localhost:49151', 'https://example.com', 'http://127.0.0.1:49151/path', 'http://user:pass@127.0.0.1:49151']) assert.throws(() => isolatedApi(url));
});
test('IPC validates exact sender, top frame, file and argument shape', () => {
  assert.ok(trustedSender(1, 1, 'file:///shell', 'file:///shell', true));
  assert.equal(trustedSender(2, 1, 'file:///shell', 'file:///shell', true), false);
  assert.equal(trustedSender(1, 1, 'https://evil.example', 'file:///shell', true), false);
  assert.equal(trustedSender(1, 1, 'file:///shell', 'file:///shell', false), false);
  for (const value of [null, [], { action: 'execute', script: 'x' }, { action: 'quit', extra: 1 }, { action: 'select', id: -1 }, { action: 'open', url: 9 }]) assert.throws(() => parseCommand(value));
  for(const value of [{action:'filler-fill',scanId:'x',fieldIds:['a'],code:'x'},{action:'filler-fill',scanId:'x',fieldIds:['a','a']},{action:'filler-plugin',path:'C:/unknown'}]) assert.throws(()=>parseCommand(value));
  assert.deepEqual(parseCommand({ action: 'open', url: 'https://example.com' }), { action: 'open', url: 'https://example.com' });
});
test('filler parity commands accept only finite bounded payloads', () => {
  assert.deepEqual(parseCommand({action:'filler-profile-save',profile:{basic:{name:'匿名候选人'}},expectedVersion:2}),
    {action:'filler-profile-save',profile:{basic:{name:'匿名候选人'}},expectedVersion:2});
  assert.deepEqual(parseCommand({action:'filler-custom-save',question:'期望城市',answer:'上海',scope:'site'}),
    {action:'filler-custom-save',question:'期望城市',answer:'上海',scope:'site'});
  assert.deepEqual(parseCommand({action:'filler-custom-save',question:'期望城市',answer:'北京',scope:'global',answerId:'answer-1'}),
    {action:'filler-custom-save',question:'期望城市',answer:'北京',scope:'global',answerId:'answer-1'});
  assert.deepEqual(parseCommand({action:'filler-custom-delete',answerId:'answer-1'}),{action:'filler-custom-delete',answerId:'answer-1'});
  assert.equal(parseCommand({action:'filler-attachment-select'}).action,'filler-attachment-select');
  assert.equal(parseCommand({action:'filler-application-save',company:'示例公司',title:'软件工程师',recordUrl:'https://careers.example/applications'}).action,'filler-application-save');
  for (const value of [
    {action:'filler-profile-save',profile:[],expectedVersion:0},
    {action:'filler-profile-save',profile:{},expectedVersion:-1},
    {action:'filler-custom-save',question:'x',answer:'y',scope:'page'},
    {action:'filler-custom-delete',answerId:'',extra:true},
    {action:'filler-application-save',company:'',title:'x',recordUrl:'https://example.com'},
    {action:'filler-application-cancel',queueId:'x',extra:true},
  ]) assert.throws(()=>parseCommand(value));
});

test('filler-fill accepts up to 300 unique fields and rejects 301 without relaxing other bounds',()=>{
  const ids=count=>Array.from({length:count},(_,i)=>`anonymous-field-${i}`);
  for(const count of [1,200,201,299,300]) {
    const command={action:'filler-fill',scanId:'synthetic-scan',fieldIds:ids(count)};
    assert.deepEqual(parseCommand(command),command);
  }
  const limit={action:'filler-fill',scanId:'s'.repeat(128),fieldIds:ids(300)};
  limit.fieldIds[299]='f'.repeat(256);
  assert.deepEqual(parseCommand(limit),limit);
  for(const command of [
    {...limit,fieldIds:[]}, {...limit,fieldIds:ids(301)},
    {...limit,fieldIds:[...ids(299),'anonymous-field-0']},
    {...limit,fieldIds:[...ids(299),'f'.repeat(257)]},
    {...limit,fieldIds:[...ids(299),'']}, {...limit,fieldIds:[...ids(299),300]},
    {...limit,scanId:''}, {...limit,scanId:'s'.repeat(129)}, {...limit,confirmed:true},
  ])assert.throws(()=>parseCommand(command));
});
test('profile, single queue and stop commands enforce exact bounded contracts',()=>{
  const id='123e4567-e89b-42d3-a456-426614174000';
  assert.deepEqual(parseCommand({action:'filler-profile-create',name:'Research',expectedVersion:3}),
    {action:'filler-profile-create',name:'Research',expectedVersion:3});
  assert.deepEqual(parseCommand({action:'filler-profile-select',profileId:id,expectedVersion:4}),
    {action:'filler-profile-select',profileId:id,expectedVersion:4});
  assert.deepEqual(parseCommand({action:'filler-profile-rename',profileId:id,name:'  Research  ',expectedVersion:5}),
    {action:'filler-profile-rename',profileId:id,name:'Research',expectedVersion:5});
  assert.deepEqual(parseCommand({action:'filler-profile-delete',profileId:id,expectedVersion:6}),
    {action:'filler-profile-delete',profileId:id,expectedVersion:6});
  assert.deepEqual(parseCommand({action:'filler-application-retry',queueId:'queue-one'}),
    {action:'filler-application-retry',queueId:'queue-one'});
  assert.deepEqual(parseCommand({action:'filler-application-correct',queueId:'queue-one',recordUrl:'https://careers.example.test/progress',city:''}),
    {action:'filler-application-correct',queueId:'queue-one',recordUrl:'https://careers.example.test/progress',city:''});
  assert.deepEqual(parseCommand({action:'filler-stop'}),{action:'filler-stop'});
  for(const value of [
    {action:'filler-profile-create',name:' ',expectedVersion:0},
    {action:'filler-profile-create',name:'x'.repeat(81),expectedVersion:0},
    {action:'filler-profile-create',name:'bad\nname',expectedVersion:0},
    {action:'filler-profile-select',profileId:'not-an-id',expectedVersion:0},
    {action:'filler-profile-delete',profileId:id,expectedVersion:-1},
    {action:'filler-application-retry',queueId:'queue-one',ids:['other']},
    {action:'filler-application-correct',queueId:'queue-one',recordUrl:'https://careers.example.test/progress',company:'Changed identity'},
    {action:'filler-stop',reload:true},
  ]) assert.throws(()=>parseCommand(value));
});

test('desktop registration is add-only without lookup, binding, sync or diagnostic commands',()=>{
  const create={action:'filler-application-save',company:'Synthetic Company',title:'Engineer',recordUrl:'https://careers.example.test/applications'};
  assert.deepEqual(parseCommand(create),create);
  for(const value of [
    {...create,applicationId:'saved-application'}, {...create,candidateIds:['candidate-one']},
    {action:'filler-application-sync',applicationId:'saved-application'},
    {action:'filler-application-find',company:'Synthetic Company',title:'Engineer'},
    {action:'filler-diagnostics-copy'}, {action:'filler-diagnostics-clear'},
  ]) assert.throws(()=>parseCommand(value));
});
test('batch registration accepts bounded new records only, never stage or existing-application binding',()=>{
  const record={company:'Synthetic Company',title:'Engineer',recordUrl:'https://careers.example.test/applications'};
  const action='filler-application-save-batch';
  assert.deepEqual(parseCommand({action,records:[record,{...record,title:'Second Engineer'}]}),{action,records:[record,{...record,title:'Second Engineer'}]});
  for(const records of [[],Array.from({length:51},()=>record),[null],[{...record,title:''}],
    [{...record,applicationId:'existing'}],[{...record,current_stage:'rejected'}],[{...record,action:'filler-application-sync'}]])
    assert.throws(()=>parseCommand({action,records}));
  assert.throws(()=>parseCommand({action,records:[record],confirmed:true}));
});
test('sandbox is mandatory and IP screening covers local address families', () => {
  assert.equal(securePreferences.nodeIntegration, false);
  assert.equal(securePreferences.sandbox, true);
  assert.equal(securePreferences.contextIsolation, true);
  for (const host of ['169.254.169.254', '100.64.1.1', '::ffff:127.0.0.1', 'fd00::1', 'foo.local']) assert.ok(isPrivateHost(host));
  assert.equal(isPrivateHost('8.8.8.8'), false);
});

test('dual-stack public CDN answers are not mistaken for private IPv6', () => {
  const answers = ['218.207.22.170', '2409:8760:1e81:56:3::1e'];
  assert.equal(answers.some(isPrivateHost), false);
  for (const host of ['2409:8c60:2600:1600:8000:0:b00:91', '2606:4700::1111', '[2001:4860:4860::8888]']) {
    assert.equal(isPrivateHost(host), false, host);
  }
  assert.equal(websiteUrl('https://[2606:4700::1111]/'), 'https://[2606:4700::1111]/');
  for (const host of ['::', '::1', 'fe80::1', 'fc00::1', 'ff02::1', '::ffff:8.8.8.8', '64:ff9b::7f00:1', '2002:7f00:1::', '2001:0000::1', '2001:0db8::1', '3fff::1']) {
    assert.equal(isPrivateHost(host), true, host);
  }
  assert.equal(['2409:8760:1e81:56:3::1e', '127.0.0.1'].some(isPrivateHost), true);
});
