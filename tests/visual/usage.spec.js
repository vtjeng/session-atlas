// Drives the hero usage explorer with JavaScript enabled. The screenshot spec
// keeps JavaScript off for pixel-stable baselines; this spec checks that the
// client-side window arithmetic reproduces the server-rendered totals.
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');
const { pathToFileURL } = require('url');
const { test, expect } = require('playwright/test');

const REPO_ROOT = path.resolve(__dirname, '../..');
// The fixture site is built and viewed in one timezone so its local days and
// the browser's "today" (fixed to the fixture refresh time) line up.
const TZ = 'America/Los_Angeles';
const FIXTURE_REFRESH = new Date('2026-03-15T14:30:00-07:00');

test.use({ javaScriptEnabled: true, timezoneId: TZ });

let siteDir;

test.beforeAll(() => {
  siteDir = fs.mkdtempSync(path.join(os.tmpdir(), 'session-atlas-usage-'));
  const result = spawnSync(
    'python3',
    ['scripts/build_screenshot_site.py', '--out', siteDir],
    { cwd: REPO_ROOT, env: { ...process.env, TZ }, stdio: 'inherit' },
  );
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`Fixture build exited with ${result.status}`);
});

test.afterAll(() => {
  if (siteDir) fs.rmSync(siteDir, { recursive: true, force: true });
});

const cardText = page => page.locator('.stats').innerText();
const axisText = async page => (await page.locator('.uaxis').innerText()).replace(/\s+/g, ' ');

test('window presets, dates, and the hash recompute the cards', async ({ page }) => {
  await page.clock.setFixedTime(FIXTURE_REFRESH);
  await page.goto(pathToFileURL(path.join(siteDir, 'index.html')).href);
  const serverCards = await cardText(page);
  const height = await page.evaluate(() => document.documentElement.scrollHeight);

  // The fixture spans four days, so "7d" ending on the refresh day covers
  // everything: the axis and every card stay as the server rendered them.
  await page.click('.preset[data-p="7"]');
  expect(await axisText(page)).toBe('Mar 12, 2026 4 DAYS Mar 15, 2026');
  expect(await cardText(page)).toBe(serverCards);
  await expect(page.locator('.preset.on')).toHaveText('all');

  // A one-day window on the docs-site day: one prompt in one four-minute
  // session, priced under a dollar, with 300k of 328k prompt tokens cached.
  // $0.181 over four minutes is $2.715 an hour, which the double rounds down.
  await page.fill('#uTo', '2026-03-12');
  await page.locator('#uTo').dispatchEvent('change');
  expect(await axisText(page)).toBe('Mar 12, 2026 1 DAY Mar 12, 2026');
  expect(page.url()).toMatch(/#2026-03-12\.\.2026-03-12$/);
  const oneDay = (await cardText(page)).replace(/\s+/g, ' ');
  expect(oneDay).toBe(
    '1 SESSION 1 INPUT 4m AGENT ACTIVE TIME 4.5k TOKENS OUT '
    + '1 DAY ACTIVE <$1 EST. API COST 1 day LONGEST STREAK 4m BUSIEST DAY · MAR 12');
  expect((await page.locator('.rates').innerText()).replace(/\s+/g, ' '))
    .toBe('$2.71 per active hour $0.18 per input 91% cache hit rate');

  // Moving the from-date past the to-date pulls the to-date along.
  await page.fill('#uFrom', '2026-03-14');
  await page.locator('#uFrom').dispatchEvent('change');
  expect(await axisText(page)).toBe('Mar 14, 2026 1 DAY Mar 14, 2026');

  // A hash navigation selects the window and metric without a reload; the
  // example-project day carries two sessions and $3 of est. API cost.
  await page.goto(pathToFileURL(path.join(siteDir, 'index.html')).href + '#2026-03-15..2026-03-15/act');
  expect(await axisText(page)).toBe('Mar 15, 2026 1 DAY Mar 15, 2026');
  await expect(page.locator('.metric.on')).toHaveText('agent active time');
  expect((await cardText(page)).replace(/\s+/g, ' ')).toContain('2 SESSIONS 3 INPUTS 29m');
  // 29 minutes rounds up to a 40-minute top gridline with a 20-minute middle.
  await expect(page.locator('#uYTop')).toHaveText('40m');
  await expect(page.locator('#uYMid')).toHaveText('20m');

  // Back to everything: the recomputed cards equal the server render exactly,
  // the hash clears, and no state change moved the page.
  await page.click('.preset[data-p="all"]');
  await page.click('.metric[data-m="cost"]');
  expect(await cardText(page)).toBe(serverCards);
  expect(new URL(page.url()).hash).toBe('');
  expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBe(height);
});

test('the minimap brush and the chart drag select windows', async ({ page }) => {
  await page.clock.setFixedTime(FIXTURE_REFRESH);
  await page.goto(pathToFileURL(path.join(siteDir, 'index.html')).href);
  const mini = await page.locator('#uMini .ubars').boundingBox();
  const col = mini.width / 4;   // four day columns

  // The brush starts over the whole history, so a drag inside it draws a new
  // window from the anchor to the pointer rather than sliding the brush.
  await page.mouse.move(mini.x + col * 1.5, mini.y + mini.height / 2);
  await page.mouse.down();
  await page.mouse.move(mini.x + col * 2.5, mini.y + mini.height / 2, { steps: 4 });
  await page.mouse.up();
  expect(await axisText(page)).toBe('Mar 13, 2026 2 DAYS Mar 14, 2026');
  expect(page.url()).toMatch(/#2026-03-13\.\.2026-03-14$/);

  // Dragging the brush body slides the window without resizing it.
  const brush = await page.locator('#uBrush').boundingBox();
  await page.mouse.move(brush.x + brush.width / 2, brush.y + brush.height / 2);
  await page.mouse.down();
  await page.mouse.move(brush.x + brush.width / 2 + col, brush.y + brush.height / 2, { steps: 4 });
  await page.mouse.up();
  expect(await axisText(page)).toBe('Mar 14, 2026 2 DAYS Mar 15, 2026');

  // Dragging across the main chart zooms into the covered days; hovering a
  // column shows that day's readout.
  await page.click('.preset[data-p="all"]');
  const plot = await page.locator('#uPlot .ubars').boundingBox();
  const pcol = plot.width / 4;
  await page.mouse.move(plot.x + pcol * 0.5, plot.y + 40);
  await page.mouse.down();
  await page.mouse.move(plot.x + pcol * 3.5, plot.y + 40, { steps: 6 });
  await page.mouse.up();
  expect(await axisText(page)).toBe('Mar 12, 2026 4 DAYS Mar 15, 2026');
  await page.mouse.move(plot.x + pcol * 3.5, plot.y + 60);
  const tip = (await page.locator('#uTip').innerText()).replace(/\s+/g, ' ');
  expect(tip).toContain('Sun · Mar 15, 2026');
  expect(tip).toContain('$2.84 est. API cost · 58.0k tokens out · 29m agent active');
  expect(tip).toContain('3 inputs · 2 sessions started');
  expect(tip).toContain('example-project $2.84');
});
