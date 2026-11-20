"""验证云端状态恢复到子路径静态站点的完整流程"""
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import qdii_market as market
spec = importlib.util.spec_from_file_location('qdii_site', ROOT / 'scripts/site.py')
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)


class SiteTest(unittest.TestCase):
    def test_cash_dividends_survive_stale_nav_and_incomplete_dividend_refresh(self):
        fund = next(f for f in market.DEFENSIVE_FUNDS if f['code'] == '511880')
        payout = ('<tr><td>2025</td><td>2025-12-30</td><td>2025-12-30</td>'
                  '<td>每10份派现金10元</td><td>2026-01-07</td></tr>')
        empty = '<tr><td>暂无分红信息!</td></tr>'
        prices = [('2025-12-29', 101, 1), ('2025-12-30', 101, 1)]
        navs = [('2025-12-29', 101), ('2025-12-30', 101)]
        dividend_rows = empty
        nav_marker = ''

        def fetch(fund, archive, cfg):
            (archive / '511880_dividends.html').write_text(
                '<title>银华日利ETF(511880)基金分红送配</title>'
                '<table><th>权益登记日</th><th>除息日</th><th>每10份分红</th><tbody>'
                + dividend_rows + '</tbody></table>', encoding='utf-8')
            (archive / '511880_nav.js').write_text('var Data_netWorthTrend = ' + json.dumps([
                {'x': int(market.datetime.fromisoformat(day).replace(tzinfo=market.SHANGHAI).timestamp()*1000),
                 'y': nav, 'unitMoney': nav_marker if day == '2025-12-30' else ''}
                for day, nav in navs]) + ';', encoding='utf-8')
            return prices, navs

        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            root = Path(first)
            with patch.object(market, 'discover', return_value=[fund]), patch.object(
                    market, 'fetch_fund', side_effect=fetch):
                self.assertEqual(market.run_update(root, {'workers': 1})['failed'], 0)
                prices.append(('2025-12-31', 100, 1))
                nav_marker = '分红：每10份派现金元'
                self.assertEqual(market.run_update(root, {'workers': 1})['failed'], 1)
                self.assertEqual(market.read_data(root)['funds'][0]['history'][-1]['date'], '2025-12-30')
                navs.pop()
                dividend_rows = payout
                self.assertEqual(market.run_update(root, {'workers': 1})['failed'], 0)
                accepted = market.read_data(root)['funds'][0]
                self.assertEqual(accepted['history'][-1]['return_index'], 101)
                self.assertEqual(accepted['distributions'][0]['date'], '2025-12-31')

                prices.append(('2026-01-05', 102, 1))
                navs.append(('2025-12-30', 101))
                nav_marker = '拆分：每份基金份额折算份'
                self.assertEqual(market.run_update(root, {'workers': 1})['failed'], 1)
                nav_marker = ''
                dividend_rows = empty
                self.assertEqual(market.run_update(root, {'workers': 1})['failed'], 1)
            with patch.object(site, 'ROOT', root):
                site.pack(root / 'state.tar.gz')
            with patch.object(site, 'ROOT', Path(second)):
                site.restore(root / 'state.tar.gz')
            restored = market.read_data(Path(second))['funds'][0]
            self.assertEqual(restored['history'], accepted['history'])
            self.assertEqual(restored['distributions'], accepted['distributions'])

    def test_catalog_fallback_survives_state_round_trip_and_publishes_updated_prices(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            root = Path(first)
            with patch.object(site, 'ROOT', root):
                site.restore(ROOT / 'seed/state.tar.gz')
                with patch.object(market, 'is_trading_day', return_value=True), patch.object(
                        market, 'discover', side_effect=TimeoutError('基金档案握手超时')), patch.object(
                        market, 'fetch_fund', return_value=([('2026-11-10', 1.3, 100)], [])):
                    result = market.update(root)
                self.assertEqual(result['failed'], 0)
                site.pack(root / 'state.tar.gz')
            target = Path(second)
            shutil.copytree(ROOT / 'web', target / 'web')
            with patch.object(site, 'ROOT', target):
                site.restore(root / 'state.tar.gz')
                site.build(target / 'dist')
            data = json.loads((target / 'dist/market.json').read_text())
            self.assertEqual(data['last_run']['status'], 'degraded')
            self.assertIn('基金档案握手超时', data['last_run']['error'])
            self.assertEqual(data['market_date'], '2026-11-10')
            self.assertTrue(all(f['history'][-1]['close'] == 1.3 for f in data['funds']))
            self.assertTrue(all(not f['distribution_error'] for f in data['funds']))

    def test_seed_round_trip_build_retains_history_and_distributions(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            with patch.object(site, 'ROOT', Path(first)):
                site.restore(ROOT / 'seed/state.tar.gz')
                site.pack(Path(first) / 'state.tar.gz')
            target = Path(second)
            shutil.copytree(ROOT / 'web', target / 'web')
            with patch.object(site, 'ROOT', target):
                site.restore(Path(first) / 'state.tar.gz')
                site.build(target / 'dist')
            data = json.loads((target / 'dist/market.json').read_text())
            self.assertTrue(data['funds'])
            self.assertTrue(all(f['history'] and not f['distribution_error'] for f in data['funds']))
            index = (target / 'dist/index.html').read_text()
            self.assertNotIn('href="/', index)
            self.assertNotIn('src="/', index)
            self.assertIn("fetch('./market.json'", (target / 'dist/app.js').read_text())
            self.assertFalse((target / 'dist/market.sqlite3').exists())
