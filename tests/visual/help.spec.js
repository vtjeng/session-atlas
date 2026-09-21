// Drives the shortcuts dialog with JavaScript enabled on the index and a
// project page: `?` and the `?` button open it, `?` again, Escape, the close
// button, and a click on the backdrop close it, and each page lists only the
// groups it has.
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
let indexUrl;
let projectUrl;

test.beforeAll(() => {
  siteDir = fs.mkdtempSync(path.join(os.tmpdir(), 'session-atlas-help-'));
  const result = spawnSync(
    'python3',
    ['scripts/build_screenshot_site.py', '--out', siteDir],
    { cwd: REPO_ROOT, env: { ...process.env, TZ }, stdio: 'inherit' },
  );
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`Fixture build exited with ${result.status}`);
  indexUrl = pathToFileURL(path.join(siteDir, 'index.html')).href;
  // The merged example project has two sessions, so its dialog lists j and k.
  const dir = fs.readdirSync(siteDir).find(name => name.startsWith('example-projec--'));
  if (!dir) throw new Error('Fixture site has no example-project page');
  projectUrl = pathToFileURL(path.join(siteDir, dir, 'index.html')).href;
});

test.afterAll(() => {
  if (siteDir) fs.rmSync(siteDir, { recursive: true, force: true });
});

const isOpen = page => page.$eval('#help', d => d.open);
const groups = page => page.$$eval('#help h3', hs => hs.map(h => h.textContent));

test('the ? key and the ? button open and close the dialog on a project page', async ({ page }) => {
  await page.goto(projectUrl);
  expect(await isOpen(page)).toBe(false);
  await page.keyboard.press('?');
  expect(await isOpen(page)).toBe(true);
  await page.keyboard.press('?');
  expect(await isOpen(page)).toBe(false);
  await page.click('#shelp');
  expect(await isOpen(page)).toBe(true);
  await page.keyboard.press('Escape');
  expect(await isOpen(page)).toBe(false);
  await page.click('#shelp');
  await page.click('#helpClose');
  expect(await isOpen(page)).toBe(false);
  // A click outside the box lands on the dialog element itself, its backdrop.
  await page.click('#shelp');
  await page.mouse.click(5, 5);
  expect(await isOpen(page)).toBe(false);
  // The fixture project has two sessions, a ribbon, and one day of activity:
  // the keys, the top bar, and the share glyph are listed, the chart is not.
  expect(await groups(page)).toEqual(['Keyboard', 'Top bar', 'Share']);
  expect(await page.locator('#help .kribbon').count()).toBe(1);
  expect(await page.locator('#help .share').count()).toBe(1);
  expect(await page.locator('#help').innerHTML()).toContain('<kbd>j</kbd> <kbd>k</kbd>');
  // With the dialog open, j does not step the session behind it.
  await page.keyboard.press('?');
  await page.keyboard.press('j');
  await page.waitForTimeout(400);
  expect(await page.locator('#sessCur').textContent()).toBe('1');
});

test('the index lists the chart shortcuts', async ({ page }) => {
  await page.goto(indexUrl);
  await page.click('#shelp');
  expect(await isOpen(page)).toBe(true);
  expect(await groups(page)).toEqual(['Chart']);
  expect(await page.locator('#help').innerHTML()).toContain('<kbd>Home</kbd> <kbd>End</kbd>');
  await page.keyboard.press('Escape');
  expect(await isOpen(page)).toBe(false);
});
