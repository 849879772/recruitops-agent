const { defineConfig } = require('@playwright/test');
module.exports = defineConfig({ testDir: './tests', testMatch: ['shell.spec.cjs','packaged.spec.cjs'], workers: 1, timeout: 60000, reporter: 'list', outputDir: 'test-results' });
