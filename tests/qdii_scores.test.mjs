import test from 'node:test';
import assert from 'node:assert/strict';
import {scoreFunds,referenceRows} from '../web/qdii/scores.mjs';
const fund=(code,growth,benchmark='标普500')=>({code,benchmark,market_size:{date:'2026-10-28',market_cap:100},history:['2021-10-28','2023-10-27','2025-10-28','2026-10-28'].map((date,i)=>({date,close:i===3?growth:1,return_price_factor:1,premium_same_date_pct:2}))});
test('同指数收益主导评分，其他指数组不改变排名；缺失完整年限不当作五年收益',()=>{
 const a=fund('a',2),b=fund('b',1.5),other=fund('c',10,'纳斯达克100');
 const rows=scoreFunds([a,b,other],'2026-10-28');
 assert.equal(rows[0].score,90);assert.equal(rows[1].score,10);
 const young={...a,history:a.history.slice(-2)};
 const result=scoreFunds([young,b],'2026-10-28')[0];
 assert.equal(result.returns[5],null);assert.equal(result.returns[3],null);
 assert.equal(result.coverage,40);
});
test('拆分调整且不使用未来行情，过期市值与不同日期溢价不参与比较',()=>{
 const a=fund('a',2),b=fund('b',2);
 a.history[0].close=4;a.history[0].return_price_factor=.25;
 a.market_size.date='2026-10-27';b.history.at(-1).premium_same_date_pct=null;
 a.history.push({date:'2026-10-29',close:100});
 const rows=scoreFunds([a,b],'2026-10-28');
 assert.equal(rows[0].returns[5],100);assert.equal(rows[0].size,null);
 assert.equal(rows[1].premium,null);
 assert.ok(rows.every(r=>r.score===50));
});
test('分红按除息日收盘价再投资，新增份额参与后续分红，并使用拆分一致单位',()=>{
 const a=fund('a',2);
 a.history.push({date:'2026-05-04',close:2,return_price_factor:.5},{date:'2026-06-04',close:2});
 a.distributions=[{date:'2026-05-04',amount:.2,return_price_factor:.5},{date:'2026-06-04',amount:.2}];
 assert.ok(Math.abs(scoreFunds([a],'2026-10-28')[0].returns[1]-142)<1e-8);
 a.distribution_error='unknown event';
 assert.equal(scoreFunds([a],'2026-10-28')[0].returns[1],null);
});
test('缺少除息日价格时收益留空，区间外分红不影响收益',()=>{
 const a=fund('a',2);
 a.distributions=[{date:'2026-05-04',amount:.1}];
 assert.equal(scoreFunds([a],'2026-10-28')[0].returns[1],null);
 a.distributions=[{date:'2025-10-28',amount:.1},{date:'2026-10-29',amount:.1}];
 assert.equal(scoreFunds([a],'2026-10-28')[0].returns[1],100);
});

test('境外参考行展示美元与人民币收益，更新失败时留空',async()=>{
 const {referenceRows}=await import('../web/qdii/scores.mjs');
 const reference={...fund('VOO',3),name:'Vanguard S&P 500 ETF',cny_history:fund('VOO',4).history};
 const rows=referenceRows([reference],'2026-10-28');
 assert.equal(rows.length,2);
 assert.equal(rows[0].fund.currency,'USD');
 assert.equal(rows[0].returns[5],200);
 assert.equal(rows[1].fund.currency,'CNY');
 assert.equal(rows[1].returns[5],300);
 assert.equal(rows[0].score,null);
 assert.equal(rows[0].size,null);
 assert.equal(rows[0].premium,null);
 reference.fx_error='汇率失败';
 assert.equal(referenceRows([reference],'2026-10-28')[1].returns[5],null);
 assert.equal(referenceRows([reference],'2026-10-28')[0].returns[5],200);
 reference.error='本次更新失败';
 assert.equal(referenceRows([reference],'2026-10-28')[0].returns[5],null);
});

test('本地暂无行情日期时境外参考仍保留两行空值',()=>{
 assert.ok(referenceRows([{code:'VOO',history:[]}],null).every(r=>r.returns[5]===null));
});

test('国内行情先进入下一交易日时，境外参考按最近完整美股日展示并标注日期',()=>{
 const reference={...fund('VOO',3),cny_history:fund('VOO',4).history};
 reference.history.push({date:'2026-11-03',close:100});
 const rows=referenceRows([reference],'2026-10-29');
 assert.equal(rows[0].date,'2026-10-28');
 assert.equal(rows[1].date,'2026-10-28');
 assert.equal(rows[0].returns[5],200);
 assert.equal(rows[1].returns[5],300);
 reference.cny_history=reference.cny_history.slice(0,-1);
 assert.equal(referenceRows([reference],'2026-10-29')[1].returns[5],null);
});
