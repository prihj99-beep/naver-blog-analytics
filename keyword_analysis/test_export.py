"""Offline export tests. Real DB files are read only; no NAVER transports."""
import hashlib
import io
import json
import threading
from types import SimpleNamespace
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from unittest.mock import patch

from keyword_analysis.export import NS, build_export, workbook
from keyword_analysis.server import Handler
from keyword_analysis.test_analysis import TOPICS, END, fixture
from keyword_analysis.trends import compare, make_request

ROOT = Path(__file__).resolve().parents[1]


def rows(content, number):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        root = ET.fromstring(archive.read(f'xl/worksheets/sheet{number}.xml'))
        return [[(''.join(c.itertext()) or None) for c in r] for r in root.findall(f'{{{NS}}}sheetData/{{{NS}}}row')]


class ExportTests(unittest.TestCase):
    def result(self):
        request = make_request(TOPICS, END)
        result = compare(request, fixture(request), TOPICS)
        result.update(demo=True, fetched_at='2026-10-19T10:00:00+09:00')
        return result

    def test_three_sheets_rows_and_database_unchanged(self):
        paths = list((ROOT/'data').glob('*.sqlite3'))
        before = [hashlib.sha256(p.read_bytes()).digest() for p in paths]
        content, missing = build_export(ROOT/'data/analytics.sqlite3', END, TOPICS, self.result())
        self.assertEqual(missing, 12)
        self.assertEqual(len(rows(content, 1)), 8)
        self.assertEqual(len(rows(content, 2)), 21)
        status = rows(content, 3)[-14:]
        self.assertEqual(len({r[0] for r in status}), 14)
        self.assertEqual(sum(r[1]=='미수집' for r in status), 12)
        self.assertEqual(before, [hashlib.sha256(p.read_bytes()).digest() for p in paths])
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(len(ET.fromstring(archive.read('xl/workbook.xml')).find(f'{{{NS}}}sheets')), 3)

    def test_missing_and_zero_are_distinct_and_precision_kept(self):
        result = self.result()
        result['groups'][0]['points'][0]['ratio'] = None
        result['groups'][0]['points'][1]['ratio'] = 0
        result['groups'][0]['points'][2]['ratio'] = 12.3456789
        content, _ = build_export(ROOT/'data/analytics.sqlite3', END, TOPICS, result)
        data = rows(content, 2)
        self.assertEqual(data[7][4:], [None, '누락'])
        self.assertEqual(data[8][4:], ['0', '조회됨'])
        self.assertEqual(data[9][4], '12.3456789')

    def test_no_result_no_db_no_fabricated_points(self):
        content, count = build_export(ROOT/'does-not-exist.sqlite3', END, TOPICS)
        self.assertEqual(count, 14)
        self.assertEqual(len(rows(content, 2)), 7)
        self.assertEqual(rows(content, 2)[0][1], '미조회')
        self.assertFalse((ROOT/'does-not-exist.sqlite3').exists())

    def test_formula_strings_and_allowlisted_fields(self):
        result = self.result()
        result['secret'] = 'SECRET-SENTINEL'
        result['groups'][0]['name'] = '=1+1'
        content, _ = build_export(ROOT/'data/analytics.sqlite3', END, TOPICS, result)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            all_xml = b''.join(archive.read(n) for n in archive.namelist())
            self.assertNotIn(b'SECRET-SENTINEL', all_xml)
            self.assertNotIn(b'<f>', all_xml)
            self.assertIn(b'=1+1', all_xml)

    def test_stale_date_rejected(self):
        with self.assertRaises(ValueError):
            build_export(ROOT/'data/analytics.sqlite3', '2026-10-17', TOPICS, self.result())

    def test_empty_collected_is_not_uncollected(self):
        with patch('keyword_analysis.export.inbound_keywords', return_value={'rows': [], 'days': [{'date': END, 'status': 'empty'}]}):
            content, count = build_export(ROOT/'unused', END, [])
        self.assertEqual(count, 0)
        self.assertEqual(rows(content, 3)[-1], [END, '수집·검색어 없음'])

    def test_endpoint_reuses_snapshot_rejects_stale_and_never_fetches(self):
        from email.message import Message
        from unittest.mock import Mock
        result = self.result()
        result['export_id'] = 'snapshot-one'
        client = Mock()
        server = SimpleNamespace(origin='http://127.0.0.1:8766', token='test-token',
            root=ROOT, stopping=threading.Event(), export_lock=threading.Lock(),
            export_result=result, topics=SimpleNamespace(list=lambda: TOPICS), client=client)
        def request(snapshot):
            handler = Handler.__new__(Handler)
            handler.server = server
            handler.path = '/api/export'
            body = json.dumps({'end': END, 'export_id': snapshot}).encode()
            handler.headers = Message()
            for key, value in {'Host':'127.0.0.1:8766', 'X-Analysis-Token':'test-token',
                'Content-Type':'application/json', 'Content-Length':str(len(body))}.items():
                handler.headers[key] = value
            handler.rfile = io.BytesIO(body)
            handler.connection = Mock()
            handler.send = Mock()
            handler.do_POST()
            return handler.send.call_args
        success = request('snapshot-one')
        self.assertEqual(len(rows(success.args[0], 2)), 21)
        self.assertEqual(success.kwargs['headers']['X-Uncollected-Days'], '12')
        self.assertEqual(request('old-snapshot').args[1], 409)
        self.assertEqual(len(rows(request(None).args[0], 2)), 7)
        client.fetch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
