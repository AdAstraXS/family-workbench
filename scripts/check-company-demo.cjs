// Local static preview, using a disposable browser profile and no external requests.
const {chromium} = require(process.env.WATCH_PLAYWRIGHT_PATH || 'playwright');
const assert = require('node:assert/strict');
const base = 'http://127.0.0.1:4318';
const key = 'family-company-research-preview-v1';
(async () => {
  const browser = await chromium.launch({channel:'msedge',headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1040}});
    const errors = [], methods = [];
    page.on('pageerror',error => errors.push(String(error)));
    await page.route('**/*',route => {
      const request = route.request();
      if (!request.url().startsWith(base + '/')) return route.abort();
      methods.push(request.method());
      return route.continue();
    });
    const saved = () => page.evaluate(k => JSON.parse(localStorage.getItem(k)), key);
    const fit = async () => assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true, 'page fits viewport');
    await page.goto(base + '/company.html');
    await page.locator('.report-title').waitFor();
    assert.equal(await page.getByRole('tab').count(),3);
    assert.equal(await page.locator('.assumption-card').count(),4);
    assert.match(await page.locator('.change-banner').innerText(),/3 项材料/);
    assert.match(await page.locator('.side-card').first().innerText(),/只读演示判断 v1/);
    await fit();
    await page.screenshot({path:'.watch-local/company-demo-desktop.png',fullPage:true});

    await page.locator('[data-evidence=earnings]').first().click();
    assert.equal(await page.locator('#evidence-dialog[open]').count(),1);
    for (const name of ['来源陈述','来源观点','引文与原文入口']) assert.equal(await page.getByRole('heading',{name,exact:true}).count(),1);
    assert.ok(await page.locator('#evidence-dialog blockquote').count());
    await page.locator('#evidence-dialog [data-close]').first().click();
    await page.getByRole('button',{name:'查看采用的资料 →'}).click();
    assert.equal(await page.locator('.evidence-table tbody tr').count(),1);
    assert.equal(await page.locator('#used-filter').inputValue(),'used');
    await page.getByRole('tab',{name:/最新变化/}).click();
    assert.equal(await page.locator('.change-item').count(),3);
    await page.locator('[data-mark=power-demo]').click();
    assert.match(await page.locator('[data-mark=power-demo]').innerText(),/已标记/);
    await page.locator('[data-select=rates-demo]').uncheck();
    await page.screenshot({path:'.watch-local/company-demo-changes.png',fullPage:true});
    await page.getByRole('tab',{name:'证据资料'}).click();
    await page.locator('#hypothesis-filter').selectOption('returns');
    assert.equal(await page.locator('.evidence-table tbody tr').count(),2);
    await page.locator('#type-filter').selectOption({label:'财报 / 官方披露'});
    assert.equal(await page.locator('.evidence-table tbody tr').count(),1);
    await page.locator('#evidence-search').fill('不存在的资料');
    assert.match(await page.locator('.empty-state').innerText(),/没有匹配/);
    await page.locator('#evidence-search').fill('');

    await page.locator('[data-action=review]').click();
    assert.equal(await page.locator('.research-only').isVisible(),false);
    await page.locator('#review-form input[name=materials]:checked').evaluateAll(inputs => inputs.forEach(input => {input.checked=false;}));
    await page.getByRole('button',{name:'生成评估预览'}).click();
    assert.match(await page.locator('#review-error').innerText(),/请选择一项/);
    await page.locator('#review-form [value=power-demo]').check();
    assert.equal(await page.locator('#review-error').innerText(),'');
    await page.screenshot({path:'.watch-local/company-demo-review.png',fullPage:true});
    await page.getByRole('button',{name:'生成评估预览'}).click();
    assert.match(await page.locator('#review-title').innerText(),/v2/);
    await page.getByRole('button',{name:'查看新研究 →'}).click();
    const v2 = await saved();
    assert.deepEqual(v2.snapshots.map(s => s.materials),[['earnings'],['earnings','power-demo']]);
    assert.match(await page.locator('.change-banner').innerText(),/2 项材料/);
    await page.reload();
    await page.locator('.report-title').waitFor();
    assert.match(await page.locator('.analysis-kicker').innerText(),/v2/);
    await page.locator('[data-action=history]').click();
    assert.equal(await page.locator('.history-item').count(),2);
    await page.locator('.history-item').last().locator('summary').click();
    assert.match(await page.locator('.history-item').last().innerText(),/没有可归因于 AI 投入/);
    await page.locator('#history-dialog [data-close]').click();

    await page.locator('[data-action=review]').click();
    await page.locator('#review-form [value=full]').check();
    await page.locator('#review-form [name=researchOnly]').check();
    assert.equal(await page.locator('#review-form input[name=materials]:disabled').count(),2);
    await page.getByRole('button',{name:'生成评估预览'}).click();
    await page.getByRole('button',{name:'查看新研究 →'}).click();
    const v3 = await saved();
    assert.deepEqual(v3.snapshots[2].materials,['earnings']);
    assert.deepEqual(v3.snapshots.slice(0,2),v2.snapshots,'previous research versions remain unchanged');
    assert.match(await page.locator('.side-card').first().innerText(),/只读演示判断 v1/);
    await page.locator('#reset-demo').click();
    assert.equal((await saved()).snapshots.length,1);

    await page.setViewportSize({width:390,height:844});
    await fit();
    await page.screenshot({path:'.watch-local/company-demo-mobile.png',fullPage:true});
    for (const name of [/最新变化/,'证据资料','研究结论']) {
      await page.getByRole('tab',{name}).click();
      await fit();
    }
    await page.locator('[data-action=review]').click();
    await page.locator('#review-form [value=full]').check();
    await fit();
    assert.equal(await page.locator('#review-dialog').evaluate(dialog => dialog.scrollWidth <= dialog.clientWidth + 1),true,'mobile dialog fits');
    await page.screenshot({path:'.watch-local/company-demo-mobile-review.png',fullPage:true});
    await page.locator('#review-dialog [data-close]').first().click();
    await page.setViewportSize({width:1440,height:1040});
    await page.locator('.sidebar a[href="/#topics"]').click();
    await page.getByRole('heading',{name:'按主题看投资新闻'}).waitFor();
    await page.locator('.sidebar a[href="/company.html"]').click();
    await page.locator('.report-title').waitFor();
    assert.deepEqual(errors,[]);
    assert.ok(methods.every(method => method === 'GET'),'demo never submits a server write');
    console.log('PASS: evidence, filters, scope selection, incremental/full research, frozen history, persistence, reset, navigation and mobile layout.');
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exitCode=1;});
