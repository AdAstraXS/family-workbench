export const directions = {support:'支持部分假设',weaken:'可能削弱',mixed:'影响有分歧',unknown:'证据不足'};
export const split = value => [...new Set(String(value).split(/[,，\n]/u).map(x=>x.trim()).filter(Boolean))];
export function contains(text, term) {
  const t = String(text).normalize('NFKC').toLowerCase(), n = String(term).normalize('NFKC').toLowerCase().trim();
  if (!n) return false;
  if (/^[a-z0-9 ._-]+$/u.test(n)) return new RegExp(`(^|[^a-z0-9])${n.replace(/[.*+?^${}()|[\]\\]/gu,'\\$&')}($|[^a-z0-9])`,'u').test(t);
  return t.includes(n);
}
export function match(rule, article) {
  if (!rule.enabled) return {eligible:false,reason:'关注已暂停'};
  const text = `${article.originalTitle} ${article.sourceText}`;
  const excluded = rule.exclude.filter(x=>contains(text,x));
  if (excluded.length) return {eligible:false,reason:`命中排除词：${excluded.join('、')}`};
  if (rule.include.length && !rule.include.some(x=>contains(text,x))) return {eligible:false,reason:'未命中包含词（任一）'};
  const hits = [...rule.aliases,...rule.topics].filter(x=>contains(text,x));
  return {eligible:!!hits.length,reason:hits.length?`命中 ${hits.join('、')}；只代表候选相关性`:'当前规则未召回；可在关注设置预览关联词'};
}
export function select(data, state) {
  return data.articles.filter(a => (state.showSynthetic || a.kind !== 'synthetic') &&
    (state.view === 'saved' ? state.members[a.id]?.saved : state.members[a.id]?.researchCandidate || match(state.rule,a).eligible) &&
    `${a.title} ${a.summary}`.toLowerCase().includes(state.search.toLowerCase()) &&
    (a.links.length ? a.links.some(l=>(state.assumption==='all'||l.assumption===state.assumption) && (state.direction==='all'||l.direction===state.direction)) : state.assumption==='all' && ['all','unknown'].includes(state.direction)));
}
export function selectNews(data,state){
  return data.articles.filter(a=>(state.showSynthetic||a.kind!=='synthetic') &&
    (!state.newsTopic||state.newsTopic==='all'||data.topics?.find(t=>t.id===state.newsTopic)?.articles.includes(a.id)) &&
    (!state.newsMarket||state.newsMarket==='all'||a.market===state.newsMarket) &&
    (!state.newsCategory||state.newsCategory==='all'||a.category===state.newsCategory) &&
    (!state.newsSource||state.newsSource==='all'||a.source===state.newsSource) &&
    (!state.dateFrom||(a.published&&a.published>=state.dateFrom)) &&
    (!state.dateTo||(a.published&&a.published<=state.dateTo)) &&
    `${a.newsTitle} ${a.newsSummary}`.toLowerCase().includes((state.newsSearch??'').toLowerCase())
  ).sort((a,b)=>(b.published??'').localeCompare(a.published??''));
}
export const chains = article => new Set(article.sources.map(s=>s.chain)).size;
export const stale = (article,state) => (article.thesisVersion ?? 1) !== state.thesis.version;
