import {scoreFunds,referenceRows} from './scores.mjs';
import {initialSelection,selectFund,selectGroup,fundColor,buildChart,escapeHtml as esc,format} from './charts.mjs';
const $=id=>document.getElementById(id);
const groups=[{key:'sp',benchmark:'标普500'},{key:'ndx',benchmark:'纳斯达克100'}];
const preferenceKey='qdii-chart-preferences-v1';
const thresholds={'标普500':60,'纳斯达克100':60};
let data=null,selected=new Set(),initialized=false;
const plots=new Map();
let saved=null;
try{saved=JSON.parse(localStorage.getItem(preferenceKey));}catch{}
if(['7','30','90','180','365','1095','1825','3650','0'].includes(saved?.range))$('range').value=saved.range;
if(['premium_same_date_pct','premium_previous_nav_pct'].includes(saved?.basis))$('basis').value=saved.basis;
for(const group of groups){
  const value=saved?.thresholdVersion===3?saved.thresholds?.[group.benchmark]:undefined;
  if(Number.isInteger(value)&&value>=0&&value<=100)thresholds[group.benchmark]=value;
  $(group.key+'-threshold').value=thresholds[group.benchmark];
}
function persist(){
  try{localStorage.setItem(preferenceKey,JSON.stringify({thresholdVersion:3,thresholds,range:$('range').value,basis:$('basis').value}));}catch{}
}
function renderScores(){
  const rows=scoreFunds(data.funds,data.market_date);
  const references=referenceRows(data.references?.funds??[],data.market_date);
  for(const group of groups){
    const sorted=rows.filter(r=>r.fund.benchmark===group.benchmark).sort((a,b)=>(b.score??-1)-(a.score??-1)||a.fund.code.localeCompare(b.fund.code));
    sorted.unshift(...references.filter(r=>r.fund.benchmark===group.benchmark));
    $(group.key+'-scores').innerHTML=`<table><thead><tr><th>标的</th><th>评分 / 100</th><th>总市值（亿元）</th><th>5年收益</th><th>3年收益</th><th>1年收益</th><th>同日溢价</th></tr></thead><tbody>`+sorted.map(r=>`<tr${r.fund.currency?' class="reference-row"':''}><th>${esc(r.fund.name)} <small>${esc(r.fund.code)}${r.fund.currency?' · '+(r.fund.currency==='USD'?'美元':'人民币')+'参考':''}</small>${r.fund.currency?`<small>收益截至 ${esc(r.date||'—')}</small>`:''}${r.error?`<small title="${esc(r.error)}">更新失败 / 暂无数据</small>`:''}</th><td><strong>${format(r.score,1)}</strong></td><td>${format(r.size==null?null:r.size/1e8)}</td>${[5,3,1].map(y=>`<td>${r.returns[y]==null?'—':format(r.returns[y])+'%'}</td>`).join('')}<td>${r.premium==null?'—':format(r.premium)+'%'}</td></tr>`).join('')+'</tbody></table>';
  }
  $('score-date').textContent=`QDII收益截至 ${data.market_date||'—'} · 同日溢价日期 ${rows[0]?.premiumDate||'—'} · 总市值快照 ${rows[0]?.sizeDate||'—'} · 汇率参考日期 ${data.references?.fx_latest_date||'—'}；境外参考收益日期见各行，缺项显示 —。`;
}
function renderOptions(){
  const rows=scoreFunds(data.funds,data.market_date);
  for(const group of groups){
    $(group.key+'-funds').innerHTML=rows.filter(r=>r.fund.benchmark===group.benchmark)
      .sort((a,b)=>(b.score??-1)-(a.score??-1)||a.fund.code.localeCompare(b.fund.code)).map(({fund:f})=>
      `<label class="fund-option" title="${esc(f.name)}"><input type="checkbox" data-code="${f.code}" ${selected.has(f.code)?'checked':''}><svg width="12" height="10" aria-hidden="true"><path d="M0 5H12" stroke="${fundColor(data.funds,f)}" stroke-width="3"/></svg><span>${esc(f.name)}</span><small>${f.code}</small></label>`
    ).join('');
    $(group.key+'-funds').querySelectorAll('input').forEach(input=>input.addEventListener('change',()=>{
      selected=selectFund(selected,input.dataset.code,input.checked);persist();renderCharts();
    }));
  }
}
function renderCharts(){
  if(!data)return;
  const basis=$('basis').value;
  document.querySelectorAll('.premium-caption').forEach(el=>{
    el.textContent=basis==='premium_same_date_pct'?'同日净值 · 事后比较':'此前净值 · 重建参考';
  });
  for(const group of groups)for(const type of ['return','premium']){
    const id=`${group.key}-${type}`,svg=$(id);
    const box=svg.getBoundingClientRect();
    const width=Math.max(280,Math.round(box.width)||680),height=Math.round(box.height)||300;
    const chart=buildChart(data.funds,{benchmark:group.benchmark,type,selected,
      end:data.market_date,days:Number($('range').value),basis,width,height});
    svg.setAttribute('viewBox',`0 0 ${width} ${height}`);
    svg.innerHTML=chart.svg;
    plots.set(id,{points:chart.points,width,type});
    svg.nextElementSibling.hidden=true;
  }
}
for(const group of groups)for(const type of ['return','premium']){
  const id=`${group.key}-${type}`,svg=$(id),tooltip=svg.nextElementSibling;
  svg.addEventListener('pointermove',event=>{
    const chart=plots.get(id);if(!chart?.points.length)return;
    const box=svg.getBoundingClientRect(),xx=(event.clientX-box.left)/box.width*chart.width;
    let nearest=chart.points[0];
    for(const point of chart.points)if(Math.abs(point.x-xx)<Math.abs(nearest.x-xx))nearest=point;
    const values=type==='candle'?[nearest]:chart.points.filter(p=>p.date===nearest.date);
    tooltip.innerHTML=`<strong>${nearest.date}</strong>`+values.map(point=>
      `<p>${esc(point.name)} · ${point.code}　${type==='return'?`起点 ${point.startDate}　`:''}${point.value>0?'+':''}${format(point.value)}%</p>`).join('');
    tooltip.hidden=false;
  });
  svg.parentElement.addEventListener('pointerleave',()=>{tooltip.hidden=true;});
}
document.querySelectorAll('[data-group]').forEach(button=>button.addEventListener('click',()=>{
  if(!data)return;
  selected=selectGroup(selected,data.funds,button.dataset.group,button.dataset.on==='true');
  persist();renderOptions();renderCharts();
}));
for(const group of groups){
  const input=$(group.key+'-threshold');
  input.addEventListener('input',()=>{
    if(!input.checkValidity())return;
    thresholds[group.benchmark]=Number(input.value);
    if(!data)return;
    selected=selectGroup(selected,data.funds,group.benchmark,false);
    const defaults=initialSelection(data.funds,undefined,scoreFunds(data.funds,data.market_date),thresholds);
    data.funds.filter(f=>f.benchmark===group.benchmark&&defaults.has(f.code)).forEach(f=>selected.add(f.code));
    persist();renderOptions();renderCharts();
  });
  input.addEventListener('change',()=>{if(!input.checkValidity())input.reportValidity();});
}
for(const id of ['range','basis'])$(id).addEventListener('change',()=>{persist();renderCharts();});
let resizeFrame;
window.addEventListener('resize',()=>{cancelAnimationFrame(resizeFrame);resizeFrame=requestAnimationFrame(renderCharts);});
async function load(){
  $('refresh').disabled=true;
  try{
    const response=await fetch('./market.json', {cache:'no-store'});if(!response.ok)throw Error(`HTTP ${response.status}`);
    const next=await response.json();
    next.funds=next.funds.filter(f=>f.active&&groups.some(g=>g.benchmark===f.benchmark));
    data=next;
    selected=initialSelection(data.funds,initialized?[...selected]:undefined,scoreFunds(data.funds,data.market_date),thresholds);
    initialized=true;renderOptions();renderCharts();renderScores();
    const run=data.last_run,navDates=data.funds.map(f=>f.latest_nav_date).filter(Boolean).sort();
    const status=$('status');
    status.classList.toggle('error',!!run&&['partial','failed','degraded'].includes(run.status));
    status.textContent=!data.funds.length?'暂无数据，请先更新行情。':
      `行情截至 ${data.market_date||'—'} · 最新净值 ${navDates.at(-1)||'—'}${
        run?.status==='running'?' · 正在更新':run?.status==='degraded'?' · 目录刷新失败，沿用已确认目录；已有标的行情与净值更新完成':run?.status==='partial'||run?.status==='failed'?' · 最近更新失败，部分数据沿用历史记录':''}`;
  }catch(error){$('status').classList.add('error');$('status').textContent=`数据读取失败：${error.message}`;}
  finally{$('refresh').disabled=false;}
}
$('refresh').addEventListener('click',load);
load();
