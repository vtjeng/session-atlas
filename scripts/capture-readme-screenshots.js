const path = require('path');
const { pathToFileURL } = require('url');
const { chromium } = require('playwright');

const siteDir = process.env.SCREENSHOT_SITE_DIR;
if (!siteDir) {
  throw new Error('SCREENSHOT_SITE_DIR must point to the synthetic fixture site');
}

const imageDir = path.resolve('docs', 'images');
const siteUrl = relativePath => pathToFileURL(path.join(siteDir, relativePath)).href;
const CAPTURE_PADDING = 24;

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({
    viewport: { width: 1440, height: 900 },
    javaScriptEnabled: false,
  });

  const requireElement = async (selector, description) => {
    const element = page.locator(selector).first();
    if (await element.count() !== 1) {
      throw new Error(`Generated fixture page is missing ${description}: ${selector}`);
    }
    return element;
  };

  const capture = async (selector, description, filename) => {
    const element = await requireElement(selector, description);
    // Preserve the element's exact raster, then add neutral space around it on
    // a second page. PNG stores pixel width and height at byte offsets 16 and 20.
    const raw = await element.screenshot();
    const width = raw.readUInt32BE(16);
    const height = raw.readUInt32BE(20);
    const background = await page.locator('body').evaluate(
      body => getComputedStyle(body).backgroundColor,
    );
    const padded = await browser.newPage({
      viewport: {
        width: width + 2 * CAPTURE_PADDING,
        height: height + 2 * CAPTURE_PADDING,
      },
      javaScriptEnabled: false,
    });
    try {
      await padded.setContent(
        `<style>html,body{margin:0;background:${background}}` +
        `body{padding:${CAPTURE_PADDING}px}</style>` +
        `<img alt="" src="data:image/png;base64,${raw.toString('base64')}">`,
      );
      await padded.screenshot({ path: path.join(imageDir, filename) });
    } finally {
      await padded.close();
    }
  };

  try {
    await page.goto(siteUrl('index.html'));
    const body = await page.locator('body').innerText();
    if (!body.includes('/home/demo/')) {
      throw new Error('Screenshot site is not the expected synthetic fixture site');
    }
    await capture('.wrap', 'the project index', 'project-index-preview.png');
    await capture('header.hero', 'the project summary', 'project-log-summary.png');
    await capture('.shelf', 'the project cards', 'project-cards.png');

    const fixtureProjectHref = await page.locator('a.proj').evaluateAll(links => {
      const matches = links.filter(link => (
        link.querySelector('.pname')?.textContent.trim() === 'example-project'
      ));
      return matches.length === 1 ? matches[0].getAttribute('href') : null;
    });
    if (!fixtureProjectHref) {
      throw new Error('Fixture index does not contain one example-project link');
    }
    await page.goto(siteUrl(fixtureProjectHref));
    await capture('header.hero', 'the project overview', 'project-overview.png');
    await capture(
      '.session-block',
      'the first synthetic session',
      'timeline-entry.png',
    );

    const pricing = await requireElement('details.pricing', 'the cost breakdown');
    await pricing.evaluate(node => { node.open = true; });
    await capture(
      '.pricing-body .tw',
      'the cost-by-model table',
      'expanded-cost.png',
    );
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
