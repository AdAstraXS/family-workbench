const config = JSON.parse(document.querySelector('#reader-config').textContent);
const surface = document.querySelector('#reading-surface');
const status = document.querySelector('#reader-status');
const csrf = document.querySelector('[name=csrfmiddlewaretoken]').value;
const toc = document.querySelector('#toc');
let view, pdf, pdfEngine, pdfPage=1, rendering=false, ready=false, restoring=false;
let revision=0, pending=null, savedKey='', saving=false, conflicted=null, saveTimer;
let selected=null, annotations=[], highlightDraw, selectionDocument, selectionEndRect, annotationSaving=false, editingAnnotation=null, cardAnnotation=null;
let pdfScale=1, requestedPDF=null;
const actions=document.querySelector('#selection-actions');
const card=document.querySelector('#highlight-card');
const preferenceKey=`reading:display:${config.member_id}:${config.book_id}`;
try{
  const saved=JSON.parse(localStorage.getItem(preferenceKey) || '{}');
  for(const [id,key,allowed] of [
    ['font-size','fontSize',['14','16','18','21','24']],
    ['letter-spacing','letterSpacing',['compact','standard','loose']],
    ['line-spacing','lineSpacing',['compact','standard','loose']],
  ])if(allowed.includes(saved[key]))document.getElementById(id).value=saved[key];
}catch{}
function saveDisplayPreferences(){
  try{localStorage.setItem(preferenceKey,JSON.stringify({
    fontSize:document.querySelector('#font-size').value,
    letterSpacing:document.querySelector('#letter-spacing').value,
    lineSpacing:document.querySelector('#line-spacing').value,
  }));}catch{}
  view?.renderer.setStyles?.(fontStyle());
}
function placeSelectionPanel(panel){
  if(!selectionEndRect)return;
  const margin=8, gap=8, footerTop=document.querySelector('.reader-toolbar').getBoundingClientRect().top;
  const availableBottom=Math.min(window.innerHeight,footerTop)-margin;
  panel.style.left='0px';panel.style.top='0px';
  const bounds=panel.getBoundingClientRect();
  const right=selectionEndRect.right+gap;
  panel.style.left=`${Math.max(margin,Math.min(right,window.innerWidth-bounds.width-margin))}px`;
  const below=selectionEndRect.bottom+gap;
  panel.style.top=`${Math.max(margin,below+bounds.height<=availableBottom?below:
    selectionEndRect.top-bounds.height-gap>=margin?selectionEndRect.top-bounds.height-gap:
    Math.min(below,availableBottom-bounds.height))}px`;
}
const setStatus = (text,important=false) => {status.textContent=text;status.classList.toggle('important',important);};
const fail = e => {setStatus(`未完成：${e.message || '请检查网络后重试。'}`,true);};
async function getJSON(url) {
  const response=await fetch(url,{credentials:'same-origin',cache:'no-store'});
  if(!response.ok || !response.headers.get('content-type')?.includes('application/json')) throw Error('登录已失效或图书不可访问，请返回图书重新打开。');
  return response.json();
}
function record(location, progress) {
  if(restoring || !ready) return;
  pending={location,progress:Math.max(0,Math.min(10000,Math.round(progress)))};
  clearTimeout(saveTimer);
  if(!conflicted) saveTimer=setTimeout(()=>save().catch(fail),800);
}
async function save() {
  if(!config.writable || !ready || !pending || saving || conflicted) return;
  const payload=pending, key=JSON.stringify(payload);
  if(key===savedKey) return;
  saving=true;
  try {
    const response=await fetch(config.position,{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-CSRFToken':csrf},
      body:JSON.stringify({...payload,revision,file_hash:config.file_hash,normalizer_version:config.normalizer_version}),keepalive:true});
    if(response.status===409) {
      conflicted=await response.json();
      document.querySelector('#conflict').hidden=false;
      setStatus('位置有冲突，自动保存已暂停。');
      return;
    }
    if(!response.ok || !response.headers.get('content-type')?.includes('application/json')) throw Error('位置未保存，请检查网络或登录状态后点击“保存位置”。');
    const result=await response.json(); revision=result.revision; savedKey=key;
    setStatus('位置已保存 · 仅自己可见');
  } finally {saving=false;}
  if(pending && JSON.stringify(pending)!==savedKey) await save();
}
async function navigate(location) {
  if(pdf) await renderPDF(location.page || 1);
  else if(location.cfi) await view.goTo(location.cfi);
  else if(Number.isInteger(location.section)) await view.goTo(location.section);
}
function turnPage(direction) {
  if(!ready)return;
  actions.hidden=true;
  (pdf?renderPDF(pdfPage+direction):(direction<0?view.prev():view.next())).catch(fail);
}
function selection(doc,index) {
  let timer, pointer=null, touching=false;
  const capture=()=>{
    if(touching || !document.querySelector('#selection-note').hidden || !card.hidden)return;
    const sel=doc.getSelection(),text=sel?.toString().trim();
    if(!config.writable || !text || !sel.rangeCount || text.length>4000){actions.hidden=true;return;}
    const range=sel.getRangeAt(0).cloneRange();let anchor;
    const end=range.cloneRange();end.collapse(false);
    const endRect=[...end.getClientRects()].at(-1) || [...range.getClientRects()].at(-1);
    if(!endRect)return;
    const frame=doc.defaultView.frameElement,frameRect=frame?.getBoundingClientRect();
    selectionEndRect={left:endRect.left+(frameRect?.left || 0),right:endRect.right+(frameRect?.left || 0),
      top:endRect.top+(frameRect?.top || 0),bottom:endRect.bottom+(frameRect?.top || 0)};
    if(pdf){
      const page=surface.querySelector('.pdf-page');
      if(!page?.querySelector('.textLayer')?.contains(range.commonAncestorContainer))return;
      const bounds=page.getBoundingClientRect();
      const rects=[...range.getClientRects()].filter(r=>r.width>0&&r.height>0).map(r=>[
        Math.max(0,(r.x-bounds.x)/bounds.width),Math.max(0,(r.y-bounds.y)/bounds.height),
        Math.min(r.width/bounds.width,1),Math.min(r.height/bounds.height,1)]);
      anchor={page:pdfPage,rects};
    }else anchor={cfi:view.getCFI(index,range),section:index};
    selected={quote:text,anchor};
    editingAnnotation=null;
    selectionDocument=doc;
    document.querySelector('#selected-text').textContent=text;
    actions.hidden=false;
    placeSelectionPanel(actions);
    document.querySelector('#selection-status').textContent='';
    document.querySelector('#annotation-status').textContent='';
  };
  const schedule=()=>{clearTimeout(timer);actions.hidden=true;timer=setTimeout(capture,700);};
  const inContent=e=>doc!==document || surface.contains(e.target);
  doc.addEventListener('pointerdown',e=>{
    if(!inContent(e))return;
    touching=true;clearTimeout(timer);actions.hidden=true;
    card.hidden=true;
    pointer={x:e.clientX,y:e.clientY,time:performance.now(),selected:!!doc.getSelection()?.toString(),moved:false};
  });
  doc.addEventListener('pointermove',e=>{if(pointer && Math.hypot(e.clientX-pointer.x,e.clientY-pointer.y)>8)pointer.moved=true;});
  doc.addEventListener('pointercancel',()=>{touching=false;pointer=null;schedule();});
  doc.addEventListener('pointerup',e=>{
    if(!pointer && !inContent(e))return;
    touching=false;const start=pointer;pointer=null;
    if(start && !start.moved && !start.selected && performance.now()-start.time<300 &&
       !doc.getSelection()?.toString() && !e.target.closest('a,button,input,select,textarea') &&
       document.querySelector('#selection-note').hidden && document.querySelector('#notes-panel').hidden){
      const bounds=surface.getBoundingClientRect();
      const frame=doc.defaultView.frameElement;
      const x=e.clientX+(frame?frame.getBoundingClientRect().left:0)-bounds.left;
      if(x>=0 && x<bounds.width*.25)turnPage(-1);
      else if(x>bounds.width*.75 && x<=bounds.width)turnPage(1);
    }
    schedule();
  });
  doc.addEventListener('keyup',schedule);
  doc.addEventListener('selectionchange',schedule);
}
function drawPDFNotes(){
  const page=surface.querySelector('.pdf-page');if(!page)return;
  page.querySelectorAll('.pdf-highlight').forEach(e=>e.remove());
  for(const note of annotations.filter(n=>n.highlight_visible && n.anchor.page===pdfPage))for(const r of note.anchor.rects){
    const span=document.createElement('span');span.className='pdf-highlight';
    [span.style.left,span.style.top,span.style.width,span.style.height]=r.map(v=>`${v*100}%`);page.append(span);
  }
}
async function refreshNotes(){
  if(view)for(const n of annotations.filter(n=>n.highlight_visible))await view.deleteAnnotation({value:n.anchor.cfi});
  annotations=(await getJSON(config.annotations)).items;
  const list=document.querySelector('#notes-list');list.replaceChildren();
  for(const n of annotations){
    const card=document.createElement('article'),quote=document.createElement('button'),body=document.createElement('p'),link=document.createElement('a');
    quote.textContent=n.quote;quote.onclick=()=>navigate(n.anchor).catch(fail);
    body.textContent=`${n.author} · ${n.visibility==='private'?'仅自己':'已分享'}：${n.note || '仅划线'}`;
    link.href=`/reading/notes/${n.id}/`;link.textContent='查看、编辑与回复';card.append(quote,body,link);
    if(n.owned && !n.note && !n.has_comments){
      const toggle=document.createElement('button');toggle.className='highlight-toggle';
      toggle.textContent='取消划线';
      toggle.onclick=()=>deleteHighlight(n,toggle);
      card.append(toggle);
    }
    list.append(card);
    if(view && n.highlight_visible)await view.addAnnotation({value:n.anchor.cfi,quote:n.quote});
  }
  if(!annotations.length)list.textContent='选中文字后可以保存划线，分享由你决定。';
  if(pdf)drawPDFNotes();
}
async function deleteHighlight(note,button){
  if(!confirm('取消这条划线？原划线记录会删除，之后可重新选中文字划线。'))return;
  button.disabled=true;
  try{
    const response=await fetch(`/reading/notes/${note.id}/highlight/`,{method:'DELETE',credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-CSRFToken':csrf},
      body:JSON.stringify({revision:note.revision})});
    if(!response.headers.get('content-type')?.includes('application/json'))throw Error('划线未更新，请检查登录状态。');
    const result=await response.json();if(!response.ok)throw Error(result.error || '划线未更新，请重试。');
    card.hidden=true;await refreshNotes();setStatus('划线已删除，可以重新划线');
  }catch(error){fail(error);}finally{button.disabled=false;}
}
function showHighlightCard(note,rect,doc=document){
  if(!note)return;
  const frameRect=doc.defaultView?.frameElement?.getBoundingClientRect();
  selectionEndRect={left:rect.left+(frameRect?.left || 0),right:rect.right+(frameRect?.left || 0),
    top:rect.top+(frameRect?.top || 0),bottom:rect.bottom+(frameRect?.top || 0)};
  cardAnnotation=note;
  document.querySelector('#card-quote').textContent=note.quote;
  document.querySelector('#card-status').textContent=note.note ? `${note.author}：${note.note}` : `划线者：${note.author}`;
  document.querySelector('#remove-highlight').hidden=!note.owned || !!note.note || note.has_comments;
  document.querySelector('#edit-highlight').hidden=!note.owned;
  document.querySelector('#open-highlight').href=`/reading/notes/${note.id}/`;
  actions.hidden=true;card.hidden=false;placeSelectionPanel(card);
}
document.querySelector('#close-highlight').onclick=()=>{card.hidden=true;};
document.querySelector('#remove-highlight').onclick=()=>{
  if(cardAnnotation)deleteHighlight(cardAnnotation,document.querySelector('#remove-highlight'));
};
document.querySelector('#edit-highlight').onclick=()=>{
  if(!cardAnnotation?.owned)return;
  editingAnnotation=cardAnnotation;
  selected={quote:cardAnnotation.quote,anchor:cardAnnotation.anchor};
  document.querySelector('#selected-text').textContent=cardAnnotation.quote;
  document.querySelector('#annotation-note').value=cardAnnotation.note;
  document.querySelector('#annotation-visibility').value=cardAnnotation.visibility;
  card.hidden=true;document.querySelector('#selection-note').hidden=false;
  placeSelectionPanel(document.querySelector('#selection-note'));
  document.querySelector('#annotation-note').focus();
};
function addToc(items,parent,go) {
  const list=document.createElement('ul');parent.append(list);
  for(const item of items) {
    const li=document.createElement('li'),button=document.createElement('button');
    button.textContent=item.label || item.title || '未命名章节';li.append(button);list.append(li);
    button.onclick=()=>go(item).then(()=>{toc.hidden=true;document.querySelector('#toggle-toc').setAttribute('aria-expanded','false');}).catch(fail);
    if(item.subitems?.length) addToc(item.subitems,li,go);
  }
}
function fontStyle() {
  const spacing={compact:'-.025em',standard:'-.012em',loose:'0em'}[document.querySelector('#letter-spacing').value];
  const lineHeight={compact:1.45,standard:1.65,loose:1.85}[document.querySelector('#line-spacing').value];
  return `html{color:#28362e;background:#fffdf7}body{font-family:Georgia,"Noto Serif SC",serif;font-size:${document.querySelector('#font-size').value}px;line-height:${lineHeight};letter-spacing:${spacing}}a{color:#38694c}img{max-width:100%;height:auto}`;
}
async function openEPUB(saved) {
  await import('./vendor/foliate/view.js');
  const {EPUB}=await import('./vendor/foliate/epub.js');
  highlightDraw=(await import('./vendor/foliate/overlayer.js')).Overlayer.highlight;
  const manifest=await getJSON(config.manifest);
  const load=async name=>{
    if(!Object.hasOwn(manifest.resources,name)) return null;
    const response=await fetch(manifest.resource_url+'?path='+encodeURIComponent(name),{credentials:'same-origin'});
    if(!response.ok) throw Error('章节不可访问，请返回图书检查权限。');
    return response;
  };
  const book=await new EPUB({loadText:async name=>(await load(name))?.text() ?? null,
    loadBlob:async(name,type)=>{const r=await load(name);return r?new Blob([await r.arrayBuffer()],{type}):null;},
    getSize:name=>manifest.resources[name] ?? 0}).init();
  view=document.createElement('foliate-view');surface.append(view);
  view.addEventListener('load', e=>{
    selection(e.detail.doc,e.detail.index);
  });
  view.addEventListener('draw-annotation',e=>{
    const {draw,range,annotation}=e.detail;
    if(range.toString().replace(/\s/g,'')!==annotation.quote.replace(/\s/g,'')){setStatus('部分划线位置未匹配，请从批注列表核对原文。');return;}
    draw(highlightDraw,{color:'#efbb32'});
  });
  view.addEventListener('show-annotation',e=>{
    if(e.detail.range?.startContainer?.ownerDocument?.getSelection()?.toString())return;
    const note=annotations.find(n=>n.anchor.cfi===e.detail.value);
    const rect=[...e.detail.range?.getClientRects() || []].at(-1);
    if(rect)showHighlightCard(note,rect,e.detail.range.startContainer.ownerDocument);
  });
  view.addEventListener('create-overlay',e=>setTimeout(()=>{for(const n of annotations.filter(n=>n.highlight_visible && n.anchor.section===e.detail.index))view.addAnnotation({value:n.anchor.cfi,quote:n.quote}).catch(fail);},0));
  view.addEventListener('external-link',e=>e.preventDefault());
  view.addEventListener('relocate',e=>{
    const d=e.detail;
    if(d.cfi){const current=view.resolveCFI(d.cfi);const link=document.querySelector('#summarize-current');link.href=link.href.split('?')[0]+`?section=${current.index+1}`;
      document.querySelector('#chapter-label').textContent=d.tocItem?.label || `第 ${current.index+1} 节`;
      link.title=`总结：${document.querySelector('#chapter-label').textContent}`;}
    const percent=Math.round((d.fraction || 0)*100);
    document.querySelector('#page-label').textContent=`约 ${percent}%`;
    if(document.activeElement!==document.querySelector('#page-number'))document.querySelector('#page-number').value=percent;
    if(d.cfi) record({cfi:d.cfi},(d.fraction || 0)*10000);
  });
  await view.open(book);
  view.renderer.setStyles?.(fontStyle());
  for(const id of ['font-size','letter-spacing','line-spacing'])document.getElementById(id).onchange=saveDisplayPreferences;
  addToc(book.toc?.length ? book.toc : manifest.sections.map((s,i)=>({label:`第 ${i+1} 节`,href:i})),toc,async item=>view.goTo(item.href));
  await view.init({lastLocation:saved.location.cfi,showTextStart:true});
  ready=true;
  if(view.lastLocation?.cfi) record({cfi:view.lastLocation.cfi},(view.lastLocation.fraction || 0)*10000);
}
async function renderPDF(number) {
  if(!Number.isInteger(number) || number<1 || number>pdf.numPages) return;
  if(rendering){requestedPDF=number;return;}
  rendering=true;
  actions.hidden=true;
  try {
    const page=await pdf.getPage(number);
    const base=page.getViewport({scale:1});
    const mode=document.querySelector('#pdf-zoom').value;
    const scale=Math.max(.1,Math.min(4,mode==='width'?(surface.clientWidth-16)/base.width:
      mode==='height'?(surface.clientHeight-16)/base.height:mode==='actual'?1:pdfScale));
    pdfScale=scale;document.querySelector('#zoom-value').textContent=`${Math.round(scale*100)}%`;
    const viewport=page.getViewport({scale});
    const container=document.createElement('div');container.className='pdf-page';
    container.style.width=`${viewport.width}px`;container.style.height=`${viewport.height}px`;
    container.style.setProperty('--total-scale-factor',String(scale));
    const canvas=document.createElement('canvas');const ratio=Math.min(devicePixelRatio || 1,2);
    canvas.width=Math.floor(viewport.width*ratio);canvas.height=Math.floor(viewport.height*ratio);
    canvas.style.width=`${viewport.width}px`;canvas.style.height=`${viewport.height}px`;
    const layer=document.createElement('div');layer.className='textLayer';container.append(canvas,layer);
    surface.replaceChildren(container);
    await page.render({canvasContext:canvas.getContext('2d'),viewport,transform:ratio===1?null:[ratio,0,0,ratio,0,0]}).promise;
    const content=await page.getTextContent();
    await new pdfEngine.TextLayer({textContentSource:content,container:layer,viewport}).render();
    pdfPage=number;surface.scrollTop=0;
    const link=document.querySelector('#summarize-current');link.textContent='总结当前页';link.href=link.href.split('?')[0]+`?page=${number}`;
    document.querySelector('#chapter-label').textContent=`第 ${number} 页`;
    document.querySelector('#page-label').textContent=`${number} / ${pdf.numPages} 页`;
    document.querySelector('#page-number').value=number;
    document.querySelector('#prev').disabled=number===1;document.querySelector('#next').disabled=number===pdf.numPages;
    setStatus(content.items.some(x=>x.str?.trim())?'选中文字可保存划线与批注':'本页未提取到文字 · 可阅读，暂不做 OCR');
    drawPDFNotes();
    record({page:number},number/pdf.numPages*10000);
    page.cleanup();
  } finally {rendering=false;if(requestedPDF!==null){const target=requestedPDF;requestedPDF=null;renderPDF(target).catch(fail);}}
}
async function openPDF(saved) {
  document.querySelector('#font-control').hidden=true;
  document.querySelector('#pdf-zoom-control').hidden=false;
  document.querySelector('#pdf-zoom').onchange=()=>renderPDF(pdfPage).catch(fail);
  for(const [id,step] of [['zoom-out',-.1],['zoom-in',.1]])document.querySelector('#'+id).onclick=()=>{
    pdfScale=Math.max(.1,Math.min(4,Math.round((pdfScale+step)*100)/100));
    document.querySelector('#pdf-zoom').value='custom';renderPDF(pdfPage).catch(fail);
  };
  document.querySelector('#page-number').min=1;
  document.querySelector('#page-number').setAttribute('aria-label','页码');
  document.querySelector('#jump-unit').textContent='页';
  pdfEngine=await import('./vendor/pdfjs/pdf.min.mjs');
  const base=new URL('./vendor/pdfjs/',import.meta.url);
  pdfEngine.GlobalWorkerOptions.workerSrc=new URL('pdf.worker.min.mjs',base).href;
  const task=pdfEngine.getDocument({url:config.file,disableAutoFetch:true,disableStream:true,isEvalSupported:false,
    cMapUrl:new URL('cmaps/',base).href,cMapPacked:true,standardFontDataUrl:new URL('standard_fonts/',base).href,
    wasmUrl:new URL('wasm/',base).href});
  task.onPassword=()=>{task.destroy();fail(Error('此 PDF 需要密码，本阶段暂不支持；原件已保留。'));};
  pdf=await task.promise;ready=true;
  document.querySelector('#page-number').max=pdf.numPages;
  await renderPDF(Math.min(saved.location.page || 1,pdf.numPages));
  selection(document);
  surface.addEventListener('click',e=>{
    if(getSelection()?.toString() || !surface.contains(e.target))return;
    const page=surface.querySelector('.pdf-page');if(!page)return;
    const bounds=page.getBoundingClientRect(),x=(e.clientX-bounds.left)/bounds.width,y=(e.clientY-bounds.top)/bounds.height;
    const note=[...annotations].reverse().find(n=>n.highlight_visible && n.anchor.page===pdfPage &&
      n.anchor.rects.some(r=>x>=r[0]&&x<=r[0]+r[2]&&y>=r[1]&&y<=r[1]+r[3]));
    if(note)showHighlightCard(note,{left:e.clientX,right:e.clientX,top:e.clientY,bottom:e.clientY});
  });
  const outline=await pdf.getOutline();
  if(outline?.length) addToc(outline.map(x=>({label:x.title,dest:x.dest})),toc,async item=>{
    const dest=typeof item.dest==='string'?await pdf.getDestination(item.dest):item.dest;
    if(dest) await renderPDF((typeof dest[0]==='number'?dest[0]:await pdf.getPageIndex(dest[0]))+1);
  });
  else toc.textContent='此 PDF 没有可用目录，请按页码跳转。';
}
document.querySelector('#prev').onclick=()=>turnPage(-1);
document.querySelector('#next').onclick=()=>turnPage(1);
document.querySelector('#page-jump').onsubmit=e=>{
  e.preventDefault();const value=Number(document.querySelector('#page-number').value);
  if(pdf)renderPDF(value).catch(fail);
  else if(view && Number.isInteger(value) && value>=0 && value<=100)view.goToFraction(value/100).catch(fail);
};
document.querySelector('#toggle-toc').onclick=()=>{toc.hidden=!toc.hidden;document.querySelector('#toggle-toc').setAttribute('aria-expanded',String(!toc.hidden));};
document.querySelector('#toggle-settings').onclick=()=>{const panel=document.querySelector('#reader-settings');panel.hidden=!panel.hidden;document.querySelector('#toggle-settings').setAttribute('aria-expanded',String(!panel.hidden));};
document.querySelector('#save-now').onclick=()=>save().catch(fail);
document.querySelector('#close-selection').onclick=()=>{document.querySelector('#selection-note').hidden=true;};
document.querySelector('#toggle-notes').onclick=()=>{const panel=document.querySelector('#notes-panel');panel.hidden=!panel.hidden;};
document.querySelector('#refresh-notes').onclick=()=>refreshNotes().catch(fail);
actions.addEventListener('pointerdown',e=>e.preventDefault());
document.querySelector('#comment-selection').onclick=()=>{
  if(!selected)return;editingAnnotation=null;actions.hidden=true;document.querySelector('#selection-note').hidden=false;
  placeSelectionPanel(document.querySelector('#selection-note'));
  document.querySelector('#annotation-note').focus();
};
document.querySelector('#highlight-selection').onclick=()=>saveAnnotation(true);
document.querySelector('#annotation-form').onsubmit=e=>{e.preventDefault();saveAnnotation(false);};
async function saveAnnotation(highlightOnly){
  if(!selected || annotationSaving)return;annotationSaving=true;
  const button=document.querySelector('#save-annotation'),message=document.querySelector('#annotation-status');button.disabled=true;
  document.querySelector('#highlight-selection').disabled=true;
  try{
    const payload=editingAnnotation ? {
      revision:editingAnnotation.revision,note:document.querySelector('#annotation-note').value,
      visibility:document.querySelector('#annotation-visibility').value,
    } : {
      ...selected,note:highlightOnly?'':document.querySelector('#annotation-note').value,
      visibility:highlightOnly?'private':document.querySelector('#annotation-visibility').value,
      file_hash:config.file_hash,normalizer_version:config.normalizer_version,
    };
    const url=editingAnnotation?`/reading/notes/${editingAnnotation.id}/annotation/`:config.annotations;
    const response=await fetch(url,{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-CSRFToken':csrf},body:JSON.stringify(payload)});
    if(!response.headers.get('content-type')?.includes('application/json'))throw Error('未保存，请检查登录或权限。');
    const result=await response.json();if(!response.ok)throw Error(result.error);
    if(result.outcome==='existing_note')throw Error('这段文字已有批注，请点原划线打开卡片后修改。');
    selected=null;editingAnnotation=null;document.querySelector('#annotation-note').value='';document.querySelector('#selection-note').hidden=true;
    actions.hidden=true;selectionDocument?.getSelection()?.removeAllRanges();
    await refreshNotes();setStatus(result.outcome==='unchanged'?'这段文字已有划线，未重复保存':
      result.outcome==='expanded'?'已将原划线更新为较长的选区':
      result.outcome==='annotated'?'已在原划线上添加批注':highlightOnly?'划线已保存':'批注已保存');
  }catch(error){message.textContent=error.message;document.querySelector('#selection-status').textContent=error.message;}
  finally{button.disabled=false;document.querySelector('#highlight-selection').disabled=false;annotationSaving=false;}
}
let resizeTimer;window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{if(pdf)renderPDF(pdfPage).catch(fail);},250);});
document.querySelector('#use-server').onclick=async()=>{
  try{
    const newest=await getJSON(config.position);restoring=true;revision=newest.revision;
    await navigate(newest.location);pending={location:newest.location,progress:newest.progress};savedKey=JSON.stringify(pending);
    conflicted=null;document.querySelector('#conflict').hidden=true;setStatus('已恢复保存的位置。');
  }catch(e){fail(e);}finally{restoring=false;}
};
document.querySelector('#use-local').onclick=async()=>{
  try{const newest=await getJSON(config.position);revision=newest.revision;conflicted=null;
    document.querySelector('#conflict').hidden=true;savedKey='';await save();}catch(e){fail(e);}
};
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='hidden')save().catch(()=>{});});
window.addEventListener('pagehide',()=>save().catch(()=>{}));
window.addEventListener('beforeunload',e=>{if(config.writable && pending && (JSON.stringify(pending)!==savedKey || saving)){e.preventDefault();e.returnValue='';}});
async function main(){
  const saved=await getJSON(config.position);revision=saved.revision;
  if(!config.writable){document.querySelector('#save-now').hidden=true;setStatus('只读账户：可以阅读，位置不会保存。');}
  await (config.format==='pdf'?openPDF(saved):openEPUB(saved));
  await refreshNotes();
  const params=new URLSearchParams(location.search),note=annotations.find(n=>n.id===params.get('annotation'));
  if(note)await navigate(note.anchor);
  else if(params.has('section'))await navigate({section:Number(params.get('section'))});
  else if(params.has('page'))await navigate({page:Number(params.get('page'))});
  if(params.get('toc')==='1'){toc.hidden=false;document.querySelector('#toggle-toc').setAttribute('aria-expanded','true');}
  if(config.format!=='pdf')setStatus(config.writable?'已打开 · 阅读位置自动保存':'只读账户：位置不会保存。');
}
main().catch(fail);
