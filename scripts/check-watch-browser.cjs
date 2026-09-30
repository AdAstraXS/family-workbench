// Isolated local preview only. Uses a separate, disposable browser profile.
const { chromium } = require(process.env.WATCH_PLAYWRIGHT_PATH || 'playwright');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({channel:'msedge', headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1360,height:960}});
    const errors=[];
    page.on('pageerror',e=>errors.push(String(e)));
    const base='http://127.0.0.1:4319';
    await page.goto(base+'/research/watch/rules/');
    if(await page.locator('[name=username]').count()){
      await page.locator('[name=username]').fill('watch-preview');
      await page.locator('[name=password]').fill('watch-local-preview');
      await page.getByRole('button',{name:'登录',exact:true}).click();
    }
    await page.goto(base+'/research/watch/companies/add/');
    await page.locator('[name=name]').fill('苹果（本机验收）');
    await page.locator('[name=symbol]').fill('AAPL');
    await page.locator('[name=market]').selectOption('US');
    await page.getByRole('button',{name:'添加并设置关注'}).click();
    assert.match(await page.locator('body').innerText(),/苹果|Apple/);
    const card=page.locator('section.iw-card').filter({hasText:'AAPL'}).first();
    await card.locator('[name=aliases]').fill('Apple，苹果');
    await card.getByRole('button',{name:'预览匹配结果'}).click();
    assert.match(await page.locator('body').innerText(),/未保存规则/);
    await page.getByRole('button',{name:'确认保存并匹配'}).click();
    await page.locator('section.iw-card').filter({hasText:'AAPL'}).first().getByRole('link',{name:'查看公司研究 →'}).click();
    for(const title of ['投研证据分析','新闻证据分析','综合分析'])
      assert.equal(await page.getByRole('heading',{name:title,exact:true}).count(),1);
    await page.screenshot({path:'.watch-local/company-review.png',fullPage:true});
    await page.goto(base+'/research/watch/topics/');
    assert.match(await page.locator('body').innerText(),/我的关注公司/);
    assert.equal(await page.locator('a.iw-card').filter({hasText:'AAPL'}).count(),1);
    await page.goto(base+'/research/watch/news/');
    await page.locator('article.iw-card h3 a').first().click();
    assert.equal(await page.getByRole('heading',{name:'事件与证据关系'}).count(),1);
    await page.locator('#company-search').fill('AAPL');
    const option=page.locator('#company-selection option').filter({hasText:'AAPL'});
    await page.locator('#company-selection').selectOption(await option.getAttribute('value'));
    await page.getByRole('button',{name:'关联到所选公司'}).click();
    await page.getByRole('link',{name:'公司研究 →',exact:true}).click();
    assert.match(await page.locator('h1').innerText(),/AAPL/);
    await page.goto(base+'/research/watch/sources/add/');
    assert.equal(await page.getByRole('button',{name:'测试读取'}).count(),1);
    await page.screenshot({path:'.watch-local/source-template.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    for(const path of ['/research/watch/sources/add/','/research/watch/topics/','/research/watch/rules/']){
      await page.goto(base+path);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,'mobile overflow '+path);
    }
    await page.screenshot({path:'.watch-local/watch-mobile.png',fullPage:true});
    assert.deepEqual(errors,[]);
    console.log('Browser: add second company, rule preview/save, research sections, source template, responsive pages passed.');
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1;});
