// Visual smoke test of synthetic layouts. Native Feishu widgets are approximated;
// graphs use the same pinned VChart release documented by Feishu for 7.27+.
const { chromium } = require('../frontend/node_modules/playwright');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const root = path.resolve(__dirname, '..');
(async () => {
  const browser = await chromium.launch({channel: process.env.PLAYWRIGHT_CHANNEL || 'chrome'});
  try {
    const bundle = path.join(root, '.local/vchart-1.12.3.min.js');
    if (process.argv.includes('--fetch-chart')) {
      const request = await browser.newContext();
      const response = await request.request.get('https://unpkg.com/@visactor/vchart@1.12.3/build/index.min.js');
      if (!response.ok()) throw new Error('Unable to download pinned chart bundle');
      fs.writeFileSync(bundle, await response.body());
      await request.close();
    }
    const businessOnly = process.argv.includes('--business');
    if (!fs.existsSync(bundle)) throw new Error('Run once with --fetch-chart to cache the public chart bundle.');
    const directory = path.join(root, '.local/feishu-display');
    const businessNames = new Set(['growth', 'growth_quantity', 'price', 'margin', 'inventory_risk', 'stocking', 'business_combined']);
    const files = fs.readdirSync(directory).filter(f=>f.endsWith('.html') && (!businessOnly || businessNames.has(f.replace('.html', ''))));
    if (files.length < (businessOnly ? businessNames.size : 13)) throw new Error('Generate synthetic fixtures first.');
    let captures=0;
    for (const file of files) {
      for (const [label, width] of [['desktop',920],['mobile',390]]) {
        const page = await browser.newPage({viewport:{width,height:1000}});
        const errors=[];page.on('pageerror',e=>errors.push(e.message));
        await page.goto(pathToFileURL(path.join(directory,file)).href);
        await page.evaluate(()=>window.chartReady);
        if (errors.length) throw new Error(file+': '+errors.join('; '));
        if (await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)) throw new Error(file+': page overflow');
        const charts = await page.locator('.chart').count();
        if (charts && await page.locator('.chart canvas').count() < charts) throw new Error(file+': chart did not render');
        await page.screenshot({path:path.join(directory,file.replace('.html',`-${label}.png`)),fullPage:true});
        const next=page.getByRole('button',{name:'下一页'}).first();
        if (await next.count() && await next.isEnabled()) {
          await next.click();
          if (!(await page.locator('.pager span').first().innerText()).startsWith('2 /')) throw new Error('Pagination did not advance');
        }
        await page.locator('details').evaluateAll(nodes=>nodes.forEach(n=>n.open=true));
        if (await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)) throw new Error(file+': expanded overflow');
        if (businessOnly) await page.screenshot({path:path.join(directory,file.replace('.html',`-${label}-expanded.png`)),fullPage:true});
        await page.close(); captures++;
      }
    }
    console.log(`${captures} desktop/mobile captures passed: chart rendering, pagination, disclosure and no page overflow.`);
  } finally {await browser.close()}
})().catch(e=>{console.error(e.message);process.exit(1)});
