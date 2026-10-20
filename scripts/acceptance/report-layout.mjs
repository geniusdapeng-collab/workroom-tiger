/** Exercise the saved Python report's real tab controls and responsive layout. */
import { createHash } from 'node:crypto';
import { mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { resolve, dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { loadChromium } from './lib/playwright.mjs';

const options = {};
for (let i = 2; i < process.argv.length; i += 2) {
  const key = process.argv[i];
  const value = process.argv[i + 1];
  if (!['--html-dir', '--evidence-dir', '--executable'].includes(key) || !value) {
    throw new Error('Usage: node scripts/acceptance/report-layout.mjs --html-dir DIR --evidence-dir NEW_DIR [--executable CHROME]');
  }
  options[key] = resolve(value);
}
if (!options['--html-dir'] || !options['--evidence-dir']) {
  throw new Error('--html-dir and --evidence-dir are required');
}
const reportDir = options['--html-dir'];
const evidenceDir = options['--evidence-dir'];
// Keep red/green evidence immutable; callers choose a new directory per run.
await mkdir(evidenceDir, { recursive: false });
const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const files = (await readdir(reportDir)).filter((name) => name.endsWith('.html')).sort();
if (!files.length) throw new Error(`No HTML reports in ${reportDir}`);
const chromium = loadChromium(join(root, 'governance'));
const browser = await chromium.launch({
  headless: true,
  ...(options['--executable'] ? { executablePath: options['--executable'] } : {}),
});
const viewports = [{ width: 320, height: 844 }, { width: 390, height: 844 },
  { width: 768, height: 1024 }, { width: 1440, height: 900 }];
const tabs = [
  { name: 'today', controls: ['btn-daily', 'daily-btn-today'], panel: 'daily-pane-today' },
  { name: 'stock', controls: ['btn-daily', 'daily-btn-stock'], panel: 'daily-pane-stock' },
  { name: 'history', controls: ['btn-daily', 'daily-btn-history'], panel: 'daily-pane-history' },
  { name: 'sim', controls: ['btn-sim'], panel: 'tab-sim' },
  { name: 'philosophy', controls: ['btn-philosophy'], panel: 'tab-philosophy' },
];
const results = [];
const inputHashes = {};
const pageErrors = [];
const networkRequests = [];
try {
  for (const file of files) {
    const path = join(reportDir, file);
    inputHashes[file] = createHash('sha256').update(await readFile(path)).digest('hex');
    for (const viewport of viewports) {
      const context = await browser.newContext({ viewport, locale: 'zh-CN', reducedMotion: 'reduce' });
      await context.route('**/*', async (route) => {
        const url = route.request().url();
        if (url.startsWith('file:') || url.startsWith('data:')) await route.continue();
        else { networkRequests.push(url); await route.abort(); }
      });
      const page = await context.newPage();
      page.on('pageerror', (error) => pageErrors.push({ file, width: viewport.width, message: error.message }));
      await page.goto(pathToFileURL(path).href, { waitUntil: 'load' });
      for (const tab of tabs) {
        for (const id of tab.controls) await page.locator(`#${id}`).click();
        const panel = page.locator(`#${tab.panel}`);
        await panel.waitFor({ state: 'visible' });
        await panel.locator('details').evaluateAll((nodes) => nodes.forEach((node) => { node.open = true; }));
        const measurement = await page.evaluate((panelId) => {
          const active = document.getElementById(panelId);
          const visible = (el) => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
          const offenders = [...document.querySelectorAll('body *')].filter((el) => {
            if (!visible(el) || el.closest('svg')) return false;
            const box = el.getBoundingClientRect();
            // An actual local scroller may contain a wider table without moving the page.
            if (el.closest('.table-scroll')) return false;
            if (el.closest('.tape-wrap') || el.classList.contains('hero-bg')) return false;
            return box.right > innerWidth + 1 || box.left < -1;
          }).map((el) => ({ tag: el.tagName, class: el.className, id: el.id,
            width: Math.round(el.getBoundingClientRect().width),
            right: Math.round(el.getBoundingClientRect().right), text: el.textContent.slice(0, 100) })).slice(0, 20);
          const tables = [...active.querySelectorAll('table')].filter(visible).map((table) => {
            const parent = table.parentElement;
            return { columns: table.rows[0]?.cells.length ?? 0, cells: table.querySelectorAll('th,td').length,
              width: Math.round(table.getBoundingClientRect().width),
              containerWidth: parent.clientWidth, containerScrollWidth: parent.scrollWidth,
              localScroll: parent.classList.contains('table-scroll') && ['auto', 'scroll'].includes(getComputedStyle(parent).overflowX),
              focusable: parent.tabIndex === 0, accessibleName: parent.getAttribute('aria-label') };
          });
          return { viewportWidth: innerWidth, documentWidth: document.documentElement.scrollWidth,
            bodyOverflowX: getComputedStyle(document.body).overflowX,
            rootOverflowX: getComputedStyle(document.documentElement).overflowX,
            panelVisible: !!visible(active), textLength: active.innerText.length, offenders, tables };
        }, tab.panel);
        const failures = [];
        if (measurement.documentWidth > viewport.width + 1) failures.push('page-horizontal-overflow');
        if (['hidden', 'clip'].includes(measurement.bodyOverflowX) || ['hidden', 'clip'].includes(measurement.rootOverflowX)) {
          failures.push('page-overflow-hidden-instead-of-fixed');
        }
        if (!measurement.panelVisible || measurement.textLength < 20) failures.push('active-panel-content-missing');
        if (measurement.offenders.length) failures.push('content-outside-viewport');
        if (measurement.tables.some((table) => !table.localScroll || !table.focusable || !table.accessibleName)) {
          failures.push('table-content-not-accessible-in-local-scroller');
        }
        let keyboardScroll = null;
        const wideScroller = panel.locator('.table-scroll').filter({ visible: true });
        for (let i = 0; i < await wideScroller.count(); i++) {
          const scroller = wideScroller.nth(i);
          if (!await scroller.evaluate((el) => el.scrollWidth > el.clientWidth + 1)) continue;
          await scroller.focus();
          await page.keyboard.press('ArrowRight');
          const moved = await page.waitForFunction((el) => el.scrollLeft > 0,
            await scroller.elementHandle(), { timeout: 1500 }).then(() => true, () => false);
          keyboardScroll = moved && await scroller.evaluate((el) => el.scrollLeft > 0);
          if (!keyboardScroll) failures.push('wide-table-not-keyboard-scrollable');
          await scroller.evaluate((el) => { el.scrollLeft = 0; });
          break;
        }
        if (viewport.width === 390 || (viewport.width === 1440 && tab.name === 'today')) {
          await panel.scrollIntoViewIfNeeded();
          await page.screenshot({ path: join(evidenceDir, `${file.replace('.html', '')}-${viewport.width}-${tab.name}.png`), animations: 'disabled' });
        }
        results.push({ file, viewport, tab: tab.name, ...measurement, keyboardScroll, failures });
      }
      await context.close();
    }
  }
} finally {
  await browser.close();
}
const failed = results.filter((item) => item.failures.length);
const report = { command: process.argv, browser: 'independent headless Chrome session; no user profile or existing UI connection',
  inputHashes, cases: results, total: results.length, passed: results.length - failed.length,
  failed: failed.length, pageErrors, blockedNetworkRequests: networkRequests,
  exitCode: failed.length || pageErrors.length ? 1 : 0 };
await writeFile(join(evidenceDir, 'layout.json'), JSON.stringify(report, null, 2));
console.log(JSON.stringify({ total: report.total, passed: report.passed, failed: report.failed,
  pageErrors, evidenceDir, failures: failed.map(({ file, viewport, tab, documentWidth, failures }) =>
    ({ file, width: viewport.width, tab, documentWidth, failures })) }, null, 2));
process.exitCode = report.exitCode;
