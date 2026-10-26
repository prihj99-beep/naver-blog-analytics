import csv
import datetime as dt
import hashlib
import io
import json
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

from analytics.core import CollectionCancelled, Item, Report, Store, collect_dates, parse_xlsx
from analytics.dashboard import APP_ID, DashboardServer, Jobs, launch, prepare_database
from analytics.dashboard_data import csv_bytes, query, xlsx_bytes

ROOT = Path(__file__).resolve().parents[1]
D12 = dt.date(2026, 10, 12)
D15 = dt.date(2026, 10, 15)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / '.setup-tmp')
        self.root = Path(self.tmp.name)
        self.db_path = self.root / 'data/analytics.sqlite3'
        self.store = Store(self.db_path)
        for date in (D12, D15):
            self.store.save(parse_xlsx(ROOT / 'verification' / f'search-inflow-{date}.xlsx'))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_existing_data_gaps_filters_repeated_and_exports(self):
        data = query(self.db_path)
        self.assertEqual([d['status'] for d in data['daily']], ['collected', 'uncollected', 'uncollected', 'collected'])
        self.assertEqual(len(data['keywords']), 7)
        self.assertEqual(len(data['routes']), 7)
        self.assertEqual(data['repeated'], [])
        self.assertEqual(len(query(self.db_path, term='수표')['keywords']), 2)
        with zipfile.ZipFile(io.BytesIO(xlsx_bytes(data))) as z:
            self.assertIn('xl/worksheets/sheet2.xml', z.namelist())
            self.assertNotIn(b'<f>', z.read('xl/worksheets/sheet1.xml'))
            self.assertIn('경매 입찰 수표 만들기'.encode(), z.read('xl/worksheets/sheet1.xml'))
        rows = list(csv.DictReader(io.StringIO(csv_bytes(data, 'keywords').decode('utf-8-sig'))))
        self.assertEqual(len(rows), 7)
        self.assertEqual(rows[0]['ratio_percent'], '33.33')
        self.store.save(Report(dt.date(2026,10,13), '유입분석', '일간', '검색 유입', (Item('법원입찰봉투', '50'),), (), 'test'))
        repeats = query(self.db_path)['repeated']
        self.assertEqual(repeats[0]['days'], 2)
        self.assertEqual(repeats[0]['dates'], ['2026-10-12', '2026-10-13'])

    def test_empty_official_file_is_distinct_from_uncollected(self):
        import xml.etree.ElementTree as ET
        from analytics.core import NS
        target = self.root / 'empty.xlsx'
        with zipfile.ZipFile(ROOT / 'verification/search-inflow-2026-10-12.xlsx') as src, zipfile.ZipFile(target,'w') as dst:
            for name in src.namelist():
                contents = src.read(name)
                if name == 'xl/worksheets/sheet1.xml':
                    root = ET.fromstring(contents)
                    sheet = root.find('m:sheetData', NS)
                    for row in list(sheet):
                        if int(row.get('r')) >= 9:
                            sheet.remove(row)
                    contents = ET.tostring(root)
                dst.writestr(name, contents)
        self.store.save(parse_xlsx(target, D12))
        data = query(self.db_path)
        self.assertEqual(data['daily'][0]['status'], 'empty')
        self.assertEqual(data['daily'][1]['status'], 'uncollected')

    def test_cancel_during_download_preserves_old_rows(self):
        before = list(self.store.db.execute('SELECT * FROM entries'))
        cancel = threading.Event()
        class Download:
            def download(_, date, directory):
                cancel.set()
                return ROOT / 'verification/search-inflow-2026-10-12.xlsx'
        with self.assertRaises(CollectionCancelled):
            collect_dates([D12], Download(), self.store, self.root, cancel_event=cancel)
        self.assertEqual(before, list(self.store.db.execute('SELECT * FROM entries')))

    def test_database_backup_preserves_original(self):
        before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        prepare_database(self.root)
        self.assertEqual(before, hashlib.sha256(self.db_path.read_bytes()).hexdigest())
        backups = list((self.root / 'data/backups').glob('*.sqlite3'))
        self.assertEqual(len(backups), 1)
        db = sqlite3.connect(backups[0])
        self.assertEqual(db.execute('SELECT COUNT(*) FROM entries').fetchone()[0], 14)
        db.close()

    def test_job_login_cancel_and_restart_without_naver(self):
        def fake_run(dates):
            jobs.update(phase='waiting_login')
            jobs.login.wait(2)
            if jobs.cancel.is_set():
                raise CollectionCancelled()
            jobs.progress(dates[0], 'success', 3, 3)
            jobs.update(phase='completed')
        jobs = Jobs(self.root, 'sample', runner=fake_run)
        jobs.start([D12])
        deadline=time.monotonic()+2
        while jobs.snapshot()['phase'] != 'waiting_login' and time.monotonic()<deadline:
            time.sleep(.01)
        with self.assertRaises(ValueError):
            jobs.start([D15])
        jobs.stop(); jobs.join()
        self.assertEqual(jobs.snapshot()['phase'], 'cancelled')
        jobs.start([D12]);
        deadline=time.monotonic()+2
        while jobs.snapshot()['phase'] != 'waiting_login' and time.monotonic()<deadline:
            time.sleep(.01)
        jobs.confirm_login(jobs.snapshot()['job_id'], 'confirm-request-0001'); jobs.join()
        self.assertEqual(jobs.snapshot()['phase'], 'completed')
        self.assertEqual(jobs.snapshot()['completed'], 1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM entries').fetchone()[0], 14)

    def test_http_security_filters_duplicate_launch_and_shutdown(self):
        selected=[]
        jobs=Jobs(self.root,'sample',runner=lambda dates:selected.extend(dates))
        server=DashboardServer(self.root,0,jobs=jobs)
        thread=threading.Thread(target=server.serve_forever)
        thread.start()
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def get(path,headers=None):
            return opener.open(urllib.request.Request(server.origin+path,headers=headers or {}),timeout=3)
        def post(path,payload,valid=True):
            headers={'Content-Type':'application/json','Origin':server.origin}
            if valid: headers['X-Dashboard-Token']=server.token
            return opener.open(urllib.request.Request(server.origin+path,data=json.dumps(payload).encode(),headers=headers),timeout=3)
        try:
            self.assertEqual(server.server_address[0],'127.0.0.1')
            with get('/health') as r:self.assertEqual(json.load(r)['app'],APP_ID)
            with get('/') as r:
                html=r.read().decode()
                self.assertIn('블로그 통계',html)
                self.assertIn(server.token,html)
                self.assertIn("frame-ancestors 'none'",r.headers['Content-Security-Policy'])
            with self.assertRaises(urllib.error.HTTPError) as error:get('/api/data',{'Host':'attacker.example'})
            self.assertEqual(error.exception.code,403)
            with self.assertRaises(urllib.error.HTTPError) as error:post('/api/cancel',{},False)
            self.assertEqual(error.exception.code,403)
            with get('/api/data?start=2026-10-12&end=2026-10-15&q=%EC%88%98%ED%91%9C') as r:self.assertEqual(len(json.load(r)['keywords']),2)
            with patch('analytics.dashboard.webbrowser.open') as opened:
                self.assertEqual(launch(server.server_port,True,self.root),'reused')
                opened.assert_called_once_with(server.origin)
            with post('/api/collect',{'start':'2026-10-12','end':'2026-10-15','existing_only':True,'request_id':'collect-request-0001'}) as r:self.assertEqual(r.status,200)
            jobs.join(); self.assertEqual(selected,[D12,D15])
            with get('/api/export?format=xlsx') as r:self.assertTrue(zipfile.is_zipfile(io.BytesIO(r.read())))
            with get('/api/export?format=csv&category=routes') as r:self.assertEqual(len(list(csv.DictReader(io.StringIO(r.read().decode('utf-8-sig'))))),7)
            with post('/api/shutdown',{}) as r:self.assertEqual(r.status,200)
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        finally:
            if thread.is_alive():server.shutdown();thread.join()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
