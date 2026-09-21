'use strict';
const { BrowserWindow } = require('electron');
// Run the real main entry without ever presenting a native window.
BrowserWindow.prototype.show = function () {};
BrowserWindow.prototype.focus = function () {};
globalThis.reviewFixture = require('../dist/main');
