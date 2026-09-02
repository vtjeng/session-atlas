const { defineConfig } = require('playwright/test');

module.exports = defineConfig({
  testDir: './tests/visual',
  testMatch: '**/*.spec.js',
  snapshotPathTemplate: 'docs/images/{arg}{ext}',
  outputDir: 'test-results',
  reporter: process.env.CI
    ? [['github'], ['html', { open: 'never' }]]
    : [['list'], ['html', { open: 'never' }]],
  use: {
    browserName: 'chromium',
    javaScriptEnabled: false,
    viewport: { width: 1440, height: 900 },
  },
  expect: {
    toMatchSnapshot: {
      maxDiffPixels: 0,
      threshold: 0,
    },
  },
});
