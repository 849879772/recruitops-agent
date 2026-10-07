const {app, BrowserWindow} = require('electron');
app.on('window-all-closed', () => {});
app.whenReady().then(() => {
  globalThis.detailsFixture = {
    BrowserWindow,
    ...require('../dist/review-record-details'),
    ...require('../dist/browser-service'),
  };
});
