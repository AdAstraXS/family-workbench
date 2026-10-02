const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/gu, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const key = 'family-company-research-preview-v1';
const candidateIds = ['copilot', 'power-demo', 'rates-demo'];
const labels = {support:'历史依据有支持',unknown:'证据仍不足',watch:'兑现条件待核对',mixed:'存在不同作用'};
let data, state, activeTab = 'conclusion', typeFilter = 'all', assumptionFilter = 'all', usedFilter = 'all', search = '', showAll = false;
const article = id => data.articles.find(a => a.id === id);
const latest = () => state.snapshots.at(-1);
const pending = () => candidateIds.filter(id => !latest().materials.includes(id));
const selectedCount = () => state.selected.filter(id => pending().includes(id)).length;
const typeOf = id => id === 'earnings' ? '财报 / 官方披露' : id === 'copilot' ? '公司新闻 / 产品公告' : id === 'power-demo' ? '行业资料' : '宏观资料';
const statusTag = (kind, text) => `<span class="pill ${esc(kind)}">${esc(text)}</span>`;
const sampleTag = a => statusTag(a.kind === 'synthetic' ? 'synthetic' : 'unknown', a.kind === 'synthetic' ? '模拟场景' : '历史资料 · ' + a.published);
const sourceLink = s => s.url?.startsWith('https://') ? `<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.name)} ↗</a>` : esc(s.name);
function toast(message) { $('#toast').textContent = message; $('#toast').style.display = 'block'; clearTimeout(toast.timer); toast.timer = setTimeout(() => $('#toast').style.display = 'none', 3400); }
function persist() { try { localStorage.setItem(key, JSON.stringify(state)); } catch { toast('本次操作仅保留在当前页面，浏览器未允许本地保存。'); } }

function assessmentRows(materials) {
  const power = materials.includes('power-demo'), rates = materials.includes('rates-demo');
  return [
    {id:'demand',status:'unknown',summary:materials.includes('copilot') ? '产品收费提供商业化线索，实际采用、付费用户与用量仍需验证。' : '云收入增长提供间接线索，不能独自证明 AI 算力需求持续增加。'},
    {id:'returns',status:power ? 'watch' : 'unknown',summary:power ? '接电延期场景提出兑现条件变化；必须核实是否涉及微软项目，才能判断投入回报影响。' : '已有收入披露，没有可归因于 AI 投入的现金流或回报率依据。'},
    {id:'growth',status:'support',summary:rates ? '历史季度云增长有披露；预算趋于谨慎场景提出未来需求风险，需核对实际订单。' : '历史季度的 Azure 收入披露有支持；AI 独立贡献和未来增长仍需拆解。'},
    {id:'valuation',status:rates ? 'mixed' : 'unknown',summary:rates ? '模拟场景中利率和盈利预期有不同作用；目前不能确定估值倍数的方向。' : '经营增长尚不足以证明估值倍数会提升，需要盈利预期、利率和风险溢价依据。'},
  ];
}

function snapshot(materials, mode, version) {
  const includesScenario = materials.some(id => article(id).kind === 'synthetic');
  return {version, mode, materials:[...materials], created:version === 1 ? '初始研究样例' : new Date().toLocaleString('zh-CN', {hour12:false}),
    headline:materials.includes('power-demo') ? '云增长有历史依据，投入兑现条件需要再核对' : '云增长已有历史依据，投入回报仍待验证',
    summary:includesScenario ? '这次演示把所选外部场景放回原有假设。它们提出核查问题，尚不足以认定微软经营已经受到影响；正式判断仍需实际公司资料验证。' : '历史云收入披露可以支持当期业务增长。AI 收入贡献、投入回报及估值条件仍有缺口，当前样例不能据此提高长期回报预期。',
    assessments:assessmentRows(materials)};
}
function defaults() { return {schema:1, selected:[...candidateIds], marked:[], snapshots:[snapshot(['earnings'], 'baseline', 1)]}; }
function valid(value) {
  const ids = ['earnings', ...candidateIds];
  const hypothesisIds = data.thesis.assumptions.map(a => a.id);
  return value?.schema === 1 && Array.isArray(value.selected) && value.selected.every(id => candidateIds.includes(id)) && Array.isArray(value.marked) && value.marked.every(id => candidateIds.includes(id)) && Array.isArray(value.snapshots) && value.snapshots.length > 0 && value.snapshots.every((s, i) => s.version === i + 1 && ['baseline','incremental','full'].includes(s.mode) && typeof s.created === 'string' && typeof s.headline === 'string' && typeof s.summary === 'string' && Array.isArray(s.materials) && s.materials.includes('earnings') && new Set(s.materials).size === s.materials.length && s.materials.every(id => ids.includes(id)) && Array.isArray(s.assessments) && s.assessments.length === 4 && new Set(s.assessments.map(row => row.id)).size === 4 && s.assessments.every(row => hypothesisIds.includes(row.id) && row.status in labels && typeof row.summary === 'string'));
}
function evidenceButtons(assumption, materials) {
  const relevant = materials.filter(id => article(id).links.some(l => l.assumption === assumption));
  return relevant.length ? relevant.map(id => `<button data-evidence="${id}"><span class="source-icon">${id === 'earnings' ? '▤' : '◈'}</span> ${esc(typeOf(id))}${article(id).kind === 'synthetic' ? ' · 模拟' : ''}</button>`).join('') : '<span class="small">本次资料没有直接依据</span>';
}
function assumptionsHTML(report) {
  return report.assessments.map(row => {
    const hypothesis = data.thesis.assumptions.find(a => a.id === row.id);
    return `<article class="assumption-card"><div class="assumption-top"><h3>${esc(hypothesis.title)}</h3>${statusTag(row.status, labels[row.status] ?? '待核对')}</div><p>${esc(row.summary)}</p><div class="evidence-chips">${evidenceButtons(row.id, report.materials)}</div></article>`;
  }).join('');
}
function commonHeader() {
  return `<header class="company-heading"><div class="company-id"><div class="company-icon">M</div><div><h1>微软 <span class="pill">MSFT · 美国</span></h1><p class="subtitle">围绕投资假设，持续跟踪依据与变化</p></div></div><div class="heading-actions"><button data-action="history">研究历史</button><button class="primary" data-action="review">重新评估 →</button></div></header>
    <div class="demo-note">交互预览：历史资料与明确标注的模拟场景；研究建议为人工样例，操作仅保存在本页演示中。</div>
    <nav class="company-tabs" role="tablist" aria-label="公司研究视图"><button id="tab-conclusion" role="tab" aria-controls="company-content" aria-selected="${activeTab === 'conclusion'}" data-tab="conclusion">研究结论</button><button id="tab-changes" role="tab" aria-controls="company-content" aria-selected="${activeTab === 'changes'}" data-tab="changes">最新变化 <span class="tab-count">${pending().length}</span></button><button id="tab-evidence" role="tab" aria-controls="company-content" aria-selected="${activeTab === 'evidence'}" data-tab="evidence">证据资料</button></nav>`;
}
function conclusion() {
  const report = latest(), count = pending().length;
  return `<div class="company-grid"><section>
    ${count ? `<div class="change-banner"><div><h2>${count} 项材料尚未纳入最近一次研究</h2><p>包含 ${pending().filter(id => article(id).kind === 'synthetic').length} 个模拟场景；先核对变化，再决定是否重评。</p></div><button data-tab="changes">查看变化 →</button></div>` : '<div class="change-banner"><div><h2>本页演示材料已纳入最新研究</h2><p>每条依据仍可展开核查；正式判断由你维护。</p></div><button data-tab="evidence">查看依据 →</button></div>'}
    <article class="panel"><div class="analysis-kicker">${statusTag('unknown', '研究建议 · 演示 v' + report.version)}<span>${esc(report.created)} · ${report.mode === 'baseline' ? '历史披露基线' : report.mode === 'incremental' ? '检查最新变化' : '完整重评'}</span></div><h2 class="report-title">${esc(report.headline)}</h2><p class="report-summary">${esc(report.summary)}</p><div class="report-foot"><span>采用 ${report.materials.length} 项材料 · 按证据链整理</span><span>基于演示判断 v1</span><button class="text-button" data-tab="evidence" data-used="yes">查看采用的资料 →</button></div>${report.version > 1 ? '<div class="report-note">本次评估已生成独立研究版本。你的正式判断还没有修改，可核对依据后自行决定。</div>' : ''}</article>
    <section class="panel"><div class="section-head"><h2>逐项检查投资假设</h2><span class="small">4 项 · 展开来源可追溯</span></div>${assumptionsHTML(report)}</section>
    ${count ? `<section class="panel"><div class="section-head"><h2>值得先看的变化</h2><button class="text-button" data-tab="changes">全部 ${count} 项 →</button></div>${pending().slice().sort((a,b) => (a === 'power-demo' ? -1 : 0) - (b === 'power-demo' ? -1 : 0)).slice(0,2).map(id => `<article class="mini-change"><div>${sampleTag(article(id))}<h3>${esc(article(id).newsTitle)}</h3><p>${esc(article(id).links[0]?.text)}</p></div><button class="text-button" data-evidence="${id}">核对 →</button></article>`).join('')}</section>` : ''}
  </section><aside class="side-stack"><section class="panel side-card"><h2>我的正式判断</h2><p class="judgment">${esc(data.thesis.summary)}</p><small>此处为只读演示判断 v1。系统研究建议与个人正式判断分别保留。</small><a class="text-button" href="${esc(data.researchUrl)}" target="_blank" rel="noopener noreferrer">查看原有判断与历史 ↗</a></section><section class="panel side-card"><h2>接下来要验证</h2><ul class="question-list"><li>投入增长能否转化为可持续现金回报？</li><li>产品收费能否兑现为实际付费与收入？</li><li>行业供给条件是否确实影响微软项目？</li></ul><p class="side-note">未解决的问题继续保留，资料增多不自动提高置信度。</p></section><section class="panel side-card"><h2>财报与官方资料</h2><p>${esc(article('earnings').newsTitle)}</p><small>历史样本 · ${esc(article('earnings').published)}<br>数据期间与原文入口随资料保留。</small><button class="text-button" data-evidence="earnings">查看披露与来源 →</button></section></aside></div>`;
}
function changes() {
  const ids = showAll ? candidateIds : pending();
  return `<div class="list-intro"><h2>发生了什么，需要重新检查什么</h2><p>按研究是否采用区分。历史材料也可能首次进入研究；“新变化”不表示发布日期是今天。</p></div><div class="filters"><button class="${!showAll ? 'primary' : ''}" data-action="pending">尚未纳入研究 ${pending().length}</button><button class="${showAll ? 'primary' : ''}" data-action="all-changes">全部材料</button><span class="small">已为重评选择 ${selectedCount()} 项</span></div>
    ${ids.map(id => { const a = article(id), used = latest().materials.includes(id); return `<article class="change-item"><div><div class="item-meta">${sampleTag(a)}<span>${esc(typeOf(id))}</span>${used ? statusTag('support','已纳入最新研究') : statusTag('watch','尚未纳入')}</div><h3>${esc(a.newsTitle)}</h3><p>${esc(a.newsSummary)}</p><div class="item-impact"><strong>可能影响 ${a.links.map(l => esc(data.thesis.assumptions.find(h => h.id === l.assumption)?.title)).join('、')}</strong>${esc(a.inference)}</div><p class="question">需要核对：${esc(a.limits)}</p></div><div class="change-controls"><button class="text-button" data-evidence="${id}">查看来源与依据 →</button><button class="text-button" data-mark="${id}">${state.marked.includes(id) ? '✓ 已标记待验证' : '标记待验证'}</button>${!used ? `<label class="check-inline"><input type="checkbox" data-select="${id}" ${state.selected.includes(id) ? 'checked' : ''}>选入重评</label>` : ''}</div></article>`; }).join('') || '<div class="empty-state">当前演示没有尚未纳入的材料。可以查看全部材料或研究历史。</div>'}`;
}
function evidence() {
  const materials = ['earnings', ...candidateIds].filter(id => (usedFilter === 'all' || latest().materials.includes(id) === (usedFilter === 'used')) && (typeFilter === 'all' || typeOf(id) === typeFilter) && (assumptionFilter === 'all' || article(id).links.some(l => l.assumption === assumptionFilter)) && (!search || (article(id).newsTitle + article(id).newsSummary).toLocaleLowerCase().includes(search.toLocaleLowerCase())));
  return `<div class="list-intro"><h2>同一项假设，放在一起核对依据</h2><p>财报、公司新闻、行业与宏观资料分别标明来源。研究推断可从每条资料展开，转载数量不增加独立事实。</p></div>
    <div class="filters"><label>来源类型 <select id="type-filter"><option value="all">全部来源类型</option>${['财报 / 官方披露','公司新闻 / 产品公告','行业资料','宏观资料'].map(t => `<option ${typeFilter === t ? 'selected' : ''}>${t}</option>`).join('')}</select></label><label>投资假设 <select id="hypothesis-filter"><option value="all">全部假设</option>${data.thesis.assumptions.map(a => `<option value="${a.id}" ${assumptionFilter === a.id ? 'selected' : ''}>${esc(a.title)}</option>`).join('')}</select></label><label>采用状态 <select id="used-filter">${[['all','全部资料'],['used','本版已采用'],['pending','本版未采用']].map(([v,label]) => `<option value="${v}" ${usedFilter === v ? 'selected' : ''}>${label}</option>`).join('')}</select></label><input type="search" id="evidence-search" aria-label="搜索证据" placeholder="搜索标题与摘录" value="${esc(search)}"></div>
    <div class="table-shell"><table class="evidence-table"><thead><tr><th>材料与来源</th><th>对应假设</th><th>研究采用状态</th></tr></thead><tbody>${materials.map(id => { const a = article(id); return `<tr><td>${sampleTag(a)}<button class="title-button" data-evidence="${id}">${esc(a.newsTitle)}</button><small>${esc(typeOf(id))} · ${a.sources.length} 个入口 / 1 条${a.kind === 'synthetic' ? '模拟' : ''}证据链</small></td><td>${a.links.map(l => esc(data.thesis.assumptions.find(h => h.id === l.assumption)?.title)).join('<br>')}</td><td>${latest().materials.includes(id) ? statusTag('support','已纳入 v' + latest().version) : statusTag('watch','尚未纳入')}<small>${state.marked.includes(id) ? '已标记待验证' : ''}</small></td></tr>`; }).join('')}</tbody></table></div>${!materials.length ? '<div class="empty-state">当前筛选没有匹配的资料。</div>' : ''}`;
}
function render() {
  $('#main').innerHTML = commonHeader() + `<div id="company-content" role="tabpanel" aria-labelledby="tab-${activeTab}">${({conclusion,changes,evidence}[activeTab])()}</div>`;
  if (activeTab === 'evidence') {
    $('#type-filter').onchange = e => { typeFilter = e.target.value; render(); };
    $('#hypothesis-filter').onchange = e => { assumptionFilter = e.target.value; render(); };
    $('#used-filter').onchange = e => { usedFilter = e.target.value; render(); };
    $('#evidence-search').oninput = e => { const position = e.target.selectionStart; search = e.target.value; render(); $('#evidence-search').focus(); $('#evidence-search').setSelectionRange(position, position); };
  }
}

function openEvidence(id) {
  const a = article(id); if (!a) return;
  $('#evidence-dialog').innerHTML = `<div class="dialog-heading"><div>${sampleTag(a)}<h2 id="evidence-title">${esc(a.newsTitle)}</h2><p>${esc(typeOf(id))} · ${a.published ? '发布：' + a.published : '模拟无真实发布日期'}</p></div><button data-close="evidence-dialog" aria-label="关闭资料">✕</button></div><section class="evidence-block"><h3>来源陈述</h3><p>${esc(a.fact)}</p></section><section class="evidence-block"><h3>来源观点</h3><p>${esc(a.opinion)}</p></section><section class="evidence-block"><h3>与投资假设的关系 · 人工分析样例</h3><p>${esc(a.inference)}</p><p>${esc(a.limits)}</p></section><section class="evidence-block"><h3>引文与原文入口</h3>${a.links.filter(l => l.quote).map(l => `<blockquote>${esc(l.quote)}</blockquote>`).join('')}${a.sources.map(s => `<p>${sourceLink(s)}</p>`).join('')}<p class="small">同源转述保留入口，按一条原始证据链整理。引文存在不等于推断已得到验证。</p></section><div class="dialog-actions">${candidateIds.includes(id) ? `<button data-mark="${id}">${state.marked.includes(id) ? '✓ 已标记待验证' : '标记待验证'}</button>` : ''}<button data-close="evidence-dialog">返回研究</button></div>`;
  $('#evidence-dialog').showModal();
}
function openReview() {
  const available = pending();
  $('#review-dialog').innerHTML = `<div class="dialog-heading"><div><h2 id="review-title">这次想检查什么？</h2><p>先确认评估范围和资料，再生成一版新的研究建议。</p></div><button data-close="review-dialog" aria-label="关闭重新评估">✕</button></div><form id="review-form"><div class="mode-options"><label class="mode-option"><input type="radio" name="mode" value="incremental" checked><span><strong>检查最新变化</strong><small>沿用上一版研究背景，重点检查新资料改变了哪些条件。</small></span></label><label class="mode-option"><input type="radio" name="mode" value="full"><span><strong>完整重评</strong><small>重新检查财务、经营及外部环境，适合财报后或投资逻辑需要调整时。</small></span></label></div><div class="mode-base" id="mode-base">背景：演示研究 v${latest().version}，已有资料 ${latest().materials.length} 项。本次重点检查新增信息。</div><h3 style="font-size:14px">选择本次新增资料</h3>${available.map(id => `<label class="select-material"><input type="checkbox" name="materials" value="${id}" ${state.selected.includes(id) ? 'checked' : ''}><span><strong>${esc(article(id).newsTitle)}</strong><small>${esc(typeOf(id))} · ${article(id).kind === 'synthetic' ? '模拟场景，无真实公司事实' : '历史材料 ' + article(id).published}</small></span></label>`).join('') || '<p class="small">暂无未采用材料。可以完整重评现有资料；无需为了生成报告重复检查。</p>'}<p id="review-error" role="alert" style="color:#9a4036;font-size:12px;margin-top:12px"></p><div class="dialog-actions"><small>演示操作，不调用模型、不产生费用。</small><button type="button" data-close="review-dialog">取消</button><button class="primary" type="submit">生成评估预览</button></div></form>`;
  $('#mode-base').insertAdjacentHTML('afterend', '<label class="check-inline research-only" hidden><input type="checkbox" name="researchOnly">仅使用投研资料（财报 / 官方披露）</label>');
  $('#review-form').onchange = () => {
    $('#review-error').textContent = '';
    const values = new FormData($('#review-form')), full = values.get('mode') === 'full', researchOnly = full && values.has('researchOnly');
    $('.research-only').hidden = !full;
    document.querySelectorAll('#review-form input[name=materials]').forEach(input => { input.disabled = researchOnly; });
    $('#mode-base').textContent = researchOnly ? '资料范围：仅采用历史财报 / 官方披露。新闻、行业和宏观场景不进入本次报告，既有历史版本保留。' : full ? `资料范围：已采用的 ${latest().materials.length} 项依据 + 本次所选新增资料，重新检查全部投资假设。` : `背景：演示研究 v${latest().version}。重点检查所选新增资料与原有假设的关系。`;
  };
  $('#review-form').onsubmit = e => {
    e.preventDefault(); const values = new FormData(e.currentTarget), mode = values.get('mode'), ids = values.getAll('materials').filter(id => available.includes(id));
    if (mode === 'incremental' && !ids.length) { $('#review-error').textContent = '请选择一项新增资料，或切换到完整重评。'; return; }
    const previousVersion = latest().version;
    const materials = mode === 'full' && values.has('researchOnly') ? ['earnings'] : [...new Set([...latest().materials, ...ids])];
    const report = snapshot(materials, mode, previousVersion + 1);
    state.snapshots.push(report); state.selected = state.selected.filter(id => !ids.includes(id)); persist();
    $('#review-dialog').innerHTML = `<div class="dialog-heading"><div><h2 id="review-title">新研究建议已生成 · 演示 v${report.version}</h2><p>上一版 v${previousVersion} 保留在研究历史中，正式判断仍为演示 v1。</p></div><button data-close="review-dialog" aria-label="关闭预览结果">✕</button></div>${statusTag('unknown', mode === 'full' ? '完整重评' : '检查最新变化')}<h3 class="result-title">${esc(report.headline)}</h3><p>${esc(report.summary)}</p><div class="result-checks"><div><h3>本次改变了什么</h3><p>${ids.includes('power-demo') ? '行业场景提出投入兑现条件的核查问题，尚未认定微软项目受到影响。' : ids.includes('copilot') ? '产品收费线索进入研究，付费采用和收入贡献仍需实际数据验证。' : '重新检查了已选资料；缺少新事实时不人为提高判断置信度。'}</p></div><div><h3>哪些问题继续保留</h3><p>AI 独立收入贡献、可归因现金回报和估值条件仍未充分验证。</p></div></div><section class="evidence-block"><h3>采用的资料 · ${report.materials.length} 项</h3><p>${report.materials.map(id => esc(article(id).newsTitle)).join('<br>')}</p></section><div class="dialog-actions"><small>研究建议供你核查，尚未修改个人判断。</small><button data-close="review-dialog">保留当前判断</button><button class="primary" data-action="view-result">查看新研究 →</button></div>`;
    activeTab = 'conclusion'; render();
  };
  $('#review-dialog').showModal();
}
function openHistory() {
  $('#history-dialog').innerHTML = `<div class="dialog-heading"><div><h2 id="history-title">研究历史</h2><p>每次评估保存当时采用的资料和结论。个人正式判断另行维护。</p></div><button data-close="history-dialog" aria-label="关闭研究历史">✕</button></div>${state.snapshots.toReversed().map(s => `<article class="history-item"><div class="item-meta">${statusTag('unknown','演示 v' + s.version)}<span>${esc(s.created)} · ${s.mode === 'baseline' ? '初始样例' : s.mode === 'full' ? '完整重评' : '检查最新变化'}</span></div><h3>${esc(s.headline)}</h3><p>${s.materials.length} 项材料 · 基于演示判断 v1</p><details><summary>查看这版研究与采用的资料</summary><p>${esc(s.summary)}</p>${assumptionsHTML(s)}<p>资料：${s.materials.map(id => esc(article(id).newsTitle)).join('；')}</p></details></article>`).join('')}`;
  $('#history-dialog').showModal();
}
document.addEventListener('click', e => {
  const button = e.target.closest('button'); if (!button) return;
  if (button.dataset.tab) { activeTab = button.dataset.tab; if (activeTab === 'evidence') { usedFilter = button.dataset.used === 'yes' ? 'used' : 'all'; if (button.dataset.used) { typeFilter = assumptionFilter = 'all'; search = ''; } } render(); }
  if (button.dataset.evidence) openEvidence(button.dataset.evidence);
  if (button.dataset.close) $('#' + button.dataset.close).close();
  if (button.dataset.mark) { const id = button.dataset.mark; state.marked = state.marked.includes(id) ? state.marked.filter(x => x !== id) : [...state.marked, id]; persist(); button.textContent = state.marked.includes(id) ? '✓ 已标记待验证' : '标记待验证'; render(); toast('待验证标记已更新，研究结论保持原样。'); }
  if (button.dataset.action === 'review') openReview();
  if (button.dataset.action === 'history') openHistory();
  if (button.dataset.action === 'pending') { showAll = false; render(); }
  if (button.dataset.action === 'all-changes') { showAll = true; render(); }
  if (button.dataset.action === 'view-result') { $('#review-dialog').close(); activeTab = 'conclusion'; render(); window.scrollTo({top:0,behavior:'smooth'}); }
});
document.addEventListener('change', e => {
  if (e.target.dataset.select) { const id = e.target.dataset.select; state.selected = e.target.checked ? [...new Set([...state.selected,id])] : state.selected.filter(x => x !== id); persist(); render(); }
});
$('#reset-demo').onclick = () => { state = defaults(); persist(); activeTab = 'conclusion'; for (const dialog of document.querySelectorAll('dialog[open]')) dialog.close(); render(); toast('已恢复本页初始演示。'); };
try {
  const response = await fetch('/data.json'); if (!response.ok) throw Error('Unavailable'); data = await response.json(); state = defaults();
  try { const saved = JSON.parse(localStorage.getItem(key) ?? 'null'); if (valid(saved)) state = saved; } catch {}
  render();
} catch { $('#main').innerHTML = '<div class="empty-state">预览未能载入，请通过本机 Demo 服务打开并刷新。</div>'; }
