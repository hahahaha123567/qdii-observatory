export const palette=['#28645b','#bc6546','#4b77a5','#9a689e','#9b8735','#d089aa','#5e884a','#b64c5b','#557c83','#7861ac','#7b6b54','#386a9a','#977247'];
export const escapeHtml = value => String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const format = (value,n=2) => value==null?'—':Number(value).toLocaleString('zh-CN',{minimumFractionDigits:n,maximumFractionDigits:n});
export function initialSelection(funds,saved,scores=[],thresholds={'标普500':60,'纳斯达克100':60}){
  const codes=new Set(funds.map(f=>f.code));
  if(Array.isArray(saved))return new Set(saved.filter(code=>codes.has(code)));
  return new Set(scores.filter(row=>codes.has(row.fund.code)&&Number.isFinite(row.score)&&
    row.score>=thresholds[row.fund.benchmark]).map(row=>row.fund.code));
}
export function selectFund(selected,code,on){
  const next=new Set(selected);on?next.add(code):next.delete(code);return next;
}
export function selectGroup(selected,funds,benchmark,on){
  const next=new Set(selected);
  funds.filter(f=>f.benchmark===benchmark).forEach(f=>on?next.add(f.code):next.delete(f.code));
  return next;
}
export function fundColor(funds,fund){
  const group=funds.filter(f=>f.benchmark===fund.benchmark).sort((a,b)=>a.code.localeCompare(b.code));
  return palette[group.findIndex(f=>f.code===fund.code)%palette.length];
}
const finite = value => typeof value==='number'&&Number.isFinite(value);
const candle = row => [row.open,row.high,row.low,row.close].every(v=>finite(v)&&v>0)&&
  row.low<=Math.min(row.open,row.close)&&Math.max(row.open,row.close)<=row.high;
export function buildChart(funds,{benchmark,selected,type,days=90,end='',basis='premium_same_date_pct',width=680,height=300}){
  const start=days&&end?new Date(Date.parse(end)-days*86400000).toISOString().slice(0,10):'';
  const series=funds.filter(f=>f.benchmark===benchmark&&selected.has(f.code)).map(f=>({
    fund:f,color:fundColor(funds,f),rows:f.history.filter(r=>(!start||r.date>=start)&&(!end||r.date<=end))
  }));
  if(type==='return')for(const s of series){
    const first=s.rows.find(row=>finite(row.close)&&row.close>0);
    if(!first){s.rows=[];continue;}
    const base=first.close*(first.return_price_factor??1);
    s.startDate=first.date;
    s.rows=s.rows.map(row=>({...row,return_pct:finite(row.close)&&row.close>0?
      (row.close*(row.return_price_factor??1)/base-1)*100:null}));
  }
  const chartValue=row=>type==='return'?row.return_pct:row[basis];
  const valid=row=>type==='candle'?candle(row):finite(chartValue(row));
  const values=series.flatMap(s=>s.rows.filter(valid).flatMap(r=>type==='candle'?[r.low,r.high]:[chartValue(r)]));
  const points=[];
  if(!values.length)return {points,svg:`<text x="${width/2}" y="${height/2}" text-anchor="middle" fill="#85908b" font-size="13">${series.length?'所选区间暂无可绘制的数据':'选择标的以查看走势'}</text>`};
  const L=58,R=16,T=18,B=32,plotW=width-L-R,plotH=height-T-B;
  let min=Infinity,max=-Infinity;
  for(const v of values){min=Math.min(min,v);max=Math.max(max,v);}
  if(type!=='candle'){min=Math.min(0,min);max=Math.max(0,max);}
  const pad=(max-min)*.09||.05;min-=pad;max+=pad;
  const dates=[...new Set(series.flatMap(s=>s.rows.map(r=>r.date)))].sort();
  const indices=new Map(dates.map((day,i)=>[day,i]));
  const step=plotW/dates.length,x=day=>L+(indices.get(day)+.5)*step;
  const y=value=>T+(max-value)/(max-min)*plotH;
  let svg='';
  for(let i=0;i<=4;i++){
    const value=min+(max-min)*i/4,yy=y(value);
    svg+=`<path d="M${L} ${yy}H${width-R}" stroke="#e8ece5"/><text x="${L-10}" y="${yy+4}" text-anchor="end" fill="#85908b" font-size="11">${format(value)}${type==='candle'?'':'%'}</text>`;
  }
  if(type!=='candle')svg+=`<path d="M${L} ${y(0)}H${width-R}" stroke="#b4beb1" stroke-dasharray="3 4"/>`;
  const ticks=Math.min(width<450?3:4,dates.length);
  for(let i=0;i<ticks;i++){
    const day=dates[Math.round(i*(dates.length-1)/(ticks-1||1))];
    svg+=`<text x="${x(day)}" y="${height-8}" text-anchor="middle" fill="#85908b" font-size="10">${day}</text>`;
  }
  series.forEach((s,index)=>{
    if(type!=='candle'){
      let line='',drawing=false;
      for(const row of s.rows){
        if(!valid(row)){drawing=false;continue;}
        const value=chartValue(row),xx=x(row.date),yy=y(value);
        line+=`${drawing?'L':'M'}${xx.toFixed(2)} ${yy.toFixed(2)} `;drawing=true;
        points.push({x:xx,y:yy,date:row.date,value,code:s.fund.code,name:s.fund.name,startDate:s.startDate});
      }
      svg+=`<path data-series="${s.fund.code}" d="${line}" fill="none" stroke="${s.color}" stroke-width="1.7" stroke-linejoin="round"/>`;
      const own=points.filter(p=>p.code===s.fund.code);
      if(own.length===1)svg+=`<circle cx="${own[0].x}" cy="${own[0].y}" r="2.5" fill="${s.color}"/>`;
      return;
    }
    const bodyW=Math.min(9,step*.75/series.length),offset=(index-(series.length-1)/2)*bodyW;
    let wick='',up='',down='';
    for(const row of s.rows){
      if(!valid(row))continue;
      const xx=x(row.date)+offset,top=Math.min(y(row.open),y(row.close));
      const h=Math.max(.75,Math.abs(y(row.close)-y(row.open))),left=xx-bodyW*.4;
      wick+=`M${xx.toFixed(2)} ${y(row.high).toFixed(2)}V${y(row.low).toFixed(2)} `;
      const body=`M${left.toFixed(2)} ${top.toFixed(2)}h${(bodyW*.8).toFixed(2)}v${h.toFixed(2)}h${(-bodyW*.8).toFixed(2)}Z `;
      if(row.close>=row.open)up+=body;else down+=body;
      points.push({x:xx,y:y(row.close),date:row.date,code:s.fund.code,name:s.fund.name,
        open:row.open,high:row.high,low:row.low,close:row.close});
    }
    svg+=`<g data-series="${s.fund.code}" stroke="${s.color}" stroke-width=".85"><path d="${wick}" fill="none"/><path d="${up}" fill="#fffefa"/><path d="${down}" fill="${s.color}"/></g>`;
  });
  return {points,svg};
}
