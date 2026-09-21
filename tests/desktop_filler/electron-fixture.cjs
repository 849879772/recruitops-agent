'use strict';
// Anonymous test process only. The parent supplies a fresh temporary userData path.
const { app, BrowserWindow } = require('electron');
const http = require('node:http');
app.setPath('userData', process.argv[2]);
app.commandLine.appendSwitch('disable-gpu');
app.whenReady().then(async () => {
  const serve = (request, response) => {
    response.setHeader('Content-Type', 'text/html');
    response.end('<!doctype html><html><body>Anonymous fixture</body></html>');
  };
  const server = http.createServer(serve), cross = http.createServer(serve);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  await new Promise(resolve => cross.listen(0, '127.0.0.1', resolve));
  globalThis.fixtureCrossOrigin = 'http://127.0.0.1:' + cross.address().port;
  app.on('will-quit', () => { server.close(); cross.close(); });
  const window = new BrowserWindow({ show: false, webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false } });
  window.webContents.session.webRequest.onBeforeRequest((details, callback) => {
    callback({ cancel: !details.url.startsWith('http://127.0.0.1:') && !details.url.startsWith('about:') });
  });
  await window.loadURL('http://127.0.0.1:' + server.address().port + '/form');
});
