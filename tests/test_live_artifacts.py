"""Read-only checks of the two explicitly authorized live collection artifacts."""
import csv
import datetime as dt
import sqlite3
import unittest
from pathlib import Path

from analytics.core import parse_xlsx

ROOT = Path(__file__).resolve().parents[1]


class LiveArtifactTests(unittest.TestCase):
    @unittest.skipUnless((ROOT / 'data/analytics.sqlite3').exists(), 'Live artifacts not collected on this machine')
    def test_live_xlsx_db_and_csv_match_official_baseline(self):
        db = sqlite3.connect(f'file:{(ROOT / "data/analytics.sqlite3").as_posix()}?mode=ro', uri=True)
        try:
            self.assertEqual(db.execute('SELECT date FROM reports ORDER BY date').fetchall(), [('2026-10-12',), ('2026-10-15',)])
            for category, filename in [('keyword', 'keywords'), ('route', 'routes')]:
                with (ROOT / 'exports' / f'{filename}.csv').open(encoding='utf-8-sig', newline='') as f:
                    csv_rows = list(csv.DictReader(f))
                self.assertEqual(len(csv_rows), 7)
                for date in [dt.date(2026, 10, 12), dt.date(2026, 10, 15)]:
                    original = parse_xlsx(ROOT / 'verification' / f'search-inflow-{date}.xlsx', date)
                    downloaded = parse_xlsx(ROOT / 'downloads' / f'search-inflow-{date}.xlsx', date)
                    self.assertEqual(original.keywords, downloaded.keywords)
                    self.assertEqual(original.routes, downloaded.routes)
                    items = original.keywords if category == 'keyword' else original.routes
                    expected = [(i.raw_text, i.ratio) for i in items]
                    self.assertEqual(db.execute('SELECT raw_text,ratio FROM entries WHERE date=? AND category=? ORDER BY position', (str(date), category)).fetchall(), expected)
                    self.assertEqual([(r['raw_text'], r['ratio_percent']) for r in csv_rows if r['date'] == str(date)], expected)
        finally:
            db.close()
