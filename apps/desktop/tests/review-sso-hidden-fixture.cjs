'use strict';
const {app,session}=require('electron');
// Every HTTPS request is answered in process. DNS is synthetic too: no live
// recruitment/authentication endpoint or real browser profile is accessed.
require('node:dns/promises').lookup=async()=>[{address:'203.0.113.20',family:4}];
globalThis.ssoFixture={requests:[],extractions:[]};
app.on('web-contents-created',(_event,wc)=>{
  const execute=wc.executeJavaScriptInIsolatedWorld.bind(wc);
  wc.executeJavaScriptInIsolatedWorld=(world,...args)=>{
    if(world===1004)globalThis.ssoFixture.extractions.push(wc.getURL());
    return execute(world,...args);
  };
});
app.whenReady().then(()=>{
  session.fromPartition('persist:recruitment').protocol.handle('https',request=>{
    const url=new URL(request.url);globalThis.ssoFixture.requests.push(request.url);
    const recruitment=url.hostname==='campus-talent.alibaba.com'?'https://campus-talent.alibaba.com':'https://career.huawei.com';
    const auth=url.hostname==='campus-talent.alibaba.com'?'https://mozi-login.alibaba-inc.com':'https://uniportal.huawei.com';
    const redirect=target=>new Response('',{status:302,headers:{Location:target}});
    const html=body=>new Response('<!doctype html><title>Recruitment</title>'+body,{headers:{'Content-Type':'text/html;charset=utf-8'}});
    if(url.hostname==='join.tencentmusic.com' && url.pathname==='/tme-downgrade') {
      return redirect('http://join.tencentmusic.com/applications/?token=private');
    }
    if(url.hostname==='mozi-login.alibaba-inc.com'||url.hostname==='uniportal.huawei.com') {
      const source=url.hostname==='mozi-login.alibaba-inc.com'?'https://campus-talent.alibaba.com':'https://career.huawei.com';
      if(url.pathname==='/return')return html(`<script>setTimeout(()=>location.replace('${source}/records'),200)</script>`);
      if(url.pathname==='/repeat')return html(`<script>setTimeout(()=>location.replace('${source}/client-sso-repeat'),200)</script>`);
      if(url.pathname==='/escape')return redirect('https://evil.example/callback?token=private');
      if(url.pathname==='/loop')return redirect(url.origin+'/loop?step='+(Number(url.searchParams.get('step'))+1));
      // Deliberately include a plausible record on the auth page: it must never
      // be extracted, even though the top-level destination is a trusted IdP.
      return html('<main>登录 / Login</main><table><tr><th>岗位名称</th><th>当前状态</th></tr><tr><td>伪造岗位</td><td>面试</td></tr></table>');
    }
    if(url.pathname==='/sso-return')return redirect(auth+'/return?token=private');
    if(url.pathname==='/sso-stay')return redirect(auth+'/login?token=private&email=person@example.test');
    if(url.pathname==='/sso-escape')return redirect(auth+'/escape');
    if(url.pathname==='/sso-loop')return redirect(auth+'/loop?step=0');
    if(url.pathname==='/client-sso-return')return html(`<main>Loading applications</main><script>setTimeout(()=>location.replace('${auth}/return'),200)</script>`);
    if(url.pathname==='/client-sso-repeat')return html(`<main>Loading applications</main><script>setTimeout(()=>location.replace('${auth}/repeat'),200)</script>`);
    if(url.pathname==='/records')return html('<table><thead><tr><th>岗位名称</th><th>当前状态</th><th>投递日期</th></tr></thead>'+
      '<tbody><tr><td>测试开发工程师</td><td>申请成功</td><td>2026-09-14</td></tr></tbody></table>');
    return html('<main>页面不存在</main>');
  });
  session.fromPartition('persist:recruitment').protocol.handle('http',request=>{
    globalThis.ssoFixture.requests.push(request.url);
    return new Response('Unexpected HTTP fixture request',{status:400});
  });
});
require('./review-hidden-fixture.cjs');
