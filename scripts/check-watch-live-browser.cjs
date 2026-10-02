// Separate local acceptance server and disposable browser profile only.
const {chromium} = require(process.env.WATCH_PLAYWRIGHT_PATH || 'playwright');
const assert = require('node:assert/strict');
(async()=>{
  const base='http://127.0.0.1:4325';
  const browser=await chromium.launch({channel:'msedge',headless:true});
  try {
    const page=await browser.newPage({viewport:{width:1440,height:960}});
    const errors=[]; page.on('pageerror',e=>errors.push(String(e)));
    const visit=async path=>{
      const r=await page.goto(base+path); assert.equal(r.status(),200,path);
    };
    await visit('/research/watch/news/');
    if(await page.locator('[name=username]').count()){
      await page.locator('[name=username]').fill('watch-preview');
      assert.ok(process.env.WATCH_PREVIEW_PASSWORD,'Provide the isolated preview password through WATCH_PREVIEW_PASSWORD.');
      await page.locator('[name=password]').fill(process.env.WATCH_PREVIEW_PASSWORD);
      await page.getByRole('button',{name:'登录',exact:true}).click();
    }
    await visit('/research/watch/news/?source=microsoft-blog&q=AMD');
    assert.equal(await page.locator('article.iw-card').count(),1);
    await page.locator('article.iw-card h3 a').click();
    assert.match(await page.locator('body').innerText(),/仅展示可用简介与摘录/);
    const detailPath=new URL(page.url()).pathname;
    await page.locator('[name=saved]').check();
    await page.locator('[name=read]').check();
    await page.locator('[name=note]').fill('真实信源验收：核对算力需求，等待现金流和收入证据。');
    await page.getByRole('button',{name:'保存',exact:true}).click();
    await visit(detailPath);
    assert.equal(await page.locator('[name=saved]').isChecked(),true);
    assert.match(await page.locator('[name=note]').inputValue(),/等待现金流和收入证据/);
    await page.locator('#company-search').fill('MSFT');
    const option=page.locator('#company-selection option').filter({hasText:'MSFT'});
    await page.locator('#company-selection').selectOption(await option.getAttribute('value'));
    await page.getByRole('button',{name:'关联到所选公司'}).click();
    const itemPath=new URL(page.url()).pathname;
    assert.match(itemPath,/\/items\/\d+\//);
    assert.equal(await page.getByRole('heading',{name:'来源陈述（待核实）',exact:true}).count()>=4,true);
    await page.locator('[name=selected]').check();
    await page.getByRole('button',{name:'保存材料选择'}).click();
    await visit(itemPath);
    assert.equal(await page.locator('[name=selected]').isChecked(),true);
    await page.screenshot({path:'.watch-local/live-evidence-desktop.png',fullPage:true});
    await page.getByRole('link',{name:'公司研究 →',exact:true}).click();
    assert.match(await page.locator('body').innerText(),/微软|MSFT|Microsoft/);
    await page.screenshot({path:'.watch-local/live-research-desktop.png',fullPage:true});
    await visit('/research/watch/items/7/');
    assert.equal(await page.locator('summary').filter({hasText:'历史分析'}).count()>=4,true);
    assert.equal(await page.getByRole('button',{name:'追加复核记录'}).count(),4);
    await page.screenshot({path:'.watch-local/live-analysis-history.png',fullPage:true});
    for(const width of [1440,390]){
      await page.setViewportSize({width,height:width===390?844:960});
      for(const path of ['/research/watch/news/','/research/watch/topics/',
        '/research/watch/items/?dossier=1&direction=unknown','/research/watch/rules/',
        '/research/watch/coverage/','/research/watch/sources/add/',detailPath,itemPath]){
        await visit(path);
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,`${width}: ${path}`);
      }
    }
    await visit('/research/watch/news/?source=zhitong');
    assert.match(await page.locator('body').innerText(),/日期未知/);
    await page.screenshot({path:'.watch-local/live-news-mobile.png',fullPage:true});
    await visit('/research/watch/coverage/');
    assert.match(await page.locator('body').innerText(),/模型总开关：关闭/);
    await page.screenshot({path:'.watch-local/live-coverage-mobile.png',fullPage:true});
    assert.deepEqual(errors,[]);
    console.log('Live browser passed: real-source filters, original excerpt, persistent bookmark/note, MSFT association, material selection, research navigation, current/history separation, desktop/mobile.');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
