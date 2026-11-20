#!/usr/bin/env python3
"""构建静态站点及可恢复的公开行情状态"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile

from qdii_market import DEFENSIVE_CASH_CODES, ROOT, cash_dividend_batches, read_data


def build(output):
    data = read_data(ROOT)
    if not data['funds'] or not data['market_date']:
        raise ValueError('没有可发布的行情数据')
    if any(f['distribution_error'] for f in data['funds']):
        raise ValueError('缺少净值原件，无法确认分红记录')
    output.mkdir(parents=True, exist_ok=True)
    for asset in (ROOT / 'web/qdii').iterdir():
        if asset.is_file():
            shutil.copy2(asset, output / asset.name)
    (output / '.nojekyll').touch()
    (output / 'market.json').write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    print(f'已构建 {output}，行情日期 {data["market_date"]}，{len(data["funds"])} 只基金')


def pack(output):
    output.parent.mkdir(parents=True, exist_ok=True)
    folder = Path('data/processed/qdii')
    with tempfile.TemporaryDirectory() as name:
        staging = Path(name)
        (staging / folder).mkdir(parents=True)
        # backup 将 WAL 中的已提交记录一并保存
        with closing(sqlite3.connect(ROOT / folder / 'market.sqlite3')) as source, closing(sqlite3.connect(staging / folder / 'market.sqlite3')) as target:
            source.backup(target)
            nav_runs = dict(target.execute('SELECT code,source_run FROM navs n WHERE date=(SELECT MAX(date) FROM navs WHERE code=n.code)'))
            dividend_runs = {code: run for code, run in nav_runs.items() if code in DEFENSIVE_CASH_CODES}
            dividend_runs.update(cash_dividend_batches(target))
        for filename in ('sizes.json', 'references.json'):
            source = ROOT / folder / filename
            if source.exists():
                shutil.copy2(source, staging / folder / filename)
        # 收益依赖最近净值和分红原件，完整批次另外归档
        for runs, suffix in ((nav_runs, 'nav.js'), (dividend_runs, 'dividends.html')):
            for code, run in runs.items():
                name = f'{code}_{suffix}'
                relative = Path('raw/market/qdii') / run / name
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
        temporary = output.with_suffix('.tmp')
        with tarfile.open(temporary, 'w:gz') as archive:
            for source in sorted(staging.rglob('*')):
                if source.is_file():
                    archive.add(source, arcname=source.relative_to(staging))
        temporary.replace(output)
    print(f'状态已保存到 {output}')


def restore(source):
    with tarfile.open(source, 'r:gz') as archive:
        for item in archive.getmembers():
            parts = Path(item.name).parts
            if not item.isfile() or '..' in parts or Path(item.name).is_absolute() or parts[:3] not in (('data', 'processed', 'qdii'), ('raw', 'market', 'qdii')):
                raise ValueError(f'状态包含无效路径: {item.name}')
        archive.extractall(ROOT, filter='data')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['build', 'pack', 'restore'])
    parser.add_argument('path', type=Path)
    args = parser.parse_args()
    {'build': build, 'pack': pack, 'restore': restore}[args.command](args.path.resolve())
