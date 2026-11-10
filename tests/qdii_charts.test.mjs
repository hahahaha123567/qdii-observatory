import test from 'node:test';
import assert from 'node:assert/strict';
import {initialSelection, selectGroup, selectFund, buildChart} from '../web/qdii/charts.mjs';

const row = (date, close=1.2, premium=20) => ({date,open:1.1,high:1.3,low:1.0,close,premium_same_date_pct:premium,premium_previous_nav_pct:10});
const funds = [
  {code:'513500',name:'博时标普500',benchmark:'标普500',history:[row('2026-10-20'),row('2026-10-21')]},
  {code:'159655',name:'华夏标普500',benchmark:'标普500',history:[row('2026-10-20')]},
  {code:'513100',name:'国泰纳指100',benchmark:'纳斯达克100',history:[row('2026-10-20')]},
];
const options = {benchmark:'标普500',selected:new Set(['513500']),end:'2026-10-21',days:90};

test('选择与取消联动同一指数两张图，取消全部不影响另一个指数',()=>{
  let selected = initialSelection(funds,['513500','513100']);
  assert.deepEqual([...selected].sort(),['513100','513500']);
  selected = selectFund(selected,'159655',true);
  for(const type of ['candle','premium']){
    const chart=buildChart(funds,{...options,selected,type});
    assert.deepEqual([...new Set(chart.points.map(p=>p.code))].sort(),['159655','513500']);
  }
  selected=selectGroup(selected,funds,'标普500',false);
  assert.deepEqual([...selected],['513100']);
  assert.equal(buildChart(funds,{...options,selected,type:'candle'}).points.length,0);
  assert.deepEqual([...initialSelection(funds,[])],[]);
});

test('日K使用真实OHLC并保留多标的，缺少OHLC不伪造蜡烛',()=>{
  const chart=buildChart(funds,{...options,type:'candle'});
  assert.equal(chart.points.length,2);
  assert.deepEqual([chart.points[0].open,chart.points[0].high,chart.points[0].low,chart.points[0].close],[1.1,1.3,1,1.2]);
  const missing=[{...funds[0],history:[{...row('2026-10-20'),open:null}]}];
  assert.equal(buildChart(missing,{...options,type:'candle'}).points.length,0);
});

test('未公布的溢价断线，日期区间不会混入未来记录',()=>{
  const history=[row('2026-10-19'),row('2026-10-20',1.2,null),row('2026-10-21'),row('2026-10-22')];
  const chart=buildChart([{...funds[0],history}],{...options,type:'premium'});
  assert.deepEqual(chart.points.map(p=>p.date),['2026-10-19','2026-10-21']);
  const line=chart.svg.match(/data-series="513500" d="([^"]+)"/)[1];
  assert.equal((line.match(/M/g)||[]).length,2);
  const prior=buildChart([{...funds[0],history}],{...options,type:'premium',basis:'premium_previous_nav_pct'});
  assert.equal(prior.points.length,3);
});


test('同组13只标的均可选择并绘制',()=>{
  const many=Array.from({length:13},(_,i)=>({...funds[2],code:String(159600+i)}));
  const selected=selectGroup(new Set(),many,'纳斯达克100',true);
  assert.equal(selected.size,13);
  const chart=buildChart(many,{...options,benchmark:'纳斯达克100',selected,type:'candle'});
  assert.equal(new Set(chart.points.map(p=>p.code)).size,13);
});

test('默认按各指数评分门槛勾选，保留等于门槛的标的；手动选择优先',()=>{
  const scores=[
    {fund:{code:'sp-low',benchmark:'标普500'},score:49.99},
    {fund:{code:'sp-edge',benchmark:'标普500'},score:50},
    {fund:{code:'ndx-low',benchmark:'纳斯达克100'},score:49.99},
    {fund:{code:'ndx-edge',benchmark:'纳斯达克100'},score:50},
    {fund:{code:'missing',benchmark:'标普500'},score:null},
  ];
  const available=scores.map(r=>r.fund);
  assert.deepEqual([...initialSelection(available,undefined,scores)],['sp-edge','ndx-edge']);
  assert.deepEqual([...initialSelection(available,['sp-low','removed'],scores)],['sp-low']);
  assert.deepEqual([...initialSelection(available,[],scores)],[]);
});
