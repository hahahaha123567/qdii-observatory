import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    'qdii_market', Path(__file__).resolve().parents[1] / 'scripts/qdii_market.py')
market = importlib.util.module_from_spec(spec)
spec.loader.exec_module(market)

ECB_FX = '''<Envelope xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref"><Cube>
<Cube time="2026-10-30"><Cube currency="USD" rate="1.1172"/><Cube currency="CNY" rate="7.4326"/></Cube>
<Cube time="2023-10-30"><Cube currency="USD" rate="1.0605"/><Cube currency="CNY" rate="7.7595"/></Cube>
</Cube></Envelope>'''

FUND = {'code': '513500', 'name': '标普500ETF博时', 'kind': 'ETF',
        'market': 1, 'benchmark': '标普500'}


class QdiiMarketTest(unittest.TestCase):
    def setUp(self):
        clock = market.datetime(2026, 11, 3, 20, 30, tzinfo=market.SHANGHAI)
        patcher = patch.object(market, 'datetime', wraps=market.datetime)
        clock_type = patcher.start()
        self.addCleanup(patcher.stop)
        ticks = iter(range(100000))
        clock_type.now.side_effect = lambda zone: (clock + market.timedelta(microseconds=next(ticks))).astimezone(zone)

    def test_trading_calendar_distinguishes_holidays_weekends_and_markets(self):
        from datetime import date
        for venue, day, expected in [
            ('CN', '2026-10-01', False), ('US', '2026-10-01', True),
            ('CN', '2026-10-10', False), ('US', '2026-11-07', False),
            ('CN', '2026-11-26', True), ('US', '2026-11-26', False),
            ('US', '2026-11-27', True), ('CN', '2026-11-03', True),
        ]:
            with self.subTest(venue=venue, day=day):
                self.assertEqual(market.is_trading_day(venue, date.fromisoformat(day)), expected)
        with self.assertRaisesRegex(ValueError, '交易日历'):
            market.is_trading_day('CN', date(2027, 1, 4))

    def test_closed_market_updates_leave_snapshots_and_status_untouched(self):
        clocks = ['2026-11-07T20:30:00+08:00', '2026-06-19T20:30:00+08:00']
        for value in clocks:
            with self.subTest(clock=value), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                folder = root / 'data/processed/qdii'
                folder.mkdir(parents=True)
                for name in ['market.sqlite3', 'sizes.json', 'references.json']:
                    (folder / name).write_text('existing snapshot')
                before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.iterdir()}
                clock = market.datetime.fromisoformat(value)
                with patch.object(market, 'datetime', wraps=market.datetime) as clock_type, patch.object(
                        market, 'download') as fetch, patch.object(market, 'run_update') as run:
                    clock_type.now.side_effect = lambda zone: clock.astimezone(zone)
                    self.assertEqual(market.update(root)['failed'], 0)
                    self.assertFalse(market.update_sizes(root))
                    self.assertFalse(market.update_references(root))
                    fetch.assert_not_called()
                    run.assert_not_called()
                self.assertFalse((root / 'raw').exists())
                self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.iterdir()})

    def test_us_calendar_uses_new_york_date_across_shanghai_midnight(self):
        clock = market.datetime.fromisoformat('2026-11-07T01:00:00+08:00')
        with patch.object(market, 'datetime', wraps=market.datetime) as clock_type:
            clock_type.now.side_effect = lambda zone: clock.astimezone(zone)
            self.assertFalse(market.is_trading_day('CN'))
            self.assertTrue(market.is_trading_day('US'))

    def test_daily_market_date_reports_the_preceding_session_if_either_market_traded(self):
        for morning, expected in [
            ('2026-11-07', '2026-11-06'),
            ('2026-11-09', ''),
            ('2026-11-27', '2026-11-26'),
            ('2026-10-02', '2026-10-01'),
        ]:
            clock = market.datetime.fromisoformat(f'{morning}T07:35:00+08:00')
            output = io.StringIO()
            with self.subTest(morning=morning), redirect_stdout(output), patch.object(
                market.datetime, 'now', side_effect=lambda zone: clock.astimezone(zone),
            ), patch.object(market.sys, 'argv', ['qdii_market.py', 'daily-market-date']):
                self.assertEqual(market.main(), 0)
                self.assertEqual(output.getvalue().strip(), expected)

    def test_saturday_daily_update_collects_friday_prices_and_both_snapshots(self):
        clock = market.datetime.fromisoformat('2026-11-07T07:35:00+08:00')
        friday = '2026-11-06'
        stamp = int(market.datetime.fromisoformat(f'{friday}T16:00:00-05:00').timestamp())
        quote = ['0'] * 46
        quote[2], quote[30], quote[45] = FUND['code'], '20261106150000', '1.2'

        def response(url, archive, name, cfg, **kwargs):
            if name.endswith('_nav.js'):
                nav_timestamp = int(market.datetime.fromisoformat(f'{friday}T00:00:00+08:00').timestamp()) * 1000
                return f'var Data_netWorthTrend = [{{"x":{nav_timestamp},"y":1}}];'
            if name.endswith('_prices.json'):
                return '{"rc":0,"data":{"klines":["2026-11-06,1.2,1.2,1.2,1.2,0,100"]}}'
            if name.endswith('_quote.txt'):
                return '"' + '~'.join(quote) + '"'
            if name == 'ecb_fx.xml':
                return ECB_FX
            return json.dumps({'chart': {'result': [{
                'meta': {'symbol': name.removesuffix('.json').upper(), 'currency': 'USD',
                         'exchangeTimezoneName': 'America/New_York'},
                'timestamp': [stamp],
                'indicators': {'quote': [{'close': [100]}], 'adjclose': [{'adjclose': [100]}]}
            }], 'error': None}})

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch.object(market.datetime, 'now', side_effect=lambda zone: clock.astimezone(zone)), patch.object(
                market, 'discover', return_value=[FUND],
            ), patch.object(market, 'download', side_effect=response), patch.object(
                market.sys, 'argv', ['qdii_market.py', '--root', d, 'update', '--market-date', friday],
            ):
                self.assertEqual(market.main(), 0)
            data = market.read_data(root)
            self.assertEqual(data['market_date'], friday)
            self.assertEqual(data['funds'][0]['market_size']['date'], friday)
            self.assertEqual([fund['history'][-1]['date'] for fund in data['references']['funds']], [friday, friday])

    def test_ecb_fx_cross_rates_are_cny_per_usd_and_reject_invalid_source(self):
        source = ECB_FX.replace('</Cube></Envelope>', '<Cube time="2000-01-03"><Cube currency="USD" rate="1"/></Cube></Cube></Envelope>')
        rows = market.parse_ecb_fx(source)
        self.assertEqual([r['date'] for r in rows], ['2023-10-30', '2026-10-30'])
        self.assertAlmostEqual(rows[0]['close'], 7.316831683168317)
        self.assertAlmostEqual(rows[1]['close'], 6.652882205513785)
        for invalid in ['<broken', '<Envelope/>', ECB_FX.replace('currency="CNY"', 'currency="JPY"'),
                        ECB_FX.replace('rate="1.1172"', 'rate="0"')]:
            with self.assertRaises(ValueError):
                market.parse_ecb_fx(invalid)

    def test_reference_update_converts_with_ecb_rates_and_exports_provenance(self):
        def response(url, archive, name, cfg):
            if 'ecb.europa.eu' in url:
                return ECB_FX
            code = 'VOO' if '/VOO?' in url else 'QQQ'
            return json.dumps({'chart': {'result': [{
                'meta': {'symbol': code, 'currency': 'USD', 'exchangeTimezoneName': 'America/New_York'},
                'timestamp': [1698696000, 1793390400],
                'indicators': {'quote': [{'close': [100, 200]}], 'adjclose': [{'adjclose': [90, 200]}]}
            }], 'error': None}})
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch.object(market, 'download', side_effect=response):
                self.assertFalse(market.update_references(root))
            data = market.read_data(root)['references']
            self.assertEqual(data['fx_source'], 'ECB · EUR/CNY ÷ EUR/USD')
            self.assertEqual(data['fx_latest_date'], '2026-10-30')
            self.assertEqual(len(data['funds']), 2)
            for fund in data['funds']:
                self.assertEqual(fund['history'][0]['close'], 90)
                self.assertEqual(fund['cny_history'][0]['date'], '2023-10-30')
                self.assertAlmostEqual(fund['cny_history'][0]['close'], 90 * 7.7595 / 1.0605)
                self.assertAlmostEqual(fund['cny_history'][1]['close'], 200 * 7.4326 / 1.1172)

    def test_reference_chart_validates_symbol_currency_and_uses_exchange_date(self):
        payload = {'chart': {'result': [{
            'meta': {'symbol': 'VOO', 'currency': 'USD', 'exchangeTimezoneName': 'America/New_York'},
            'timestamp': [1793390400],
            'indicators': {'quote': [{'close': [100]}], 'adjclose': [{'adjclose': [90]}]}
        }], 'error': None}}
        rows = market.parse_reference_chart(json.dumps(payload), 'VOO', 'USD')
        self.assertEqual(rows, [{'date': '2026-10-30', 'close': 100.0, 'adjusted_close': 90.0}])
        with self.assertRaises(ValueError):
            market.parse_reference_chart(json.dumps(payload), 'QQQ', 'USD')
        with self.assertRaises(ValueError):
            market.parse_reference_chart(json.dumps(payload), 'VOO', 'CNY')

    def test_reference_returns_convert_adjusted_prices_with_historical_fx(self):
        prices = [{'date': '2025-10-30', 'close': 100, 'adjusted_close': 90},
                  {'date': '2026-10-30', 'close': 120, 'adjusted_close': 120}]
        fx = [{'date': '2025-10-29', 'close': 7}, {'date': '2026-10-30', 'close': 6}]
        rows = market.reference_history(prices, fx)
        self.assertEqual([r['close'] for r in rows], [630, 720])
        self.assertEqual(rows[0]['fx_date'], '2025-10-29')
        self.assertEqual(market.reference_history(prices, fx[:1]), [rows[0]])

    def test_failed_reference_update_is_visible_and_keeps_raw_provenance(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with market.connect(root):
                pass
            with patch.object(market, 'download', side_effect=RuntimeError('网络失败')):
                self.assertTrue(market.update_references(root))
            snapshot = json.loads((root / 'data/processed/qdii/references.json').read_text())
            self.assertEqual([r['code'] for r in snapshot['funds']], ['VOO', 'QQQ'])
            self.assertTrue(all(r['error'] and not r['history'] for r in snapshot['funds']))
            self.assertEqual(len(list((root / 'raw/market/qdii').iterdir())), 1)

    def test_market_size_validates_identity_timestamp_and_units(self):
        fields = [''] * 46
        fields[2], fields[30], fields[45] = '513500', '20261028150000', '286.88'
        source = 'v_sh513500="' + '~'.join(fields) + '";'
        row = market.parse_market_size(source, '513500')
        self.assertEqual(row['date'], '2026-10-28')
        self.assertEqual(row['market_cap'], 28688000000)
        with self.assertRaises(ValueError):
            market.parse_market_size(source, '159941')
        fields[45] = 'nan'
        with self.assertRaises(ValueError):
            market.parse_market_size('v="' + '~'.join(fields) + '";', '513500')


    def test_defensive_cash_distributions_reinvest_on_exchange_ex_date(self):
        archive_html = """<title>银华日利ETF(511880)基金分红送配</title>
        <table><th>权益登记日</th><th>除息日</th><th>每10份分红</th><tbody>
        <tr><td>2025年</td><td>2025-12-30</td><td>2025-12-30</td><td>每10份派现金10.0000元</td><td>2026-01-07</td></tr>
        </tbody></table>"""
        history = [{'date': '2025-12-30', 'close': 101, 'return_price_factor': 1},
                   {'date': '2025-12-31', 'close': 100, 'return_price_factor': 1},
                   {'date': '2026-01-05', 'close': 102, 'return_price_factor': 1}]
        events = market.defensive_distributions(archive_html, '511880')
        self.assertEqual(events[0]['date'], '2025-12-31')
        market.apply_defensive_returns(history, events, [], '511880')
        self.assertAlmostEqual(history[1]['return_index'], history[0]['return_index'])
        self.assertAlmostEqual(history[2]['return_index'] / history[0]['return_index'] - 1, 0.02)
        incomplete = history[::2]
        with self.assertRaisesRegex(ValueError, '2025-12-31 缺少除息日收盘价'):
            events = market.defensive_distributions(archive_html, '511880')
            market.apply_defensive_returns(incomplete, events, [], '511880')
        holiday = archive_html.replace('2025-12-30', '2026-09-30')
        self.assertEqual(market.defensive_distributions(holiday, '511880')[0]['date'], '2026-10-08')

    def test_defensive_money_returns_include_published_weekend_income(self):
        incomes = [('2026-10-23', 3), ('2026-10-25', 2), ('2026-10-26', 1)]
        cash = []
        market.apply_defensive_returns(cash, [], incomes, '000198')
        self.assertEqual([row['date'] for row in cash], [day for day, _ in incomes])
        self.assertAlmostEqual(cash[-1]['return_index'] / cash[0]['return_index'], 1.0002 * 1.0001)
        etf = [{'date': '2026-10-23', 'close': 100}, {'date': '2026-10-26', 'close': 101},
               {'date': '2026-10-27', 'close': 102}]
        market.apply_defensive_returns(etf, [], incomes, '511990')
        self.assertAlmostEqual(etf[1]['return_index'] / etf[0]['return_index'], 1.01 * 1.0003 * 1.0002)
        self.assertNotIn('return_index', etf[2])
        etf = [{k: v for k, v in row.items() if k != 'return_index'} for row in etf]
        market.apply_defensive_returns(etf, [], [*incomes[:2], ('2026-10-27', 1)], '511990')
        self.assertNotIn('return_index', etf[1])
        self.assertNotIn('return_index', etf[2])
        market.apply_defensive_returns(etf, [], [*incomes, ('2026-10-27', 2)], '511990')
        self.assertAlmostEqual(etf[2]['return_index'] / etf[0]['return_index'], 1.0003 * 1.0002 * 1.0001 * 1.02)

    def test_shortened_money_snapshot_keeps_previously_disclosed_returns(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fund = next(f for f in market.DEFENSIVE_FUNDS if f['code'] == '000198')
            days = ['2026-10-30', '2026-11-01', '2026-11-02']

            def response(url, archive, name, cfg):
                data = [(int(market.datetime.fromisoformat(day).replace(tzinfo=market.SHANGHAI).timestamp()*1000), 1)
                        for day in days]
                source = 'var fS_code = "000198"; var Data_millionCopiesIncome = ' + json.dumps(data) + ';'
                (archive / name).write_text(source)
                return source

            with patch.object(market, 'discover', return_value=[fund]), patch.object(
                    market, 'download', side_effect=response):
                self.assertEqual(market.update(root)['failed'], 0)
                prior = market.read_data(root)['funds'][0]['history']
                days[:] = ['2026-11-02', '2026-11-03']
                self.assertEqual(market.update(root)['failed'], 1)
            self.assertEqual(market.read_data(root)['funds'][0]['history'], prior)

    def test_defensive_close_does_not_change_qdii_market_date(self):
        fund = next(f for f in market.DEFENSIVE_FUNDS if f['code'] == '511580')
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with market.connect(root) as db:
                for item, day in [(FUND, '2026-11-02'), (fund, '2026-11-03')]:
                    db.execute('INSERT INTO funds(code, name, kind, market, benchmark) VALUES'
                               '(:code,:name,:kind,:market,:benchmark)', item)
                    db.execute('INSERT INTO prices(code,date,close) VALUES(?,?,?)', (item['code'], day, 100))
            self.assertEqual(market.read_data(root)['market_date'], '2026-11-02')

    def test_premiums_do_not_fill_missing_same_day_nav_or_bridge_gaps(self):
        rows = market.join_history(
            [('2026-10-19', 1.1, 100), ('2026-10-20', 1.2, 100),
             ('2026-10-21', 1.3, 100), ('2026-10-22', 1.4, 100)],
            [('2026-10-16', 1.0), ('2026-10-19', 1.0), ('2026-10-21', 1.25)])
        self.assertAlmostEqual(rows[0]['premium_same_date_pct'], 10)
        self.assertIsNone(rows[1]['premium_same_date_pct'])
        self.assertEqual(rows[1]['previous_nav_date'], '2026-10-19')
        self.assertAlmostEqual(rows[1]['premium_previous_nav_pct'], 20)
        self.assertIsNone(rows[2]['premium_change_pp'])
        self.assertEqual(rows[3]['previous_nav_date'], '2026-10-21')

    def test_failed_refresh_preserves_prior_history_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch.object(market, 'discover', return_value=[FUND]), patch.object(
                market, 'fetch_fund', return_value=(
                    [('2026-10-20', 1.2, 100)], [('2026-10-20', 1.0)])):
                result = market.update(root)
            self.assertEqual(result['failed'], 0)
            with patch.object(market, 'discover', return_value=[FUND]), patch.object(
                market, 'fetch_fund', side_effect=ValueError('净值数据为空')):
                result = market.update(root)
            self.assertEqual(result['failed'], 1)
            data = market.read_data(root)
            self.assertEqual(data['funds'][0]['history'][0]['close'], 1.2)
            self.assertIn('净值数据为空', data['funds'][0]['error'])
            self.assertEqual(data['last_run']['status'], 'partial')

    def test_repeated_updates_are_idempotent_and_revise_nav(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch.object(market, 'discover', return_value=[FUND]), patch.object(
                market, 'fetch_fund', return_value=(
                    [('2026-10-20', 1.2, 100)], [('2026-10-20', 1.0)])):
                market.update(root)
                market.update(root)
            with patch.object(market, 'discover', return_value=[FUND]), patch.object(
                market, 'fetch_fund', return_value=(
                    [('2026-10-20', 1.2, None)], [('2026-10-20', 1.1)])):
                market.update(root)
            data = market.read_data(root)
            self.assertEqual(len(data['funds'][0]['history']), 1)
            self.assertEqual(data['funds'][0]['history'][0]['turnover'], 100)
            self.assertAlmostEqual(data['funds'][0]['history'][0]['premium_same_date_pct'], 100*(1.2/1.1-1))

    def test_parsers_reject_empty_and_invalid_data(self):
        for value in ['var Data_netWorthTrend = [];',
                      'var Data_netWorthTrend = [{"x":0,"y":-1}];']:
            with self.assertRaises(ValueError):
                market.parse_nav(value)
        with self.assertRaises(ValueError):
            market.parse_prices({'rc': 0, 'data': None})

    def test_discovery_failure_keeps_existing_catalog_active(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch.object(market, 'discover', return_value=[FUND]), patch.object(
                market, 'fetch_fund', return_value=(
                    [('2026-10-20', 1.2, 100)], [('2026-10-20', 1.0)])):
                market.update(root)
            with market.connect(root) as db:
                db.execute('INSERT INTO funds(code,active) VALUES(?,0)', ('159941',))
            with patch.object(market, 'discover', side_effect=ValueError('上游目录为空')), patch.object(
                    market, 'fetch_fund', return_value=(
                        [('2026-11-03', 1.3, 100)], [('2026-11-03', 1.1)])) as fetch, patch.object(
                    market, 'update_sizes', return_value=False), patch.object(
                    market, 'update_references', return_value=False), patch.object(
                    market.sys, 'argv', ['qdii_market.py', '--root', str(root), 'update']):
                self.assertEqual(market.main(), 0)
            self.assertEqual(fetch.call_args.args[0], FUND)
            self.assertEqual(fetch.call_count, 1)
            data = market.read_data(root)
            self.assertEqual(data['funds'][0]['active'], 1)
            self.assertEqual(data['funds'][0]['history'][-1]['date'], '2026-11-03')
            self.assertEqual(data['last_run']['status'], 'degraded')
            self.assertEqual(data['last_run']['discovered'], 0)
            self.assertEqual(data['last_run']['succeeded'], 1)
            self.assertIn('上游目录为空', data['last_run']['error'])

    def test_discovery_failure_without_active_catalog_stops_update(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with market.connect(root) as db:
                db.execute('INSERT INTO funds(code,active) VALUES(?,0)', ('159941',))
            with patch.object(market, 'discover', side_effect=ValueError('上游目录为空')), patch.object(
                    market, 'fetch_fund') as fetch:
                result = market.update(root)
            self.assertEqual(result['failed'], 1)
            fetch.assert_not_called()
            data = market.read_data(root)
            self.assertEqual(data['last_run']['status'], 'failed')

    def test_cached_catalog_does_not_hide_quote_failure_and_discovery_can_recover(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch.object(market, 'discover', return_value=[FUND]), patch.object(
                    market, 'fetch_fund', return_value=(
                        [('2026-10-20', 1.2, 100)], [('2026-10-20', 1.0)])):
                market.update(root)
            with patch.object(market, 'discover', side_effect=ValueError('目录超时')), patch.object(
                    market, 'fetch_fund', side_effect=ValueError('净值超时')):
                result = market.update(root)
            self.assertEqual(result['failed'], 1)
            data = market.read_data(root)
            self.assertEqual(data['last_run']['status'], 'partial')
            self.assertIn('目录超时', data['last_run']['error'])
            self.assertIn('净值超时', data['funds'][0]['error'])
            self.assertEqual(data['funds'][0]['history'][0]['close'], 1.2)
            with patch.object(market, 'discover', return_value=[FUND]), patch.object(
                    market, 'fetch_fund', return_value=(
                        [('2026-11-03', 1.3, 100)], [('2026-11-03', 1.1)])):
                market.update(root)
            data = market.read_data(root)
            self.assertEqual(data['last_run']['status'], 'success')
            self.assertIsNone(data['last_run']['error'])

    def test_profile_uses_full_name_to_exclude_unlisted_funds(self):
        row = ['160213', '', '国泰纳斯达克100指数', '指数型-海外股票']
        source = '<th>基金全称</th><td>国泰纳斯达克100指数证券投资基金</td>'
        with patch.object(market, 'download', return_value=source):
            self.assertIsNone(market.profile_fund(row, Path('/unused'), market.settings(Path('/unused'))))
        row[0] = '161130'
        source = ('<th>基金全称</th><td>易方达纳斯达克100联接基金（LOF）</td>'
                  '<th>跟踪标的</th><td>纳斯达克100指数</td>')
        with patch.object(market, 'download', return_value=source):
            fund = market.profile_fund(row, Path('/unused'), market.settings(Path('/unused')))
        self.assertEqual(fund['kind'], 'LOF')
        self.assertEqual(fund['benchmark'], '纳斯达克100')

    def test_tencent_paginates_backwards_and_keeps_turnover_unknown(self):
        from datetime import date, timedelta
        first = date(2025, 1, 1)
        batch = [[(first + timedelta(days=i)).isoformat(), '1', '1.2', '1.3', '1', '10']
                 for i in range(640)]
        older = [['2024-12-31', '1', '1.1', '1.2', '1', '10']]
        def payload(rows):
            return json.dumps({'code': 0, 'data': {'sh513500': {'day': rows}}})
        with patch.object(market, 'download', side_effect=[payload(batch), payload(older)]) as fetch:
            result = market.tencent_prices(FUND, Path('/unused'), market.settings(Path('/unused')))
        self.assertEqual(len(result), 641)
        self.assertEqual(result[0], ('2024-12-31', 1.1, None))
        self.assertIn('2024-12-31', fetch.call_args_list[1].args[0])

    def test_scope_excludes_sector_and_equal_weight_indices(self):
        row = ['159509', '', '纳指科技ETF景顺', '指数型-海外股票']
        for index in ['纳斯达克科技市值加权指数', '标普500医疗保健等权重指数',
                      '标普500等权重指数', '纳斯达克生物科技指数']:
            source = ('<th>基金全称</th><td>某交易型开放式指数证券投资基金</td>'
                      f'<th>跟踪标的</th><td>{index}</td>')
            with self.subTest(index=index), patch.object(market, 'download', return_value=source):
                self.assertIsNone(market.profile_fund(row, Path('/unused'), market.settings(Path('/unused'))))

    def test_closed_day_price_cache_still_refreshes_nav(self):
        clock = market.datetime(2026, 10, 23, 17, tzinfo=market.SHANGHAI)
        source = 'var Data_netWorthTrend = ' + json.dumps([
            {'x': int(clock.timestamp()*1000), 'y': 1.05}]) + ';'
        cfg = market.settings(Path('/unused'))
        cfg['_latest_prices'] = {'513500': clock.date().isoformat()}
        with patch.object(market, 'datetime', wraps=market.datetime) as clock_type, patch.object(
            market, 'download', return_value=source) as fetch:
            clock_type.now.return_value = clock
            prices, navs = market.fetch_fund(FUND, Path('/unused'), cfg)
        self.assertEqual(prices, [])
        self.assertEqual(navs[-1][1], 1.05)
        self.assertEqual(fetch.call_count, 1)
        self.assertIn('pingzhongdata', fetch.call_args.args[0])

    def test_incremental_tencent_request_stops_at_overlap(self):
        from datetime import date, timedelta
        first = date(2025, 1, 1)
        batch = [[(first + timedelta(days=i)).isoformat(), '1', '1.2', '1.3', '1', '10']
                 for i in range(640)]
        source = json.dumps({'code': 0, 'data': {'sh513500': {'day': batch}}})
        cfg = market.settings(Path('/unused'))
        cfg['_price_start'] = '2025-02-01'
        with patch.object(market, 'download', return_value=source) as fetch:
            rows = market.tencent_prices(FUND, Path('/unused'), cfg)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(rows[0][0], '2025-02-01')
        self.assertEqual(len(rows), 609)

    def test_archived_ohlc_backfill_preserves_nav_and_uses_matching_quote(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch.object(market, 'discover', return_value=[FUND]), patch.object(
                market, 'fetch_fund', return_value=(
                    [('2026-10-20', 1.2, 100)], [('2026-10-20', 1.0)])):
                market.update(root)
            before = market.read_data(root)['funds'][0]['history'][0]
            archive = root / 'raw/market/qdii' / before['source_run']
            source = {'code': 0, 'data': {'sh513500': {'day': [
                ['2026-10-20', '1.1', '1.2', '1.3', '1.05', '10'],
                ['2026-10-21', '1.1', '1.4', '1.5', '1.05', '10']]}}}
            (archive / '513500_tencent_0.json').write_text(json.dumps(source))
            (archive / '513500_tencent_0.json.request.json').write_text(json.dumps({'url': 'https://example.com'}))
            count = market.backfill_ohlc(root)
            after = market.read_data(root)['funds'][0]['history'][0]
            self.assertEqual(count, 1)
            self.assertEqual((after['open'], after['high'], after['low']), (1.1, 1.3, 1.05))
            self.assertEqual(after['nav'], before['nav'])
            self.assertEqual(after['premium_same_date_pct'], before['premium_same_date_pct'])
            self.assertEqual(after['source_run'], before['source_run'])
            self.assertEqual(len(market.read_data(root)['funds'][0]['history']), 1)

    def test_ohlc_rejects_impossible_candle(self):
        with tempfile.TemporaryDirectory() as d:
            archive = Path(d)
            source = {'rc': 0, 'data': {'klines': ['2026-10-20,1.1,1.2,1.15,1.05,10,100']}}
            (archive / '513500_prices.json').write_text(json.dumps(source))
            with self.assertRaisesRegex(ValueError, 'OHLC'):
                market.archive_ohlc(archive, '513500')

    def test_server_does_not_expose_private_project_files(self):
        import threading
        import urllib.request
        import urllib.error
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'AGENTS.md').write_text('private')
            server = market.make_server(root, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                with urllib.request.urlopen(base + '/api/data') as r:
                    self.assertEqual(json.load(r)['funds'], [])
                for path in ['/AGENTS.md', '/data/manual/account_balances.csv', '/../AGENTS.md']:
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(base + path)
                    self.assertEqual(error.exception.code, 404)
                    error.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
