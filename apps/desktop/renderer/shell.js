const $ = id => document.getElementById(id);
let fillerScanId = '';
const fillerSelected = new Set();
let currentState, sidebarTab='scan', targetKey='', fieldSignature='', invalidatedPlan='';
let profileDraft, profileSource='', profileVersion, profileIdentity='', profileDirty=false, profileConflict=false, pendingAction='';
let candidatesSignature='', queueSignature='', customAnswersSignature='', editingAnswerId='', hostTab;
let applicationDrafts=[];
let retryFailures=[], retryFeedback='', retryReady=false, queueFeedback='', stopFeedback='', stopPending=false, pendingFieldCount=0;
let oneClickRun,oneClickFeedback='',oneClickRemaining=[],oneClickContext='';
const capabilityFor={
  'profile-import':'persistentProfile','profile-export':'persistentProfile','profile-save':'persistentProfile',
  'profile-create':'persistentProfile','profile-select':'persistentProfile','profile-rename':'persistentProfile','profile-delete':'persistentProfile',
  'demo-enable':'persistentProfile','demo-restore':'persistentProfile','attachment-select':'attachment','attachment-clear':'attachment',
  prepare:'repeatedSections','custom-save':'customAnswers','custom-delete':'customAnswers',
  'application-detect':'applications','application-save':'applications','application-save-batch':'applications',
  'application-flush':'offlineQueue','application-cancel':'offlineQueue','application-retry':'offlineQueue','application-correct':'offlineQueue'
};
async function command(value) {
  if(oneClickRun&&!oneClickRun.cancelled&&(['home','workbench','open','back','forward','reload','clear-site'].includes(value.action)||
      value.action==='select'&&value.id!==currentState?.active||value.action==='close'&&value.id===currentState?.active)) {
    oneClickRun.cancelled=true;oneClickRun.reason='页面切换请求已发出，已取消后续填写。';
    if(currentState)renderFiller(currentState);
  }
  try { const state=await window.desktop.command(value); if(state) render(state); return state; }
  catch (error) {
    $('notice').textContent = error.message; $('notice').style.display = 'block';
    $('filler-ui-error').textContent='操作未完成：'+String(error.message).slice(0,250);$('filler-ui-error').hidden=false;
    if(/^filler-profile-(?:save|create|select|rename|delete)$/.test(value.action)&&/stale_revision/.test(String(error.message))) {
      try { const state=await window.desktop.state(); if(state)render(state); } catch {}
    }
    return null;
  }
}
function allowed(action,run) {
  const f=currentState?.filler || {}, capability=capabilityFor[action];
  return (!oneClickRun || oneClickRun===run&&!run.cancelled) && !pendingAction && !f.busy && !f.busyOperation && (!capability || f.capabilities?.[capability]===true) &&
    (!Array.isArray(f.supportedActions) || f.supportedActions.includes('filler-'+action));
}
function setSidebarTab(tab,focus=false) {
  if(!['scan','profile','applications'].includes(tab)) return;
  sidebarTab=tab;
  for(const name of ['scan','profile','applications']) {
    $('filler-tab-'+name).setAttribute('aria-selected',String(name===tab));
    $('filler-tab-'+name).tabIndex=name===tab?0:-1;$('filler-view-'+name).hidden=name!==tab;
  }
  $('filler-panel').dataset.activeTab=tab;
  if(focus) $('filler-tab-'+tab).focus();
}
function planUsable(run) {
  return !!fillerScanId && invalidatedPlan!==fieldSignature && !profileDirty && !profileConflict && allowed('fill',run);
}
function fillContext(state) {
  const f=state?.filler||{},tab=state?.tabs.find(item=>item.id===state.active),p=f.profile||{};
  return JSON.stringify([tab?.id,tab?.url,state?.runtime?.instanceId,p.mode,p.activeProfileId,p.version??f.profileVersion,p.data]);
}
function canOneClick() {
  const f=currentState?.filler||{},tab=currentState?.tabs.find(item=>item.id===currentState.active);
  return !!tab&&!tab.loading&&!profileDirty&&!profileConflict&&(f.profile?.ready??f.profileReady)&&
    (f.pluginReady??f.available)&&allowed('scan')&&allowed('fill');
}
async function oneClickFill() {
  if(!canOneClick())return;
  const run={context:fillContext(currentState),cancelled:false,reason:''};
  oneClickRun=run;oneClickContext=run.context;oneClickFeedback='正在扫描当前页面…';oneClickRemaining=[];
  $('filler-advanced').open=false;
  const usable=result=>!!result&&!run.cancelled&&!profileDirty&&!profileConflict&&fillContext(currentState)===run.context&&
    !currentState.filler?.busy&&!currentState.filler?.busyOperation;
  const scan=async()=>{
    const prior=currentState.filler?.scanId;
    const result=await fillerAction('scan',{},undefined,run),f=result?.filler;
    if(!usable(result)||!f?.scanId||f.scanId===prior||!['complete','partial'].includes(f.scanState||'complete')) {
      oneClickFeedback='扫描未完成，未开始填写。';return null;
    }
    return f;
  };
  try {
    let f=await scan();if(!f)return;
    const missingRows=(f.repeaters||[]).some(item=>item.supported!==false&&typeof item.sectionId==='string'&&
      Number.isSafeInteger(item.count)&&item.count>=0&&Number.isSafeInteger(item.desired)&&item.desired>item.count);
    if(missingRows&&allowed('prepare',run)) {
      oneClickFeedback='正在补齐重复经历…';
      const prepared=await fillerAction('prepare',{},undefined,run);
      if(!usable(prepared)){oneClickFeedback='经历准备未完成，未开始填写；页面可能已新增部分行。';return;}
      oneClickFeedback='正在重新扫描当前页面…';
      f=await scan();if(!f)return;
    }
    const fields=f.fields||[],matchable=fields.filter(field=>typeof field.fieldId==='string'&&field.fieldId&&field.fillable!==false&&!field.blocked);
    const fieldIds=[...new Set(matchable.map(field=>field.fieldId))];
    const unmatched=fields.filter(field=>!fieldIds.includes(field.fieldId));
    oneClickRemaining=unmatched.map(field=>`${field.label||'未命名字段'}：${field.reason?fillReason(field.reason):field.blocked?'暂不支持自动填写':'需补充答案'}`);
    const coverage=Number(f.scanSummary?.framesFailed)>0?`另有 ${Number(f.scanSummary.framesFailed)} 个页面框架未扫描。`:'';
    const missing=(f.repeaters||[]).filter(item=>Number.isFinite(item.desired)&&Number.isFinite(item.count)&&item.desired>item.count);
    for(const item of missing)oneClickRemaining.push(`${item.label||'重复经历'}：仍缺 ${item.desired-item.count} 行`);
    const remaining=(unmatched.length?`另有 ${unmatched.length} 项需手动处理。`:'')+(missing.length?`仍有 ${missing.length} 组经历未补齐。`:'');
    if(!fieldIds.length){oneClickFeedback='未发现可填写字段。'+remaining+coverage;return;}
    oneClickFeedback=`正在填写 ${fieldIds.length} 项…`;
    const result=await fillerAction('fill',{scanId:f.scanId,fieldIds},undefined,run);
    const results=new Map((result?.filler?.results||currentState?.filler?.results||[]).map(item=>[item.fieldId,item]));
    const counts={success:0,failed:0,skipped:0,unknown:0};
    for(const id of fieldIds) {
      const item=results.get(id),kind=item?resultKind(item):'unknown';counts[kind]++;
      if(kind!=='success') {
        const field=matchable.find(candidate=>candidate.fieldId===id);
        oneClickRemaining.push(`${field.label||'未命名字段'}：${item?.reason?fillReason(item.reason):kind==='skipped'?'已跳过':kind==='failed'?'填写失败':'结果未确认'}`);
      }
    }
    oneClickFeedback=`已填写 ${counts.success} 项；失败 ${counts.failed} 项，跳过 ${counts.skipped} 项，未确认 ${counts.unknown} 项。`+remaining+coverage;
    if(!result)oneClickFeedback='填写请求未完成，已生效内容可能保留。'+oneClickFeedback;
  } finally {
    if(run.cancelled)oneClickFeedback=run.reason+oneClickFeedback;
    oneClickRun=undefined;oneClickContext=fillContext(currentState);renderFiller(currentState);
  }
}
function selectionIdentity(fieldId) {
  try {
    const value=JSON.parse(fieldId);
    if(Array.isArray(value)&&typeof value[2]==='string'&&typeof value[5]==='string')return {frameId:value[2],rawId:value[5]};
  } catch {}
  return undefined;
}
const fillReasonLabels={
  filler_field_failed:'网站未接受此字段',filler_field_changed_rescan:'字段已变化',filler_navigation_changed:'页面已变化',
  filler_deadline_exceeded:'字段处理超时',filler_answer_conflict:'答案存在冲突',filler_answer_missing:'未找到匹配答案',
  filler_answer_label_ambiguous:'字段名称不唯一，不能安全映射',filler_answer_type_invalid:'答案类型不适用',
  filler_custom_control_unsupported:'自定义控件暂不支持',filler_attachment_unsupported:'仅支持已识别的简历附件',
  filler_option_missing:'页面没有匹配选项',filler_scan_expired:'扫描已过期'
};
function fillReason(reason) {
  return typeof reason==='string'&&/^filler_[a-z0-9_]{1,80}$/.test(reason)?(fillReasonLabels[reason]||'填写失败（'+reason.slice(7)+'）'):'未提供详细原因';
}
function queueErrorLabel(error) {
  const labels={offline:'本地服务离线',request_timeout:'请求超时',instance_changed:'实例已变化',unverified_connection:'连接未验证',
    invalid_response:'服务响应无效',transport_or_storage_failed:'传输或本地存储失败',retry_limit:'已达重试上限',
    pending_identity_ambiguous:'公司岗位匹配多条需人工核对'};
  if(typeof error!=='string'||!error)return '';
  if(labels[error])return labels[error];
  const status=/^http_(\d{3})$/.exec(error);
  return status?'服务返回 HTTP '+status[1]:'需要检查后再试';
}
function validProfileName(value) {
  if(typeof value!=='string')return false;
  const name=value.normalize('NFC').trim();
  return !!name&&name.length<=80&&!/[\u0000-\u001f\u007f]/.test(name)&&new TextEncoder().encode(name).length<=240;
}
function validCorrectedUrl(value) {
  if(typeof value!=='string'||value.length>2048||/[\s\\]/.test(value))return false;
  try {
    const url=new URL(value),route=decodeURIComponent(url.pathname+'/'+url.hash).toLowerCase();
    return ['http:','https:'].includes(url.protocol)&&!url.username&&!url.password&&
      !/(?:^|[/#!_-])(?:job|jobs|position|positions|jobdetail|job-detail|detail|apply)(?:[/.?!_-]|$)/.test(route);
  } catch { return false; }
}
async function fillerAction(action,payload={},retryTargets,run) {
  if(!allowed(action,run)) return;
  if(action==='fill' && !planUsable(run)) return;
  const selectedBefore=action==='fill'?(currentState?.filler?.fields||[]).filter(field=>(payload.fieldIds||[]).includes(field.fieldId)):[];
  const queueBefore=['application-flush','application-retry','application-correct'].includes(action)?
    JSON.parse(JSON.stringify(currentState?.filler?.application?.queue||[])):[];
  if(action==='fill'){retryFailures=[];retryFeedback='';retryReady=false;}
  if(action==='scan'&&!retryTargets){retryFailures=[];retryFeedback='';retryReady=false;}
  if(action==='application-flush')queueFeedback='';
  else if(action.startsWith('application-'))queueFeedback='';
  stopFeedback='';pendingFieldCount=action==='fill'?(payload.fieldIds||[]).length:0;
  $('filler-ui-error').hidden=true;
  pendingAction=action;renderFiller(currentState);
  const result=await command({action:'filler-'+action,...payload});
  pendingAction='';pendingFieldCount=0;
  if(result && action==='profile-save') {
    const data=result.filler?.profile?.data;
    if(data && JSON.stringify(data)===JSON.stringify(profileDraft)) {
      profileDirty=false;profileConflict=false;profileSource=JSON.stringify(data);
      profileVersion=result.filler.profile.version ?? result.filler.profileVersion;
    }
  }
  if(result&&action==='fill') {
    const results=result.filler?.results||[];
    retryFailures=results.filter(item=>resultKind(item)==='failed').map(item=>{
      const field=selectedBefore.find(candidate=>candidate.fieldId===item.fieldId),identity=selectionIdentity(field?.fieldId||'');
      return {label:field?.label||'未命名字段',frameId:field?.frameId||identity?.frameId,rawId:identity?.rawId,reason:fillReason(item.reason)};
    });
    retryReady=false;
    retryFeedback=retryFailures.length?`有 ${retryFailures.length} 个字段未完成；请核对原因后重新扫描。`:'';
  }
  if(result&&action==='scan'&&Array.isArray(retryTargets)) {
    const fields=result.filler?.fields||[];
    const matches=fields.filter(field=>{
      const identity=selectionIdentity(field.fieldId);
      return identity&&retryTargets.some(target=>target.frameId===identity.frameId&&target.rawId===identity.rawId);
    });
    fillerSelected.clear();for(const field of matches)fillerSelected.add(field.fieldId);
    for(const row of $('filler-fields').children) {
      const checkbox=row.querySelector('input');if(checkbox)checkbox.checked=fillerSelected.has(row.dataset.fieldId);
    }
    retryReady=matches.length>0;
    const missing=retryTargets.length-matches.length;
    retryFeedback=matches.length?`已重新识别 ${matches.length} 个失败字段${missing?`，另有 ${missing} 个未匹配`:''}；确认后可重试。`:'失败字段未能重新识别，请检查当前页面。';
  }
  if(result&&action==='application-flush') {
    const queue=result.filler?.application?.queue||[],afterIds=new Set(queue.map(item=>item.id||item.queueId));
    const sent=queueBefore.filter(item=>!afterIds.has(item.id||item.queueId)).length;
    queueFeedback=`重试完成：补传成功 ${sent} 条，仍待处理 ${queue.length} 条。`;
  }
  if(result&&action==='application-retry') {
    const id=payload.queueId,queue=result.filler?.application?.queue||[],pending=queue.find(item=>(item.queueId||item.id)===id);
    queueFeedback=pending?`该条仍待处理：${queueErrorLabel(pending.error)||'服务未确认成功'}。`:'该条补传已完成。';
  }
  if(result&&action==='application-correct') {
    const item=(result.filler?.application?.queue||[]).find(value=>(value.queueId||value.id)===payload.queueId);
    const registration=item?.registration||{};
    const saved=registration.record_url===payload.recordUrl && (payload.city===undefined||registration.city===payload.city);
    queueFeedback=saved?'链接/城市纠错已保存，身份未变；请点击“重试此条”继续补传。':
      (result.filler?.application?.message||'纠错结果未确认，请检查该条队列。');
  }
  if(currentState) renderFiller(currentState);
  return result;
}
function markProfileDirty() {
  profileDirty=true;invalidatedPlan=fieldSignature;
  if(currentState) renderFiller(currentState);
}
const profileLabels={root:'简历资料',basic:'基本信息',fullName:'姓名',name:'名称',gender:'性别',birthDate:'出生日期',phone:'电话',email:'邮箱',city:'城市',address:'地址',education:'教育经历',educations:'教育经历',school:'学校',major:'专业',degree:'学历',startDate:'开始日期',endDate:'结束日期',work:'工作经历',workExperience:'工作经历',internships:'实习经历',projects:'项目经历',projectExperience:'项目经历',company:'公司',title:'职位或标题',description:'内容',skills:'技能',languages:'语言能力',certificates:'证书',awards:'获奖经历',customAnswers:'自定义答案',summary:'个人简介'};
Object.assign(profileLabels,{age:'年龄',nationality:'民族',countryRegion:'国家或地区',height:'身高',weight:'体重',idNumber:'证件号码',phoneType:'电话类型',nativePlace:'籍贯',currentResidence:'现居地',studyMode:'学习形式',applicantStatus:'应聘身份',failedCourses:'挂科情况',passedCET4:'四级通过情况',englishLevel:'英语水平',englishScore:'英语成绩',emergencyContact:'紧急联系人',emergencyPhone:'紧急联系电话',recruitmentSource:'招聘来源',relativesAtCompany:'公司内亲属',healthStatus:'健康状况',interviewSite:'面试地点',acceptLocationTransfer:'接受地点调剂',acceptRoleTransfer:'接受岗位调剂',desiredSalary:'期望薪资',targetRole:'目标岗位',preferredCity:'意向城市',college:'院系',overseasStudy:'海外学习经历',gpa:'绩点',gpaPersonalScore:'个人绩点',gpaFullScore:'绩点满分',gradeRank:'成绩排名',advisor:'导师',role:'角色',organization:'组织',results:'成果',level:'级别',date:'日期',publications:'论文与出版',type:'类型',authorOrder:'作者排序',honors:'荣誉',selfIntroduction:'自我介绍'});
const profileTemplates={
  basic:'fullName gender age birthDate nationality countryRegion height weight idNumber phone phoneType email nativePlace currentResidence studyMode applicantStatus failedCourses passedCET4 englishLevel englishScore emergencyContact emergencyPhone recruitmentSource relativesAtCompany healthStatus interviewSite acceptLocationTransfer acceptRoleTransfer desiredSalary targetRole preferredCity',
  education:'school college major degree studyMode startDate endDate overseasStudy gpa gpaPersonalScore gpaFullScore gradeRank advisor',
  projects:'name role organization startDate endDate summary results',awards:'level date name description',publications:'name type date authorOrder',certificates:'type name date'
};
function blankRecord(section) {return Object.fromEntries((profileTemplates[section]||'').split(' ').filter(Boolean).map(key=>[key,'']));}
function editableProfile(data) {
  const draft=JSON.parse(JSON.stringify(data));
  draft.basic={...blankRecord('basic'),...(draft.basic||{})};
  for(const key of Object.keys(profileTemplates).filter(key=>key!=='basic')) {
    if(!Object.hasOwn(draft,key)) draft[key]=[];
    if(Array.isArray(draft[key]))draft[key]=draft[key].map(value=>value&&typeof value==='object'&&!Array.isArray(value)?{...blankRecord(key),...value}:value);
  }
  for(const key of ['skills','honors','selfIntroduction'])if(!Object.hasOwn(draft,key))draft[key]='';
  return draft;
}
function renderProfileEditor() {
  const root=$('filler-profile-editor');root.replaceChildren();
  if(!profileDraft) {const p=document.createElement('p');p.textContent='尚未提供可编辑资料，请先导入 JSON。';root.append(p);return;}
  let nodeCount=0;
  function editor(value,parent,key,depth=0,segments=[]) {
    if(++nodeCount>1500 || depth>12) {const p=document.createElement('p');p.textContent='此部分层级过深，请通过 JSON 导入编辑。';return p;}
    const name=profileLabels[key] || String(key), object=value!==null && typeof value==='object';
    if(!object) {
      const label=document.createElement('label');label.className='profile-value';label.append(document.createTextNode(name));
      const input=typeof value==='boolean'?document.createElement('select'):document.createElement(typeof value==='string'&&(value.length>100||value.includes('\n'))?'textarea':'input');
      if(typeof value==='boolean') for(const item of [true,false]) {const option=document.createElement('option');option.value=String(item);option.textContent=item?'是':'否';input.append(option);}
      if(input.tagName==='INPUT') input.type=typeof value==='number'?'number':'text';
      if(typeof value==='number') input.step='any';
      input.value=value===null?'':String(value);input.setAttribute('aria-label',name);input.dataset.profileInput='true';input.dataset.profilePath=segments.join('.');
      input.oninput=()=>{if(typeof value==='number' && (input.value===''||!Number.isFinite(Number(input.value)))) {input.setCustomValidity('请输入有效数字');return;}input.setCustomValidity('');parent[key]=typeof value==='boolean'?input.value==='true':typeof value==='number'?Number(input.value):input.value;markProfileDirty();};
      label.append(input);return label;
    }
    const group=document.createElement('fieldset'),legend=document.createElement('legend');legend.textContent=name;group.append(legend);
    for(const child of Object.keys(value)) {
      if(['__proto__','constructor','prototype'].includes(child)) continue;
      const row=document.createElement('div');row.append(editor(value[child],value,child,depth+1,[...segments,child]));
      const remove=document.createElement('button');remove.type='button';remove.className='profile-remove';remove.textContent='×';remove.title='删除 '+(profileLabels[child]||child);remove.setAttribute('aria-label',remove.title);
      remove.onclick=()=>{if(Array.isArray(value)) value.splice(Number(child),1);else delete value[child];markProfileDirty();renderProfileEditor();};row.append(remove);group.append(row);
    }
    const additions=document.createElement('details'),summary=document.createElement('summary');summary.textContent=Array.isArray(value)?'新增经历或条目':'添加字段';additions.append(summary);
    const keyInput=document.createElement('input');keyInput.placeholder='字段名';keyInput.setAttribute('aria-label',name+' 新字段名');if(!Array.isArray(value)) additions.append(keyInput);
    if(Array.isArray(value)&&profileTemplates[key]) {
      const addRecord=document.createElement('button');addRecord.type='button';addRecord.textContent='新增'+name;
      addRecord.onclick=()=>{value.push(blankRecord(key));markProfileDirty();renderProfileEditor();};group.append(addRecord);
    }
    const json=document.createElement('textarea');json.rows=2;json.value=Array.isArray(value)?'{}':'""';json.setAttribute('aria-label',name+' 新条目 JSON');additions.append(json);
    const add=document.createElement('button');add.textContent='添加';add.type='button';
    add.onclick=()=>{try {const next=JSON.parse(json.value),newKey=keyInput.value.trim();if(!Array.isArray(value)&&(!newKey||['__proto__','constructor','prototype'].includes(newKey)||Object.hasOwn(value,newKey))) throw new Error();if(Array.isArray(value))value.push(next);else value[newKey]=next;markProfileDirty();renderProfileEditor();}catch{$('filler-ui-error').textContent='字段名需唯一，条目必须是有效 JSON。';$('filler-ui-error').hidden=false;}};
    additions.append(add);group.append(additions);return group;
  }
  root.append(editor(profileDraft,{root:profileDraft},'root'));
}
function resultKind(result) {
  if(['success','failed','skipped'].includes(result.status)) return result.status;
  return result.ok===true?'success':result.ok===false?'failed':'unknown';
}
function renderFiller(state) {
  if(!state) return;
  const f=state.filler || {}, selected=state.tabs.find(tab=>tab.id===state.active),caps=f.capabilities || {};
  if(oneClickRun&&!oneClickRun.cancelled&&(fillContext(state)!==oneClickRun.context||selected?.loading||profileDirty||profileConflict||
      !(f.profile?.ready??f.profileReady)||!(f.pluginReady??f.available))) {
    oneClickRun.cancelled=true;oneClickRun.reason='页面、实例或资料已变化，已取消后续填写。';
  }
  if(oneClickRun&&f.busyOperation?.cancelRequested&&!oneClickRun.cancelled) {
    oneClickRun.cancelled=true;oneClickRun.reason='已请求停止后续填写，已生效内容保留。';
  }
  if(!oneClickRun&&oneClickContext&&oneClickContext!==fillContext(state)) {
    oneClickFeedback='';oneClickRemaining=[];oneClickContext='';
  }
  $('filler-one-click').disabled=!canOneClick();
  $('filler-one-click-feedback').textContent=oneClickRun?.cancelled?oneClickRun.reason:oneClickFeedback;
  $('filler-one-click-feedback').hidden=!oneClickFeedback&&!oneClickRun?.cancelled;
  $('filler-remaining').replaceChildren();$('filler-remaining').hidden=!oneClickRemaining.length;
  for(const message of oneClickRemaining) {const p=document.createElement('p');p.textContent=message;$('filler-remaining').append(p);}
  const nextHostTab=f.tabs?.activeTab??f.activeTab;
  if(nextHostTab!==undefined&&nextHostTab!==hostTab){hostTab=nextHostTab;setSidebarTab(nextHostTab);}
  const key=selected?`${selected.id}:${selected.url}`:'', changedTarget=!!targetKey&&key!==targetKey;
  if(key!==targetKey) {
    targetKey=key;invalidatedPlan=fieldSignature;fillerSelected.clear();retryFailures=[];retryFeedback='';retryReady=false;queueFeedback='';editingAnswerId='';
    $('filler-application-confirm').checked=false;$('filler-custom-field').value='';
    $('filler-custom-question').value='';$('filler-custom-answer').value='';
    for(const name of ['company','title','url','city']) $('filler-application-'+name).value='';
    candidatesSignature='';
  }
  const open=!!f.open && !!selected;
  $('filler-panel').hidden=!open;$('recruitment-surface').hidden=!open;document.body.classList.toggle('filler-visible',open);
  $('filler-panel').setAttribute('aria-busy',String(!!oneClickRun||!!pendingAction||!!f.busy||!!f.busyOperation));
  $('filler-target').textContent=selected?`${selected.title || '当前官网'} · ${selected.url}`:'未选择官网';
  $('filler-open').disabled=!selected||!f.available||selected.loading;
  const operation=f.busyOperation?.type || (typeof f.busyOperation==='string'?f.busyOperation:'');
  const timedOut=!!f.busyOperation?.timedOut;
  const cancelRequested=!!f.busyOperation&&typeof f.busyOperation==='object'&&f.busyOperation.cancelRequested===true;
  const operationLabels={scanning:'正在扫描当前页面…',filling:pendingFieldCount?`正在填写 ${pendingFieldCount} 项…`:'正在填写当前页面…',
    preparing:'正在准备重复经历…',undoing:'正在撤销本次填写…',uploading:'正在上传附件…',reading:'正在读取当前页岗位…'};
  const pendingLabels={scan:'正在扫描当前页面…',fill:pendingFieldCount?`正在填写 ${pendingFieldCount} 项…`:'正在填写当前页面…',
    undo:'正在撤销本次填写…',prepare:'正在准备重复经历…','profile-save':'正在保存简历资料…','custom-save':'正在保存自定义答案…',
    'application-detect':'正在读取当前页岗位…','application-save-batch':'正在保存选中的岗位…','application-flush':'正在重试待补传记录…','application-retry':'正在补传所选记录…','application-correct':'正在保存链接/城市纠错…'};
  $('filler-status').textContent=cancelRequested?(f.message||'停止请求已发送；等待当前字段操作结束。'):
    stopFeedback|| (timedOut?(f.message||'操作仍在执行；当前结果可能部分生效。'):
    (f.busy||f.busyOperation)?(operationLabels[operation]||'正在处理当前页面…'):
    pendingAction?(pendingLabels[pendingAction]||'正在处理当前请求…'):
    sidebarTab==='applications'?(f.application?.message||'待识别岗位'):(f.message||'准备就绪'));
  $('filler-status').dataset.operation=operation;
  const canRequestStop=!!selected&&Array.isArray(f.supportedActions)&&f.supportedActions.includes('filler-stop')&&
    ['filling','preparing'].includes(operation)&&!cancelRequested&&!stopPending;
  $('filler-stop').disabled=!canRequestStop&&(!oneClickRun||oneClickRun.cancelled||stopPending);
  $('filler-frame-note').textContent=caps.frames?'框架覆盖以本次扫描结果为准。':'仅顶层表单；iframe 字段暂不可用。';
  const data=f.profile?.data, source=data&&typeof data==='object'&&!Array.isArray(data)?JSON.stringify(data):'';
  const observedIdentity=`${f.profile?.mode||'personal'}:${f.profile?.activeProfileId||'legacy'}`;
  const observedVersion=f.profile?.version??f.profileVersion;
  if(!profileDirty) {
    if(source!==profileSource||observedIdentity!==profileIdentity) {
      if(profileIdentity&&(source!==profileSource||observedIdentity!==profileIdentity))invalidatedPlan=fieldSignature;
      profileSource=source;profileIdentity=observedIdentity;profileDraft=source?editableProfile(JSON.parse(source)):undefined;renderProfileEditor();
    }
    if(Number.isSafeInteger(observedVersion))profileVersion=observedVersion;
    profileConflict=false;
  }
  else {
    if(observedIdentity!==profileIdentity||source!==profileSource&&source!==JSON.stringify(profileDraft))profileConflict=true;
    if(Number.isSafeInteger(observedVersion))profileVersion=observedVersion;
  }
  const signature=key+'|'+(f.scanId||'')+'|'+JSON.stringify(f.fields||[])+'|'+JSON.stringify(f.results||[]);
  if(changedTarget) invalidatedPlan=signature;
  if(signature!==fieldSignature) {
    fieldSignature=signature;fillerScanId=f.scanId||'';fillerSelected.clear();$('filler-fields').replaceChildren();
    $('filler-custom-field').replaceChildren(new Option('仅保存答案',''));
    let fieldIndex=0;
    for(const field of f.fields||[]) {
      const row=document.createElement('div');row.className='filler-field';row.dataset.fieldId=field.fieldId;
      const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.checked=field.fillable!==false&&!field.blocked;
      checkbox.id='filler-field-choice-'+fieldIndex++;checkbox.disabled=!checkbox.checked;checkbox.setAttribute('aria-label','填写 '+field.label);
      if(checkbox.checked) fillerSelected.add(field.fieldId);
      checkbox.onchange=()=>{if(checkbox.checked)fillerSelected.add(field.fieldId);else fillerSelected.delete(field.fieldId);renderFiller(currentState);};
      const label=document.createElement('label');label.htmlFor=checkbox.id;const strong=document.createElement('strong');strong.textContent=field.label||field.fieldId;label.append(strong);
      const value=document.createElement('span');value.textContent=typeof field.value==='string'?field.value:JSON.stringify(field.value??'');
      const result=(f.results||[]).find(item=>item.fieldId===field.fieldId)||field;
      const status=document.createElement('small');status.className='field-result';
      status.textContent=field.fillable===false?(field.blocked?'不支持自动填写：'+fillReason(field.reason):'需补充自定义答案：'+fillReason(field.reason)):
        resultKind(result)==='success'?'已填写':resultKind(result)==='failed'?'失败：'+fillReason(result.reason):resultKind(result)==='skipped'?'已跳过：'+fillReason(result.reason):field.blocked?'不支持填写':'待确认';
      row.append(checkbox,label,value,status);
      const edit=document.createElement('button');edit.type='button';edit.className='filler-candidate-edit';edit.textContent=field.fillable===false?'添加自定义答案':'编辑文本答案';
      edit.disabled=typeof field.value!=='string'||field.blocked===true||!allowed('custom-save')||!f.scanId;
      edit.title=field.fillable===false?'保存后重新扫描；仅当前网站路径使用此答案':typeof field.value==='string'?'保存为仅当前网站的问题答案':'仅文本字段可直接编辑答案';
      edit.onclick=()=>{
        if(typeof field.value!=='string')return;
        $('filler-custom').open=true;$('filler-custom-field').value=field.fieldId;$('filler-custom-question').value=field.label||'';
        $('filler-custom-answer').value=field.value;$('filler-custom-scope').value='site';editingAnswerId='';renderFiller(currentState);$('filler-custom-answer').focus();
      };
      row.append(edit);$('filler-fields').append(row);
      if(!field.blocked) $('filler-custom-field').append(new Option(field.label||field.fieldId,field.fieldId));
    }
  }
  const fields=f.fields||[],eligible=fields.filter(field=>field.fillable!==false&&!field.blocked);
  const needsAnswer=fields.filter(field=>field.fillable===false&&!field.blocked).length,unsupported=fields.filter(field=>field.blocked).length;
  const scanState=f.scanState||(f.scanId?'complete':'idle'),scanSummary=f.scanSummary||{};
  const blockedOrigins=Array.isArray(f.blockedFrameOrigins)?f.blockedFrameOrigins:[];
  $('filler-frame-allow').hidden=!blockedOrigins.length;
  $('filler-frame-allow').textContent=blockedOrigins.length>1?`允许扫描嵌入表单（${blockedOrigins.length} 个待授权）`:'允许扫描嵌入表单';
  $('filler-frame-allow').disabled=!blockedOrigins.length||!allowed('frame-allow')||!!selected?.loading||profileDirty||profileConflict;
  const fieldsById=new Map(fields.map(field=>[field.fieldId,field]));
  for(const row of $('filler-fields').children) {
    const field=fieldsById.get(row.dataset.fieldId),edit=row.querySelector('.filler-candidate-edit');
    if(field&&edit)edit.disabled=typeof field.value!=='string'||field.blocked===true||!allowed('custom-save')||!f.scanId;
  }
  const results=f.results||[],success=results.filter(r=>resultKind(r)==='success').length,failed=results.filter(r=>resultKind(r)==='failed').length,skipped=results.filter(r=>resultKind(r)==='skipped').length;
  const hasScan=['complete','partial','failed'].includes(scanState)||!!f.scanId;
  $('filler-counts').textContent=results.length?`结果 ${results.length}：成功 ${success} / 失败 ${failed} / 跳过 ${skipped} / 未确认 ${Math.max(0,results.length-success-failed-skipped)}`:
    hasScan?`控件 ${Number(scanSummary.controlsSeen)||0} · 可填写 ${eligible.length} · 需答案 ${needsAnswer} · 不支持 ${unsupported} · 安全校验跳过 ${Number(scanSummary.filtered)||0} · 已选 ${fillerSelected.size}`:`识别 ${eligible.length} 项 · 已选 ${fillerSelected.size} 项`;
  const failureDetails=retryFailures.map(item=>`${item.label}：${item.reason}`);
  $('filler-fill-feedback').textContent=[retryFeedback,...failureDetails].filter(Boolean).join('；');
  $('filler-fill-feedback').hidden=!retryFeedback&&!failureDetails.length;
  $('filler-retry-failed').hidden=!retryFailures.length||retryReady;
  $('filler-retry-failed').disabled=!allowed('scan')||!retryFailures.some(item=>item.frameId&&item.rawId)||!!selected?.loading||profileDirty||profileConflict;
  let emptyMessage='尚未扫描当前页面。';
  if(scanState==='scanning')emptyMessage='正在扫描当前页面…';
  else if(scanState==='failed')emptyMessage=Number(scanSummary.framesFailed)>0?
    `扫描失败：${Number(scanSummary.framesFailed)} 个页面框架不可用或受限。请确认表单页已加载且框架在允许范围内。`:
    '扫描失败：未能读取页面框架。请确认表单页已加载后重试。';
  else if(scanState==='consumed')emptyMessage='本次扫描预览已使用，请重新扫描。';
  else if(['complete','partial'].includes(scanState)) {
    const controls=Number(scanSummary.controlsSeen)||0,filtered=Number(scanSummary.filtered)||0,attachments=Number(scanSummary.attachments)||0;
    emptyMessage=controls&&filtered?
      `扫描完成：读取到 ${controls} 个控件，但 ${filtered} 个未通过可见性或安全校验，没有可用候选。请确认控件仍可见且未禁用。`:
      controls&&attachments?
        `扫描完成：未发现可填写字段；识别到 ${attachments} 个简历附件入口。`:
        controls?
          `扫描完成：发现 ${controls} 个控件，但没有可填写或可映射字段；控件可能使用当前不支持的自定义组件。`:
          '扫描完成：未发现可处理的表单控件；页面可能仍在渲染，或使用当前不支持的自定义组件。';
    if(scanState==='partial')emptyMessage+=`另有 ${Number(scanSummary.framesFailed)||0} 个页面框架不可用或受限。`;
  }
  $('filler-empty').hidden=fields.length>0;$('filler-empty').textContent=emptyMessage;
  $('filler-select-all').disabled=!eligible.length||!planUsable();$('filler-select-all').checked=eligible.length>0&&fillerSelected.size===eligible.length;$('filler-select-all').indeterminate=fillerSelected.size>0&&fillerSelected.size<eligible.length;
  $('filler-confirm').disabled=!planUsable()||!fillerSelected.size;
  $('filler-plan-note').textContent=profileDirty?'资料有未保存改动，旧预览已失效。':profileConflict?'资料版本已变化，请核对后重新载入。':invalidatedPlan===fieldSignature&&f.scanId?'页面或资料已变化，请重新扫描。':'';
  for(const [action,capability] of Object.entries(capabilityFor)) {
    const button=$('filler-'+action);if(button) {button.disabled=!allowed(action);button.title=caps[capability]?'':'此能力尚未连接';}
  }
  $('filler-scan').disabled=!allowed('scan')||!!selected?.loading||profileDirty||profileConflict||!(f.profile?.ready??f.profileReady)||!(f.pluginReady??f.available);
  $('filler-undo').disabled=!allowed('undo')||!f.undoReady;
  $('filler-prepare').disabled=!allowed('prepare')||profileDirty||!fillerScanId;
  $('filler-profile-save').disabled=!allowed('profile-save')||!profileDraft||!profileDirty||profileConflict;
  $('filler-profile-discard').disabled=!profileDirty||!!pendingAction||!!oneClickRun;
  $('filler-profile-import').disabled=!allowed('profile-import')||profileDirty;
  $('filler-profile-export').disabled=!allowed('profile-export')||!f.profile?.ready||profileDirty;
  $('filler-demo-enable').disabled=!allowed('demo-enable')||profileDirty||f.profile?.mode==='demo';
  $('filler-demo-restore').disabled=!allowed('demo-restore')||profileDirty||f.profile?.mode!=='demo';
  const personalProfiles=Array.isArray(f.profile?.personalProfiles)?f.profile.personalProfiles.filter(item=>item&&typeof item.id==='string'&&typeof item.name==='string'):[];
  const activeProfileId=typeof f.profile?.activeProfileId==='string'?f.profile.activeProfileId:'';
  const activeProfileName=typeof f.profile?.activeProfileName==='string'?f.profile.activeProfileName:'';
  const profileNameInput=$('filler-profile-name');
  if(document.activeElement!==profileNameInput)profileNameInput.value=activeProfileName;
  const requestedProfileName=profileNameInput.value.normalize('NFKC').trim().toLowerCase();
  const duplicateProfileName=personalProfiles.some(item=>item.name.normalize('NFKC').trim().toLowerCase()===requestedProfileName);
  const duplicateOtherProfileName=personalProfiles.some(item=>item.id!==activeProfileId&&item.name.normalize('NFKC').trim().toLowerCase()===requestedProfileName);
  const profileOptionsSignature=JSON.stringify(personalProfiles);
  const profileSelect=$('filler-profile-select');
  if(profileSelect.dataset.signature!==profileOptionsSignature) {
    profileSelect.dataset.signature=profileOptionsSignature;profileSelect.replaceChildren();
    for(const item of personalProfiles)profileSelect.append(new Option(item.name,item.id));
  }
  profileSelect.value=activeProfileId;
  const canManageProfile=f.profile?.mode!=='demo'&&!profileDirty&&!profileConflict&&!oneClickRun&&!pendingAction&&!f.busy&&!f.busyOperation&&Number.isSafeInteger(profileVersion);
  for(const input of $('filler-profile-editor').querySelectorAll('input,textarea,select,button'))input.disabled=!!oneClickRun||!!pendingAction||!!f.busy||!!f.busyOperation;
  profileSelect.disabled=!personalProfiles.length||!allowed('profile-select')||profileDirty||profileConflict||!Number.isSafeInteger(profileVersion);
  $('filler-profile-create').disabled=!canManageProfile||!allowed('profile-create')||!validProfileName(profileNameInput.value)||duplicateProfileName;
  profileNameInput.disabled=!canManageProfile;
  $('filler-profile-rename').disabled=!canManageProfile||!activeProfileId||!allowed('profile-rename')||!validProfileName(profileNameInput.value)||
    duplicateOtherProfileName||profileNameInput.value.normalize('NFC').trim()===activeProfileName;
  $('filler-profile-delete-confirm').disabled=!canManageProfile||personalProfiles.length<=1;
  $('filler-profile-delete').disabled=!canManageProfile||personalProfiles.length<=1||!$('filler-profile-delete-confirm').checked||!allowed('profile-delete');
  $('filler-profile-status').textContent=(f.profile?.mode==='demo'?'演示资料':activeProfileName||'个人资料')+' · '+(profileConflict?'版本冲突，未覆盖本地改动':profileDirty?'有未保存改动':f.profile?.ready?'已载入':'尚未配置')+(caps.persistentProfile?'':' · 持久保存尚未连接');
  $('filler-attachment-status').textContent=f.attachment?.ready?`${f.attachment.name} · ${Math.ceil((f.attachment.size||0)/1024)} KB`:'未选择附件';
  $('filler-attachment-clear').disabled=!allowed('attachment-clear')||!f.attachment?.ready;
  const targets=f.attachmentTargets||[];$('filler-upload-row').hidden=!targets.length;
  const targetSignature=JSON.stringify(targets);if($('filler-upload-target').dataset.signature!==targetSignature){$('filler-upload-target').dataset.signature=targetSignature;$('filler-upload-target').replaceChildren();for(const target of targets)$('filler-upload-target').append(new Option(target.label||'简历附件',target.fieldId));}
  $('filler-attachment-upload').disabled=!f.attachment?.ready||!targets.length||!planUsable()||!!pendingAction;
  $('filler-custom-save').disabled=!allowed('custom-save')||!$('filler-custom-question').value.trim()||!$('filler-custom-answer').value.trim()||profileDirty;
  $('filler-custom-save').textContent=$('filler-custom-field').value?'保存答案并重新扫描':'保存自定义答案';
  let origin='',pathname='';try {const url=new URL(selected?.url);origin=url.origin;pathname=url.pathname;}catch{}
  $('filler-custom-origin').textContent=$('filler-custom-scope').value==='global'?'此答案可用于所有网站，请确认适用范围。':origin;
  const answers=Array.isArray(f.customAnswers)?f.customAnswers:[],answersKey=JSON.stringify(answers)+'|'+allowed('custom-save')+'|'+allowed('custom-delete')+'|'+origin+'|'+pathname;
  if(answersKey!==customAnswersSignature){customAnswersSignature=answersKey;$('filler-custom-answers').replaceChildren();for(const answer of answers){
    if(!answer||typeof answer.id!=='string')continue;const row=document.createElement('div');row.className='queue-row';
    const text=document.createElement('p');text.textContent=`${answer.label||'未命名问题'} · ${answer.origin==='*'?'所有网站':answer.origin||'指定网站'}`;
    const edit=document.createElement('button');edit.type='button';edit.textContent='编辑';
    edit.disabled=!allowed('custom-save')||(answer.origin!=='*'&&(answer.origin!==origin||answer.pathname!==pathname));
    edit.title=edit.disabled?'仅可在答案所属网站和路径编辑':'编辑自定义答案';
    edit.onclick=()=>{editingAnswerId=answer.id;$('filler-custom-question').value=answer.label||'';$('filler-custom-answer').value=answer.value||'';$('filler-custom-scope').value=answer.origin==='*'?'global':'site';
      const matched=(f.fields||[]).find(field=>field.label===answer.label);$('filler-custom-field').value=matched?.fieldId||'';renderFiller(currentState);};
    const remove=document.createElement('button');remove.type='button';remove.textContent='删除';remove.disabled=!allowed('custom-delete');remove.onclick=async()=>{if(await fillerAction('custom-delete',{answerId:answer.id}))editingAnswerId='';};
    row.append(text,edit,remove);$('filler-custom-answers').append(row);
  }}
  renderApplications(f);
}
function selectedApplications() { return applicationDrafts.filter(item=>item.selected&&!['saved','queued'].includes(item.result)); }
function refreshApplicationSelection() {
  const selected=selectedApplications();
  $('filler-application-title').value=selected.length===1?selected[0].title:'';
  $('filler-application-confirm').checked=false;
  renderFiller(currentState);
}
function renderApplications(f) {
  const application=f.application||{},candidates=application.candidates||[],queue=application.queue||[];
  const signature=JSON.stringify(candidates);
  if(signature!==candidatesSignature) {
    candidatesSignature=signature;$('filler-candidates').replaceChildren();
    applicationDrafts=candidates.map((item,index)=>({...item,title:item.title||item.job_title||'',selected:index===0}));
    const first=applicationDrafts[0];
    if(first){
      $('filler-application-company').value=first.company||first.company_name||'';
      $('filler-application-title').value=first.title;
      $('filler-application-url').value=first.recordUrl||'';
      $('filler-application-city').value=first.city||'';
    }
    $('filler-application-confirm').checked=false;
    for(const item of applicationDrafts) {
      const id=item.id||item.candidateId;
      const label=document.createElement('label');label.className='filler-check';const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.value=id||'';
      checkbox.onchange=()=>{item.selected=checkbox.checked;refreshApplicationSelection();};
      const text=document.createElement('span'),title=document.createElement('strong');title.textContent=item.title;text.append(title);
      for(const [name,value] of [['投递时间',item.date],['官网状态',item.sourceStatus]])if(value){const detail=document.createElement('small');detail.textContent=`${name}：${value}`;text.append(detail);}
      const result=document.createElement('small');result.className='application-result';result.hidden=true;text.append(result);
      label.append(checkbox,text);$('filler-candidates').append(label);
    }
  }
  const selected=selectedApplications(),available=applicationDrafts.filter(item=>!['saved','queued'].includes(item.result));
  const busy=!!pendingAction||!!f.busy||!!f.busyOperation;
  for(const [index,row] of [...$('filler-candidates').children].entries()){
    const item=applicationDrafts[index],checkbox=row.querySelector('input');
    checkbox.checked=item.selected;checkbox.disabled=busy||!available.includes(item);
    row.querySelector('strong').textContent=item.title;
    const result=row.querySelector('.application-result');result.hidden=!item.result;result.dataset.status=item.result||'';
    result.textContent=item.result==='saved'?'已保存':item.result==='queued'?'待补传':item.result==='failed'?'未保存：'+(item.error==='foreground_changed'?'页面已变化':queueErrorLabel(item.error)):'';
  }
  $('filler-candidate-toolbar').hidden=!applicationDrafts.length;
  $('filler-candidate-count').textContent=`已选 ${selected.length} / 共 ${applicationDrafts.length} 个岗位`;
  $('filler-candidate-all').checked=!!available.length&&selected.length===available.length;
  $('filler-candidate-all').indeterminate=selected.length>0&&selected.length<available.length;
  $('filler-candidate-all').disabled=busy||!available.length;
  $('filler-application-title-label').hidden=applicationDrafts.length>0&&selected.length!==1;
  for(const name of ['company','title','url','city'])$('filler-application-'+name).disabled=busy;
  $('filler-application-confirm').disabled=busy;
  $('filler-application-message').textContent=queueFeedback||application.message||'核对公司、岗位和进度链接后确认新增登记。';
  const queueKey=JSON.stringify(queue)+'|'+allowed('application-cancel')+'|'+allowed('application-retry')+'|'+allowed('application-correct');
  if(queueKey!==queueSignature){queueSignature=queueKey;$('filler-queue').replaceChildren();for(const item of queue){
    const id=item.queueId||item.id,registration=item.registration||{};
    const row=document.createElement('div');row.className='queue-row';row.dataset.queueId=id||'';const text=document.createElement('p');
    const attempts=Number.isInteger(item.attempts)&&item.attempts>=0&&item.attempts<=3?`尝试 ${item.attempts}/3`:'';
    const error=queueErrorLabel(item.error);const status=[attempts,error?'失败原因：'+error:'待补传'].filter(Boolean).join(' · ');
    text.textContent=`${item.company||registration.company||'待传记录'} · ${item.title||registration.title||''} · ${status}`;
    const actions=document.createElement('div');actions.className='queue-actions';
    const retry=document.createElement('button');retry.type='button';retry.className='queue-retry';retry.textContent='重试此条';
    retry.disabled=!allowed('application-retry')||!id;retry.onclick=()=>fillerAction('application-retry',{queueId:id});actions.append(retry);
    const cancel=document.createElement('button');cancel.type='button';cancel.className='queue-cancel';cancel.textContent='取消';cancel.disabled=!allowed('application-cancel')||!id;
    cancel.onclick=()=>fillerAction('application-cancel',{queueId:id});actions.append(cancel);
    const correction=document.createElement('details');correction.className='queue-correction';
    const summary=document.createElement('summary');summary.textContent='更正进度链接或城市';correction.append(summary);
    const note=document.createElement('p');note.className='queue-correction-note';note.textContent='仅更改链接和城市；公司与岗位身份保持不变，保存后需单独重试。';correction.append(note);
    const fields=document.createElement('div');fields.className='queue-correction-fields';
    const urlLabel=document.createElement('label');urlLabel.append(document.createTextNode('进度链接'));
    const urlInput=document.createElement('input');urlInput.type='url';urlInput.maxLength=2048;urlInput.value=registration.record_url||'';urlInput.dataset.queueId=id||'';urlInput.dataset.queueField='recordUrl';urlInput.setAttribute('aria-label','更正进度链接');urlLabel.append(urlInput);
    const cityLabel=document.createElement('label');cityLabel.append(document.createTextNode('城市'));
    const cityInput=document.createElement('input');cityInput.type='text';cityInput.maxLength=255;cityInput.value=registration.city||'';cityInput.dataset.queueId=id||'';cityInput.dataset.queueField='city';cityInput.setAttribute('aria-label','更正城市');cityLabel.append(cityInput);
    fields.append(urlLabel,cityLabel);correction.append(fields);
    const correct=document.createElement('button');correct.type='button';correct.className='queue-correct';correct.textContent='确认只更正链接/城市';
    const refreshCorrection=()=>{correct.disabled=!allowed('application-correct')||!id||!validCorrectedUrl(urlInput.value.trim());};
    urlInput.oninput=refreshCorrection;cityInput.oninput=refreshCorrection;refreshCorrection();
    correct.onclick=()=>fillerAction('application-correct',{queueId:id,recordUrl:urlInput.value.trim(),city:cityInput.value.trim()});
    correction.append(correct);row.append(text,actions,correction);$('filler-queue').append(row);
  }}
  $('filler-queue-count').textContent=String(application.pendingCount??queue.length);
  $('filler-application-flush').disabled=!allowed('application-flush')||!queue.length;
  let urlValid=false;try {const url=new URL($('filler-application-url').value);urlValid=url.protocol==='https:'&&!url.username&&!url.password;}catch{}
  const hasTitles=applicationDrafts.length?selected.length>0&&selected.every(item=>item.title.trim()):!!$('filler-application-title').value.trim();
  $('filler-application-save').textContent=applicationDrafts.length?`确认新增 ${selected.length} 条投递`:'确认新增登记';
  $('filler-application-save').disabled=!allowed(selected.length>1?'application-save-batch':'application-save')||!$('filler-application-confirm').checked||!$('filler-application-company').value.trim()||!hasTitles||!urlValid;
}
function render(state) {
  currentState=state;
  const stages = {resources:'正在校验本地运行资源',preflight:'正在准备本地服务',instance:'正在打开当前用户数据',initdb:'正在初始化本地数据库',database:'正在连接本地数据库',migration:'正在检查数据结构',backup:'正在备份本地数据',api:'正在启动工作台服务',runtime:'工作台服务已就绪'};
  const diagnostics = state.active === null && !state.workbenchRequested;
  const failed = state.runtime.status === 'failed' || !!state.configurationError || state.workbenchError;
  const pending = !diagnostics && !failed && (state.active === null || state.active === 'workbench') && (state.workbenchLoading || (state.workbenchRequested && state.runtime.status !== 'ready'));
  $('workbench-progress').hidden = !pending;
  $('startup-error').hidden = diagnostics || !failed || typeof state.active === 'number';
  $('startup-error-message').textContent = state.workbenchError ? (state.notice || '工作台加载失败，请查看启动状态后重试。') : `工作台暂时无法启动，请查看启动状态。${state.runtime.code || state.configurationError || ''}`;
  $('startup-message').textContent = state.workbenchLoading ? '正在加载工作台，请稍候…' : `${stages[state.runtime.stage] || '正在启动本地服务'}，就绪后自动进入。`;
  $('workbench').title = pending ? '启动完成后自动进入工作台，无需重复点击' : '打开工作台';
  $('home-page').hidden = !diagnostics;
  $('tabs').replaceChildren();
  for (const tab of state.tabs) {
    const group = document.createElement('div'); group.className = 'tab';
    const select = document.createElement('button'); select.textContent = tab.title || '招聘官网'; select.title = tab.title; select.setAttribute('role', 'tab'); select.setAttribute('aria-selected', String(state.active === tab.id)); select.onclick = () => command({ action: 'select', id: tab.id });
    const close = document.createElement('button'); close.textContent = '×'; close.title = '关闭标签页'; close.setAttribute('aria-label', '关闭 ' + tab.title); close.onclick = () => command({ action: 'close', id: tab.id }); group.append(select, close); $('tabs').append(group);
  }
  const selected = state.tabs.find(tab => tab.id === state.active);
  renderFiller(state);
  if (document.activeElement !== $('address')) $('address').value = selected?.url || '';
  $('back').disabled = !selected?.back; $('forward').disabled = !selected?.forward;
  $('reload').disabled = !selected && !(state.active === 'workbench' && state.runtime.status === 'ready');
  $('clear-site').disabled = !selected;
  $('loading').textContent = selected?.loading ? '加载中' : selected?.error ? '加载失败' : selected?.resourceWarning ? '资源提示' : '';
  $('loading').title = selected?.error || selected?.resourceWarning || '';
  $('api-status').textContent = failed ? `启动失败：${state.runtime.code || state.configurationError}` : state.runtime.status === 'stopping' ? '正在停止当前服务' : stages[state.runtime.stage] || '尚未启动';
  $('runtime-instance').textContent = state.runtime.instanceId || '未创建';
  $('browser-status').textContent = state.browser.connected ? '已连接 / 顶层页面证据' : '不可用';
  $('writes-status').textContent = state.writesEnabled ? '已对当前实例显式开启' : '关闭';
  $('runtime-writes').hidden = !diagnostics;
  $('runtime-writes').disabled = state.runtime.status !== 'ready' || state.runtimeRestarting;
  $('runtime-writes').textContent = state.writesEnabled ? '高级：切换只读模式' : '恢复正常业务模式';
  $('runtime-writes').onclick = () => command({ action: state.writesEnabled ? 'disable-writes' : 'enable-writes' });
  $('capture').disabled = !selected || !state.browser.connected;
  $('capture-result').hidden = !state.captureDraft || state.active !== null;
  $('capture-result').textContent = state.captureDraft ? JSON.stringify(state.captureDraft, null, 2) : '';
  $('use-capture').hidden = !state.captureDraft?.result?.draft || state.active !== null;
  $('use-capture').disabled = !state.apiConfigured;
  $('configuration-error').textContent = state.configurationError;
  const noticeText = state.notice || selected?.error || selected?.resourceWarning || (state.runtime.status==='ready'&&!state.writesEnabled ? '只读模式 · 登录与文件选择由本人操作' : '');
  $('notice').textContent = noticeText;
  $('notice').title = noticeText;
  $('notice').hidden = !noticeText;
  document.documentElement.style.setProperty('--desktop-content-top', noticeText ? '144px' : '112px');
}
window.addEventListener('DOMContentLoaded', () => {
  $('startup-diagnostics').onclick = () => command({action:'home'});
  for(const action of ['open','close','plugin','profile','scan','undo']) $('filler-'+action).onclick=()=>command({action:'filler-'+action});
  for(const action of ['scan','undo','frame-allow',...Object.keys(capabilityFor).filter(action=>!['profile-save','profile-create','profile-select','profile-rename','profile-delete',
    'custom-save','application-save','application-cancel','application-retry','application-correct'].includes(action))]) {
    const button=$('filler-'+action);if(button)button.onclick=()=>fillerAction(action);
  }
  for(const tab of ['scan','profile','applications']) {
    $('filler-tab-'+tab).onclick=()=>{setSidebarTab(tab);if(currentState)renderFiller(currentState);};
    $('filler-tab-'+tab).onkeydown=event=>{const names=['scan','profile','applications'];let next=names.indexOf(sidebarTab);if(event.key==='ArrowRight')next=(next+1)%3;else if(event.key==='ArrowLeft')next=(next+2)%3;else if(event.key==='Home')next=0;else if(event.key==='End')next=2;else return;event.preventDefault();setSidebarTab(names[next],true);};
  }
  $('filler-confirm').onclick=()=>fillerAction('fill',{scanId:fillerScanId,fieldIds:[...fillerSelected]});
  $('filler-one-click').onclick=oneClickFill;
  $('filler-select-all').onchange=()=>{
    fillerSelected.clear();for(const field of currentState?.filler?.fields||[]) if($('filler-select-all').checked&&field.fillable!==false&&!field.blocked)fillerSelected.add(field.fieldId);
    for(const row of $('filler-fields').children) row.querySelector('input').checked=fillerSelected.has(row.dataset.fieldId);renderFiller(currentState);
  };
  $('filler-profile-save').onclick=async()=>{
    if(!profileDraft||!profileDirty||profileConflict||$('filler-profile-editor').querySelectorAll(':invalid').length>0) return;
    if(new TextEncoder().encode(JSON.stringify(profileDraft)).length>1048576){$('filler-ui-error').textContent='资料超过 1 MiB，未提交保存。';$('filler-ui-error').hidden=false;return;}
    const payload={profile:JSON.parse(JSON.stringify(profileDraft))};if(Number.isSafeInteger(profileVersion)&&profileVersion>=0)payload.expectedVersion=profileVersion;
    await fillerAction('profile-save',payload);
  };
  $('filler-profile-select').onchange=()=>{
    const profileId=$('filler-profile-select').value;
    if(!profileId||profileDirty||profileConflict||!Number.isSafeInteger(profileVersion))return renderFiller(currentState);
    if(profileId!==currentState?.filler?.profile?.activeProfileId)return fillerAction('profile-select',{profileId,expectedVersion:profileVersion});
  };
  $('filler-profile-name').oninput=()=>renderFiller(currentState);
  $('filler-profile-create').onclick=()=>{
    const name=$('filler-profile-name').value;
    if(!validProfileName(name)||profileDirty||profileConflict||!Number.isSafeInteger(profileVersion))return;
    return fillerAction('profile-create',{name:name.normalize('NFC').trim(),expectedVersion:profileVersion});
  };
  $('filler-profile-rename').onclick=()=>{
    const name=$('filler-profile-name').value,profileId=currentState?.filler?.profile?.activeProfileId;
    if(!profileId||!validProfileName(name)||profileDirty||profileConflict||!Number.isSafeInteger(profileVersion))return;
    return fillerAction('profile-rename',{profileId,name:name.normalize('NFC').trim(),expectedVersion:profileVersion});
  };
  $('filler-profile-delete-confirm').onchange=()=>renderFiller(currentState);
  $('filler-profile-delete').onclick=async()=>{
    const profileId=currentState?.filler?.profile?.activeProfileId;
    if(!profileId||!$('filler-profile-delete-confirm').checked||profileDirty||profileConflict||!Number.isSafeInteger(profileVersion))return;
    if(await fillerAction('profile-delete',{profileId,expectedVersion:profileVersion}))$('filler-profile-delete-confirm').checked=false;
    renderFiller(currentState);
  };
  $('filler-profile-discard').onclick=()=>{profileDirty=false;profileConflict=false;profileSource='';profileDraft=undefined;renderFiller(currentState);renderProfileEditor();};
  for(const name of ['field','question','answer','scope']) $('filler-custom-'+name).oninput=()=>renderFiller(currentState);
  $('filler-custom-field').onchange=()=>{const field=(currentState.filler.fields||[]).find(f=>f.fieldId===$('filler-custom-field').value);if(field)$('filler-custom-question').value=field.label||'';renderFiller(currentState);};
  $('filler-custom-save').onclick=async()=>{
    const fieldId=$('filler-custom-field').value,field=(currentState?.filler?.fields||[]).find(item=>item.fieldId===fieldId),identity=selectionIdentity(field?.fieldId||'');
    const payload={question:$('filler-custom-question').value.trim(),answer:$('filler-custom-answer').value,scope:$('filler-custom-scope').value};if(editingAnswerId)payload.answerId=editingAnswerId;
    if(await fillerAction('custom-save',payload)) {
      editingAnswerId='';$('filler-custom-question').value='';$('filler-custom-answer').value='';renderFiller(currentState);
      if(fieldId)await fillerAction('scan',{},identity?[{...identity,label:field?.label||''}]:undefined);
    }
  };
  $('filler-retry-failed').onclick=()=>{const targets=retryFailures.filter(item=>item.frameId&&item.rawId);if(targets.length)return fillerAction('scan',{},targets);};
  $('filler-stop').onclick=async()=>{
    const f=currentState?.filler||{},operation=f.busyOperation?.type||(typeof f.busyOperation==='string'?f.busyOperation:'');
    if(oneClickRun&&!oneClickRun.cancelled) {
      oneClickRun.cancelled=true;oneClickRun.reason='已取消后续步骤；当前操作可能部分生效，页面不会刷新。';renderFiller(currentState);
    }
    if(stopPending||!['filling','preparing'].includes(operation)||f.busyOperation?.cancelRequested||
      !Array.isArray(f.supportedActions)||!f.supportedActions.includes('filler-stop'))return;
    stopPending=true;stopFeedback='正在向本地填写服务发送停止请求。';renderFiller(currentState);
    const result=await command({action:'filler-stop'});
    stopPending=false;stopFeedback=result?'':'停止请求未被确认，请查看当前操作状态。';renderFiller(currentState);
  };
  for(const name of ['company','title','url','city']) $('filler-application-'+name).oninput=()=>{
    if(name==='title'&&selectedApplications().length===1)selectedApplications()[0].title=$('filler-application-title').value;
    $('filler-application-confirm').checked=false;renderFiller(currentState);
  };
  $('filler-candidate-all').onchange=()=>{for(const item of applicationDrafts)if(!['saved','queued'].includes(item.result))item.selected=$('filler-candidate-all').checked;refreshApplicationSelection();};
  $('filler-application-confirm').onchange=()=>renderFiller(currentState);
  $('filler-application-save').onclick=async()=>{
    if($('filler-application-save').disabled)return;
    const payload={company:$('filler-application-company').value.trim(),title:$('filler-application-title').value.trim(),recordUrl:$('filler-application-url').value.trim()};
    if($('filler-application-city').value.trim())payload.city=$('filler-application-city').value.trim();
    const selected=selectedApplications(),source=applicationDrafts;
    if(selected.length>1){
      const result=await fillerAction('application-save-batch',{records:selected.map(item=>({...payload,title:item.title.trim()}))});
      if(result&&applicationDrafts===source){
        for(const item of result.filler?.application?.batchResults||[]){
          const draft=selected[item.index];if(!draft)continue;
          draft.result=item.status;draft.error=item.error;
          if(item.status==='saved'||item.status==='queued')draft.selected=false;
        }
        refreshApplicationSelection();
      }
      return result;
    }
    const result=await fillerAction('application-save',payload);
    if(result&&selected.length===1&&applicationDrafts===source){
      const draft=selected[0],queue=result.filler?.application?.queue||[];
      draft.result=queue.some(item=>item.registration?.company===payload.company&&item.registration?.title===payload.title)?'queued':'saved';
      draft.selected=false;refreshApplicationSelection();
    }
    return result;
  };
  $('filler-attachment-upload').onclick=()=>fillerAction('attachment-upload',{scanId:fillerScanId,fieldId:$('filler-upload-target').value});
  renderProfileEditor();setSidebarTab('scan');
  for (const action of ['home', 'workbench', 'back', 'forward', 'reload', 'clear-site', 'hide', 'quit', 'capture', 'use-capture']) $(action).onclick = () => command({ action });
  $('address-form').onsubmit = event => { event.preventDefault(); command({ action: 'open', url: $('address').value }); };
  window.desktop.onState(render); window.desktop.state().then(render);
});
