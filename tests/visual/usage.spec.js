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
// Bar heights as numbers: Python and JavaScript round an exact .xx5 tie
// differently, which is a hundredth of a percent, so heights compare loosely.
const heights = html => [...html.matchAll(/height:([\d.]+)%/g)].map(m => Number(m[1]));
const expectSameBars = (a, b) => {
  const ha = heights(a), hb = heights(b);
  expect(ha.length).toBe(hb.length);
  ha.forEach((h, i) => expect(Math.abs(h - hb[i])).toBeLessThanOrEqual(0.011));
};

test('window presets, dates, and the hash recompute the cards', async ({ page }) => {
  await page.clock.setFixedTime(FIXTURE_REFRESH);
  await page.goto(pathToFileURL(path.join(siteDir, 'index.html')).href);
  const serverCards = await cardText(page);
  const height = await page.evaluate(() => document.documentElement.scrollHeight);

  // Under "auto" the four-day fixture plots hourly: the server rendered 96
  // bars with twelve-hour ticks, and a forced client repaint reproduces them.
  await expect(page.locator('.interval.eff')).toHaveText('hour');
  const staticBars = await page.locator('#uPlot .ubars').innerHTML();
  const staticAxis = await page.locator('#uAxis').innerHTML();
  await page.selectOption('#uMet', 'tok');
  await page.selectOption('#uMet', 'cost');
  expectSameBars(await page.locator('#uPlot .ubars').innerHTML(), staticBars);
  expect(await page.locator('#uAxis').innerHTML()).toBe(staticAxis);
  expect((await page.locator('.uaxis').innerText()).replace(/\s+/g, ' '))
    .toBe('Mar 12 12:00 Mar 13 12:00 Mar 14 12:00 Mar 15 12:00');

  // "7d" ending on the refresh day covers everything: the select snaps back
  // to "all" and every tile stays as the server rendered it. The daily view
  // is chosen explicitly for the day-based checks below.
  await page.selectOption('#uWin', '7');
  expect(await windowText(page)).toBe('2026-03-12 2026-03-15 · 4 days');
  expect(await page.locator('#uWin').inputValue()).toBe('all');
  await page.click('.interval[data-i="day"]');
  expect((await page.locator('.uaxis').innerText()).replace(/\s+/g, ' ')).toBe('Mar 12 Mar 13 Mar 14 Mar 15');
  expect(await cardText(page)).toBe(serverCards);

  // A one-day window on the docs-site day: one prompt in one four-minute
  // session, priced under a dollar, with 300k of 328k prompt tokens cached.
  // $0.181 over four minutes is $2.715 an hour, which the double rounds down.
  await page.fill('#uTo', '2026-03-12');
  await page.locator('#uTo').dispatchEvent('change');
  expect(await windowText(page)).toBe('2026-03-12 2026-03-12 · 1 day');
  expect(await page.locator('#uWin').inputValue()).toBe('custom');
  // The y scale is locked to the whole history, so the one-day window keeps
  // the $4 top gridline instead of rescaling to its own $0.18 peak.
  await expect(page.locator('#uYTop')).toHaveText('$4');
  expect(new URL(page.url()).search).toBe('?from=2026-03-12&to=2026-03-12&interval=day');
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

  // Query fields select the window, metric, and interval on load; the
  // example-project day carries two sessions and $3 of est. API cost.
  await page.goto(pathToFileURL(path.join(siteDir, 'index.html')).href + '?from=2026-03-15&to=2026-03-15&metric=act&interval=day');
  expect(await windowText(page)).toBe('2026-03-15 2026-03-15 · 1 day');
  await expect(page.locator('#uMet')).toHaveValue('act');
  await expect(page.locator('#uMet option:checked')).toHaveText('agent active time');
  expect((await cardText(page)).replace(/\s+/g, ' ')).toContain('SESSIONS 2 1.5 inputs per session INPUTS 3');
  // 29 minutes on the busiest day rounds up to a 40-minute top gridline.
  await expect(page.locator('#uYTop')).toHaveText('40m');
  await expect(page.locator('#uYMid')).toHaveText('20m');

  // Hourly bars for that day: 24 columns, the readout resting on the last
  // active hour, and the model and project splits on their own lines.
  await page.goto(pathToFileURL(path.join(siteDir, 'index.html')).href + '?from=2026-03-15&to=2026-03-15&metric=act&interval=hour');
  await expect(page.locator('.interval.on')).toHaveText('hour');
  expect(await page.locator('#uPlot .ubars i').count()).toBe(24);
  const hourly = await readoutText(page);
  expect(hourly).toContain('Sun · Mar 15, 2026 · 10:00–11:00');
  // With agent active time plotted, the splits are by active time too: the
  // 10:00 hour holds the Codex session's nine estimated minutes plus the four
  // minutes of the /review entry that ran past ten o'clock, since an entry's
  // usage is spread over the hours it ran. (innerText carries no space after
  // the inline-block key, and the cells break lines in innerText.)
  const line = async id => (await page.locator(id).innerText()).replace(/\s+/g, ' ').trim();
  expect(await line('#uRo2')).toBe('MODELS gpt-5.6-sol 9m sonnet-5 4m');
  expect(await line('#uRo3')).toBe('PROJECTS example-project 13m');
  // Weekly bars: the four fixture days share one Monday-based week, in the
  // plot and in the minimap, which follows the interval.
  await page.click('.interval[data-i="week"]');
  await page.selectOption('#uWin', 'all');
  expect(await page.locator('#uPlot .ubars i').count()).toBe(1);
  expect(await page.locator('#uMini .ubars i').count()).toBe(1);
  expect(await readoutText(page)).toContain('Mar 12 – Mar 15, 2026');
  expect(new URL(page.url()).search).toBe('?metric=act&interval=week');
  await page.click('.interval[data-i="day"]');
  expect(await page.locator('#uMini .ubars i').count()).toBe(4);
  // Back on "auto" the four days plot hourly again, and so does the minimap.
  await page.click('.interval[data-i="auto"]');
  expect(await page.locator('#uPlot .ubars i').count()).toBe(96);
  expect(await page.locator('#uMini .ubars i').count()).toBe(96);
  expect(new URL(page.url()).search).toBe('?metric=act');
  await page.click('.interval[data-i="day"]');

  // Back to everything: the recomputed tiles equal the server render exactly,
  // the query clears, and no state change moved the page.
  await page.selectOption('#uWin', 'all');
  await page.selectOption('#uMet', 'cost');
  await page.click('.interval[data-i="auto"]');
  expect(await cardText(page)).toBe(serverCards);
  expect(new URL(page.url()).search).toBe('');
  expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBe(height);
});

test('a phone-width plot draws days under auto and a two-line readout', async ({ page }) => {
  await page.setViewportSize({ width: 420, height: 900 });
  await page.clock.setFixedTime(FIXTURE_REFRESH);
  await page.goto(pathToFileURL(path.join(siteDir, 'index.html')).href);
  // The server drew 96 hourly bars for a wide plot. In a 282px plot each hour
  // would get under 3px, so the first client render draws the four days.
  await expect(page.locator('.interval.eff')).toHaveText('day');
  await expect(page.locator('#uPlot .ubars i')).toHaveCount(4);
  // Each readout line is two lines tall at a fixed 32px, so the frame stays put.
  expect(Math.round((await page.locator('#uRo1').boundingBox()).height)).toBe(32);
  // Widening the window brings the hourly bars back.
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(page.locator('.interval.eff')).toHaveText('hour');
  await expect(page.locator('#uPlot .ubars i')).toHaveCount(96);
});

test('the minimap brush and the chart drag select windows', async ({ page }) => {
  await page.clock.setFixedTime(FIXTURE_REFRESH);
  await page.goto(pathToFileURL(path.join(siteDir, 'index.html')).href + '?interval=day');
  const mini = await page.locator('#uMini .ubars').boundingBox();
  const col = mini.width / 4;   // four day columns

  // The brush starts over the whole history, so a drag inside it draws a new
  // window from the anchor to the pointer rather than sliding the brush.
  await page.mouse.move(mini.x + col * 1.5, mini.y + mini.height / 2);
  await page.mouse.down();
  await page.mouse.move(mini.x + col * 2.5, mini.y + mini.height / 2, { steps: 4 });
  await page.mouse.up();
  expect(await windowText(page)).toBe('2026-03-13 2026-03-14 · 2 days');
  expect(new URL(page.url()).search).toBe('?from=2026-03-13&to=2026-03-14&interval=day');

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
  await page.click('.interval[data-i="day"]');
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
  expect(hovered).toMatch(/MODELS\s*sonnet-5 \$0\.18 PROJECTS\s*docs-site \$0\.18/);
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
