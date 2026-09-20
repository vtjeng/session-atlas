// Drives the collapsible sessions with JavaScript enabled: a header click folds
// its session and the fold survives a reload, a ribbon click into a folded
// session unfolds it, and the sticky bar's caret folds or unfolds every session.
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');
const { pathToFileURL } = require('url');
const { test, expect } = require('playwright/test');

const REPO_ROOT = path.resolve(__dirname, '../..');
const TZ = 'America/Los_Angeles';

test.use({ javaScriptEnabled: true, timezoneId: TZ });

let siteDir;
let pageUrl;

test.beforeAll(() => {
  siteDir = fs.mkdtempSync(path.join(os.tmpdir(), 'session-atlas-sessions-'));
  const result = spawnSync(
    'python3',
    ['scripts/build_screenshot_site.py', '--out', siteDir],
    { cwd: REPO_ROOT, env: { ...process.env, TZ }, stdio: 'inherit' },
  );
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`Fixture build exited with ${result.status}`);
  // The merged example project is the fixture page with two sessions: two
  // Claude entries in the first and one Codex entry in the second.
  const dir = fs.readdirSync(siteDir).find(name => name.startsWith('example-projec--'));
  if (!dir) throw new Error('Fixture site has no example-project page');
  pageUrl = pathToFileURL(path.join(siteDir, dir, 'index.html')).href;
});

test.afterAll(() => {
  if (siteDir) fs.rmSync(siteDir, { recursive: true, force: true });
});

const openStates = page => page.$$eval('details.session-block', ds => ds.map(d => d.open));
const tickCount = page => page.locator('.mm-tick').count();
const hash = page => page.evaluate(() => location.hash);
// The minimap rebuilds 120 ms after the page resizes and the hash follows the
// reading line 200 ms after it moves, so state is polled rather than read once.
const expectTicks = (page, n) => expect.poll(() => tickCount(page)).toBe(n);

test('a header click folds its session and the fold survives a reload', async ({ page }) => {
  await page.goto(pageUrl);
  expect(await openStates(page)).toEqual([true, true]);
  await expectTicks(page, 3);

  const first = page.locator('details.session-block').nth(0);
  const header = first.locator('summary.sess');
  const before = await header.boundingBox();
  await header.locator('.stitle').click();
  expect(await openStates(page)).toEqual([false, true]);
  // Folding moves only what is below the header: the header itself stays put.
  expect(await header.boundingBox()).toEqual(before);
  await expectTicks(page, 1);
  // The folded entries leave the current-entry tracking, so the hash names
  // the shown entry in the second session.
  const shownIds = await page.$$eval('details.session-block[open] .entry', es => es.map(e => e.id));
  await expect.poll(() => hash(page)).toBe('#' + shownIds[0]);
  expect(await page.evaluate(() => localStorage.getItem('session-atlas:folded:' + location.pathname)))
    .toBe(JSON.stringify([await header.getAttribute('id')]));

  await page.reload();
  expect(await openStates(page)).toEqual([false, true]);
  await expectTicks(page, 1);

  // The permalink in a folded header scrolls to the header without unfolding it.
  await header.locator('a.lbl').click();
  await page.waitForTimeout(400);
  expect(await openStates(page)).toEqual([false, true]);
});

test('a ribbon click into a folded session unfolds it', async ({ page }) => {
  await page.goto(pageUrl);
  const first = page.locator('details.session-block').nth(0);
  await first.locator('summary.sess .stitle').click();
  expect(await openStates(page)).toEqual([false, true]);
  await expectTicks(page, 1);
  // The first session's second entry is the only entry whose ribbon position
  // differs from a session-start dot, so a click there reaches an entry.
  const rf = Number(await first.locator('.entry').nth(1).getAttribute('data-rf'));
  const track = await page.locator('#rtrack').boundingBox();
  await page.mouse.click(track.x + track.width * rf / 100, track.y + track.height / 2);
  await expect.poll(() => openStates(page)).toEqual([true, true]);
  await expectTicks(page, 3);
});

test('the sticky caret folds and unfolds every session', async ({ page }) => {
  await page.goto(pageUrl);
  const fold = page.locator('#sfold');
  await expect(fold).toHaveAttribute('aria-label', 'collapse all sessions');
  await fold.click();
  expect(await openStates(page)).toEqual([false, false]);
  await expectTicks(page, 0);
  await expect(fold).toHaveAttribute('aria-label', 'expand all sessions');
  // With no entry shown, the hash names a session header, which a reload can
  // scroll to.
  await expect.poll(() => hash(page)).toMatch(/^#session-/);

  await page.reload();
  expect(await openStates(page)).toEqual([false, false]);
  await fold.click();
  expect(await openStates(page)).toEqual([true, true]);
  await expectTicks(page, 3);
  expect(await page.evaluate(() => localStorage.getItem('session-atlas:folded:' + location.pathname)))
    .toBeNull();
});
