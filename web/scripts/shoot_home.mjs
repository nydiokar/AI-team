import { chromium } from "@playwright/test";
import { mkdirSync } from "node:fs";

const TOKEN = process.env.DASH_TOKEN;
const BASE = "http://localhost:5180";
const OUT = "/home/cifran/dev/AI-team/docs/home-screen";
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();

async function shoot(name, viewport) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  // Pair the device: the hash token is stripped + persisted to localStorage.
  await page.goto(`${BASE}/#token=${TOKEN}`, { waitUntil: "domcontentloaded" });
  await page.goto(`${BASE}/home`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("text=AI-Team", { timeout: 15000 });
  await page.waitForTimeout(1500); // let the slow-poll data settle
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: true });
  console.log(`wrote ${OUT}/${name}.png`);
  await ctx.close();
}

await shoot("home-phone", { width: 390, height: 844 });
await shoot("home-desktop", { width: 1280, height: 900 });

// The usage meters sit below the node list; scroll the inner <main> to the
// bottom and frame them at phone width.
{
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  await page.goto(`${BASE}/#token=${TOKEN}`, { waitUntil: "domcontentloaded" });
  await page.goto(`${BASE}/home`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("text=Accounts & usage", { timeout: 15000 });
  await page.waitForTimeout(1500);
  await page.evaluate(() => {
    const main = document.querySelector("main");
    if (main) main.scrollTop = main.scrollHeight;
  });
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${OUT}/home-usage.png` });
  console.log(`wrote ${OUT}/home-usage.png`);
  await ctx.close();
}

await browser.close();
