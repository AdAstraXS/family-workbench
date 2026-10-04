// Untrusted pages are parsed as inert DOM: no scripts, resources, or browser cookies.
// The source bind mount must not shadow the image's verified Linux dependencies.
const path = require('node:path');
const load = process.env.NODE_PATH
  ? require('node:module').createRequire(path.join(process.env.NODE_PATH.split(path.delimiter)[0], '_runtime.cjs'))
  : require;
const { JSDOM } = load('jsdom');
const { Readability } = load('@mozilla/readability');
const fs = require('node:fs');

const VERSION = 'readability-0.6.0-v1';
if (process.argv.includes('--check')) {
  process.stdout.write(VERSION);
  process.exit(0);
}
const MIN_TEXT = 120;
const NOISE = /(?:^|[\s_-])(?:ads?|advert(?:isement|ising)?|banner|comments?|commentbox|feedcommentbox|disqus|recommend(?:ed|ation|ations|box)?|related(?:posts|articles|topic)?|social-share|share-buttons|newsletter|cookie-banner|quicklike|footbar)(?:$|[\s_-])/i;
const DROP_TAGS = 'script,style,noscript,iframe,form,button,nav,footer,aside,[hidden],[aria-hidden="true"]';

function clean(root) {
  for (const el of Array.from(root.querySelectorAll(DROP_TAGS))) el.remove();
  for (const el of Array.from(root.querySelectorAll('*'))) {
    const style = el.getAttribute('style') || '';
    if (/(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\s*(?:!important)?\s*(?:;|$)/i.test(style)
        || NOISE.test(`${el.id} ${el.className}`)) el.remove();
  }
  for (const img of Array.from(root.querySelectorAll('img'))) {
    const width = Number(img.getAttribute('width'));
    const height = Number(img.getAttribute('height'));
    const raw = img.getAttribute('data-fullres-src') || img.getAttribute('data-original')
      || img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || '';
    const srcset = img.getAttribute('data-srcset') || img.getAttribute('srcset') || '';
    const candidate = raw || (srcset ? srcset.split(',').pop().trim().split(/\s+/)[0] : '') || img.getAttribute('src') || '';
    if ((img.hasAttribute('width') && img.hasAttribute('height') && width <= 1 && height <= 1)
        || /(?:^|\s)boundary-pic(?:\s|$)/i.test(img.className)
        || /\/\/(?:record|pixel|tracking)\./i.test(candidate)) {
      img.remove();
      continue;
    }
    if (candidate) img.setAttribute('src', candidate);
    // Readability must not restore a tracking placeholder over the chosen lazy image.
    for (const name of ['srcset', 'data-srcset', 'data-src', 'data-original', 'data-lazy-src', 'data-fullres-src']) img.removeAttribute(name);
  }
}

function extract(inputHtml, url) {
  const dom = new JSDOM(inputHtml, { url });
  try {
    const doc = dom.window.document;
    // Metadata and title remain available before noise removal. Never execute JSON-LD.
    clean(doc.body);
    let method = 'mozilla-readability';
    const host = new URL(url).hostname.toLowerCase();
    const selector = host === 'news.futunn.com' ? '#content .origin_content, #content' :
      host === 'mp.weixin.qq.com' ? '#js_content' : null;
    const scoped = selector && doc.querySelector(selector);
    if (scoped && scoped.textContent.trim().length >= MIN_TEXT) {
      const chosen = scoped.cloneNode(true);
      doc.body.replaceChildren(chosen);
      method += host === 'news.futunn.com' ? '+futunn' : '+wechat';
    }
    const article = new Readability(doc, { charThreshold: MIN_TEXT, maxElemsToParse: 30000, keepClasses: true }).parse();
    if (!article || !article.content) return null;
    const output = new JSDOM(article.content, { url });
    try {
      clean(output.window.document.body);
      const body = output.window.document.body;
      const text = body.textContent.replace(/\s+/g, ' ').trim();
      const linkText = Array.from(body.querySelectorAll('a')).reduce((n, a) => n + a.textContent.trim().length, 0);
      if (text.length < MIN_TEXT || linkText / Math.max(text.length, 1) > 0.5) return null;
      // A login/paywall with no substantial article must not be labelled a complete archive.
      if (text.length < 600 && /登录后(?:查看|阅读)|订阅后(?:查看|阅读)|付费(?:解锁|阅读)|sign in to (?:read|continue)|subscribe to continue/i.test(text)) return null;
      return { html: body.innerHTML, title: article.title || '', author: article.byline || '',
        method, version: VERSION, text_length: text.length, image_count: body.querySelectorAll('img').length };
    } finally { output.window.close(); }
  } finally { dom.window.close(); }
}

try {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  let result = null;
  for (const source of [input.rawHtml, input.html]) {
    if (typeof source !== 'string' || !source.includes('<')) continue;
    result = extract(source, input.url);
    if (result) break;
  }
  process.stdout.write(JSON.stringify(result ? { ok: true, article: result } : { ok: false, reason: 'uncertain' }));
} catch (_) {
  // Do not echo page content, embedded secrets, source URLs, or interpreter traces.
  process.stdout.write(JSON.stringify({ ok: false, reason: 'parser' }));
  process.exitCode = 1;
}
