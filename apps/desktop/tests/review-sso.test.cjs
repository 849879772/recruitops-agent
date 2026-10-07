const {test}=require('node:test');
const assert=require('node:assert/strict');
const {EventEmitter}=require('node:events');
const {ReviewNavigationPolicy,reviewNavigationDiagnostics}=require('../dist/review-readiness');
const {waitForReviewAuthentication}=require('../dist/browser-service');

test('official SSO allows exact HTTPS pairs only and never expands the evidence origin',()=>{
  for(const [source,auth] of [
    ['https://campus-talent.alibaba.com','https://mozi-login.alibaba-inc.com'],
    ['https://career.huawei.com','https://uniportal.huawei.com']
  ]) {
    const policy=new ReviewNavigationPolicy(source+'/applications');
    assert.equal(policy.note(auth+'/login?token=secret&email=person@example.test','will_redirect'),true);
    assert.equal(policy.origin,source);
    assert.equal(policy.awaitingAuthentication,true);
    assert.equal(policy.note(auth+'/login?token=secret&email=person@example.test','will_navigate'),true);
    assert.equal(policy.diagnostic(auth+'/login').authNavigation.hops,1,'duplicate event is not a second hop');
    assert.equal(policy.note(source+'/applications','will_redirect'),true);
    const diagnostic=policy.diagnostic(source+'/applications');
    assert.equal(diagnostic.authNavigation.returnedToRecruitment,true);
    assert.equal(diagnostic.authNavigation.hops,2);
    assert.ok(!JSON.stringify(diagnostic).includes('secret'));
    assert.ok(!JSON.stringify(diagnostic).includes('person@'));
    for(const target of [auth.replace('https:','http:'),auth+':444',auth+'.evil.test',
      auth.replace('://','://user:password@'),'https://other.example/login','blob:'+source+'/opaque']) {
      const denied=new ReviewNavigationPolicy(source+'/applications');
      assert.equal(denied.note(target,'will_redirect'),false,target);
      assert.equal(denied.note(source+'/applications','will_redirect'),false,'a refusal cannot be erased');
    }
  }
  assert.equal(new ReviewNavigationPolicy('https://unrelated.example/applications')
    .note('https://uniportal.huawei.com/login','will_redirect'),false);
  assert.equal(reviewNavigationDiagnostics('https://career.huawei.com/applications',
    'https://uniportal.huawei.com/login').ssoCandidate,true);
});

test('official authentication loops are bounded and third-party hops remain denied',()=>{
  const policy=new ReviewNavigationPolicy('https://career.huawei.com/applications');
  for(let i=0;i<8;i++)assert.equal(policy.note('https://uniportal.huawei.com/login?step='+i,'will_redirect'),true);
  assert.equal(policy.note('https://uniportal.huawei.com/login?step=8','will_redirect'),false);
  assert.equal(policy.diagnostic('https://uniportal.huawei.com/login').reason,'official_sso_hop_limit');
  const sameUrlLoop=new ReviewNavigationPolicy('https://career.huawei.com/applications');
  for(let i=0;i<8;i++)assert.equal(sameUrlLoop.note('https://uniportal.huawei.com/login','will_redirect'),true);
  assert.equal(sameUrlLoop.note('https://uniportal.huawei.com/login','will_redirect'),false);
  const escape=new ReviewNavigationPolicy('https://career.huawei.com/applications');
  escape.note('https://uniportal.huawei.com/login','will_redirect');
  assert.equal(escape.note('https://attacker.example/callback','will_redirect'),false);
});

test('an authorized SSO observation race gets one recovery, never an unbounded retry loop',()=>{
  const source='https://campus-talent.alibaba.com/applications';
  const policy=new ReviewNavigationPolicy(source);
  assert.equal(policy.claimObservationRecovery(),false,'no SSO evidence means no auth recovery');
  policy.note('https://mozi-login.alibaba-inc.com/login','will_redirect');
  policy.note(source,'will_redirect');
  assert.equal(policy.claimObservationRecovery(),true);
  assert.equal(policy.claimObservationRecovery(),false);
  assert.equal(policy.diagnostic(source).reason,'official_sso_reobservation_limit');
  assert.equal(policy.diagnostic(source).reobservationCount,1);
  const denied=new ReviewNavigationPolicy(source);
  denied.note('https://unapproved.example/login','will_redirect');
  assert.equal(denied.claimObservationRecovery(),false);
});

test('real-run navigation causes stay precise without granting more origins or exposing URL tokens',()=>{
  const lenovo=new ReviewNavigationPolicy('https://talent.lenovo.com.cn/account/applications?token=private');
  assert.equal(lenovo.note('https://passport.lenovo.com/auth/login?code=secret','will_navigate'),false);
  const blocked=lenovo.diagnostic('https://talent.lenovo.com.cn/account/applications?token=private');
  assert.equal(blocked.reason,'will_navigate_cross_origin');
  assert.equal(blocked.restriction,'unapproved_origin');
  assert.equal(blocked.ssoCandidate,true,'auth-looking is a diagnostic, never permission');
  const alibaba=new ReviewNavigationPolicy('https://campus-talent.alibaba.com/campus/applications');
  alibaba.note('https://mozi-login.alibaba-inc.com/login','will_redirect');
  assert.equal(alibaba.note('http://campus-talent.alibaba.com/campus/applications?code=secret','will_redirect'),false);
  const downgraded=alibaba.diagnostic('about:blank');
  assert.equal(downgraded.reason,'will_redirect_cross_origin');
  assert.equal(downgraded.restriction,'https_downgrade');
  for(const item of [blocked,downgraded]) {
    assert.ok(!JSON.stringify(item).includes('private'));
    assert.ok(!JSON.stringify(item).includes('secret'));
  }
});

test('TME HTTPS downgrade remains blocked before any application extraction',()=>{
  const source='https://join.tencentmusic.com/applications?token=private';
  const policy=new ReviewNavigationPolicy(source);
  assert.equal(policy.note('http://join.tencentmusic.com/applications/?code=secret','will_redirect'),false);
  const diagnostic=policy.diagnostic(source);
  assert.equal(diagnostic.reason,'will_redirect_cross_origin');
  assert.equal(diagnostic.restriction,'https_downgrade');
  assert.equal(diagnostic.ssoCandidate,false);
  assert.equal(diagnostic.authNavigation,undefined);
  assert.ok(!JSON.stringify(diagnostic).includes('private'));
  assert.ok(!JSON.stringify(diagnostic).includes('secret'));
});

test('quiet official SSO may return after ten seconds, but the fifteen-second cap never expands',async t=>{
  const original='https://campus-talent.alibaba.com/applications';
  const auth='https://mozi-login.alibaba-inc.com/ssoLogin.htm?token=private';
  const policy=new ReviewNavigationPolicy(original);policy.note(auth,'will_redirect');
  let clock=100000,url=auth;
  t.mock.method(Date,'now',()=>clock);
  const wc={isDestroyed:()=>false,isLoadingMainFrame:()=>false,getURL:()=>{
    clock+=500;
    if(clock>=111000 && url===auth){url=original;policy.note(url,'will_redirect');}
    return url;
  }};
  await waitForReviewAuthentication(wc,policy,115000);
  const diagnostic=policy.diagnostic(url);
  assert.equal(diagnostic.authWait.outcome,'returned');
  assert.ok(diagnostic.authWait.elapsedMs>10000);
  assert.ok(diagnostic.authWait.elapsedMs<15000);
  assert.equal(diagnostic.authWait.budgetMs,15000);
  assert.equal(diagnostic.authWait.progressCount,1);
  assert.equal(diagnostic.authNavigation.returnedToRecruitment,true);
});

test('Ali and Huawei stable authentication timeouts retain precise passive-return diagnostics',async t=>{
  let clock=200000;
  t.mock.method(Date,'now',()=>clock);
  for(const [original,auth,provider] of [
    ['https://campus-talent.alibaba.com/applications','https://mozi-login.alibaba-inc.com/ssoLogin.htm?token=private','alibaba'],
    ['https://career.huawei.com/applications','https://uniportal.huawei.com/login?code=secret','huawei'],
  ]) {
    const policy=new ReviewNavigationPolicy(original);policy.note(auth,'will_redirect');
    const wc={isDestroyed:()=>false,isLoadingMainFrame:()=>false,getURL:()=>{clock+=500;return auth;}};
    await assert.rejects(waitForReviewAuthentication(wc,policy,clock+30000),/^Error: authentication_recovery_timeout$/);
    const diagnostic=policy.diagnostic(auth);
    assert.equal(diagnostic.authNavigation.provider,provider);
    assert.equal(diagnostic.authNavigation.returnedToRecruitment,false);
    assert.equal(diagnostic.authNavigation.hops,1);
    assert.equal(diagnostic.authWait.outcome,'timeout');
    assert.equal(diagnostic.authWait.budgetMs,15000);
    assert.equal(diagnostic.authWait.progressCount,0);
    assert.ok(diagnostic.authWait.elapsedMs>=15000);
    assert.ok(!JSON.stringify(diagnostic).includes('private'));
    assert.ok(!JSON.stringify(diagnostic).includes('secret'));
  }
});

test('passive authentication waits for original origin return without executing any page code',async()=>{
  const original='https://career.huawei.com/applications';
  const auth='https://uniportal.huawei.com/login?token=secret';
  const policy=new ReviewNavigationPolicy(original);
  policy.note(auth,'will_redirect');
  let url=auth,scriptCalls=0;
  const wc=Object.assign(new EventEmitter(),{isDestroyed:()=>false,isLoadingMainFrame:()=>false,
    getURL:()=>url,executeJavaScriptInIsolatedWorld:()=>{scriptCalls++;throw Error('must not extract');}});
  setTimeout(()=>{policy.note(original,'will_redirect');url=original;},20);
  await waitForReviewAuthentication(wc,policy,Date.now()+500,undefined,300);
  assert.equal(scriptCalls,0);
  assert.equal(policy.diagnostic(url).authNavigation.returnedToRecruitment,true);
  assert.equal(policy.diagnostic(url).authWait.outcome,'returned');
});

test('unobserved authentication reports recovery timeout; cancellation and foreign redirect stay distinct',async()=>{
  const original='https://career.huawei.com/applications',auth='https://uniportal.huawei.com/login';
  const policy=new ReviewNavigationPolicy(original);policy.note(auth,'will_redirect');
  const wc={isDestroyed:()=>false,isLoadingMainFrame:()=>false,getURL:()=>auth};
  await assert.rejects(waitForReviewAuthentication(wc,policy,Date.now()+200,undefined,35),/^Error: authentication_recovery_timeout$/);
  assert.equal(policy.diagnostic(auth).authWait.outcome,'timeout');
  const controller=new AbortController();controller.abort();
  await assert.rejects(waitForReviewAuthentication(wc,policy,Date.now()+200,controller.signal),/browser_cancelled/);
  assert.equal(policy.diagnostic(auth).authWait.outcome,'cancelled');
  policy.note('https://foreign.example/login','will_redirect');
  await assert.rejects(waitForReviewAuthentication(wc,policy,Date.now()+200),/browser_navigation_changed/);
  assert.equal(policy.diagnostic(auth).authWait.outcome,'navigation_denied');
});

test('a delayed passive SSO return survives the former three-second cutoff', {timeout:6000}, async()=>{
  const original='https://career.huawei.com/applications',auth='https://uniportal.huawei.com/callback';
  const policy=new ReviewNavigationPolicy(original);policy.note(auth,'will_redirect');
  let url=auth;
  const wc={isDestroyed:()=>false,isLoadingMainFrame:()=>false,getURL:()=>url};
  const timer=setTimeout(()=>{policy.note(original,'will_redirect');url=original;},3250);
  try { await waitForReviewAuthentication(wc,policy,Date.now()+5000); }
  finally {clearTimeout(timer);}
  assert.equal(policy.diagnostic(url).authNavigation.returnedToRecruitment,true);
});

test('SSO never extends beyond the operation deadline or waits on normal recruitment pages',async()=>{
  const original='https://career.huawei.com/applications',auth='https://uniportal.huawei.com/callback';
  const policy=new ReviewNavigationPolicy(original);policy.note(auth,'will_redirect');
  const started=Date.now();
  await assert.rejects(waitForReviewAuthentication({isDestroyed:()=>false,isLoadingMainFrame:()=>true,getURL:()=>auth},
    policy,started+40),/authentication_recovery_timeout/);
  assert.ok(Date.now()-started<200);
  const normal=new ReviewNavigationPolicy(original);
  await waitForReviewAuthentication({isDestroyed:()=>false,isLoadingMainFrame:()=>false,getURL:()=>original},normal,Date.now()+500);
  assert.equal(normal.authVisited,false);
});

test('real hidden browser follows only narrow passive SSO and never extracts authentication pages',{timeout:35000},async t=>{
  const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
  const {_electron}=require('playwright');
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'recruitops-sso-anonymous-'));
  const env=Object.fromEntries(['SystemRoot','WINDIR','PATH','TEMP','TMP','COMSPEC','APPDATA','LOCALAPPDATA','USERPROFILE']
    .filter(key=>process.env[key]).map(key=>[key,process.env[key]]));
  let application;
  t.after(async()=>{if(application)await application.close();fs.rmSync(root,{recursive:true,force:true,maxRetries:5});});
  application=await _electron.launch({args:[path.join(__dirname,'review-sso-hidden-fixture.cjs')],
    env:{...env,RECRUITOPS_DESKTOP_TEST:'1',RECRUITOPS_DESKTOP_DATA_DIR:root,RECRUITOPS_DESKTOP_TEST_RUNTIME:'filler'}});
  const shell=await application.firstWindow();await shell.waitForLoadState();
  await shell.waitForFunction(async()=>!!(await window.desktop.state()).filler.capabilities.persistentProfile);
  for(const source of ['https://campus-talent.alibaba.com','https://career.huawei.com']) {
    for(const route of ['/sso-return','/client-sso-return','/client-sso-repeat','/sso-stay','/sso-escape','/sso-loop']) {
      const outcome=await application.evaluate(async({BrowserWindow},{source,route})=>{
        try {
          const result=await globalThis.reviewFixture.reviewPage(source+route,'official-sso',['fixture-app'],undefined,Date.now()+7000);
          return {result,visible:BrowserWindow.getAllWindows().some(window=>window.isVisible())};
        } catch(error) {return {code:error.message,navigation:error.navigation,
          visible:BrowserWindow.getAllWindows().some(window=>window.isVisible())};}
      },{source,route});
      assert.equal(outcome.visible,false);
      if(route.endsWith('return')) {
        assert.equal(outcome.result?.review_readiness,'records',JSON.stringify(outcome));
        assert.equal(new URL(outcome.result.result.page.page_url).origin,source);
        assert.equal(outcome.result.result.navigation_diagnostics.authNavigation.returnedToRecruitment,true);
        assert.equal(outcome.result.result.database_updated,false);
      } else {
        assert.equal(outcome.code,route==='/sso-stay'?'authentication_recovery_timeout':'browser_navigation_changed',JSON.stringify(outcome));
        assert.ok(!JSON.stringify(outcome).includes('private'));
        assert.ok(!JSON.stringify(outcome).includes('person@'));
        if(route==='/sso-loop')assert.equal(outcome.navigation.reason,'official_sso_hop_limit');
        if(route==='/client-sso-repeat') {
          assert.equal(outcome.navigation.reason,'official_sso_reobservation_limit');
          assert.equal(outcome.navigation.reobservationCount,1);
          assert.equal(outcome.navigation.phase,'observation');
        }
        if(route==='/sso-stay') {
          assert.equal(outcome.navigation.authNavigation.returnedToRecruitment,false);
          assert.equal(outcome.navigation.authWait.outcome,'timeout');
          assert.equal(outcome.navigation.phase,'initial_load');
        }
      }
    }
  }
  const downgraded=await application.evaluate(async()=>{
    try {
      await globalThis.reviewFixture.reviewPage('https://join.tencentmusic.com/tme-downgrade','tme-fixture',['fixture-app'],undefined,Date.now()+5000);
      return {unexpectedSuccess:true};
    } catch(error) {return {code:error.message,navigation:error.navigation,lastObservation:error.lastObservation};}
  });
  assert.equal(downgraded.code,'browser_navigation_changed');
  assert.equal(downgraded.navigation.restriction,'https_downgrade');
  assert.equal(downgraded.navigation.phase,'initial_load');
  assert.equal(downgraded.lastObservation.pageState,'not_observed');
  assert.equal(downgraded.lastObservation.recordCount,0);
  const observed=await application.evaluate(()=>globalThis.ssoFixture);
  assert.ok(observed.requests.some(url=>url.startsWith('https://uniportal.huawei.com')));
  assert.ok(observed.requests.some(url=>url.startsWith('https://mozi-login.alibaba-inc.com')));
  assert.ok(!observed.requests.some(url=>url.startsWith('https://evil.example')),'third domain must be rejected before request');
  assert.ok(!observed.requests.some(url=>url.startsWith('http://join.tencentmusic.com')),'HTTPS downgrade must be rejected before HTTP request');
  assert.ok(observed.extractions.length>0);
  assert.ok(observed.extractions.every(url=>['campus-talent.alibaba.com','career.huawei.com'].includes(new URL(url).hostname)),
    'no status extractor can execute at the IdP origin');
});
