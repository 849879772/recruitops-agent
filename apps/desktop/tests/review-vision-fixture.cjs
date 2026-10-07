const {app,BrowserWindow}=require('electron');

function internalListFixtureHtml(){
  return '<!doctype html><style>body{margin:0;height:100vh;overflow:hidden}header{height:60px}#records{height:calc(100vh - 70px);width:85%;overflow-y:auto;background:#eee}.card{height:230px;box-sizing:border-box;padding:12px;background:#3574ea}.card:nth-child(2){background:#00aa66}.card:nth-child(3){background:#ef6633}.card h2{margin:0;height:35px}.card .date{margin-top:145px}.hidden{display:none;overflow:auto;height:300px}</style>'+
    '<header><input value="private-account"><span>person@example.test 13912345678</span></header>'+
    '<main id="records"><article class="card"><h2>岗位一 软件工程师</h2><p class="date">投递简历 2026-10-01</p></article>'+
    '<article class="card"><h2>岗位二 测试工程师</h2><p class="date">投递简历 2026-10-02</p></article>'+
    '<article class="card" id="last-card"><h2>岗位三 AI开发工程师</h2><p class="date" id="last-date">投递简历 2026-10-05</p></article></main>'+
    '<nav style="position:fixed;right:0;top:70px;overflow:auto;height:200px;width:12%"><div style="height:1000px">岗位导航 工程师 投递简历</div></nav>'+
    '<div class="hidden"><div style="height:1000px">岗位 工程师 投递简历</div></div>';
}

async function waitForRenderedSurface(wc){
  const deadline=Date.now()+5000;
  let layoutTimer;
  const document=await Promise.race([
    wc.executeJavaScript('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve({readyState:document.readyState,width:innerWidth,height:innerHeight,scroll:scrollY}))))'),
    new Promise((_resolve,reject)=>{layoutTimer=setTimeout(()=>reject(new Error('fixture_layout_ready_timeout')),5000);}),
  ]).finally(()=>clearTimeout(layoutTimer));
  const frame=await new Promise((resolve,reject)=>{
    const cleanup=()=>{clearTimeout(timer);wc.removeListener('paint',paint);wc.removeListener('destroyed',destroyed);};
    const destroyed=()=>{cleanup();reject(new Error('fixture_renderer_destroyed_before_paint'));};
    const paint=(_event,_rect,image)=>{
      if(image.isEmpty())return;
      const size=image.getSize();
      if(size.width<document.width||size.height<document.height)return;
      cleanup();resolve(size);
    };
    const timer=setTimeout(()=>{cleanup();reject(new Error('fixture_offscreen_paint_timeout'));},Math.max(1,deadline-Date.now()));
    wc.on('paint',paint);wc.once('destroyed',destroyed);
    // loadURL resolves before the hidden compositor necessarily has a copyable
    // surface. Require a real offscreen repaint, not a delay or mocked bitmap.
    wc.invalidate();
  });
  return {document,frame};
}

app.whenReady().then(()=>{
  globalThis.captureFixture={BrowserWindow,waitForRenderedSurface,internalListFixtureHtml,...require('../dist/review-vision')};
});
