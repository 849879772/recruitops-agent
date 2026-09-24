const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {parseCommand}=require('../dist/policy');

const html=fs.readFileSync(path.join(__dirname,'../renderer/index.html'),'utf8');
const shell=fs.readFileSync(path.join(__dirname,'../renderer/shell.js'),'utf8');

class TextNode { constructor(text){this.textContent=String(text);} }
class Element {
  constructor(tag,ownerDocument){
    this.tagName=String(tag).toUpperCase();this.ownerDocument=ownerDocument;this.children=[];this.dataset={};this.attributes={};this.style={};
    this.className='';this.hidden=false;this.disabled=false;this.checked=false;this.indeterminate=false;this.value='';this.type='';this.name='';this.title='';this.open=false;this.tabIndex=0;this._textContent='';
    this.classList={toggle:()=>{}};
  }
  get textContent(){return this._textContent+this.children.map(child=>child.textContent||'').join('');}
  set textContent(value){this._textContent=String(value??'');this.children=[];}
  append(...nodes){for(const node of nodes)this.children.push(typeof node==='string'?new TextNode(node):node);}
  appendChild(node){this.append(node);return node;}
  replaceChildren(...nodes){this.children=[];this._textContent='';this.append(...nodes);}
  setAttribute(name,value){this.attributes[name]=String(value);}
  getAttribute(name){return this.attributes[name]??null;}
  focus(){this.ownerDocument.activeElement=this;}
  setCustomValidity(value){this.validationMessage=value;}
  querySelector(selector){
    if(selector==='input')return descendants(this).find(item=>item.tagName==='INPUT')||null;
    if(selector==='.filler-candidate-edit')return descendants(this).find(item=>item.className==='filler-candidate-edit')||null;
    if(selector==='strong')return descendants(this).find(item=>item.tagName==='STRONG')||null;
    if(selector==='.application-result')return descendants(this).find(item=>item.className==='application-result')||null;
    return null;
  }
  querySelectorAll(selector){return selector==='input,textarea,select,button'?descendants(this).filter(item=>['INPUT','TEXTAREA','SELECT','BUTTON'].includes(item.tagName)):[];}
  click(){if(this.disabled)return;return this.onclick?.({target:this,preventDefault(){}});}
}
function descendants(root){return root.children.flatMap(child=>child instanceof Element?[child,...descendants(child)]:[]);}
class Document {
  constructor(){
    this.elements=new Map();this.activeElement=null;this.body=new Element('body',this);
    this.documentElement={style:{setProperty(){}}};
    for(const [,id] of html.matchAll(/\bid="([^"]+)"/g))this.getElementById(id);
  }
  getElementById(id){if(!this.elements.has(id)){const item=new Element('div',this);item.id=id;this.elements.set(id,item);}return this.elements.get(id);}
  createElement(tag){return new Element(tag,this);}
  createTextNode(text){return new TextNode(text);}
}
function clone(value){return JSON.parse(JSON.stringify(value));}
function field(scanId,rawId,label,value){
  const id=JSON.stringify(['a'.repeat(32),'11','7:9','1:https://careers.example.test/form',scanId,rawId]);
  return {fieldId:id,frameId:'7:9',label,value,fillable:true,blocked:false};
}
function initialState({fields,queue=[],candidates=[]}={}){
  const profile={basic:{fullName:'Synthetic Candidate'}};
  const profileId='123e4567-e89b-42d3-a456-426614174000';
  return {active:11,tabs:[{id:11,title:'Anonymous ATS',url:'https://careers.example.test/form',loading:false}],
    runtime:{status:'ready',stage:'runtime',instanceId:'anonymous-instance'},browser:{connected:false},apiConfigured:false,writesEnabled:false,
    runtimeRestarting:false,workbenchLoading:false,workbenchRequested:false,workbenchError:false,configurationError:'',
    filler:{available:true,open:true,pluginReady:true,profileReady:true,scanId:'scan-one',undoReady:false,busy:false,message:'Ready',
      capabilities:{persistentProfile:true,attachment:true,repeatedSections:true,frames:true,customAnswers:true,diagnostics:true,applications:true,offlineQueue:true},
      supportedActions:['filler-scan','filler-frame-allow','filler-fill','filler-prepare','filler-undo','filler-attachment-upload','filler-profile-save','filler-profile-create','filler-profile-select','filler-profile-rename','filler-profile-delete',
        'filler-demo-enable','filler-demo-restore','filler-stop','filler-custom-save','filler-custom-delete','filler-application-detect','filler-application-save','filler-application-save-batch',
        'filler-application-flush','filler-application-cancel','filler-application-retry','filler-application-correct','filler-application-sync-page'],
      profile:{ready:true,mode:'personal',version:1,data:profile,activeProfileId:profileId,activeProfileName:'个人资料',personalProfiles:[{id:profileId,name:'个人资料'}]},
      attachment:{ready:false},fields:fields||[field('scan-one','rf-name','姓名','Synthetic Candidate')],results:[],customAnswers:[],diagnostics:[],
      application:{candidates,existing:[],queue,pendingCount:queue.length,message:'待用户确认'}}};
}
async function setup(options={}){
  const document=new Document(),state=initialState(options),commands=[],primaryId=state.filler.profile.activeProfileId;
  document.getElementById('filler-one-click-upload').checked=true;
  const profileSlots=new Map([[primaryId,{data:clone(state.filler.profile.data),answers:[]}]]),demoSlot={data:clone(state.filler.profile.data),answers:[]};
  let nextProfile=0;
  const activeSlot=()=>state.filler.profile.mode==='demo'?demoSlot:profileSlots.get(state.filler.profile.activeProfileId);
  const setActive=(profileId,mode)=>{
    const f=state.filler;f.profile.mode=mode;
    if(mode==='personal')f.profile.activeProfileId=profileId;
    const selected=f.profile.personalProfiles.find(item=>item.id===f.profile.activeProfileId);
    f.profile.activeProfileName=selected?.name||'';
    const slot=activeSlot();f.profile.data=clone(slot?.data||{});f.customAnswers=clone(slot?.answers||[]);
  };
  const clearScan=()=>{state.filler.scanId='';state.filler.fields=[];state.filler.results=[];};
  function OptionCtor(text,value){const item=document.createElement('option');item.textContent=text;item.value=value===undefined?'':String(value);return item;}
  let listener,scanNumber=1;const failedFields=new Set(options.failedFields||[]);
  const window={listeners:[],addEventListener(type,handler){if(type==='DOMContentLoaded')this.listeners.push(handler);}};
  window.desktop={
    state:async()=>clone(state),
    onState(handler){listener=handler;return()=>{};},
    async command(raw){
      const command=parseCommand(JSON.parse(JSON.stringify(raw)));commands.push(command);
      await options.beforeCommand?.(command,state,()=>listener?.(clone(state)));
      const f=state.filler;
      const expectVersion=()=>{if(command.expectedVersion!==f.profile.version)throw new Error('filler_stale_revision');};
      if(command.action==='filler-demo-enable'||command.action==='filler-demo-restore'){
        f.profile.version++;setActive(f.profile.activeProfileId,command.action==='filler-demo-enable'?'demo':'personal');clearScan();
      }
      if(command.action==='filler-profile-save'){
        if(command.expectedVersion!==f.profile.version)throw new Error('filler_stale_revision');
        activeSlot().data=clone(command.profile);f.profile.data=clone(command.profile);f.profile.version++;clearScan();
      }
      if(command.action==='filler-profile-create'){
        expectVersion();nextProfile++;
        const id=`22222222-2222-4222-8222-${String(nextProfile).padStart(12,'0')}`;
        f.profile.personalProfiles.push({id,name:command.name});profileSlots.set(id,{data:{},answers:[]});f.profile.version++;
        setActive(id,'personal');clearScan();
      }
      if(command.action==='filler-profile-select'){
        expectVersion();if(!f.profile.personalProfiles.some(item=>item.id===command.profileId))throw new Error('filler_profile_not_found');
        f.profile.version++;setActive(command.profileId,'personal');clearScan();
      }
      if(command.action==='filler-profile-rename'){
        expectVersion();const item=f.profile.personalProfiles.find(value=>value.id===command.profileId);if(!item)throw new Error('filler_profile_not_found');
        item.name=command.name;f.profile.version++;setActive(f.profile.activeProfileId,f.profile.mode);
      }
      if(command.action==='filler-profile-delete'){
        expectVersion();if(f.profile.personalProfiles.length<=1)throw new Error('filler_profile_last_required');
        f.profile.personalProfiles=f.profile.personalProfiles.filter(item=>item.id!==command.profileId);profileSlots.delete(command.profileId);f.profile.version++;
        if(f.profile.activeProfileId===command.profileId)setActive(f.profile.personalProfiles[0].id,f.profile.mode);
      }
      if(command.action==='filler-custom-save'){
        const page=new URL(state.tabs[0].url),origin=command.scope==='global'?'*':page.origin,pathname=command.scope==='global'?'*':page.pathname;
        const slot=activeSlot(),answers=slot.answers.filter(item=>item.id!==command.answerId);
        answers.push({id:command.answerId||'synthetic-answer',origin,pathname,label:command.question,value:command.answer});slot.answers=answers;f.customAnswers=clone(answers);
        f.profile.version++;clearScan();
      }
      if(command.action==='filler-scan'){
        const page=new URL(state.tabs[0].url),answer=f.customAnswers.find(item=>(item.origin==='*'||item.origin===page.origin)&&
          (item.pathname==='*'||item.pathname===page.pathname)&&item.label==='姓名');
        const step=options.scanSteps?.[Math.min(scanNumber-1,options.scanSteps.length-1)]||{};
        const source=step.fields||options.scanFields||[{rawId:'rf-name',label:'姓名',value:f.profile.data.basic?.fullName||'Synthetic Candidate'}];
        const scanId='scan-'+(++scanNumber);f.scanId=scanId;f.results=[];
        f.fields=source.map(item=>({...item,...field(scanId,item.rawId,item.label,item.label==='姓名'&&answer?answer.value:item.value),
          fillable:item.fillable??true,blocked:item.blocked??false}));
        f.repeaters=clone(step.repeaters||[]);f.scanState=step.scanState||'complete';f.scanSummary=clone(step.scanSummary||{});
      }
      if(command.action==='filler-frame-allow'){
        f.blockedFrameOrigins=[];f.scanState='complete';f.scanSummary={framesScanned:2,framesFailed:0};
      }
      if(command.action==='filler-prepare'){clearScan();f.scanState='consumed';}
      if(command.action==='filler-attachment-upload'){clearScan();f.scanState='consumed';f.message='附件已选择并由网站确认接收，请重新扫描解析后的表单。';}
      if(command.action==='filler-fill'){
        f.results=command.fieldIds.map(id=>{
          const selected=f.fields.find(item=>item.fieldId===id),raw=selected?JSON.parse(selected.fieldId)[5]:'';
          const failed=failedFields.has(raw);return {fieldId:id,status:failed?'failed':'success',ok:!failed,reason:failed?'filler_option_missing':''};
        });
        f.fields=[];f.scanId='';f.scanState='consumed';f.undoReady=true;
      }
      if(command.action==='filler-application-detect'){
        f.application.candidates=options.candidates||[{id:'candidate-one',company:'Synthetic Robotics',title:'Controls Engineer',recordUrl:'https://careers.example.test/progress'}];
      }
      if(command.action==='filler-application-save')state.lastApplicationSave=command;
      if(command.action==='filler-application-sync-page')state.lastApplicationSync=command;
      if(command.action==='filler-application-flush')f.application.queue=f.application.queue.map(item=>({...item,attempts:Math.min(3,(item.attempts||0)+1),error:'http_503'}));
      if(command.action==='filler-application-cancel')f.application.queue=f.application.queue.filter(item=>(item.queueId||item.id)!==command.queueId);
      if(command.action==='filler-application-correct'){
        const item=f.application.queue.find(value=>(value.queueId||value.id)===command.queueId);
        if(!item)throw new Error('pending_not_found');
        item.registration={...item.registration,record_url:command.recordUrl,city:command.city??item.registration.city};
        f.application.message='待补传链接已修正，尚未写入投递记录；请点击该条重试。';
      }
      if(command.action==='filler-application-retry'){
        if(options.retryError)f.application.queue=f.application.queue.map(item=>(item.queueId||item.id)===command.queueId?{...item,error:options.retryError}:item);
        else f.application.queue=f.application.queue.filter(item=>(item.queueId||item.id)!==command.queueId);
      }
      if(command.action==='filler-stop'){
        if(!['filling','preparing'].includes(f.busyOperation?.type))throw new Error('当前操作不能取消，请等待结束。');
        f.busyOperation.cancelRequested=true;f.message='正在停止后续填写，已填内容保留；等待网站当前操作结束。';
      }
      if(command.action==='reload'){
        f.busy=false;f.busyOperation=undefined;f.fields=[];f.scanId='';f.message='页面已变化，请重新扫描。';
      }
      f.application.pendingCount=f.application.queue.length;
      await options.afterCommand?.(command,state,()=>listener?.(clone(state)));
      return clone(state);
    }
  };
  const context=vm.createContext({window,document,URL,TextEncoder,structuredClone,Option:OptionCtor,console,setTimeout,clearTimeout});
  vm.runInContext(shell,context,{filename:'renderer/shell.js'});
  for(const handler of window.listeners)handler();
  await new Promise(resolve=>setImmediate(resolve));
  return {document,state,commands,push(){listener?.(clone(state));}};
}
function byText(root,text){return descendants(root).find(item=>item.textContent===text);}

test('blocked embedded form offers an explicit grant action and clears it after rescanning',async()=>{
  const ui=await setup();
  ui.state.filler.blockedFrameOrigins=['https://forms.example.test'];
  ui.state.filler.scanState='partial';
  ui.push();
  const button=ui.document.getElementById('filler-frame-allow');
  assert.equal(button.hidden,false);
  assert.equal(button.disabled,false);
  await button.click();
  assert.equal(ui.commands.at(-1).action,'filler-frame-allow');
  assert.equal(button.hidden,true);
});

test('profile mode version and scoped custom-answer edit flow survive the renderer bridge',async()=>{
  const ui=await setup();
  await ui.document.getElementById('filler-demo-enable').click();
  assert.equal(ui.state.filler.profile.mode,'demo');
  const name=descendants(ui.document.getElementById('filler-profile-editor')).find(item=>item.dataset.profilePath==='basic.fullName');
  name.value='Edited Demo';name.oninput();
  await ui.document.getElementById('filler-profile-save').click();
  assert.equal(ui.commands.at(-1).action,'filler-profile-save');assert.equal(ui.commands.at(-1).expectedVersion,2);
  await ui.document.getElementById('filler-demo-restore').click();
  assert.equal(ui.state.filler.profile.mode,'personal');assert.equal(ui.state.filler.profile.data.basic.fullName,'Synthetic Candidate');
  await ui.document.getElementById('filler-demo-enable').click();
  assert.equal(ui.state.filler.profile.mode,'demo');assert.equal(ui.state.filler.profile.data.basic.fullName,'Edited Demo');
  await ui.document.getElementById('filler-scan').click();
  assert.equal(ui.state.filler.scanId,'scan-2');
  assert.equal(typeof ui.state.filler.fields[0].value,'string');
  assert.equal(ui.state.filler.capabilities.customAnswers,true);
  assert.ok(ui.state.filler.supportedActions.includes('filler-custom-save'));
  const edit=byText(ui.document.getElementById('filler-fields'),'编辑文本答案');assert.equal(edit.disabled,false);
  await edit.click();
  assert.equal(ui.document.getElementById('filler-custom-scope').value,'site');
  ui.document.getElementById('filler-custom-answer').value='Synthetic corrected answer';
  await ui.document.getElementById('filler-custom-save').click();
  assert.deepEqual(ui.commands.slice(-2).map(item=>item.action),['filler-custom-save','filler-scan']);
  assert.equal(Object.hasOwn(ui.commands.at(-2),'fieldId'),false);
  assert.equal(ui.state.filler.fields[0].value,'Synthetic corrected answer');
  const savedAnswerEdit=byText(ui.document.getElementById('filler-custom-answers'),'编辑');assert.equal(savedAnswerEdit.disabled,false);
  await savedAnswerEdit.click();
  ui.document.getElementById('filler-custom-answer').value='Synthetic edited answer';
  await ui.document.getElementById('filler-custom-save').click();
  assert.deepEqual(ui.commands.slice(-2).map(item=>item.action),['filler-custom-save','filler-scan']);
  assert.equal(ui.commands.at(-2).answerId,'synthetic-answer');
  assert.equal(ui.state.filler.fields[0].value,'Synthetic edited answer');
  assert.equal(ui.document.getElementById('filler-confirm').disabled,false);
  await ui.document.getElementById('filler-confirm').click();
  assert.equal(ui.commands.at(-1).action,'filler-fill');assert.equal(ui.commands.at(-1).fieldIds.length,1);
});

test('named profiles create, save, rename, switch, and delete through versioned commands',async()=>{
  const ui=await setup(),primaryId=ui.state.filler.profile.activeProfileId,name=ui.document.getElementById('filler-profile-name');
  name.focus();name.value='Research';name.oninput();
  assert.equal(ui.document.getElementById('filler-profile-create').disabled,false);
  await ui.document.getElementById('filler-profile-create').click();
  const created=ui.commands.at(-1),researchId=ui.state.filler.profile.activeProfileId;
  assert.deepEqual(created,{action:'filler-profile-create',name:'Research',expectedVersion:1});
  assert.notEqual(researchId,primaryId);assert.equal(ui.state.filler.profile.activeProfileName,'Research');
  assert.deepEqual(ui.state.filler.profile.data,{});

  ui.document.getElementById('filler-custom-question').value='Synthetic profile question';
  ui.document.getElementById('filler-custom-answer').value='Research-only answer';
  ui.document.getElementById('filler-custom-scope').value='global';
  for(const id of ['filler-custom-question','filler-custom-answer','filler-custom-scope'])ui.document.getElementById(id).oninput();
  await ui.document.getElementById('filler-custom-save').click();
  assert.equal(ui.commands.at(-1).action,'filler-custom-save');assert.equal(ui.state.filler.customAnswers[0].value,'Research-only answer');

  const fullName=descendants(ui.document.getElementById('filler-profile-editor')).find(item=>item.dataset.profilePath==='basic.fullName');
  fullName.value='Synthetic Research Candidate';fullName.oninput();
  const saveProfile=ui.document.getElementById('filler-profile-save');
  assert.equal(saveProfile.disabled,false,JSON.stringify({status:ui.document.getElementById('filler-profile-status').textContent,version:ui.state.filler.profile.version,identity:ui.state.filler.profile.activeProfileId}));
  await saveProfile.click();
  assert.equal(ui.commands.at(-1).action,'filler-profile-save');assert.equal(ui.commands.at(-1).expectedVersion,3);
  assert.equal(ui.state.filler.profile.data.basic.fullName,'Synthetic Research Candidate');

  name.focus();name.value='Research Resume';name.oninput();
  await ui.document.getElementById('filler-profile-rename').click();
  assert.deepEqual(ui.commands.at(-1),{action:'filler-profile-rename',profileId:researchId,name:'Research Resume',expectedVersion:4});
  assert.equal(ui.state.filler.profile.activeProfileName,'Research Resume');

  const select=ui.document.getElementById('filler-profile-select');select.focus();select.value=primaryId;await select.onchange();
  assert.deepEqual(ui.commands.at(-1),{action:'filler-profile-select',profileId:primaryId,expectedVersion:5});
  assert.equal(ui.state.filler.profile.activeProfileName,'个人资料');
  assert.equal(ui.state.filler.profile.data.basic.fullName,'Synthetic Candidate');
  assert.deepEqual(ui.state.filler.customAnswers,[]);

  select.focus();select.value=researchId;await select.onchange();
  assert.deepEqual(ui.commands.at(-1),{action:'filler-profile-select',profileId:researchId,expectedVersion:6});
  assert.equal(ui.state.filler.customAnswers[0].value,'Research-only answer');

  const confirmation=ui.document.getElementById('filler-profile-delete-confirm');confirmation.checked=true;confirmation.onchange();
  await ui.document.getElementById('filler-profile-delete').click();
  assert.deepEqual(ui.commands.at(-1),{action:'filler-profile-delete',profileId:researchId,expectedVersion:7});
  assert.equal(ui.state.filler.profile.activeProfileId,primaryId);
  assert.equal(ui.state.filler.profile.activeProfileName,'个人资料');
  assert.equal(confirmation.checked,false);
  assert.deepEqual(ui.state.filler.profile.personalProfiles.map(item=>item.id),[primaryId]);
});

test('unsaved profile edits block switching and remote revision changes preserve the draft as a conflict',async()=>{
  const ui=await setup(),initialName=descendants(ui.document.getElementById('filler-profile-editor')).find(item=>item.dataset.profilePath==='basic.fullName');
  initialName.value='Unsaved Synthetic Edit';initialName.oninput();
  const select=ui.document.getElementById('filler-profile-select'),selected=select.value;
  select.value='22222222-2222-4222-8222-000000000001';select.onchange();
  assert.equal(ui.commands.some(item=>item.action==='filler-profile-select'),false);
  assert.equal(select.value,selected);

  ui.state.filler.profile.data={basic:{fullName:'Remote Synthetic Edit'}};ui.state.filler.profile.version++;ui.push();
  assert.match(ui.document.getElementById('filler-profile-status').textContent,/版本冲突/);
  assert.equal(ui.document.getElementById('filler-profile-save').disabled,true);
  assert.equal(descendants(ui.document.getElementById('filler-profile-editor')).find(item=>item.dataset.profilePath==='basic.fullName').value,'Unsaved Synthetic Edit');
  await ui.document.getElementById('filler-profile-discard').click();
  assert.equal(descendants(ui.document.getElementById('filler-profile-editor')).find(item=>item.dataset.profilePath==='basic.fullName').value,'Remote Synthetic Edit');
});

test('failed fields are explained and only the rescanned failure is selected for retry',async()=>{
  const ui=await setup({failedFields:['rf-bad'],scanFields:[
    {rawId:'rf-bad',label:'地区选择',value:'Synthetic city'},{rawId:'rf-good',label:'姓名',value:'Synthetic'}]});
  assert.doesNotMatch(html,/id="filler-diagnostic-panel"|id="filler-diagnostics(?:-copy|-clear)?"/);
  await ui.document.getElementById('filler-scan').click();
  await ui.document.getElementById('filler-confirm').click();
  assert.match(ui.document.getElementById('filler-fill-feedback').textContent,/地区选择.*页面没有匹配选项/);
  await ui.document.getElementById('filler-retry-failed').click();
  assert.equal(ui.state.filler.fields.length,2);
  assert.equal(ui.document.getElementById('filler-confirm').disabled,false);
  await ui.document.getElementById('filler-confirm').click();
  const retried=ui.commands.filter(item=>item.action==='filler-fill').at(-1);
  assert.equal(retried.fieldIds.length,1);assert.equal(JSON.parse(retried.fieldIds[0])[5],'rf-bad');
});

test('new registration saves reviewed fields directly without looking up existing applications',async()=>{
  const ui=await setup();
  ui.document.getElementById('filler-tab-applications').click();
  assert.equal(ui.commands.length,0);
  assert.doesNotMatch(html,/filler-application-(?:existing|find)/);
  assert.doesNotMatch(html,/关联已有投递|查找已有记录/);
  for(const [name,value] of Object.entries({company:'Synthetic Robotics',title:'Controls Engineer',url:'https://careers.example.test/progress',city:'Shanghai'})) {
    const input=ui.document.getElementById('filler-application-'+name);input.value=value;input.oninput();
  }
  const confirm=ui.document.getElementById('filler-application-confirm');confirm.checked=true;confirm.onchange();
  await ui.document.getElementById('filler-application-save').click();
  assert.deepEqual(ui.commands.map(item=>item.action),['filler-application-save']);
  assert.deepEqual(ui.state.lastApplicationSave,{action:'filler-application-save',company:'Synthetic Robotics',title:'Controls Engineer',recordUrl:'https://careers.example.test/progress',city:'Shanghai'});
  assert.equal('applicationId' in ui.state.lastApplicationSave,false);
  assert.equal('candidateIds' in ui.state.lastApplicationSave,false);
});

test('a detected job-list URL is not mistaken for a progress page and can be saved without one',async()=>{
  const ui=await setup({candidates:[{id:'candidate-one',company:'Synthetic Robotics',title:'Controls Engineer',recordUrl:'https://careers.example.test/campus/jobs'}]});
  ui.document.getElementById('filler-tab-applications').click();
  assert.equal(ui.document.getElementById('filler-application-url').value,'');
  assert.match(ui.document.getElementById('filler-application-url-note').textContent,/可先新增投递/);
  const url=ui.document.getElementById('filler-application-url');
  url.value='https://careers.example.test/campus/jobs';url.oninput();
  const confirm=ui.document.getElementById('filler-application-confirm');confirm.checked=true;confirm.onchange();
  assert.equal(ui.document.getElementById('filler-application-save').disabled,true);
  assert.match(ui.document.getElementById('filler-application-url-note').textContent,/不是投递进度页/);
  url.value='';url.oninput();confirm.checked=true;confirm.onchange();
  assert.equal(ui.document.getElementById('filler-application-save').disabled,false);
  await ui.document.getElementById('filler-application-save').click();
  assert.equal(ui.state.lastApplicationSave.recordUrl,'');
});

test('current-page progress sync selects identified cards independently of registration',async()=>{
  const ui=await setup({candidates:[
    {id:'candidate-0',company:'Synthetic Robotics',title:'Controls Engineer',sourceStatus:'笔试',recordUrl:'https://careers.example.test/progress'},
    {id:'candidate-1',company:'Synthetic Robotics',title:'Research Engineer',sourceStatus:'面试',recordUrl:'https://careers.example.test/progress'}
  ]});
  const button=ui.document.getElementById('filler-application-sync-page');
  assert.equal(button.disabled,false);
  const rows=descendants(ui.document.getElementById('filler-candidates')).filter(item=>item.tagName==='INPUT');
  rows[1].checked=true;rows[1].onchange();
  await button.click();
  assert.deepEqual(ui.state.lastApplicationSync,{action:'filler-application-sync-page',company:'Synthetic Robotics',candidateIds:['candidate-0','candidate-1']});
  assert.equal(ui.state.lastApplicationSave,undefined);
});

test('DOM candidate selection keeps shared company and URL editable without saved-record lookup',async()=>{
  const ui=await setup({candidates:[
    {id:'candidate-one',company:'Synthetic Robotics',title:'Controls Engineer',recordUrl:'https://careers.example.test/progress'},
    {id:'candidate-no-company',title:'Research Engineer',recordUrl:'https://ats.example.test/progress/42'}
  ]});
  ui.document.getElementById('filler-tab-applications').click();
  assert.equal(ui.commands.length,0);
  await ui.document.getElementById('filler-application-detect').click();
  assert.deepEqual(ui.commands.map(item=>item.action),['filler-application-detect']);
  const radios=descendants(ui.document.getElementById('filler-candidates')).filter(item=>item.tagName==='INPUT');
  radios[0].checked=true;radios[0].onchange();
  assert.equal(ui.document.getElementById('filler-application-company').value,'Synthetic Robotics');
  assert.equal(ui.document.getElementById('filler-application-title').value,'Controls Engineer');
  assert.equal(ui.document.getElementById('filler-application-url').value,'https://careers.example.test/progress');
  radios[0].checked=false;radios[0].onchange();
  radios[1].checked=true;radios[1].onchange();
  assert.equal(ui.document.getElementById('filler-application-company').value,'Synthetic Robotics');
  assert.equal(ui.document.getElementById('filler-application-title').value,'Research Engineer');
  assert.equal(ui.document.getElementById('filler-application-url').value,'https://careers.example.test/progress');
  assert.doesNotMatch(ui.document.getElementById('filler-candidates').textContent,/ats\.example\.test/);
  ui.document.getElementById('filler-application-company').value='Manually entered company';
  ui.document.getElementById('filler-application-company').oninput();
  ui.document.getElementById('filler-application-confirm').checked=true;ui.document.getElementById('filler-application-confirm').onchange();
  await ui.document.getElementById('filler-application-save').click();
  assert.deepEqual(ui.state.lastApplicationSave,{action:'filler-application-save',company:'Manually entered company',title:'Research Engineer',recordUrl:'https://careers.example.test/progress'});
  assert.equal('applicationId' in ui.state.lastApplicationSave,false);
  assert.equal('candidateIds' in ui.state.lastApplicationSave,false);
});

test('single registration candidate prefills automatically and periodic rendering preserves manual edits',async()=>{
  const ui=await setup();
  ui.state.filler.message='页面已变化，请重新扫描。';
  ui.document.getElementById('filler-tab-applications').click();
  await ui.document.getElementById('filler-application-detect').click();
  assert.equal(ui.document.getElementById('filler-application-title').value,'Controls Engineer');
  assert.equal(ui.document.getElementById('filler-candidates').querySelector('input').checked,true);
  assert.equal(ui.document.getElementById('filler-application-url').value,'https://careers.example.test/progress');
  assert.doesNotMatch(ui.document.getElementById('filler-status').textContent,/重新扫描/);
  const company=ui.document.getElementById('filler-application-company');company.value='Corrected Company';company.oninput();
  ui.push();
  assert.equal(company.value,'Corrected Company');
  assert.deepEqual(ui.commands.map(item=>item.action),['filler-application-detect']);
});

test('queue retry shows service error state for pending registration',async()=>{
  const queue=[{id:'queue-one',attempts:1,error:'http_503',registration:{company:'Synthetic Robotics',title:'Controls Engineer',record_url:'https://careers.example.test/progress'}}];
  const ui=await setup({queue,candidates:[{id:'candidate-one',company:'Synthetic Robotics',title:'Controls Engineer',recordUrl:'https://careers.example.test/progress'}]});
  await ui.document.getElementById('filler-tab-applications').click();
  await ui.document.getElementById('filler-application-detect').click();
  const radio=ui.document.getElementById('filler-candidates').querySelector('input');radio.checked=true;radio.onchange();
  assert.equal(ui.document.getElementById('filler-application-company').value,'Synthetic Robotics');
  assert.equal(ui.document.getElementById('filler-application-title').value,'Controls Engineer');
  ui.document.getElementById('filler-application-title').value='Edited Controls Engineer';ui.document.getElementById('filler-application-title').oninput();
  ui.document.getElementById('filler-application-url').value='https://careers.example.test/progress';ui.document.getElementById('filler-application-url').oninput();
  ui.document.getElementById('filler-application-confirm').checked=true;ui.document.getElementById('filler-application-confirm').onchange();
  await ui.document.getElementById('filler-application-save').click();
  assert.equal(ui.state.lastApplicationSave.title,'Edited Controls Engineer');assert.equal('candidateIds' in ui.state.lastApplicationSave,false);
  assert.match(ui.document.getElementById('filler-queue').textContent,/尝试 1\/3.*HTTP 503/);
  await ui.document.getElementById('filler-application-flush').click();
  assert.match(ui.document.getElementById('filler-application-message').textContent,/补传成功 0 条，仍待处理 1 条/);
  assert.match(ui.document.getElementById('filler-queue').textContent,/尝试 2\/3.*HTTP 503/);
});

test('single queue correction sends only link and city, preserves identity, then retries one item',async()=>{
  const queue=[
    {id:'queue-one',attempts:3,error:'pending_identity_ambiguous',registration:{company:'Synthetic Robotics',title:'Controls Engineer',
      application_id:'synthetic-app-one',record_url:'https://careers.example.test/applications',city:'Synthetic City'}},
    {id:'queue-two',attempts:1,error:'offline',registration:{company:'Example Labs',title:'Research Engineer',record_url:'https://careers.example.test/progress',city:'Other City'}}
  ];
  const ui=await setup({queue,retryError:'pending_identity_ambiguous'});ui.document.getElementById('filler-tab-applications').click();
  const root=ui.document.getElementById('filler-queue');
  assert.match(root.textContent,/公司岗位匹配多条需人工核对/);
  let row=descendants(root).find(item=>item.className==='queue-row'&&item.dataset.queueId==='queue-one');
  const inputs=descendants(row).filter(item=>item.dataset.queueId==='queue-one'&&item.dataset.queueField);
  assert.deepEqual(inputs.map(item=>item.dataset.queueField).sort(),['city','recordUrl']);
  const url=inputs.find(item=>item.dataset.queueField==='recordUrl'),city=inputs.find(item=>item.dataset.queueField==='city');
  url.value='https://careers.example.test/progress';url.oninput();city.value='Shanghai';city.oninput();
  const correction=descendants(row).find(item=>item.className==='queue-correct');assert.equal(correction.disabled,false);
  await correction.click();
  const corrected=ui.commands.at(-1);
  assert.equal(corrected.action,'filler-application-correct');assert.deepEqual(Object.keys(corrected).sort(),['action','city','queueId','recordUrl']);
  assert.deepEqual({queueId:corrected.queueId,recordUrl:corrected.recordUrl,city:corrected.city},
    {queueId:'queue-one',recordUrl:'https://careers.example.test/progress',city:'Shanghai'});
  let stored=ui.state.filler.application.queue.find(item=>item.id==='queue-one').registration;
  assert.deepEqual({company:stored.company,title:stored.title,application_id:stored.application_id,record_url:stored.record_url,city:stored.city},
    {company:'Synthetic Robotics',title:'Controls Engineer',application_id:'synthetic-app-one',record_url:'https://careers.example.test/progress',city:'Shanghai'});
  assert.match(ui.document.getElementById('filler-application-message').textContent,/身份未变.*单独重试|身份未变.*重试此条/);

  row=descendants(root).find(item=>item.className==='queue-row'&&item.dataset.queueId==='queue-one');
  await descendants(row).find(item=>item.className==='queue-retry').click();
  assert.deepEqual(ui.commands.at(-1),{action:'filler-application-retry',queueId:'queue-one'});
  assert.equal(ui.state.filler.application.queue.length,2);
  assert.match(ui.document.getElementById('filler-application-message').textContent,/公司岗位匹配多条需人工核对/);
  assert.match(root.textContent,/Example Labs/);
});

test('stop is available only for filling/preparing and sends filler-stop without reloading',async()=>{
  const ui=await setup(),stop=ui.document.getElementById('filler-stop'),tabs=JSON.stringify(ui.state.tabs);
  for(const type of ['scanning','undoing','uploading']) {
    ui.state.filler.busy=true;ui.state.filler.busyOperation={type,cancelRequested:false};ui.push();
    const before=ui.commands.length;assert.equal(stop.disabled,true);await stop.click();assert.equal(ui.commands.length,before);
  }
  ui.state.filler.busy=true;ui.state.filler.busyOperation={type:'filling',timedOut:true,cancelRequested:false};
  ui.state.filler.message='填写仍在执行；当前结果可能部分生效。';ui.push();
  assert.match(ui.document.getElementById('filler-status').textContent,/当前结果可能部分生效/);assert.equal(stop.disabled,false);
  await stop.click();
  assert.equal(ui.commands.at(-1).action,'filler-stop');assert.equal(ui.state.filler.busyOperation.cancelRequested,true);
  assert.equal(JSON.stringify(ui.state.tabs),tabs);assert.equal(ui.commands.some(item=>item.action==='reload'),false);
  assert.equal(stop.disabled,true);assert.match(ui.document.getElementById('filler-status').textContent,/等待网站当前操作结束/);

  ui.state.filler.busyOperation={type:'preparing',cancelRequested:false};ui.push();assert.equal(stop.disabled,false);
  await stop.click();assert.equal(ui.commands.at(-1).action,'filler-stop');
  ui.state.filler.busy=false;ui.state.filler.busyOperation=undefined;ui.state.filler.message='填写已停止，已完成内容保留；未刷新页面。';ui.push();
  assert.match(ui.document.getElementById('filler-status').textContent,/已完成内容保留/);
});

test('scan empty states distinguish idle, filtered controls, and frame execution failure',async()=>{
  const ui=await setup(),f=ui.state.filler,empty=ui.document.getElementById('filler-empty');
  f.fields=[];f.scanId='';f.scanState='idle';f.scanSummary={};ui.push();
  assert.match(empty.textContent,/尚未扫描当前页面/);

  f.scanId='synthetic-scan';f.scanState='complete';
  f.scanSummary={framesScanned:1,framesFailed:0,controlsSeen:6,matched:0,needsAnswer:0,unsupported:0,attachments:0,filtered:6};
  ui.push();
  assert.match(empty.textContent,/扫描完成：读取到 6 个控件，但 6 个未通过可见性或安全校验/);
  assert.doesNotMatch(empty.textContent,/尚未扫描/);
  assert.match(ui.document.getElementById('filler-counts').textContent,/安全校验跳过 6/);

  f.scanId='';f.scanState='failed';f.scanSummary={framesScanned:0,framesFailed:2,controlsSeen:0,matched:0,needsAnswer:0,unsupported:0,attachments:0,filtered:0};
  f.message='扫描失败：未能读取任何页面框架。';ui.push();
  assert.match(empty.textContent,/扫描失败：2 个页面框架不可用或受限/);
  assert.match(ui.document.getElementById('filler-counts').textContent,/控件 0/);
});

test('scan candidate rows directly explain missing answers and unsupported controls',async()=>{
  const ui=await setup(),f=ui.state.filler;
  f.scanState='complete';f.scanId='synthetic-scan';
  f.scanSummary={framesScanned:1,framesFailed:0,controlsSeen:2,matched:0,needsAnswer:1,unsupported:1,attachments:0,filtered:0};
  f.message='扫描完成：没有可自动填写字段；1 项没有唯一资料答案，可添加站点自定义答案；1 项控件暂不支持自动填写。';
  f.fields=[
    {...field('synthetic-scan','rf-site','面试站点',''),fillable:false,blocked:false,reason:'filler_answer_missing'},
    {...field('synthetic-scan','rf-photo','照片',''),fillable:false,blocked:true,reason:'filler_attachment_unsupported'},
  ];
  ui.push();
  const rows=ui.document.getElementById('filler-fields').children;
  assert.equal(rows.length,2);
  assert.match(rows[0].textContent,/需补充自定义答案：未找到匹配答案/);
  assert.match(rows[1].textContent,/不支持自动填写：仅支持已识别的简历附件/);
  assert.match(ui.document.getElementById('filler-counts').textContent,/需答案 1 · 不支持 1/);
  assert.match(ui.document.getElementById('filler-status').textContent,/没有可自动填写字段/);
});

const syntheticFields=[
  {rawId:'rf-name',label:'姓名',value:'Synthetic Candidate'},
  {rawId:'rf-school',label:'学校',value:'Example University'},
  {rawId:'rf-unanswered',label:'补充问题',value:'',fillable:false,reason:'filler_answer_missing'},
  {rawId:'rf-password',label:'密码',value:'',blocked:true,reason:'filler_custom_control_unsupported'}
];
const missingRepeater={sectionId:'7:9:education',desired:2,count:1};
function deferred(){let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};}

test('one click fresh-scans then fills every eligible field without an attachment target',async()=>{
  const ui=await setup({scanFields:syntheticFields}),button=ui.document.getElementById('filler-one-click');
  assert.match(html,/<button id="filler-one-click"[^>]*>一键扫描并填写<\/button>/);
  assert.match(html,/<details id="filler-advanced">/);
  assert.ok(html.indexOf('id="filler-scan"')<html.indexOf('id="filler-advanced"'));
  assert.ok(html.indexOf('id="filler-confirm"')<html.indexOf('id="filler-advanced"'));
  assert.equal(ui.document.getElementById('filler-advanced').open,false);
  const selectAll=ui.document.getElementById('filler-select-all');selectAll.checked=false;selectAll.onchange();
  assert.equal(ui.document.getElementById('filler-confirm').disabled,true);
  ui.state.filler.attachment={ready:true,name:'anonymous.pdf'};ui.push();
  assert.deepEqual(ui.commands,[],'rendering or opening the panel never starts autofill');
  assert.equal(button.disabled,false);await button.click();
  assert.deepEqual(ui.commands.map(item=>item.action),['filler-scan','filler-fill']);
  const fill=ui.commands.at(-1);assert.equal(fill.scanId,'scan-2');
  assert.deepEqual(fill.fieldIds.map(id=>JSON.parse(id)[5]),['rf-name','rf-school']);
  assert.ok(fill.fieldIds.every(id=>JSON.parse(id)[4]==='scan-2'));
  assert.match(ui.document.getElementById('filler-one-click-feedback').textContent,/已填写 2 项.*失败 0 项.*另有 2 项需手动处理/);
  assert.match(ui.document.getElementById('filler-remaining').textContent,/补充问题.*未找到匹配答案.*密码/);
  assert.equal(ui.document.getElementById('filler-advanced').open,true);
  assert.doesNotMatch(ui.document.getElementById('filler-remaining').textContent,/Synthetic Candidate|Example University/);
  assert.equal(ui.document.getElementById('filler-undo').disabled,false);
  assert.equal(button.disabled,false);
});

test('one click uploads an explicitly selected single resume target before rescanning and filling',async()=>{
  const ui=await setup({scanFields:syntheticFields.slice(0,2)});
  ui.state.filler.attachment={ready:true,name:'anonymous.pdf'};
  ui.state.filler.attachmentTargets=[{fieldId:'resume-target',label:'简历附件'}];ui.push();
  ui.document.getElementById('filler-one-click-upload').checked=true;
  await ui.document.getElementById('filler-one-click').click();
  assert.deepEqual(ui.commands.map(item=>item.action),['filler-scan','filler-attachment-upload','filler-scan','filler-fill']);
  assert.deepEqual(ui.commands[1],{action:'filler-attachment-upload',scanId:'scan-2',fieldId:'resume-target',confirmed:true});
  assert.equal(ui.commands[3].scanId,'scan-3');
});

test('one click never guesses among multiple resume upload targets',async()=>{
  const ui=await setup();
  ui.state.filler.attachment={ready:true,name:'anonymous.pdf'};
  ui.state.filler.attachmentTargets=[{fieldId:'one'},{fieldId:'two'}];ui.push();
  ui.document.getElementById('filler-one-click-upload').checked=true;
  await ui.document.getElementById('filler-one-click').click();
  assert.deepEqual(ui.commands.map(item=>item.action),['filler-scan']);
  assert.match(ui.document.getElementById('filler-one-click-feedback').textContent,/无法唯一确定简历上传位置/);
});

test('one click prefers the unique explicit resume upload field over a generic CV field',async()=>{
  const ui=await setup({scanFields:syntheticFields.slice(0,2)});
  ui.state.filler.attachment={ready:true,name:'anonymous.pdf'};
  ui.state.filler.attachmentTargets=[{fieldId:'generic',label:'CV'},{fieldId:'resume',label:'简历附件'}];ui.push();
  await ui.document.getElementById('filler-one-click').click();
  assert.deepEqual(ui.commands.map(item=>item.action),['filler-scan','filler-attachment-upload','filler-scan','filler-fill']);
  assert.equal(ui.commands[1].fieldId,'resume');
});

test('one click prepares supported missing rows once and fills only the rescanned plan even if rows remain missing',async()=>{
  const ui=await setup({scanSteps:[
    {fields:[],repeaters:[missingRepeater]},
    {fields:syntheticFields,repeaters:[missingRepeater]}
  ]});
  await ui.document.getElementById('filler-one-click').click();
  assert.deepEqual(ui.commands.map(item=>item.action),['filler-scan','filler-prepare','filler-scan','filler-fill']);
  assert.deepEqual(ui.commands[1],{action:'filler-prepare'});
  assert.equal(ui.commands.at(-1).scanId,'scan-3');
  assert.ok(ui.commands.at(-1).fieldIds.every(id=>JSON.parse(id)[4]==='scan-3'));
  assert.match(ui.document.getElementById('filler-one-click-feedback').textContent,/仍有 1 组经历未补齐/);
  assert.match(ui.document.getElementById('filler-remaining').textContent,/重复经历：仍缺 1 行/);
});

test('one click skips unavailable or unnecessary prepare and does not call fill for zero matches',async t=>{
  for(const mode of ['unsupported','complete','capability-missing','command-missing','zero'])await t.test(mode,async()=>{
    const repeater=mode==='unsupported'?{...missingRepeater,supported:false}:mode==='complete'?{...missingRepeater,count:2}:missingRepeater;
    const ui=await setup({scanSteps:[{fields:mode==='zero'?syntheticFields.slice(2):syntheticFields,repeaters:mode==='zero'?[]:[repeater]}]});
    if(mode==='capability-missing')ui.state.filler.capabilities.repeatedSections=false;
    if(mode==='command-missing')ui.state.filler.supportedActions=ui.state.filler.supportedActions.filter(action=>action!=='filler-prepare');
    ui.push();await ui.document.getElementById('filler-one-click').click();
    assert.deepEqual(ui.commands.map(item=>item.action),mode==='zero'?['filler-scan']:['filler-scan','filler-fill']);
    if(mode==='zero')assert.match(ui.document.getElementById('filler-one-click-feedback').textContent,/未发现可填写字段/);
  });
});

test('one click reports failed, skipped, absent results, and partial frame coverage without counting them as success',async()=>{
  const fields=Array.from({length:4},(_,i)=>({rawId:'rf-'+i,label:'匿名字段'+i,value:'Synthetic '+i}));
  const ui=await setup({scanSteps:[{fields,scanState:'partial',scanSummary:{framesFailed:1}}],afterCommand(command,state){
    if(command.action==='filler-fill')state.filler.results=[
      {fieldId:command.fieldIds[0],status:'success',ok:true},
      {fieldId:command.fieldIds[1],status:'failed',ok:false,reason:'filler_option_missing'},
      {fieldId:command.fieldIds[2],status:'skipped',ok:true}
    ];
  }});
  await ui.document.getElementById('filler-one-click').click();
  assert.match(ui.document.getElementById('filler-one-click-feedback').textContent,/已填写 1 项；失败 1 项，跳过 1 项，未确认 1 项.*1 个页面框架未扫描/);
  const remaining=ui.document.getElementById('filler-remaining').textContent;
  assert.doesNotMatch(remaining,/匿名字段0/);assert.match(remaining,/匿名字段1.*页面没有匹配选项.*匿名字段2.*已跳过.*匿名字段3.*结果未确认/);
});

test('one click stops on scan/prepare errors, stale scan responses, and a still-running timeout',async t=>{
  for(const mode of ['scan-error','prepare-error','scan-stale','scan-failed','scan-timeout','prepare-timeout'])await t.test(mode,async()=>{
    const ui=await setup({scanSteps:[{fields:syntheticFields,repeaters:[missingRepeater]}],
      beforeCommand(command){if(mode===command.action.slice(7)+'-error')throw new Error('Synthetic operation failure');},
      afterCommand(command,state){
        if(mode==='scan-stale'&&command.action==='filler-scan')state.filler.scanId='scan-one';
        if(mode==='scan-failed'&&command.action==='filler-scan')state.filler.scanState='failed';
        if(mode===command.action.slice(7)+'-timeout'){
          state.filler.busy=true;state.filler.busyOperation={type:command.action==='filler-scan'?'scanning':'preparing',timedOut:true};
        }
      }});
    await ui.document.getElementById('filler-one-click').click();
    assert.deepEqual(ui.commands.map(item=>item.action),mode.startsWith('prepare-')?['filler-scan','filler-prepare']:['filler-scan']);
    assert.match(ui.document.getElementById('filler-one-click-feedback').textContent,/未完成，未开始填写/);
    if(mode.endsWith('error'))assert.equal(ui.document.getElementById('filler-ui-error').hidden,false);
  });
});

test('one click locks repeated clicks and cancels after tab, URL, runtime, or profile changes including a restored snapshot',async t=>{
  const changes={
    tab:state=>{state.tabs.push({...state.tabs[0],id:12});state.active=12;},
    url:state=>{state.tabs[0].url='https://careers.example.test/other';},
    instance:state=>{state.runtime.instanceId='other-anonymous-instance';},
    profile:state=>{state.filler.profile.activeProfileId='other-profile';},
    version:state=>{state.filler.profile.version++;},
    loading:state=>{state.tabs[0].loading=true;}
  };
  for(const [name,change] of Object.entries(changes))await t.test(name,async()=>{
    const entered=deferred(),release=deferred();
    const ui=await setup({beforeCommand:async(command,state,push)=>{
      if(command.action!=='filler-scan')return;
      entered.resolve();await release.promise;
      const original=clone(state);change(state);push();Object.assign(state,original);push();
    }});
    const button=ui.document.getElementById('filler-one-click'),running=button.click();await entered.promise;
    try {
      assert.equal(button.disabled,true);await button.click();
      assert.equal(ui.document.getElementById('filler-demo-enable').disabled,true);
      assert.ok(descendants(ui.document.getElementById('filler-profile-editor')).filter(item=>item.dataset.profileInput).every(item=>item.disabled));
    } finally {release.resolve();await running;}
    assert.deepEqual(ui.commands.map(item=>item.action),['filler-scan']);
    assert.match(ui.document.getElementById('filler-one-click-feedback').textContent,/已取消后续填写/);
  });
});

test('stop during a one-click scan cancels later writes without sending stop to a noncancellable scan or reloading',async()=>{
  const entered=deferred(),release=deferred();
  const ui=await setup({beforeCommand:async(command,state,push)=>{
    if(command.action!=='filler-scan')return;
    state.filler.busy=true;state.filler.busyOperation={type:'scanning'};push();entered.resolve();await release.promise;
    state.filler.busy=false;state.filler.busyOperation=undefined;
  }});
  const running=ui.document.getElementById('filler-one-click').click();await entered.promise;
  try {const stop=ui.document.getElementById('filler-stop');assert.equal(stop.disabled,false);await stop.click();}
  finally {release.resolve();await running;}
  assert.deepEqual(ui.commands.map(item=>item.action),['filler-scan']);
  assert.match(ui.document.getElementById('filler-one-click-feedback').textContent,/已取消后续步骤/);
});

test('one-click stop during prepare/fill sends filler-stop, never rescans afterward, and retains confirmed results',async t=>{
  for(const action of ['prepare','fill'])await t.test(action,async()=>{
    const entered=deferred(),release=deferred();
    const ui=await setup({scanSteps:[{fields:syntheticFields,repeaters:action==='prepare'?[missingRepeater]:[]}],
      beforeCommand:async(command,state,push)=>{
        if(command.action!=='filler-'+action)return;
        state.filler.busy=true;state.filler.busyOperation={type:action==='prepare'?'preparing':'filling',cancelRequested:false};
        push();entered.resolve();await release.promise;state.filler.busy=false;state.filler.busyOperation=undefined;
      }});
    const running=ui.document.getElementById('filler-one-click').click();await entered.promise;
    try {assert.equal(ui.document.getElementById('filler-stop').disabled,false);await ui.document.getElementById('filler-stop').click();}
    finally {release.resolve();await running;}
    assert.deepEqual(ui.commands.map(item=>item.action),['filler-scan','filler-'+action,'filler-stop']);
    assert.match(ui.document.getElementById('filler-one-click-feedback').textContent,/已取消后续步骤/);
    if(action==='fill'){
      assert.match(ui.document.getElementById('filler-one-click-feedback').textContent,/已填写 2 项/);
      assert.equal(ui.document.getElementById('filler-undo').disabled,false);
    }
  });
});

test('one click stays unavailable until profile, plugin, scan/fill support, and saved draft are ready',async t=>{
  for(const mode of ['profile','plugin','scan','fill','dirty'])await t.test(mode,async()=>{
    const ui=await setup();
    if(mode==='profile')ui.state.filler.profile.ready=false;
    if(mode==='plugin')ui.state.filler.pluginReady=false;
    if(['scan','fill'].includes(mode))ui.state.filler.supportedActions=ui.state.filler.supportedActions.filter(item=>item!=='filler-'+mode);
    if(mode==='dirty'){
      const input=descendants(ui.document.getElementById('filler-profile-editor')).find(item=>item.dataset.profilePath==='basic.fullName');
      input.value='Unsaved Synthetic Candidate';input.oninput();
    }
    ui.push();const button=ui.document.getElementById('filler-one-click');assert.equal(button.disabled,true);await button.click();
    assert.deepEqual(ui.commands,[]);
  });
});

test('one-click fill errors and unacknowledged confirmation never become successful results',async t=>{
  for(const mode of ['error','unacknowledged','timeout'])await t.test(mode,async()=>{
    const ui=await setup({scanFields:syntheticFields,beforeCommand(command){
      if(mode==='error'&&command.action==='filler-fill')throw new Error('Synthetic fill rejected');
    },afterCommand(command,state){
      if(command.action!=='filler-fill')return;
      state.filler.results=[];state.filler.undoReady=false;
      if(mode==='timeout'){
        state.filler.results=[{fieldId:command.fieldIds[0],status:'success',ok:true}];state.filler.undoReady=true;
        state.filler.busy=true;state.filler.busyOperation={type:'filling',timedOut:true};
      }
    }});
    await ui.document.getElementById('filler-one-click').click();
    assert.deepEqual(ui.commands.map(item=>item.action),['filler-scan','filler-fill']);
    const feedback=ui.document.getElementById('filler-one-click-feedback').textContent;
    assert.match(feedback,mode==='timeout'?/已填写 1 项.*未确认 1 项/:/已填写 0 项.*未确认 2 项/);
    if(mode==='error')assert.match(feedback,/填写请求未完成/);
    if(mode==='timeout'){
      assert.equal(ui.document.getElementById('filler-one-click').disabled,true);
      assert.equal(ui.document.getElementById('filler-stop').disabled,false);
    }
  });
});

test('one click sends all 201 or 300 fresh matches through the actual policy without truncating',async t=>{
  for(const count of [201,300])await t.test(String(count),async()=>{
    const fields=Array.from({length:count},(_,i)=>({rawId:'rf-'+i,label:'Synthetic field '+i,value:'Synthetic value '+i}));
    const ui=await setup({scanFields:fields});
    await ui.document.getElementById('filler-one-click').click();
    assert.deepEqual(ui.commands.map(item=>item.action),['filler-scan','filler-fill']);
    assert.equal(ui.commands.at(-1).fieldIds.length,count);
    assert.deepEqual(ui.commands.at(-1).fieldIds.map(id=>JSON.parse(id)[5]),fields.map(item=>item.rawId));
    assert.match(ui.document.getElementById('filler-one-click-feedback').textContent,new RegExp(`已填写 ${count} 项；失败 0 项，跳过 0 项，未确认 0 项`));
  });
});

test('stop after automatic prepare completes but before its response returns prevents a new scan or fill',async()=>{
  const prepared=deferred(),release=deferred();
  const ui=await setup({scanSteps:[{fields:syntheticFields,repeaters:[missingRepeater]}],afterCommand:async(command,state,push)=>{
    if(command.action!=='filler-prepare')return;
    assert.equal(state.filler.scanState,'consumed');push();prepared.resolve();await release.promise;
  }});
  const running=ui.document.getElementById('filler-one-click').click();await prepared.promise;
  try {
    const stop=ui.document.getElementById('filler-stop');assert.equal(stop.disabled,false);await stop.click();
    assert.deepEqual(ui.commands.map(item=>item.action),['filler-scan','filler-prepare']);
  } finally {release.resolve();await running;}
  assert.deepEqual(ui.commands.map(item=>item.action),['filler-scan','filler-prepare']);
  assert.match(ui.document.getElementById('filler-one-click-feedback').textContent,/已取消后续步骤/);
});

test('page changes during prepare remain cancelled even when the prepare response restores the old page',async t=>{
  for(const mode of ['tab','url','loading','service-stop'])await t.test(mode,async()=>{
    const ui=await setup({scanSteps:[{fields:syntheticFields,repeaters:[missingRepeater]}],afterCommand(command,state,push){
      if(command.action!=='filler-prepare')return;
      const original=clone(state);
      if(mode==='tab'){state.tabs.push({...state.tabs[0],id:12});state.active=12;}
      if(mode==='url')state.tabs[0].url='https://careers.example.test/other';
      if(mode==='loading')state.tabs[0].loading=true;
      if(mode==='service-stop')state.filler.busyOperation={type:'preparing',cancelRequested:true};
      push();Object.assign(state,original);push();
    }});
    await ui.document.getElementById('filler-one-click').click();
    assert.deepEqual(ui.commands.map(item=>item.action),['filler-scan','filler-prepare']);
    assert.match(ui.document.getElementById('filler-one-click-feedback').textContent,mode==='service-stop'?/已请求停止后续填写/:/已取消后续填写/);
  });
});

test('navigation intent cancels automatic prepare before a delayed tab/reload response publishes new state',async t=>{
  for(const action of ['select','reload'])await t.test(action,async()=>{
    const preparing=deferred(),releasePrepare=deferred(),navigation=deferred(),releaseNavigation=deferred();
    const ui=await setup({scanSteps:[{fields:syntheticFields,repeaters:[missingRepeater]}],beforeCommand:async(command,state,push)=>{
      if(command.action==='filler-prepare'){
        state.filler.busy=true;state.filler.busyOperation={type:'preparing'};push();preparing.resolve();await releasePrepare.promise;
        state.filler.busy=false;state.filler.busyOperation=undefined;
      }
      if(command.action===action){navigation.resolve();await releaseNavigation.promise;if(action==='select')state.active=command.id;}
    }});
    ui.state.tabs.push({id:12,title:'Second Anonymous ATS',url:'https://careers.example.test/second',loading:false});ui.push();
    const running=ui.document.getElementById('filler-one-click').click();await preparing.promise;
    const navigating=(action==='select'?byText(ui.document.getElementById('tabs'),'Second Anonymous ATS'):ui.document.getElementById('reload')).click();
    await navigation.promise;
    try {
      assert.equal(ui.state.active,11,'navigation has not yet published its new page');
      releasePrepare.resolve();await running;
      assert.deepEqual(ui.commands.map(item=>item.action),['filler-scan','filler-prepare',action]);
      assert.match(ui.document.getElementById('filler-one-click-feedback').textContent,/页面切换请求已发出，已取消后续填写/);
    } finally {releasePrepare.resolve();releaseNavigation.resolve();await Promise.all([running,navigating]);}
    assert.deepEqual(ui.commands.map(item=>item.action),['filler-scan','filler-prepare',action]);
  });
});
