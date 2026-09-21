'use strict';
globalThis.fillerExecutor = new (require('../dist/filler-frame-executor').FillerFrameExecutor)();
globalThis.FillerService = require('../dist/filler-service').FillerService;
globalThis.fillerAdapter = require('../../../packages/desktop_filler/index.cjs');
require('../../../tests/desktop_filler/electron-fixture.cjs');
