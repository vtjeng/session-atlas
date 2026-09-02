const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');
const { test, expect } = require('playwright/test');

const REPO_ROOT = path.resolve(__dirname, '../..');
const BASELINE_DIR = path.join(REPO_ROOT, 'docs', 'images');
const SCREENSHOT_NAMES = fs.readdirSync(BASELINE_DIR)
  .filter(name => name.endsWith('.png'))
  .sort();

let actualDir;

test.beforeAll(() => {
  actualDir = fs.mkdtempSync(path.join(os.tmpdir(), 'session-atlas-visual-'));
  const result = spawnSync(
    'bash',
    ['scripts/capture-readme-screenshots.sh'],
    {
      cwd: REPO_ROOT,
      env: { ...process.env, SCREENSHOT_IMAGE_DIR: actualDir },
      stdio: 'inherit',
    },
  );
  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    throw new Error(`Screenshot capture exited with status ${result.status}`);
  }
  const actualNames = fs.readdirSync(actualDir)
    .filter(name => name.endsWith('.png'))
    .sort();
  expect(actualNames).toEqual(SCREENSHOT_NAMES);
});

test.afterAll(() => {
  if (actualDir) {
    fs.rmSync(actualDir, { recursive: true, force: true });
  }
});

for (const name of SCREENSHOT_NAMES) {
  test(`matches committed ${name} baseline`, () => {
    const actualPath = path.join(actualDir, name);
    expect(fs.existsSync(actualPath), `Missing generated screenshot: ${name}`).toBe(true);
    expect(fs.readFileSync(actualPath)).toMatchSnapshot(name);
  });
}
