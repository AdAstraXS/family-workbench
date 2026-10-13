/* Monetary previews use integer arithmetic; chart coordinates use Numbers only. */
(() => {
  "use strict";
  const palette = ["#5f8264","#cbab68","#718fa4","#c27f6d","#9383a0","#94ab77","#ba9773","#679b94","#b986a1","#858c59"];
  const node = (tag, text, cls) => {const el=document.createElement(tag); if(text!==undefined)el.textContent=text; if(cls)el.className=cls; return el;};
  function scaled(value, places) {
    if(!/^[0-9]+(?:\.[0-9]*)?$/.test(String(value)))throw new Error("invalid decimal");
    const [whole, fraction=""] = String(value).split(".");
    return BigInt(whole)*10n**BigInt(places)+BigInt((fraction+"0".repeat(places)).slice(0,places)||"0");
  }
  function cents(value) {
    const [whole, fraction=""] = String(value).split(".");
    return scaled(whole+"."+fraction,2)+(Number(fraction[2]||0)>=5?1n:0n);
  }
  function money(value) {
    const amount = typeof value==="bigint"?value:cents(value);
    const whole=(amount/100n).toString().replace(/\B(?=(\d{3})+(?!\d))/g,",");
    return whole+"."+(amount%100n).toString().padStart(2,"0");
  }
  const data=document.getElementById("fund-pie-data");
  if(data){
    const rows=JSON.parse(data.textContent);
    document.querySelectorAll("[data-fund-pie]").forEach(host=>{
      const key=host.dataset.fundPie;
      const items=rows.map((row,index)=>({...row,index,value:Number(row[key]||0)})).filter(row=>row.value>0);
      const total=items.reduce((sum,row)=>sum+row.value,0);
      if(!total){host.append(node("p","暂无可展示的正金额数据。","fund-empty"));return;}
      const donut=node("div",undefined,"fund-donut"),legend=node("ul",undefined,"fund-legend");
      let start=0;const stops=[];
      items.forEach(row=>{
        const end=start+row.value/total*100,color=palette[row.index%palette.length];
        stops.push(color+" "+start+"% "+end+"%");start=end;
        const li=node("li"),dot=node("i"),label=node("span",row.label),amount=node("em");
        dot.style.backgroundColor=color;
        amount.append(node("b",(row.value/total*100).toFixed(2)+"%"),node("span","¥ "+money(row[key])));
        li.append(dot,label,amount);legend.append(li);
      });
      donut.style.background="conic-gradient("+stops.join(",")+")";
      donut.setAttribute("role","img");donut.setAttribute("aria-label",items.length+"条定投的金额比例，详见图例");
      donut.append(node("span",items.length+" 条定投"));host.append(donut,legend);
    });
  }
  const form=document.querySelector("[data-fund-form]"),estimate=document.querySelector("[data-fund-estimate]");
  if(form&&estimate){
    const counts={day:252n,week:52n,month:12n,quarter:4n,year:1n};
    function update(){
      try{
        const amount=cents(form.elements.amount.value),rate=scaled(form.elements.fee_rate.value,6);
        const annual=amount*counts[form.elements.frequency.value],monthly=(annual+6n)/12n;
        const fee=(amount*rate+50000000n)/100000000n;
        estimate.textContent="每月计划 ¥ "+money(monthly)+" · 每年计划 ¥ "+money(annual)+" · 每次手续费约 ¥ "+money(fee);
      }catch{estimate.textContent="填写频率、金额和费率后查看折算。";}
    }
    form.addEventListener("input",update);update();
  }
  const navData=document.getElementById("fund-nav-data"),lineHost=document.querySelector("[data-fund-line]");
  if(navData&&lineHost){
    const rows=JSON.parse(navData.textContent);
    if(!rows.length){lineHost.append(node("p","当前范围暂无净值数据。","fund-empty"));return;}
    const NS="http://www.w3.org/2000/svg",svg=document.createElementNS(NS,"svg");
    svg.setAttribute("viewBox","0 0 960 320");svg.classList.add("fund-line-svg");svg.setAttribute("tabindex","0");
    svg.setAttribute("role","img");svg.setAttribute("aria-label","单位净值走势，可用左右方向键查看日期和净值");
    const vals=rows.map(row=>Number(row[1])),times=rows.map(row=>Date.parse(row[0]+"T00:00:00Z"));
    let low=Math.min(...vals),high=Math.max(...vals),span=high-low;
    if(!span){span=Math.max(high*.02,.001);low-=span/2;high+=span/2;}
    const x=i=>70+((times[i]-times[0])/(times.at(-1)-times[0]||1))*870;
    const y=i=>275-(vals[i]-low)/(high-low)*240;
    function add(tag,attrs,text){const el=document.createElementNS(NS,tag);for(const[k,v]of Object.entries(attrs))el.setAttribute(k,v);if(text!==undefined)el.textContent=text;svg.append(el);return el;}
    for(let i=0;i<5;i++){const yy=35+i*60;add("line",{x1:70,x2:940,y1:yy,y2:yy,stroke:"#e4eae1"});add("text",{x:60,y:yy+4,"text-anchor":"end",fill:"#6f7d70","font-size":12},(high-(high-low)*i/4).toFixed(4));}
    add("text",{x:70,y:309,fill:"#6f7d70","font-size":12},rows[0][0]);
    add("text",{x:940,y:309,"text-anchor":"end",fill:"#6f7d70","font-size":12},rows.at(-1)[0]);
    add("path",{d:rows.map((_,i)=>(i?"L":"M")+x(i).toFixed(2)+","+y(i).toFixed(2)).join(" "),fill:"none",stroke:"#557d57","stroke-width":2,"stroke-linejoin":"round"});
    if(rows.length===1)add("circle",{cx:x(0),cy:y(0),r:4,fill:"#557d57"});
    const cross=add("line",{x1:940,x2:940,y1:35,y2:275,stroke:"#90a58c","stroke-dasharray":"4 4"});
    const dot=add("circle",{cx:x(rows.length-1),cy:y(rows.length-1),r:4,fill:"#557d57"});
    const readout=node("div",undefined,"fund-chart-readout");readout.setAttribute("aria-live","polite");
    let index=rows.length-1;
    function show(i){index=i;cross.setAttribute("x1",x(i));cross.setAttribute("x2",x(i));dot.setAttribute("cx",x(i));dot.setAttribute("cy",y(i));readout.textContent=rows[i][0]+" · 单位净值 "+rows[i][1]+" 元/份";}
    svg.addEventListener("pointermove",event=>{const box=svg.getBoundingClientRect(),coordinate=(event.clientX-box.left)/box.width*960;let best=0;for(let i=1;i<rows.length;i++)if(Math.abs(x(i)-coordinate)<Math.abs(x(best)-coordinate))best=i;show(best);});
    svg.addEventListener("keydown",event=>{if(["ArrowLeft","ArrowRight"].includes(event.key)){event.preventDefault();show(Math.min(rows.length-1,Math.max(0,index+(event.key==="ArrowLeft"?-1:1))));}});
    lineHost.append(svg,readout);show(index);
  }
  if(document.querySelector("[data-fund-poll]")){
    let tries=0;
    const timer=setInterval(async()=>{
      if(document.hidden||tries++>180){if(tries>180)clearInterval(timer);return;}
      try{const response=await fetch(location.href,{headers:{"X-Requested-With":"XMLHttpRequest"}});
        if(!response.ok)return;
        const doc=new DOMParser().parseFromString(await response.text(),"text/html");
        if(!doc.querySelector("[data-fund-poll]")){clearInterval(timer);location.reload();return;}
        const old=document.querySelector(".fund-updates"),fresh=doc.querySelector(".fund-updates");if(old&&fresh)old.replaceWith(fresh);
      }catch{/* Keep the last visible job state on a network failure. */}
    },5000);
  }
})();
