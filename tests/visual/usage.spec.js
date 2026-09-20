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
// The window as the toolbar states it: both date inputs and the day count.
const windowText = async page => [
  await page.locator('#uFrom').inputValue(),
  await page.locator('#uTo').inputValue(),
  (await page.locator('#uDays').innerText()).trim(),
].join(' ');
const readoutText = async page => (await page.locator('#uRo').innerText()).replace(/\s+/g, ' ');

test('window presets, dates, and the hash recompute the cards', async ({ page }) => {
  await page.clock.setFixedTime(FIXTURE_REFRESH);
  await page.goto(pathToFileURL(path.join(siteDir, 'index.html')).href);
  const serverCards = await cardText(page);
  const height = await page.evaluate(() => document.documentElement.scrollHeight);

  // The fixture spans four days, so "7d" ending on the refresh day covers
  // everything: the select snaps back to "all", the day ticks stay daily, and
  // every tile stays as the server rendered it.
  await page.selectOption('#uWin', '7');
  expect(await windowText(page)).toBe('2026-03-12 2026-03-15 · 4 days');
  expect(await page.locator('#uWin').inputValue()).toBe('all');
  expect((await page.locator('.uaxis').innerText()).replace(/\s+/g, ' ')).toBe('Mar 12 Mar 13 Mar 14 Mar 15');
  expect(await cardText(page)).toBe(serverCards);

  // A one-day window on the docs-site day: one prompt in one four-minute
  // session, priced under a dollar, with 300k of 328k prompt tokens cached.
  // $0.181 over four minutes is $2.715 an hour, which the double rounds down.
  await page.fill('#uTo', '2026-03-12');
  await page.locator('#uTo').dispatchEvent('change');
  expect(await windowText(page)).toBe('2026-03-12 2026-03-12 · 1 day');
  expect(await page.locator('#uWin').inputValue()).toBe('custom');
  expect(page.url()).toMatch(/#2026-03-12\.\.2026-03-12$/);
  const oneDay = (await cardText(page)).replace(/\s+/g, ' ');
  expect(oneDay).toBe(
    'SESSION 1 1.0 inputs per session INPUT 1 $0.18 per input '
    + 'AGENT ACTIVE TIME 4m $2.71 per active hour TOKENS OUT 4.5k 4.5k per input '
    + 'DAY ACTIVE 1 of 1 day EST. API COST <$1 91% cache hit rate '
    + 'LONGEST STREAK 1 day Mar 12 BUSIEST DAY 4m Thu · Mar 12');

  // Moving the from-date past the to-date pulls the to-date along.
  await page.fill('#uFrom', '2026-03-14');
  await page.locator('#uFrom').dispatchEvent('change');
  expect(await windowText(page)).toBe('2026-03-14 2026-03-14 · 1 day');

  // A hash navigation selects the window and metric without a reload; the
  // example-project day carries two sessions and $3 of est. API cost.
  await page.goto(pathToFileURL(path.join(siteDir, 'index.html')).href + '#2026-03-15..2026-03-15/act');
  expect(await windowText(page)).toBe('2026-03-15 2026-03-15 · 1 day');
  await expect(page.locator('.metric.on')).toHaveText('agent active time');
  expect((await cardText(page)).replace(/\s+/g, ' ')).toContain('SESSIONS 2 1.5 inputs per session INPUTS 3');
  // 29 minutes rounds up to a 40-minute top gridline with a 20-minute middle.
  await expect(page.locator('#uYTop')).toHaveText('40m');
  await expect(page.locator('#uYMid')).toHaveText('20m');

  // Back to everything: the recomputed tiles equal the server render exactly,
  // the hash clears, and no state change moved the page.
  await page.selectOption('#uWin', 'all');
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
  expect(await windowText(page)).toBe('2026-03-13 2026-03-14 · 2 days');
  expect(page.url()).toMatch(/#2026-03-13\.\.2026-03-14$/);

  // Dragging the brush body slides the window without resizing it.
  const brush = await page.locator('#uBrush').boundingBox();
  await page.mouse.move(brush.x + brush.width / 2, brush.y + brush.height / 2);
  await page.mouse.down();
  await page.mouse.move(brush.x + brush.width / 2 + col, brush.y + brush.height / 2, { steps: 4 });
  await page.mouse.up();
  expect(await windowText(page)).toBe('2026-03-14 2026-03-15 · 2 days');

  // Dragging across the main chart zooms into the covered days; hovering a
  // column shows that day in the readout, and a click pins it there.
  await page.selectOption('#uWin', 'all');
  const plot = await page.locator('#uPlot .ubars').boundingBox();
  const pcol = plot.width / 4;
  await page.mouse.move(plot.x + pcol * 0.5, plot.y + 40);
  await page.mouse.down();
  await page.mouse.move(plot.x + pcol * 3.5, plot.y + 40, { steps: 6 });
  await page.mouse.up();
  expect(await windowText(page)).toBe('2026-03-12 2026-03-15 · 4 days');
  // The readout rests on the last active day until a hover replaces it.
  expect(await readoutText(page)).toContain('Sun · Mar 15, 2026');
  await page.mouse.move(plot.x + pcol * 0.5, plot.y + 60);
  const hovered = await readoutText(page);
  expect(hovered).toContain('Thu · Mar 12, 2026');
  expect(hovered).toContain('$0.18 est. API cost · 4.5k tokens out · 4m agent active · 1 input · 1 session started');
  expect(hovered).toContain('sonnet-5 $0.18 · docs-site $0.18');
  await page.mouse.move(plot.x + pcol * 2.5, plot.y + 60);
  expect(await readoutText(page)).toContain('Sat · Mar 14, 2026 unpin no activity');
  // Click to pin Mar 12: leaving the chart keeps it, and the unpin button clears it.
  await page.mouse.click(plot.x + pcol * 0.5, plot.y + 60);
  await page.mouse.move(10, 10);
  expect(await readoutText(page)).toContain('Thu · Mar 12, 2026');
  await expect(page.locator('#uPin')).toBeEnabled();
  await page.click('#uPin');
  expect(await readoutText(page)).toContain('Sun · Mar 15, 2026');
  await expect(page.locator('#uPin')).toBeDisabled();
  // Arrow keys step the pinned day while the chart has focus.
  await page.locator('#uPlot').focus();
  await page.keyboard.press('ArrowLeft');
  expect(await readoutText(page)).toContain('Sat · Mar 14, 2026');
  await page.keyboard.press('Home');
  expect(await readoutText(page)).toContain('Thu · Mar 12, 2026');
});
