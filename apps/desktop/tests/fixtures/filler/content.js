chrome.runtime.onMessage.addListener((message,_sender,reply)=>{
  if(message.type==='RESUME_SCAN') {
    const el=document.querySelector('#name');
    el.setAttribute('data-local-resume-field-id','fixture-name');
    reply({ok:true,totalFields:1,emptyFields:1,matches:[{fieldId:'fixture-name',key:'name',label:'姓名',controlKind:'text',value:message.resume.name}]});
  }
  if(message.type==='RESUME_FILL') {
    for(const field of message.assignments) {
      const el=document.querySelector('[data-local-resume-field-id="'+field.fieldId+'"]');
      fixtureUndo.push({el,value:el.value});el.value=field.value;
      el.dispatchEvent(new Event('input',{bubbles:true}));
    }
    reply({ok:true,filled:message.assignments.length,failed:[]});
  }
  if(message.type==='RESUME_UNDO') {
    const restored=fixtureUndo.length;
    for(const entry of fixtureUndo.splice(0).reverse()) entry.el.value=entry.value;
    reply({ok:true,restored,failed:[]});
  }
});
