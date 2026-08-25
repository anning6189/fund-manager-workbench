const { chromium } = require('playwright');
const path = require('path');
(async () => {
  const outDir = __dirname;
  const browser = await chromium.launch({ channel: 'chrome' });
  const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
  await page.goto('http://127.0.0.1:8765/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(3200);
  const picks = await page.$$('.pick-card');
  console.log('大事件卡片数:', picks.length);
  if (picks.length) {
    await picks[0].click();
    await page.waitForTimeout(1200);
    await page.screenshot({ path: path.join(outDir, 'pick-drawer.png') });
    const btn = await page.$('.ev-actions a');
    console.log('来源按钮:', btn ? await btn.innerText() : '无');
  }
  await browser.close();
  console.log('DONE');
})();
