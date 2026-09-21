// Drives the share controls with JavaScript enabled: a session's `share`, an
// entry's `share`, and the `s` key each download a standalone page that keeps
// the unit's fields and drops the session id, anchors, fragment links, the
// controls, and the prompt clipping.
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');
const { pathToFileURL } = require('url');
const { test, expect } = require('playwright/test');

const REPO_ROOT = path.resolve(__dirname, '../..');
const TZ = 'America/Los_Angeles';

test.use({ javaScriptEnabled: true, timezoneId: TZ, acceptDownloads: true });

let siteDir;
let pageUrl;

test.beforeAll(() => {
  siteDir = fs.mkdtempSync(path.join(os.tmpdir(), 'session-atlas-share-'));
  const result = spawnSync(
    'python3',
    ['scripts/build_screenshot_site.py', '--out', siteDir],
    { cwd: REPO_ROOT, env: { ...process.env, TZ }, stdio: 'inherit' },
  );
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`Fixture build exited with ${result.status}`);
  // The merged example project: two Claude entries in the first session and
  // one Codex entry in the second.
  const dir = fs.readdirSync(siteDir).find(name => name.startsWith('example-projec--'));
  if (!dir) throw new Error('Fixture site has no example-project page');
  pageUrl = pathToFileURL(path.join(siteDir, dir, 'index.html')).href;
});

test.afterAll(() => {
  if (siteDir) fs.rmSync(siteDir, { recursive: true, force: true });
});

const download = async (page, action) => {
  const [dl] = await Promise.all([page.waitForEvent('download'), action()]);
  return { name: dl.suggestedFilename(), html: fs.readFileSync(await dl.path(), 'utf8') };
};
// The extract's markup after its stylesheet, so a selector such as `.emark` in
// the copied CSS does not count as a leftover element.
const bodyOf = html => html.slice(html.indexOf('<body'));

test('a session share downloads the session without its id or anchors', async ({ page }) => {
  await page.goto(pageUrl);
  const { name, html } = await download(
    page, () => page.click('details.session-block:nth-of-type(1) summary.sess .share'));
  expect(name).toBe('example-project--session-01.html');
  const body = bodyOf(html);
  // Kept: the stylesheet, the project's name and path, the session label and
  // date, the title, the two entries with their one day rule, and the open fold.
  expect(html).toContain('<style>');
  expect(body).toContain('<h1>example-project</h1>');
  expect(body).toContain('/home/demo/src/example-project');
  expect(body).toContain('<b>session 01</b> · <b>Mar 15, 2026</b>');
  expect(body).toContain('Can we add cached search results to the project page?');
  expect(body.match(/class="entry /g)).toHaveLength(2);
  expect(body.match(/class="day"/g)).toHaveLength(1);
  expect(body).toContain('<details class="session-block" open');
  // Dropped: the fixture's session id, every id and fragment link, the share
  // controls, the timeline marks, the scroll data attributes, and the clipping.
  expect(body).not.toContain('11111111');
  expect(body).not.toMatch(/ id="/);
  expect(body).not.toMatch(/href="#/);
  expect(body).not.toContain('class="share"');
  expect(body).not.toContain('emark');
  expect(body).not.toMatch(/ data-/);
  expect(body).not.toContain('ask clip');
  // The extract's tab icon is the atlas's favicon with its background and
  // stroke swapped: the page's own icon has a dark rect and a cream stroke.
  const icon = /<link rel="icon" type="image\/svg\+xml" href="data:image\/svg\+xml,([^"]+)">/.exec(html);
  expect(icon).not.toBeNull();
  const svg = decodeURIComponent(icon[1]);
  expect(svg).toContain('<rect width="64" height="64" rx="13" fill="#e9e6df"/>');
  expect(svg).toContain('stroke="#15171b"');
  // The footer credits the generator and links to its repository.
  expect(body).toContain('<footer>shared via <a href="https://github.com/vtjeng/session-atlas">session-atlas</a> · session exported ');
  // The click landed on the button, not the summary, so the session stays open.
  expect(await page.$eval('details.session-block', d => d.open)).toBe(true);
  // The extract renders on its own.
  const extract = path.join(siteDir, 'extract.html');
  fs.writeFileSync(extract, html);
  await page.goto(pathToFileURL(extract).href);
  expect(await page.evaluate(() => document.querySelectorAll('.entry').length)).toBe(2);
});

test('an entry share downloads that entry with its day rule and session header', async ({ page }) => {
  await page.goto(pageUrl);
  const entry = page.locator('details.session-block:nth-of-type(1) .entry').nth(1);
  await entry.hover();
  const { name, html } = await download(page, () => entry.locator('.share').click());
  // The first session's second entry is the /review command at 09:58 Pacific
  // time on Mar 15, so the file name carries that local stamp.
  expect(name).toBe('example-project--session-01--2026-03-15-0958.html');
  const body = bodyOf(html);
  expect(body.match(/class="entry /g)).toHaveLength(1);
  expect(body.match(/class="day"/g)).toHaveLength(1);
  expect(body).toContain('cache invalidation');
  // The extract says it is a subset: the header's date line counts the entry,
  // a note above it counts the one earlier entry, no note follows the last
  // entry, and the header's totals are labelled as the whole session's.
  expect(body).toContain('<b>session 01</b> · entry <b>2 of 2</b>');
  expect(body).toContain('<div class="gapnote">· · · 1 earlier entry not shown</div>');
  expect(body).not.toContain('later entr');
  expect(body).toContain('<span class="sstats">whole session: 1 prompt');
  expect(body).not.toContain('11111111');
});

test('the s key shares the session at the reading line', async ({ page }) => {
  await page.goto(pageUrl);
  const { name } = await download(page, () => page.keyboard.press('s'));
  expect(name).toBe('example-project--session-01.html');
});
