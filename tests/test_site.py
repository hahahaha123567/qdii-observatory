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
spec = importlib.util.spec_from_file_location('qdii_site', ROOT / 'scripts/site.py')
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)


class SiteTest(unittest.TestCase):
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
