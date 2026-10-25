import { test, expect, type Page } from "@playwright/test";

// Offline browser coverage only: every API request is intercepted. No agent or
// paid-provider endpoint is contacted, and fixtures never ship in the UI.
test.use({ baseURL: "http://127.0.0.1:5187", viewport: { width: 1440, height: 1100 } });
const repo = "/workspace/AI-team";
const otherRepo = "/workspace/design-system";
type Usage = { input: number; output: number; cache_read: number; cache_creation: number; total: number };
const tokens = (n: number): Usage => ({ input: n * .2, output: n * .1, cache_read: n * .6, cache_creation: n * .1, total: n });
const usd = (known: number, unpriced = 0, total = 1) => ({ known, unpriced_tokens: unpriced, coverage_pct: total ? Math.round((1 - unpriced / total) * 1000) / 10 : 100 });
const bucket = (known: number, n: number, unpriced = 0) => ({ tokens: tokens(n), usd: usd(known, unpriced, n) });
const records = [
  { date: "2026-10-20", model: "claude-sonnet-4-6", project: repo, value: 24.35, n: 2_000_000 },
  { date: "2026-10-21", model: "claude-opus-4-6", project: repo, value: 72.8, n: 3_000_000 },
  { date: "2026-10-22", model: "gpt-5.4", project: otherRepo, value: 18.2, n: 1_000_000 },
  { date: "2026-10-23", model: "claude-sonnet-4-6", project: repo, value: 41.65, n: 4_000_000 },
  { date: "2026-10-24", model: "claude-opus-4-6", project: repo, value: 95.1, n: 6_000_000 },
  { date: "2026-10-25", model: "gpt-5.4", project: otherRepo, value: 38.4, n: 5_000_000 },
  { date: "2026-10-26", model: "unpriced-preview", project: repo, value: 0, n: 1_000_000 },
];
type Options = { empty?: boolean; unpriced?: boolean; failProjects?: boolean; failDaily?: boolean; delayDaily?: number; longLabels?: boolean };

async function setup(page: Page, options: Options = {}) {
  await page.clock.setFixedTime(new Date("2026-10-26T12:00:00Z"));
  await page.addInitScript(() => localStorage.setItem("ai_team_dash_token", "offline-browser-fixture"));
  const requests: URL[] = [];
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    requests.push(url);
    const reply = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/api/events/stream") return route.fulfill({ status: 200, contentType: "text/event-stream", body: ": fixture\n\n" });
    if (url.pathname === "/api/cost/explorer") {
      const daily = url.searchParams.get("granularity") === "day";
      if (options.failDaily && daily || options.failProjects && !daily) return reply({ detail: "Fixture outage" }, 500);
      if (daily && options.delayDaily) await new Promise((resolve) => setTimeout(resolve, options.delayDaily));
      const from = url.searchParams.get("from")!;
      const to = url.searchParams.get("to")!;
      const path = url.searchParams.get("repo_path");
      const items = options.empty ? [] : records.filter((item) => (!path || item.project === path) && item.date >= from.slice(0, 10) && item.date <= to.slice(0, 10));
      const sums = new Map<string, { value: number; n: number; unpriced: number }>();
      for (const item of items) {
        const key = daily ? `${item.date}|${item.model}` : item.project;
        const prior = sums.get(key) ?? { value: 0, n: 0, unpriced: 0 };
        sums.set(key, { value: prior.value + (options.unpriced ? 0 : item.value), n: prior.n + item.n, unpriced: prior.unpriced + (options.unpriced || item.value === 0 ? item.n : 0) });
      }
      const series = [...sums.entries()].map(([key, value]) => {
        const [date, model] = key.split("|");
        return { ...bucket(value.value, value.n, value.unpriced),
          bucket: daily ? date : "", dim: options.longLabels ? "very-long-model-or-project-name-".repeat(8) + key : daily ? model : key, models: [] };
      });
      const total = items.reduce((sum, item) => sum + item.n, 0);
      const unpriced = options.unpriced ? total : items.filter((item) => item.value === 0).reduce((sum, item) => sum + item.n, 0);
      return reply({ ok: true, dimension: daily ? "model" : "project", granularity: daily ? "day" : "none", from, to, repo_path: path,
        series, totals: bucket(options.unpriced ? 0 : items.reduce((sum, item) => sum + item.value, 0), total, unpriced),
        unattributed: { ...bucket(options.empty ? 0 : 4.2, options.empty ? 0 : 100_000), models: [] } });
    }
    if (url.pathname === "/api/cost/projects") return reply({ projects: [{ repo_path: repo, tokens: tokens(16_000_000) }, { repo_path: otherRepo, tokens: tokens(6_000_000) }, { repo_path: "<no repo path>", tokens: tokens(1000) }] });
    if (url.pathname === "/api/cost/top") return reply({ by: url.searchParams.get("by"), rows: options.empty ? [] : [
      { ...bucket(150.25, 8_000_000), session_id: "session-alpha-123", repo_path: repo, backend: "claude", role: "worker", models: [{ model: "claude-opus-4-6", known: true, usd_total: 150.25, tokens_total: 8_000_000 }] },
      { ...bucket(75.5, 7_000_000), session_id: "session-beta-456", repo_path: otherRepo, backend: "codex", role: "manager", models: [{ model: "gpt-5.4", known: true, usd_total: 75.5, tokens_total: 7_000_000 }] },
    ], totals: bucket(225.75, 15_000_000) });
    if (url.pathname === "/api/cost/alerts") return reply({ enabled: false, budgets: {}, alerts: [], enforcement: {} });
    if (url.pathname === "/api/work/affiliations/sessions") return reply({ affiliations: [{ session_id: "session-alpha-123", flow_run_id: "case-one", role: "worker", objective_lock: "Ship the analytics dashboard" }] });
    if (url.pathname === "/api/cases/case-one/usage") return reply({ flow_run_id: "case-one", case: { objective_lock: "Ship the analytics dashboard" }, sessions: [{ ...bucket(120, 5_000_000), session_id: "session-alpha-123", role: "worker", models: [] }], mgr_vs_workers: { manager: bucket(30, 1_000_000), workers: bucket(120, 5_000_000), workers_share_pct: 80, worker_sessions: 1 }, totals: bucket(150, 6_000_000) });
    if (url.pathname === "/api/sessions") return reply({ sessions: [] });
    if (url.pathname.includes("health")) return reply({ status: "ok" });
    return reply({ ok: true, events: [], alerts: [], sessions: [], nodes: [] });
  });
  await page.goto("/cost");
  await expect(page.getByRole("heading", { name: "Cost overview." })).toBeVisible();
  return requests;
}

test("desktop chart, exact data, metric controls, CSV export and screenshot", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await setup(page);
  await expect(page.getByRole("region", { name: "Cost summary", exact: true })).toContainText("$290.5");
  await expect(page.getByRole("region", { name: "Model mix", exact: true })).toContainText("unpriced tokens");
  await page.screenshot({ path: "/tmp/cost-desktop.png" });
  await page.getByRole("group", { name: "Timeline metric" }).getByRole("button", { name: "Tokens" }).click();
  await expect(page.getByRole("region", { name: "Spend over time", exact: true })).toContainText("22.0M");
  await page.getByLabel("Inspect UTC day").selectOption("2026-10-24");
  await expect(page.getByRole("region", { name: "Spend over time", exact: true })).toContainText("6.0M");
  await page.getByText("View daily data", { exact: true }).click();
  await expect(page.getByRole("table")).toBeVisible();
  await expect(page.getByRole("table")).toContainText("6,000,000");
  const downloadEvent = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export daily CSV" }).click();
  const download = await downloadEvent;
  expect(download.suggestedFilename()).toBe("cost-daily-2026-10-19.csv");
  await download.saveAs("/tmp/cost-daily-export.csv");
  await page.getByRole("heading", { name: "Top sessions", exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: "/tmp/cost-desktop-sessions.png" });
  expect(errors).toEqual([]);
});

for (const width of [320, 390, 768]) {
  test(`no horizontal overflow at ${width}px with long labels`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await setup(page, { longLabels: true });
    await expect(page.getByRole("region", { name: "Cost summary", exact: true })).toContainText("$290.5");
    const overflow = await page.evaluate(() => [...document.querySelectorAll(".cost-dashboard, .cost-panel, main")].map((el) => ({ width: el.clientWidth, scroll: el.scrollWidth })).filter((el) => el.scroll > el.width + 1));
    expect(overflow).toEqual([]);
    const rangeTarget = await page.getByRole("button", { name: "24h", exact: true }).boundingBox();
    expect(rangeTarget?.height).toBeGreaterThanOrEqual(44);
    const caseTarget = await page.getByRole("button", { name: /View case lifetime cost/ }).boundingBox();
    expect(caseTarget?.width).toBeGreaterThanOrEqual(44);
    expect(caseTarget?.height).toBeGreaterThanOrEqual(44);
    await page.screenshot({ path: `/tmp/cost-${width}.png` });
  });
}

test("range, project, sort and refreshed rolling boundaries reach the existing APIs", async ({ page }) => {
  const requests = await setup(page);
  await page.getByRole("button", { name: "24h", exact: true }).click();
  await expect.poll(() => requests.filter((url) => url.pathname === "/api/cost/top").at(-1)?.searchParams.get("from")).toBe("2026-10-25T12:00:00.000Z");
  await page.getByLabel("Filter by project").selectOption(repo);
  await expect.poll(() => requests.filter((url) => url.pathname === "/api/cost/top").at(-1)?.searchParams.get("repo_path")).toBe(repo);
  await page.getByRole("group", { name: "Session ranking" }).getByRole("button", { name: "Tokens" }).click();
  await expect.poll(() => requests.filter((url) => url.pathname === "/api/cost/top").at(-1)?.searchParams.get("by")).toBe("tokens");
  await page.clock.setFixedTime(new Date("2026-10-26T13:00:00Z"));
  await page.getByRole("button", { name: "Refresh cost data" }).click();
  await expect.poll(() => requests.filter((url) => url.pathname === "/api/cost/explorer").at(-1)?.searchParams.get("to")).toBe("2026-10-26T13:00:00.000Z");
});

test("case dialog has lifetime scope, traps focus, closes with Escape and returns focus", async ({ page }) => {
  await setup(page);
  const trigger = page.getByRole("button", { name: "View case lifetime cost: Ship the analytics dashboard" });
  await trigger.click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("Case lifetime usage");
  await expect(dialog).toContainText("$120.00");
  for (let i = 0; i < 5; i++) {
    await page.keyboard.press("Tab");
    expect(await dialog.evaluate((el) => el.contains(document.activeElement))).toBe(true);
  }
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  await expect(trigger).toBeFocused();
});

test("a failed project panel leaves summary and model data visible", async ({ page }) => {
  await setup(page, { failProjects: true });
  await expect(page.getByRole("region", { name: "Project allocation", exact: true })).toContainText("unavailable");
  await expect(page.getByRole("region", { name: "Cost summary", exact: true })).toContainText("$290.5");
  await expect(page.getByRole("region", { name: "Model mix", exact: true })).toContainText("claude-opus");
});

test("empty usage and all-unpriced usage are distinct", async ({ page }) => {
  await setup(page, { empty: true });
  await expect(page.getByRole("region", { name: "Spend over time", exact: true })).toContainText("No recorded usage");
  await expect(page.getByRole("region", { name: "Cost summary", exact: true })).not.toContainText("100.0%");
  await page.unroute("**/api/**");
  await setup(page, { unpriced: true });
  await expect(page.getByRole("region", { name: "Spend over time", exact: true })).toContainText("Usage recorded without a known price");
  await expect(page.getByRole("region", { name: "Cost summary", exact: true })).toContainText("0.0%");
});

test("new scope never shows the previous project's data while loading", async ({ page }) => {
  await setup(page, { delayDaily: 400 });
  await expect(page.getByRole("region", { name: "Cost summary", exact: true })).toContainText("$290.5");
  await page.getByLabel("Filter by project").selectOption(otherRepo);
  await expect(page.getByRole("region", { name: "Cost summary", exact: true })).not.toContainText("$290.5");
  await expect(page.getByRole("region", { name: "Cost summary", exact: true })).toContainText("$56.60");
});
