#!/usr/bin/env python3
"""采集公开场内 QDII 日行情，并提供只读本地页面"""
import argparse
from contextlib import contextmanager
import hashlib
import bisect
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import fcntl
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import plistlib
import re
import sqlite3
import subprocess
import sys
import time
import threading
import xml.etree.ElementTree as ET
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SHANGHAI = ZoneInfo('Asia/Shanghai')
CATALOG_URL = 'https://fund.eastmoney.com/js/fundcode_search.js'
PRICE_URL = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'
REQUEST_LOCK = threading.Lock()
LAST_REQUEST = {}
# (份额拆分日, 拆分后首个交易日, 拆分倍数)
SHARE_SPLITS = {
    '159941': (('2022-07-04', '2022-07-05', 4),),
    '513100': (('2022-01-13', '2022-01-14', 5),),
    '513500': (('2022-03-29', '2022-03-30', 2),),
}


def now():
    return datetime.now(SHANGHAI).isoformat(timespec='seconds')


def settings(root):
    path = root / 'config/qdii_market.json'
    return json.loads(path.read_text()) if path.exists() else {
        'timeout_seconds': 25, 'attempts': 3, 'retry_delay_seconds': 1,
        'workers': 4}


def is_trading_day(venue, day=None):
    calendar = json.loads((ROOT / 'config/qdii_trading_calendar.json').read_text())[venue]
    day = day or datetime.now(ZoneInfo(calendar['timezone'])).date()
    year = calendar['years'].get(str(day.year))
    if year is None:
        raise ValueError(f'{venue} 交易日历未覆盖 {day.year} 年，请更新 config/qdii_trading_calendar.json')
    return day.weekday() < 5 and not any(
        start <= day.isoformat() <= end for start, end in year['closed_ranges'])


@contextmanager
def connect(root):
    path = root / 'data/processed/qdii/market.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS funds (
            code TEXT PRIMARY KEY, name TEXT, kind TEXT, market INTEGER,
            benchmark TEXT, active INTEGER DEFAULT 1, updated_at TEXT,
            last_attempt TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS prices (
            code TEXT, date TEXT, close REAL, turnover REAL,
            first_seen_at TEXT, last_seen_at TEXT, source_run TEXT, price_source TEXT,
            PRIMARY KEY(code,date));
        CREATE TABLE IF NOT EXISTS navs (
            code TEXT, date TEXT, nav REAL, first_seen_at TEXT,
            last_seen_at TEXT, source_run TEXT, PRIMARY KEY(code,date));
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT,
            status TEXT, discovered INTEGER, succeeded INTEGER, failed INTEGER,
            error TEXT);
    ''')
    columns = {row['name'] for row in db.execute('PRAGMA table_info(prices)')}
    for name in ('open', 'high', 'low'):
        if name not in columns:
            db.execute(f'ALTER TABLE prices ADD COLUMN {name} REAL')
    try:
        with db:
            yield db
    finally:
        db.close()


def download(url, archive, name, cfg, encoding="utf-8-sig"):
    error = None
    for attempt in range(cfg['attempts']):
        try:
            host = urlsplit(url).hostname
            with REQUEST_LOCK:
                delay = cfg.get('min_request_interval_seconds', 1) - (time.monotonic() - LAST_REQUEST.get(host, 0))
                if delay > 0:
                    time.sleep(delay)
                LAST_REQUEST[host] = time.monotonic()
            request = Request(url, headers={
                'User-Agent': 'Mozilla/5.0', 'Referer': 'https://fund.eastmoney.com/'})
            with urlopen(request, timeout=cfg['timeout_seconds']) as response:
                payload = response.read()
            with (archive / name).open('xb') as file:
                file.write(payload)
            with (archive / f'{name}.request.json').open('x', encoding='utf-8') as file:
                json.dump(dict(url=url, fetched_at=now(),
                               sha256=hashlib.sha256(payload).hexdigest()), file)
            return payload.decode(encoding)
        except (OSError, UnicodeError) as exc:
            error = exc
            if getattr(exc, 'code', None) in (401, 403, 429, 501):
                break
            if attempt + 1 < cfg['attempts']:
                time.sleep(cfg['retry_delay_seconds'] * (attempt + 1))
    raise RuntimeError(f'{name}: {error}') from error


def profile_fund(row, archive, cfg):
    code, _, name, category, *_ = row
    source = download(f'https://fundf10.eastmoney.com/jbgk_{code}.html',
                      archive, f'{code}_profile.html', cfg)
    def field(label):
        match = re.search(r'<th[^>]*>' + label + r'</th>\s*<td[^>]*>(.*?)</td>', source, re.S)
        if not match:
            raise ValueError(f'{code} 基金档案缺少 {label}')
        return html.unescape(re.sub('<[^>]+>', '', match[1])).strip()
    fullname = field('基金全称')
    if re.search(r'LOF|上市开放式', fullname, re.I):
        kind = 'LOF'
    elif '交易型开放式' in fullname and '联接' not in fullname:
        kind = 'ETF'
    else:
        return None
    benchmark = field('跟踪标的')
    if benchmark in ('标准普尔500指数', '标普500指数'):
        benchmark = '标普500'
    elif benchmark in ('纳斯达克100指数', '纳斯达克100', '纳斯达克100指数(总回报)'):
        benchmark = '纳斯达克100'
    if benchmark not in ('标普500', '纳斯达克100'):
        return None
    return dict(code=code, name=name, kind=kind,
                market=1 if code.startswith('5') else 0, benchmark=benchmark)


def discover(archive, cfg):
    source = download(CATALOG_URL, archive, 'fund_catalog.js', cfg)
    catalog = json.loads(source[source.index('['):source.rindex(']') + 1])
    candidates = [row for row in catalog
                  if re.search('标普|纳斯达克|纳指', row[2])
                  and re.search('海外|QDII', row[3], re.I)
                  and re.fullmatch(r'(15|16|50|51)\d{4}', row[0])]
    funds = []
    # 基金档案全称辨别 ETF / LOF，避免仅凭代码把普通场外指数基金纳入
    with ThreadPoolExecutor(max_workers=cfg['workers'], thread_name_prefix='qdii-profile') as pool:
        for fund in pool.map(lambda row: profile_fund(row, archive, cfg), candidates):
            if fund:
                funds.append(fund)
    if not funds:
        raise ValueError('基金目录为空，拒绝覆盖历史目录')
    return sorted(funds, key=lambda row: row['code'])


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f'非正有限价格或净值: {value}')
    return number


def valid_date(value):
    datetime.strptime(value, '%Y-%m-%d')
    return value


def parse_nav(source):
    match = re.search(r'var\s+Data_netWorthTrend\s*=\s*(\[.*?\])\s*;', source, re.S)
    if not match:
        raise ValueError('未找到单位净值序列 Data_netWorthTrend')
    rows = {}
    for item in json.loads(match[1]):
        day = datetime.fromtimestamp(item['x'] / 1000, SHANGHAI).date().isoformat()
        rows[day] = positive(item['y'])
    if not rows:
        raise ValueError('单位净值序列为空')
    return sorted(rows.items())


def parse_distributions(source, code):
    match = re.search(r'var\s+Data_netWorthTrend\s*=\s*(\[.*?\])\s*;', source, re.S)
    if not match:
        raise ValueError('缺少分红事件来源')
    events = []
    for item in json.loads(match[1]):
        text = item.get('unitMoney', '').strip()
        if not text:
            continue
        day = datetime.fromtimestamp(item['x']/1000, SHANGHAI).date().isoformat()
        if '拆分' in text and any(day == split[0] for split in SHARE_SPLITS.get(code, ())):
            continue
        cash = re.fullmatch(r'分红[：:]每份派现金([0-9.]+)元', text)
        if not cash:
            raise ValueError(f'{code} {day} 无法确认的分红或份额事件: {text}')
        events.append(dict(date=day, amount=positive(cash[1]),
                           return_price_factor=historical_price_factor(code, day)))
    return events


def parse_prices(payload):
    body = payload.get('data')
    if payload.get('rc') != 0 or not body or not body.get('klines'):
        raise ValueError('场内收盘行情为空或上游返回错误')
    rows = {}
    clock = datetime.now(SHANGHAI)
    for item in body['klines']:
        fields = item.split(',')
        day, close, turnover = valid_date(fields[0]), positive(fields[2]), float(fields[6])
        if not math.isfinite(turnover) or turnover < 0:
            raise ValueError(f'{day} 成交额无效')
        # 当天盘中日线仍在变化，不作为日收盘价持久化
        if day > clock.date().isoformat() or (day == clock.date().isoformat() and clock.hour < 16):
            continue
        rows[day] = (day, close, turnover)
    if not rows:
        raise ValueError('没有已收盘的日行情')
    return sorted(rows.values())


def tencent_prices(fund, archive, cfg):
    code = fund['code']
    symbol = ('sh' if fund['market'] == 1 else 'sz') + code
    end = datetime.now(SHANGHAI).date().isoformat()
    rows = {}
    page = 0
    while True:
        query = urlencode({'param': f'{symbol},day,,{end},640,'})
        payload = json.loads(download(
            f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?{query}',
            archive, f'{code}_tencent_{page}.json', cfg))
        if payload.get('code') != 0:
            raise ValueError(f'{code} 腾讯行情返回错误: {payload.get("msg")}')
        body = payload.get('data', {}).get(symbol, {})
        batch = body.get('day', [])
        if not batch:
            if page == 0:
                raise ValueError(f'{code} 腾讯无未复权日行情')
            break
        for row in batch:
            day = valid_date(row[0])
            clock = datetime.now(SHANGHAI)
            if day > clock.date().isoformat() or (day == clock.date().isoformat() and clock.hour < 16):
                continue
            rows[day] = (day, positive(row[2]), None)
        earliest = min(row[0] for row in batch)
        if earliest > end:
            raise ValueError(f'{code} 腾讯行情分页未前进')
        if len(batch) < 640 or earliest <= cfg.get('_price_start', '1990-01-01'):
            break
        next_end = (datetime.fromisoformat(earliest) - timedelta(days=1)).date().isoformat()
        if next_end >= end:
            raise ValueError(f'{code} 腾讯行情分页未前进')
        end = next_end
        page += 1
    if not rows:
        raise ValueError(f'{code} 无已收盘行情')
    return [row for row in sorted(rows.values()) if row[0] >= cfg.get('_price_start', '1990-01-01')]


def fetch_fund(fund, archive, cfg):
    code = fund['code']
    nav_source = download(f'https://fund.eastmoney.com/pingzhongdata/{code}.js',
                          archive, f'{code}_nav.js', cfg)
    navs = parse_nav(nav_source)
    clock = datetime.now(SHANGHAI)
    last_price = cfg.get('_latest_prices', {}).get(code)
    if not cfg.get('_full_history') and last_price == clock.date().isoformat() and clock.hour >= 16:
        return [], navs
    cfg = dict(cfg)
    cfg['_price_start'] = ((datetime.fromisoformat(last_price) - timedelta(days=14)).date().isoformat()
                           if last_price and not cfg.get('_full_history') else '1990-01-01')
    query = urlencode(dict(secid=f"{fund['market']}.{code}", klt=101, fqt=0,
                           beg=cfg['_price_start'].replace('-', ''), end='20500101',
                           fields1='f1,f2,f3,f4,f5,f6',
                           fields2='f51,f52,f53,f54,f55,f56,f57'))
    try:
        prices = parse_prices(json.loads(download(f'{PRICE_URL}?{query}', archive,
                                                  f'{code}_prices.json', cfg)))
    except (RuntimeError, ValueError, OSError) as exc:
        print(f'{code} 东方财富行情不可用，切换腾讯: {exc}', file=sys.stderr, flush=True)
        prices = tencent_prices(fund, archive, cfg)
    if not set(row[0] for row in prices).intersection(row[0] for row in navs):
        raise ValueError('净值与行情日期完全无交集')
    return prices, navs


def archive_ohlc(archive, code):
    files = sorted(file for file in archive.glob(f'{code}_tencent_*.json')
                   if re.fullmatch(rf'{code}_tencent_\d+\.json', file.name))
    batches = []
    if files:
        for file in files:
            payload = json.loads(file.read_text(encoding='utf-8-sig'))
            if payload.get('code') != 0:
                raise ValueError(f'{code} 原始腾讯行情状态错误')
            for body in payload.get('data', {}).values():
                batches.extend(body.get('day', []))
    elif (archive / f'{code}_prices.json').exists():
        payload = json.loads((archive / f'{code}_prices.json').read_text(encoding='utf-8-sig'))
        if payload.get('rc') != 0:
            raise ValueError(f'{code} 原始东方财富行情状态错误')
        batches = [row.split(',') for row in (payload.get('data') or {}).get('klines', [])]
    candles = {}
    for row in batches:
        day = valid_date(row[0])
        opening, close, high, low = map(positive, row[1:5])
        if not low <= min(opening, close) <= max(opening, close) <= high:
            raise ValueError(f'{code} {day} OHLC 价格矛盾')
        candles[day] = (opening, high, low, close)
    return candles


def store_ohlc(db, candles, code, run):
    before = db.total_changes
    db.executemany('''UPDATE prices SET open=?,high=?,low=?
                      WHERE code=? AND date=? AND close=? AND source_run=?''',
                   [(opening, high, low, code, day, close, run)
                    for day, (opening, high, low, close) in candles.items()])
    return db.total_changes - before


def backfill_ohlc(root):
    folder = root / 'data/processed/qdii'
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / 'update.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('已有 QDII 更新运行中') from exc
        with connect(root) as db:
            batches = db.execute('''SELECT DISTINCT p.code,p.source_run FROM prices p
                JOIN funds f ON p.code=f.code WHERE f.active=1
                AND (p.open IS NULL OR p.high IS NULL OR p.low IS NULL)''').fetchall()
            count = 0
            for batch in batches:
                archive = root / 'raw/market/qdii' / batch['source_run']
                candles = archive_ohlc(archive, batch['code'])
                count += store_ohlc(db, candles, batch['code'], batch['source_run'])
            return count


def historical_price_factor(code, day):
    factor = 1.0
    for _, effective_day, ratio in SHARE_SPLITS.get(code, ()):
        if day < effective_day:
            factor /= ratio
    return factor


def premium_price_factor(code, day):
    factor = 1.0
    for split_day, effective_day, ratio in SHARE_SPLITS.get(code, ()):
        if day == split_day and day < effective_day:
            factor /= ratio
    return factor


def join_history(prices, navs, code=None):
    nav_map = dict(navs)
    nav_dates = sorted(nav_map)
    rows = []
    previous_same = None
    for day, close, turnover in sorted(prices):
        nav = nav_map.get(day)
        index = bisect.bisect_left(nav_dates, day) - 1
        previous_day = nav_dates[index] if index >= 0 else None
        previous_nav = nav_map.get(previous_day)
        premium_factor = premium_price_factor(code, day)
        return_factor = historical_price_factor(code, day)
        premium = (close * premium_factor / nav - 1) * 100 if nav else None
        reference = (close / previous_nav - 1) * 100 if previous_nav else None
        rows.append(dict(date=day, close=close, turnover=turnover, nav=nav,
                         previous_nav=previous_nav, previous_nav_date=previous_day,
                         nav_gap_days=(datetime.fromisoformat(day) - datetime.fromisoformat(previous_day)).days
                         if previous_day else None,
                         premium_same_date_pct=premium,
                         premium_price_factor=premium_factor,
                         return_price_factor=return_factor,
                         premium_previous_nav_pct=reference,
                         premium_change_pp=premium - previous_same
                         if premium is not None and previous_same is not None else None))
        previous_same = premium
    return rows


def update(root, full_history=False):
    if not is_trading_day('CN'):
        print('A股休市：跳过行情及净值更新，沿用上个交易日数据', flush=True)
        return dict(discovered=0, succeeded=0, failed=0, skipped='market_closed')
    cfg = settings(root)
    cfg['_full_history'] = full_history
    folder = root / 'data/processed/qdii'
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / 'update.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('已有 QDII 更新运行中') from exc
        return run_update(root, cfg)


def run_update(root, cfg):
    stamp = datetime.now(SHANGHAI).strftime('%Y%m%dT%H%M%S%f')
    archive = root / 'raw/market/qdii' / stamp
    archive.mkdir(parents=True)
    started = now()
    with connect(root) as db:
        db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)',
                   (stamp, started, None, 'running', 0, 0, 0, None))
    with connect(root) as db:
        cfg['_latest_prices'] = dict(db.execute('SELECT code,MAX(date) FROM prices GROUP BY code').fetchall())
    try:
        funds = discover(archive, cfg)
    except Exception as exc:
        with connect(root) as db:
            db.execute('UPDATE runs SET status=?,finished_at=?,failed=?,error=? WHERE id=?',
                       ('failed', now(), 1, str(exc), stamp))
        print(f'目录更新失败: {exc}', file=sys.stderr, flush=True)
        return dict(discovered=0, succeeded=0, failed=1)
    with connect(root) as db:
        db.execute('UPDATE funds SET active=0')
        for fund in funds:
            db.execute('''INSERT INTO funds(code,name,kind,market,benchmark,active)
                          VALUES(:code,:name,:kind,:market,:benchmark,1)
                          ON CONFLICT(code) DO UPDATE SET name=excluded.name,
                          kind=excluded.kind,market=excluded.market,
                          benchmark=excluded.benchmark,active=1''', fund)
    succeeded = failed = 0
    with ThreadPoolExecutor(max_workers=cfg['workers'], thread_name_prefix='qdii-fetch') as pool:
        tasks = {pool.submit(fetch_fund, fund, archive, cfg): fund for fund in funds}
        for task in as_completed(tasks):
            fund = tasks[task]
            code = fund['code']
            try:
                prices, navs = task.result()
                observed = now()
                candles = archive_ohlc(archive, code)
                with connect(root) as db:
                    db.executemany('''INSERT INTO prices
                        (code,date,close,turnover,first_seen_at,last_seen_at,source_run,price_source)
                        VALUES(?,?,?,?,?,?,?,?)
                        ON CONFLICT(code,date) DO UPDATE SET close=excluded.close,
                        turnover=COALESCE(excluded.turnover,prices.turnover),last_seen_at=excluded.last_seen_at,
                        source_run=excluded.source_run,price_source=excluded.price_source''',
                        [(code, day, close, turnover, observed, observed, stamp,
                          'tencent' if (archive / f'{code}_tencent_0.json').exists() else 'eastmoney')
                         for day, close, turnover in prices])
                    store_ohlc(db, candles, code, stamp)
                    db.executemany('''INSERT INTO navs VALUES(?,?,?,?,?,?)
                        ON CONFLICT(code,date) DO UPDATE SET nav=excluded.nav,
                        last_seen_at=excluded.last_seen_at,source_run=excluded.source_run''',
                        [(code, day, nav, observed, observed, stamp) for day, nav in navs])
                    db.execute('UPDATE funds SET updated_at=?,last_attempt=?,error=NULL WHERE code=?',
                               (observed, observed, code))
                succeeded += 1
                print(f'{code} {fund["name"]}: {len(prices)} 日行情 / {len(navs)} 日净值', flush=True)
            except Exception as exc:
                failed += 1
                with connect(root) as db:
                    db.execute('UPDATE funds SET last_attempt=?,error=? WHERE code=?',
                               (now(), str(exc), code))
                print(f'{code} 更新失败: {exc}', file=sys.stderr, flush=True)
    with connect(root) as db:
        db.execute('''UPDATE runs SET finished_at=?,status=?,discovered=?,succeeded=?,failed=?
                      WHERE id=?''', (now(), 'partial' if failed else 'success',
                                      len(funds), succeeded, failed, stamp))
    return dict(discovered=len(funds), succeeded=succeeded, failed=failed)


def parse_market_size(source, code):
    fields = source.split('"')[1].split('~')
    if fields[2] != code or not re.fullmatch(r'\d{14}', fields[30]):
        raise ValueError('市值行情代码或时间无效')
    return dict(code=code, market_cap=positive(fields[45])*100000000,
                date=datetime.strptime(fields[30], '%Y%m%d%H%M%S').date().isoformat())


def update_sizes(root):
    if not is_trading_day('CN'):
        print('A股休市：跳过市值更新，沿用上个交易日数据', flush=True)
        return False
    cfg = settings(root)
    archive = root / 'raw/market/qdii' / (datetime.now(SHANGHAI).strftime('%Y%m%dT%H%M%S%f') + '_sizes')
    archive.mkdir(parents=True)
    with connect(root) as db:
        funds = [dict(r) for r in db.execute('SELECT code,market FROM funds WHERE active=1')]
    result = dict(observed_at=now(), source='腾讯行情 · 总市值（元）', funds=[], errors={})
    for fund in funds:
        code = fund['code']
        try:
            source = download('https://qt.gtimg.cn/q=' + ('sh' if fund['market'] else 'sz') + code,
                              archive, code + '_quote.txt', cfg, encoding='gbk')
            result['funds'].append(parse_market_size(source, code))
        except (RuntimeError, ValueError, IndexError) as exc:
            result['errors'][code] = str(exc)
    target = root / 'data/processed/qdii/sizes.json'
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    temporary.replace(target)
    print(f"市值更新: {len(result['funds'])} 成功 / {len(result['errors'])} 失败", flush=True)
    return bool(result['errors'])


REFERENCE_FUNDS = (
    dict(code='VOO', name='Vanguard S&P 500 ETF', benchmark='标普500'),
    dict(code='QQQ', name='Invesco QQQ', benchmark='纳斯达克100'),
)


def parse_reference_chart(source, symbol, currency):
    payload = json.loads(source)['chart']
    if payload.get('error') or not payload.get('result'):
        raise ValueError(f'{symbol}: 境外参考行情为空或错误')
    result = payload['result'][0]
    meta = result['meta']
    if meta['symbol'] != symbol or meta['currency'] != currency:
        raise ValueError(f'{symbol}: 境外行情代码或币种不匹配')
    timezone = ZoneInfo(meta['exchangeTimezoneName'])
    closes = result['indicators']['quote'][0]['close']
    adjusted = result['indicators'].get('adjclose', [{}])[0].get('adjclose', [])
    rows = []
    for i, stamp in enumerate(result['timestamp']):
        if closes[i] is None:
            continue
        row = dict(date=datetime.fromtimestamp(stamp, timezone).date().isoformat(),
                   close=positive(closes[i]))
        if currency == 'USD':
            # 总收益直接使用分红及拆分复权价，不再次加分红
            row['adjusted_close'] = positive(adjusted[i])
        rows.append(row)
    if not rows:
        raise ValueError(f'{symbol}: 无有效日线')
    return sorted(rows, key=lambda row: row['date'])


def parse_ecb_fx(source):
    try:
        document = ET.fromstring(source)
    except ET.ParseError as exc:
        raise ValueError('ECB 汇率 XML 无效') from exc
    rows = []
    for cube in document.findall('.//{http://www.ecb.int/vocabulary/2002-08-01/eurofxref}Cube[@time]'):
        rates = {r.attrib['currency']: r.attrib['rate'] for r in cube}
        if 'USD' not in rates or 'CNY' not in rates:
            continue
        # 两者均为每欧元报价，相除得到每美元对应的人民币数量
        rows.append(dict(date=valid_date(cube.attrib['time']),
                         close=positive(rates['CNY']) / positive(rates['USD'])))
    if not rows:
        raise ValueError('ECB 汇率数据为空')
    return sorted(rows, key=lambda row: row['date'])


def reference_history(prices, fx):
    rates = {r['date']: r['close'] for r in fx}
    dates = sorted(rates)
    rows = []
    for row in prices:
        index = bisect.bisect_right(dates, row['date']) - 1
        if index < 0:
            continue
        fx_date = dates[index]
        if (datetime.fromisoformat(row['date']) - datetime.fromisoformat(fx_date)).days > 7:
            continue
        rows.append(dict(date=row['date'], close=row['adjusted_close'] * rates[fx_date],
                         fx_date=fx_date, usd_cny=rates[fx_date]))
    return rows


def update_references(root):
    if not is_trading_day('US'):
        print('美股休市：跳过境外参考更新，沿用上个交易日数据', flush=True)
        return False
    cfg = settings(root)
    archive = root / 'raw/market/qdii' / (datetime.now(SHANGHAI).strftime('%Y%m%dT%H%M%S%f') + '_references')
    archive.mkdir(parents=True)
    result = dict(observed_at=now(), source='Yahoo Finance · 分红拆分复权日线',
                  fx_source='ECB · EUR/CNY ÷ EUR/USD', fx_latest_date=None, funds=[])
    fx, fx_error = [], None
    try:
        source = download('https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml',
                          archive, 'ecb_fx.xml', cfg)
        fx = parse_ecb_fx(source)
        result['fx_latest_date'] = fx[-1]['date']
    except (RuntimeError, ValueError, KeyError, IndexError, TypeError) as exc:
        fx_error = str(exc)
    for fund in REFERENCE_FUNDS:
        item = dict(fund, history=[], cny_history=[], error=None, fx_error=fx_error)
        try:
            source = download('https://query1.finance.yahoo.com/v8/finance/chart/' + fund['code'] +
                              '?range=10y&interval=1d&events=div%2Csplits', archive, fund['code'].lower() + '.json', cfg)
            prices = parse_reference_chart(source, fund['code'], 'USD')
            item['history'] = [dict(date=r['date'], close=r['adjusted_close']) for r in prices]
            item['cny_history'] = reference_history(prices, fx)
        except (RuntimeError, ValueError, KeyError, IndexError, TypeError) as exc:
            item['error'] = str(exc)
        item['source_run'] = archive.name
        result['funds'].append(item)
    target = root / 'data/processed/qdii/references.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    temporary.replace(target)
    failed = any(f['error'] or f['fx_error'] for f in result['funds'])
    print('境外参考更新: ' + ('部分或全部失败，详见 references.json' if failed else 'VOO / QQQ 美元与人民币完成'), flush=True)
    return failed


def read_data(root):
    reference_path = root / 'data/processed/qdii/references.json'
    references = json.loads(reference_path.read_text()) if reference_path.exists() else dict(
        funds=[dict(f, history=[], cny_history=[], error='尚未采集境外参考行情') for f in REFERENCE_FUNDS])
    path = root / 'data/processed/qdii/market.sqlite3'
    if not path.exists():
        return dict(generated_at=now(), funds=[], last_run=None, market_date=None, references=references)
    db = sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN')
        last = db.execute('SELECT * FROM runs ORDER BY id DESC LIMIT 1').fetchone()
        sizes_path = root / 'data/processed/qdii/sizes.json'
        sizes = json.loads(sizes_path.read_text()) if sizes_path.exists() else {}
        sizes_by_code = {r['code']: r for r in sizes.get('funds', [])}
        funds = []
        market_date = db.execute('SELECT MAX(p.date) FROM prices p JOIN funds f ON p.code=f.code WHERE f.active=1').fetchone()[0]
        for fund in db.execute('SELECT * FROM funds WHERE active=1 ORDER BY code'):
            item = dict(fund)
            item['market_size'] = sizes_by_code.get(fund['code'])
            item['market_size_error'] = sizes.get('errors', {}).get(fund['code'])
            prices = [tuple(row) for row in db.execute(
                'SELECT date,close,turnover FROM prices WHERE code=? ORDER BY date', (fund['code'],))]
            navs = [tuple(row) for row in db.execute(
                'SELECT date,nav FROM navs WHERE code=? ORDER BY date', (fund['code'],))]
            item['nav_history'] = [dict(row) for row in db.execute(
                'SELECT date,nav,first_seen_at,last_seen_at,source_run FROM navs WHERE code=? ORDER BY date',
                (fund['code'],))]
            nav_meta = {row['date']: row for row in item['nav_history']}
            item['distributions'] = []
            item['distribution_error'] = None
            try:
                source_run = item['nav_history'][-1]['source_run']
                source_path = root / 'raw/market/qdii' / source_run / (fund['code'] + '_nav.js')
                item['distributions'] = parse_distributions(source_path.read_text(), fund['code'])
            except (OSError, ValueError, IndexError, TypeError) as exc:
                item['distribution_error'] = str(exc)

            item['history'] = join_history(prices, navs, fund['code'])
            for row in item['history']:
                row['nav_source_run'] = nav_meta.get(row['date'], {}).get('source_run')
                row['nav_first_seen_at'] = nav_meta.get(row['date'], {}).get('first_seen_at')
            provenance = {row['date']: dict(row) for row in db.execute(
                'SELECT date,open,high,low,price_source,source_run,first_seen_at,last_seen_at FROM prices WHERE code=?',
                (fund['code'],))}
            for row in item['history']:
                row.update(provenance[row['date']])
            item['latest_nav_date'] = navs[-1][0] if navs else None
            item['latest_nav'] = navs[-1][1] if navs else None
            item['stale'] = bool(not prices or prices[-1][0] != market_date)
            funds.append(item)
        return dict(generated_at=now(), market_date=market_date,
                    last_run=dict(last) if last else None, funds=funds, references=references)
    finally:
        db.close()


def make_server(root, port):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                      '/favicon.svg': ('favicon.svg', 'image/svg+xml'),
                      '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                      '/charts.mjs': ('charts.mjs', 'text/javascript; charset=utf-8'),
                      '/scores.mjs': ('scores.mjs', 'text/javascript; charset=utf-8'),
                      '/style.css': ('style.css', 'text/css; charset=utf-8')}
            if path in ('/api/data', '/market.json'):
                payload = json.dumps(read_data(root), ensure_ascii=False, allow_nan=False).encode()
                content_type = 'application/json; charset=utf-8'
            elif path in assets:
                name, content_type = assets[path]
                payload = (ROOT / 'web/qdii' / name).read_bytes()
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(payload)

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def install_schedule(root):
    if sys.platform != 'darwin':
        raise RuntimeError('自动安装仅支持 macOS；其他系统请按文档配置定时任务')
    directory = Path.home() / 'Library/LaunchAgents'
    directory.mkdir(parents=True, exist_ok=True)
    logs = root / 'data/processed/qdii'
    logs.mkdir(parents=True, exist_ok=True)
    label = 'local.finance.qdii-update'
    plist = dict(Label=label,
                 ProgramArguments=[sys.executable, str(ROOT / 'scripts/qdii_market.py'),
                                   '--root', str(root), 'update'],
                 WorkingDirectory=str(root),
                 StartCalendarInterval=dict(Hour=20, Minute=30),
                 StandardOutPath=str(logs / 'update.log'),
                 StandardErrorPath=str(logs / 'update-error.log'))
    target = directory / f'{label}.plist'
    if target.exists() and plistlib.loads(target.read_bytes()) == plist:
        print(f'定时配置已存在: {target}')
    else:
        import os
        domain = f'gui/{os.getuid()}'
        if target.exists():
            subprocess.run(['launchctl', 'bootout', domain, str(target)], capture_output=True)
        target.write_bytes(plistlib.dumps(plist))
        subprocess.run(['launchctl', 'bootstrap', domain, str(target)], check=True)
        print(f'已安装每日 20:30 更新: {target}')


def install_service(root, port):
    if sys.platform != 'darwin':
        raise RuntimeError('后台服务自动安装仅支持 macOS')
    import os
    directory = Path.home() / 'Library/LaunchAgents'
    directory.mkdir(parents=True, exist_ok=True)
    logs = root / 'data/processed/qdii'
    logs.mkdir(parents=True, exist_ok=True)
    label = 'local.finance.qdii-web'
    target = directory / f'{label}.plist'
    plist = dict(Label=label,
                 ProgramArguments=[sys.executable, str(ROOT / 'scripts/qdii_market.py'),
                                   '--root', str(root), 'serve', '--port', str(port)],
                 WorkingDirectory=str(root), RunAtLoad=True, KeepAlive=True,
                 ThrottleInterval=15,
                 StandardOutPath=str(logs / 'web.log'),
                 StandardErrorPath=str(logs / 'web-error.log'))
    domain = f'gui/{os.getuid()}'
    loaded = subprocess.run(['launchctl', 'print', f'{domain}/{label}'], capture_output=True)
    if loaded.returncode == 0:
        if target.exists() and plistlib.loads(target.read_bytes()) == plist:
            print(f'后台服务已加载: http://127.0.0.1:{port}')
            return
        subprocess.run(['launchctl', 'bootout', f'{domain}/{label}'], check=True)
    target.write_bytes(plistlib.dumps(plist))
    subprocess.run(['launchctl', 'bootstrap', domain, str(target)], check=True)
    print(f'已安装后台服务: http://127.0.0.1:{port}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    commands = parser.add_subparsers(dest='command', required=True)
    update_cmd = commands.add_parser('update')
    update_cmd.add_argument('--full-history', action='store_true', help='重新请求全部历史价格，跳过当日缓存')
    serve = commands.add_parser('serve')
    serve.add_argument('--port', type=int, default=8765)
    commands.add_parser('install-schedule')
    commands.add_parser('update-sizes')
    commands.add_parser('update-references', help='更新 VOO / QQQ 分红复权日线与历史美元人民币汇率')
    service = commands.add_parser('install-service', help='安装登录自动启动的 macOS 网页后台服务')
    service.add_argument('--port', type=int, default=8876)
    commands.add_parser('backfill-ohlc', help='从原始响应补充已有行情的开高低价，不联网')
    export = commands.add_parser('export')
    export.add_argument('--output', type=Path, default=ROOT / 'data/processed/qdii/market.json')
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == 'update':
        result = update(root, args.full_history)
        print(json.dumps(result, ensure_ascii=False))
        sizes_failed = update_sizes(root)
        references_failed = update_references(root)
        return 1 if result['failed'] or sizes_failed or references_failed else 0
    if args.command == 'update-references':
        return int(update_references(root))
    if args.command == 'update-sizes':
        return int(update_sizes(root))
    if args.command == 'serve':
        server = make_server(root, args.port)
        print(f'Local: http://127.0.0.1:{server.server_port}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    elif args.command == 'install-service':
        install_service(root, args.port)
    elif args.command == 'backfill-ohlc':
        print(f'已从原始行情补入 {backfill_ohlc(root)} 条 OHLC')
    elif args.command == 'export':
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(read_data(root), ensure_ascii=False, allow_nan=False), encoding='utf-8')
        print(args.output)
    else:
        install_schedule(root)
    return 0


if __name__ == '__main__':
    sys.exit(main())
