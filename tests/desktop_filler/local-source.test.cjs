'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const path=require('node:path');
const {createRequire}=require('node:module');
const api=require('../../packages/desktop_filler/index.cjs');

const folder=process.env.RECRUITOPS_TEST_LOCAL_FILLER_DIR;
test('opt-in actual local engine scans synthetic form, fills confirmed field, undoes',
  {skip:!folder},async()=>{
    // Explicit opt-in only: import the local project's jsdom dependency, never its profile.
    const {JSDOM}=createRequire(path.join(folder,'package.json'))('jsdom');
    const dom=new JSDOM('<form><label for="name">姓名</label><input id="name" name="name"><label for="code">验证码</label><input id="code" name="otp"><button type="submit">提交</button></form>',
      {url:'https://fixture.example.test/apply',runScripts:'outside-only',pretendToBeVisual:true});
    try {
      const win=dom.window;
      win.CSS={escape:value=>value};
      win.HTMLElement.prototype.getClientRects=function(){return [{width:100,height:20}];};
      win.HTMLElement.prototype.getBoundingClientRect=function(){return {width:100,height:20,top:0,left:0,bottom:20,right:100};};
      win.HTMLElement.prototype.scrollIntoView=function(){};
      win.HTMLElement.prototype.animate=function(){return {finished:Promise.resolve()};};
      const bundle=api.loadLocalFiller(folder);
      const profile={basic:{fullName:'Synthetic Candidate'}};
      const before=win.document.querySelector('#name').value;
      const scan=await win.eval(api.buildScanScript(bundle,profile));
      assert.equal(win.document.querySelector('#name').value,before);
      assert.equal(scan.ok,true);
      assert.ok(scan.matches.length>0,'synthetic name should map through actual core');
      assert.equal(scan.matches.some(m=>/验证码|otp/.test(m.label+' '+m.key)),false);
      const result=await win.eval(api.buildFillScript(bundle,{scanId:scan.scanId,fieldIds:[scan.matches[0].fieldId],confirmed:true}));
      assert.equal(result.filled,1);
      assert.equal(win.document.querySelector('#name').value,'Synthetic Candidate');
      const undo=await win.eval(api.buildUndoScript(bundle));
      assert.equal(undo.restored,1);
      assert.equal(win.document.querySelector('#name').value,before);
      win.location.hash='next-step';
      await assert.rejects(win.eval(api.buildFillScript(bundle,{scanId:scan.scanId,fieldIds:[scan.matches[0].fieldId],confirmed:true})),/navigation_changed/);
      const rescanned=await win.eval(api.buildScanScript(bundle,profile));
      assert.equal(rescanned.ok,true);
      assert.ok(rescanned.matches.length>0);
      await assert.rejects(win.eval(api.buildUndoScript(bundle)),/nothing_to_undo/);
    }finally{dom.window.close();}
  });
