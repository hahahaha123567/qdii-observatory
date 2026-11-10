const finite=v=>typeof v==='number'&&Number.isFinite(v);
const price=r=>r.close*(r.return_price_factor??1);
export function scoreFunds(funds,end){
  const sizeDate=funds.map(f=>f.market_size?.date).filter(Boolean).sort().at(-1)||end;
  const premiumDate=funds.flatMap(f=>f.history.filter(r=>r.date<=end&&finite(r.premium_same_date_pct)).map(r=>r.date)).sort().at(-1)||end;
  const rows=funds.map(f=>{
    const history=f.history.filter(r=>r.date<=end&&finite(r.close)&&r.close>0).sort((a,b)=>a.date.localeCompare(b.date));
    const last=history.at(-1),returns={};
    const pricesByDate=new Map(history.map(r=>[r.date,price(r)]));
    for(const years of [5,3,1]){
      const target=new Date(end+'T00:00:00Z');target.setUTCFullYear(target.getUTCFullYear()-years);
      const date=target.toISOString().slice(0,10);
      const first=history.filter(r=>r.date<=date).at(-1);
      let shares=1;
      for(const dividend of (f.distributions??[]).filter(r=>first&&r.date>first.date&&r.date<=end)){
        const reinvestPrice=pricesByDate.get(dividend.date);
        if(!finite(reinvestPrice)||reinvestPrice<=0){shares=null;break;}
        shares*=1+dividend.amount*(dividend.return_price_factor??1)/reinvestPrice;
      }
      returns[years]=!f.distribution_error&&shares!=null&&last?.date===end&&first&&(Date.parse(date)-Date.parse(first.date))/86400000<=7?(price(last)*shares/price(first)-1)*100:null;
    }
    const size=f.market_size?.date===sizeDate&&sizeDate>=end&&finite(f.market_size.market_cap)&&f.market_size.market_cap>0?f.market_size.market_cap:null;
    const premiumRow=history.find(r=>r.date===premiumDate);
    return {fund:f,returns,size,premium:finite(premiumRow?.premium_same_date_pct)?premiumRow.premium_same_date_pct:null,date:end,sizeDate,premiumDate};
  });
  const metrics=[[r=>r.returns[5],35,1],[r=>r.returns[3],25,1],[r=>r.returns[1],20,1],[r=>r.size,10,1],[r=>r.premium,10,-1]];
  for(const row of rows){
    let sum=0,coverage=0;
    for(const [get,weight,direction] of metrics){
      const value=get(row);if(!finite(value))continue;
      const peers=rows.filter(r=>r.fund.benchmark===row.fund.benchmark&&finite(get(r)));
      const less=peers.filter(r=>(value-get(r))*direction>0).length;
      const equal=peers.filter(r=>get(r)===value).length;
      const rank=peers.length===1?.5:(less+(equal-1)/2)/(peers.length-1);
      sum+=weight*rank;coverage+=weight;
    }
    row.coverage=coverage;row.score=coverage?100*sum/coverage:null;
  }
  return rows;
}

export function referenceRows(funds,end){
  return funds.flatMap(fund=>{
    const referenceDate=end?(fund.history??[]).filter(r=>r.date<=end&&finite(r.close)&&r.close>0).map(r=>r.date).sort().at(-1):null;
    return ['USD','CNY'].map(currency=>{
      const error=fund.error||(currency==='CNY'?fund.fx_error:null);
      const history=error?[]:(currency==='USD'?fund.history:fund.cny_history)??[];
      const f={...fund,currency,history,market_size:null,distributions:[],distribution_error:error};
      const row=referenceDate?scoreFunds([f],referenceDate)[0]:{fund:f,date:null,returns:{5:null,3:null,1:null}};
      return {...row,score:null,size:null,premium:null,error};
    });
  });
}
