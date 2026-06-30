const { chromium } = require('@playwright/test');
const path = require('path');
const fs = require('fs');

const BASE = 'http://localhost:5173';
const TOKEN = 'localtest123';
const OUT = path.join(__dirname, 'screenshots');
if (!fs.existsSync(OUT)) fs.mkdirSync(OUT);

const PAGES = [
  { name: 'sessions', url: '/sessions' },
  { name: 'tasks', url: '/tasks' },
  { name: 'system', url: '/system' },
];

async function authenticate(page) {
  await page.goto(BASE);
  await page.waitForTimeout(500);
  const input = page.locator('input').first();
  if (await input.isVisible()) {
    await input.fill(TOKEN);
    await page.locator('button').first().click();
    await page.waitForTimeout(1500);
  }
}

(async () => {
  const browser = await chromium.launch({ headless: true });

  // Mobile
  const mobile = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await authenticate(mobile);

  for (const { name, url } of PAGES) {
    await mobile.goto(BASE + url);
    await mobile.waitForTimeout(2000);
    await mobile.screenshot({ path: path.join(OUT, `${name}-mobile.png`) });
    console.log(`Captured ${name} (mobile)`);
  }

  // Desktop
  const desktop = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await authenticate(desktop);

  for (const { name, url } of PAGES) {
    await desktop.goto(BASE + url);
    await desktop.waitForTimeout(2000);
    await desktop.screenshot({ path: path.join(OUT, `${name}-desktop.png`) });
    console.log(`Captured ${name} (desktop)`);
  }

  await browser.close();
  console.log('Done. Screenshots in', OUT);
})();
