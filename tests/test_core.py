import csv
import datetime as dt
import tempfile
import unittest
import zipfile
from pathlib import Path

from analytics.core import Store, collect_dates, date_range, parse_xlsx
from analytics.browser import AccessRestricted

ROOT = Path(__file__).resolve().parents[1]
D12 = dt.date(2026, 10, 12)
D15 = dt.date(2026, 10, 15)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / '.setup-tmp')
        self.path = Path(self.tmp.name)
        self.store = Store(self.path / 'test.sqlite3')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def source(self, date):
        return ROOT / 'verification' / f'search-inflow-{date}.xlsx'

    def test_official_fixtures(self):
        a, b = parse_xlsx(self.source(D12), D12), parse_xlsx(self.source(D15), D15)
        self.assertEqual([i.raw_text for i in a.keywords], ['경매 입찰 수표 만들기', '법원입찰봉투', '인천법원경매 입찰봉투 구매'])
        self.assertEqual([i.ratio for i in a.keywords], ['33.33'] * 3)
        self.assertEqual([i.raw_text for i in b.keywords], ['경매 입찰보증금 준비', '부동산 경매 엑셀 무료', '아파트 경매 후기', '입찰보증금 수표 기간'])
        self.assertEqual([i.ratio for i in b.keywords], ['25'] * 4)
        self.assertEqual(len(a.routes), 3)
        self.assertEqual(len(b.routes), 4)
        self.assertEqual(b.routes[0].raw_text, '네이버 블로그검색_모바일')
        self.assertEqual(a.kind, '유입분석')

    def test_bad_dimension_and_independent_columns(self):
        import re
        target = self.path / 'bad-dimension.xlsx'
        with zipfile.ZipFile(self.source(D15)) as src, zipfile.ZipFile(target, 'w') as dest:
            for name in src.namelist():
                data = src.read(name)
                if name == 'xl/worksheets/sheet1.xml':
                    data = re.sub(rb'<dimension[^>]*/>', b'<dimension ref="A1:B2"/>', data)
                    # Remove one route pair while retaining its independent keyword.
                    data = re.sub(rb'<c r="[CD]12"[^>]*>.*?</c>', b'', data)
                dest.writestr(name, data)
        report = parse_xlsx(target, D15)
        self.assertEqual(len(report.keywords), 4)
        self.assertEqual(len(report.routes), 3)

    def test_duplicate_and_csv(self):
        for _ in range(2):
            for date in [D12, D15]:
                self.store.save(parse_xlsx(self.source(date), date))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM reports').fetchone()[0], 2)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM entries').fetchone()[0], 14)
        self.store.export(self.path / 'exports')
        for name in ['keywords', 'routes']:
            with (self.path / 'exports' / (name + '.csv')).open(encoding='utf-8-sig', newline='') as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 7)
            self.assertEqual({r['date'] for r in rows}, {str(D12), str(D15)})
            self.assertNotIn('count', rows[0])

    def test_failure_preserves_existing(self):
        self.store.save(parse_xlsx(self.source(D12)))
        before = list(self.store.db.execute('SELECT * FROM entries'))
        class Broken:
            def download(_, date, directory):
                return self.source(D15)  # Wrong-date download must not overwrite D12.
        result = collect_dates([D12], Broken(), self.store, self.path)
        self.assertTrue(result[0][1].startswith('failed'))
        self.assertEqual(before, list(self.store.db.execute('SELECT * FROM entries')))
        class NetworkFailure:
            def download(_, date, directory):
                raise RuntimeError('download failed')
        collect_dates([D12], NetworkFailure(), self.store, self.path)
        self.assertEqual(before, list(self.store.db.execute('SELECT * FROM entries')))

    def test_transaction_rollback(self):
        self.store.save(parse_xlsx(self.source(D12)))
        before = list(self.store.db.execute('SELECT * FROM reports'))
        self.store.db.execute("CREATE TRIGGER reject_entry BEFORE INSERT ON entries BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(Exception):
            self.store.save(parse_xlsx(self.source(D12)))
        self.assertEqual(before, list(self.store.db.execute('SELECT * FROM reports')))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM entries').fetchone()[0], 6)

    def test_period_reexecution_offline(self):
        # Consecutive dates use synthetic metadata; this test makes NO Naver requests.
        selected = date_range(D12, D12 + dt.timedelta(days=2))
        fixture = self.source(D12)
        class Fake:
            calls = []
            def download(_, date, directory):
                _.calls.append(date)
                target = self.path / f'{date}.xlsx'
                with zipfile.ZipFile(fixture) as src, zipfile.ZipFile(target, 'w') as dest:
                    for name in src.namelist():
                        dest.writestr(name, src.read(name).replace(b'2026.10.12.', date.strftime('%Y.%m.%d.').encode()))
                return target
        fake, waits = Fake(), []
        for _ in range(2):
            results = collect_dates(selected, fake, self.store, self.path, sleep=waits.append)
            self.assertTrue(all(r[1] == 'success' for r in results))
        self.assertEqual(fake.calls, selected * 2)
        self.assertEqual(waits, [3] * 4)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM reports').fetchone()[0], 3)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM entries').fetchone()[0], 18)
        with self.assertRaises(ValueError):
            date_range(D15, D12)

    def test_restriction_stops(self):
        class Blocked:
            calls = 0
            def download(_, date, directory):
                _.calls += 1
                raise AccessRestricted('blocked')
        fake = Blocked()
        with self.assertRaises(AccessRestricted):
            collect_dates([D12, D15], fake, self.store, self.path)
        self.assertEqual(fake.calls, 1)


if __name__ == '__main__':
    unittest.main()
