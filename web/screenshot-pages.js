const { chromium } = require('@playwright/test');
const path = require('path');
const fs = require('fs');

const BASE = 'http://localhost:5173';
const OUT = path.join(__dirname, 'screenshots');
if (!fs.existsSync(OUT)) fs.mkdirSync(OUT);

const PAGES = [
  { name: 'sessions', hash: '#/' },
  { name: 'tasks', hash: '#/tasks' },
  { name: 'files', hash: '#/files' },
  { name: 'system', hash: '#/system' },
];

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } }); // iPhone 14 Pro size

  for (const { name, hash } of PAGES) {
    await page.goto(BASE + '/' + hash);
    await page.waitForTimeout(1500);
    await page.screenshot({ path: path.join(OUT, `${name}.png`), fullPage: false });
    console.log(`Captured ${name}`);
  }

  // Also grab desktop view of sessions
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto(BASE + '/#/');
  await page.waitForTimeout(1500);
  await page.screenshot({ path: path.join(OUT, 'sessions-desktop.png'), fullPage: false });
  console.log('Captured sessions-desktop');

  await browser.close();
  console.log('Done. Screenshots in', OUT);
})();
