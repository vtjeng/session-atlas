#!/usr/bin/env node

const fs = require('fs');
const { createRequire } = require('module');
const path = require('path');
const { pathToFileURL } = require('url');
const { parseArgs } = require('util');

const LOAD_TIMEOUT_MS = 120000;
const SETTLE_MS = 100;
const USAGE = 'usage: node scripts/profile-page.js PAGE.html [--repeats N]';

let parsed;
try {
  parsed = parseArgs({
    options: {
      repeats: {type: 'string', short: 'r', default: '2'},
      help: {type: 'boolean', short: 'h'},
    },
    allowPositionals: true,
    strict: true,
  });
} catch (error) {
  console.error(error.message);
  console.error(USAGE);
  process.exit(2);
}

if (parsed.values.help) {
  console.log(`${USAGE}\n\nProfiles a generated local HTML page with Chromium.`);
  process.exit(0);
}

if (parsed.positionals.length !== 1) {
  console.error(USAGE);
  process.exit(2);
}

const file = parsed.positionals[0];
const repeats = Number(parsed.values.repeats);

if (!Number.isInteger(repeats) || repeats < 1) {
  console.error('--repeats must be a positive integer');
  process.exit(2);
}

if (!fs.existsSync(file)) {
  console.error(`page not found: ${file}`);
  process.exit(2);
}

const requireFromRepo = createRequire(path.join(process.cwd(), 'package.json'));
const { chromium } = requireFromRepo('playwright');

async function profilePage() {
  const browser = await chromium.launch({headless: true});
  const results = [];

  try {
    for (let i = 0; i < repeats; i += 1) {
      const context = await browser.newContext();
      const page = await context.newPage();
      const client = await context.newCDPSession(page);
      await client.send('Performance.enable');

      const started = performance.now();
      await page.goto(pathToFileURL(path.resolve(file)).href, {
        waitUntil: 'load',
        timeout: LOAD_TIMEOUT_MS,
      });
      await page.waitForTimeout(SETTLE_MS);

      const dom = await page.evaluate(() => ({
        allElements: document.querySelectorAll('*').length,
        entries: document.querySelectorAll('.entry').length,
        sessions: document.querySelectorAll('.session-block').length,
        responseItems: document.querySelectorAll('.response-item').length,
        details: document.querySelectorAll('details').length,
        toolLogs: document.querySelectorAll('.telog').length,
        minimapTicks: document.querySelectorAll('.mm-tick').length,
        minimapSessions: document.querySelectorAll('.mm-sess').length,
        htmlBytes: new TextEncoder().encode(
          document.documentElement.outerHTML).length,
        responseHtmlBytes: [...document.querySelectorAll('.response-item')]
          .reduce((total, element) => total + element.outerHTML.length, 0),
        toolLogHtmlBytes: [...document.querySelectorAll('.telog')]
          .reduce((total, element) => total + element.outerHTML.length, 0),
      }));
      const response = await client.send('Performance.getMetrics');
      const metrics = Object.fromEntries(
        response.metrics.map(({name, value}) => [name, value]));
      const navigation = await page.evaluate(() => {
        const entry = performance.getEntriesByType('navigation')[0];
        return {
          domContentLoadedMs: entry.domContentLoadedEventEnd,
          loadEventMs: entry.loadEventEnd,
        };
      });

      results.push({
        wallMs: performance.now() - started,
        ...navigation,
        taskMs: (metrics.TaskDuration || 0) * 1000,
        scriptMs: (metrics.ScriptDuration || 0) * 1000,
        recalcStyleMs: (metrics.RecalcStyleDuration || 0) * 1000,
        layoutMs: (metrics.LayoutDuration || 0) * 1000,
        layoutCount: metrics.LayoutCount || 0,
        recalcStyleCount: metrics.RecalcStyleCount || 0,
        jsHeapMb: (metrics.JSHeapUsedSize || 0) / 1024 / 1024,
        nodes: metrics.Nodes || 0,
        ...dom,
      });

      await context.close();
    }
  } finally {
    await browser.close();
  }

  console.log(JSON.stringify({repeats, samples: results}, null, 2));
}

profilePage().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
