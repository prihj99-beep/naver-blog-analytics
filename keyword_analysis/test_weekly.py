"""User sample parsing and temporary-only storage; no external requests."""
import copy
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from keyword_analysis.weekly import WeeklyStore, parse_text, comparisons
from keyword_analysis.model import Conflict
from keyword_analysis.export import build_export
from keyword_analysis.test_export import rows
from keyword_analysis import test_analysis as analysis_tests

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = (Path(__file__).parent/'fixtures/creator_advisor_2026-09-28_2026-10-04.md').read_text(encoding='utf-8')
PLAIN = '''2026. 09. 28. ~ 2026. 10. 04.
유입검색어 분석
채널 조회수
7
검색 유입률
57.14%
경매 보증금 수표 준비 14.29%
김천 경매 물건 14.29%
보증수표 양식 14.29%
은행 입찰보증수표 14.29%
'''


class ParseWeeklyTests(unittest.TestCase):
    def test_real_sample_exact_keywords_metrics_dates_and_urls(self):
        report = parse_text(SAMPLE)
        self.assertEqual(report['errors'], [])
        self.assertEqual((report['start'],report['end'],report['channel_views'],report['search_rate']), ('2026-09-28','2026-10-04',7,'57.14'))
        self.assertEqual([r['raw_text'] for r in report['rows']], ['경매 보증금 수표 준비','김천 경매 물건','보증수표 양식','은행 입찰보증수표'])
        self.assertEqual([r['ratio'] for r in report['rows']], ['14.29']*4)
        for row in report['rows']:
            query = parse_qs(urlsplit(row['detail_url']).query)
            self.assertEqual(query['query'], [row['raw_text']])
            self.assertEqual(query['startDate'], ['2026-09-28'])
        self.assertTrue(any('데이터 없음' in w for w in report['warnings']))

    def test_plain_synthetic_not_claimed_as_real_sample(self):
        report = parse_text(PLAIN)
        self.assertFalse(report['errors'])
        self.assertEqual(len(report['rows']), 4)
        self.assertTrue(all(r['detail_url'] is None for r in report['rows']))

    def test_plain_two_lines_and_zero(self):
        report = parse_text('2026.09.28.~2026.10.04.\n유입검색어 분석\n검색어 원문\n0%')
        self.assertFalse(report['errors'])
        self.assertEqual(report['rows'][0]['ratio'], '0')
        self.assertIsNone(report['channel_views'])
        self.assertIsNone(report['search_rate'])

    def test_url_encoded_keyword_and_plus_spaces(self):
        self.assertFalse(parse_text(SAMPLE.replace('%20', '+'))['errors'])

    def test_url_mismatch_dates_keyword_interval_and_untrusted_host(self):
        for before, after in [('startDate=2026-09-28','startDate=2026-09-27'), ('query=%EA%B2%BD','query=wrong%EA%B2%BD'),
                              ('interval=week','interval=date'), ('creator-advisor.naver.com/new-windows','evil.example/new-windows'),
                              ('metric=cv','metric=cv&token=secret'), ('metric=cv','metric=cv&metric=cv')]:
            with self.subTest(after=after):
                self.assertTrue(parse_text(SAMPLE.replace(before, after, 1))['errors'])

    def test_identical_duplicate_row_deduplicated_conflicting_rejected(self):
        line = next(line for line in SAMPLE.splitlines() if line.startswith('- [경매 보증금'))
        report = parse_text(SAMPLE.replace(line, line+'\n'+line))
        self.assertFalse(report['errors']); self.assertEqual(len(report['rows']), 4)
        self.assertTrue(parse_text(SAMPLE.replace(line, line+'\n'+line.replace('14.29%', '15%')))['errors'])

    def test_missing_metrics_are_null_missing_dates_and_keywords_are_errors(self):
        report = parse_text(SAMPLE.replace('채널 조회수\n7','').replace('검색 유입률\n57.14%',''))
        self.assertFalse(report['errors']); self.assertIsNone(report['channel_views']); self.assertIsNone(report['search_rate'])
        self.assertTrue(parse_text(SAMPLE.replace('2026. 09. 28. ~ 2026. 10. 04.', ''))['errors'])
        self.assertTrue(parse_text('2026.09.28.~2026.10.04.\n유입검색어 분석\n조회한 기간의 데이터가 없습니다.')['errors'])

    def test_unsupported_or_partial_rows_are_not_silently_dropped(self):
        self.assertTrue(parse_text(PLAIN + '잘린 검색어 행')['errors'])
        self.assertTrue(parse_text(PLAIN.replace('14.29%','101%',1))['errors'])
        self.assertTrue(parse_text(SAMPLE.replace('14.29%]', '비율누락]', 1))['errors'])
        self.assertTrue(parse_text(PLAIN.replace('10. 04.','10. 05.'))['errors'])


class WeeklyStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT/'.setup-tmp')
        self.path = Path(self.temp.name)/'weekly.sqlite3'
        self.store = WeeklyStore(self.path)
        self.before = {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'data').glob('*.sqlite3')}

    def tearDown(self):
        self.assertEqual(self.before, {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'data').glob('*.sqlite3')})
        self.temp.cleanup()

    def test_preview_does_not_create_database(self):
        self.assertEqual(self.store.list(), [])
        self.assertIsNone(self.store.preview(SAMPLE)['existing_revision'])
        self.assertFalse(self.path.exists())

    def test_save_reopen_and_exact_original_preserved(self):
        self.store.save(SAMPLE, None, True)
        self.assertEqual(WeeklyStore(self.path).list()[0]['rows'], parse_text(SAMPLE)['rows'])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT raw_text FROM weekly_sources').fetchone()[0], SAMPLE)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM weekly_keywords').fetchone()[0], 4)

    def test_same_paste_no_duplicate_new_revision_only_on_replacement(self):
        self.store.save(SAMPLE, None, True)
        self.assertTrue(self.store.preview(SAMPLE)['identical'])
        self.assertFalse(self.store.save(SAMPLE, 1, True)['saved'])
        changed = SAMPLE.replace('채널 조회수\n7','채널 조회수\n8')
        preview = self.store.preview(changed)
        self.assertEqual(preview['diff']['metrics'][0]['field'], 'channel_views')
        self.assertEqual(self.store.save(changed,1,True)['report']['revision'], 2)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM weekly_sources').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM weekly_keywords').fetchone()[0], 4)

    def test_stale_concurrent_save_and_failure_preserve_old_data(self):
        self.store.save(SAMPLE,None,True)
        before = self.path.read_bytes()
        for revision in (None, 2):
            with self.assertRaises(Conflict): self.store.save(SAMPLE,revision,True)
        with self.assertRaises(ValueError): self.store.save('invalid',1,True)
        with self.assertRaises(ValueError): self.store.save(SAMPLE,1,False)
        with self.assertRaises(ValueError): self.store.save(SAMPLE.replace('channelId=nekkorangkkerong','channelId=other'),1,True)
        self.assertEqual(self.path.read_bytes(), before)

    def test_diff_added_removed_ratio_and_url(self):
        self.store.save(PLAIN,None,True)
        preview = self.store.preview(PLAIN.replace('김천 경매 물건','새 검색어').replace('은행 입찰보증수표 14.29%','은행 입찰보증수표 12%'))
        self.assertEqual(preview['diff']['added'], ['새 검색어'])
        self.assertEqual(preview['diff']['removed'], ['김천 경매 물건'])
        self.assertEqual(preview['diff']['changed'][0]['after']['ratio'], '12')

    def test_previous_week_exact_gap_new_repeat_missing_and_pp(self):
        first = parse_text(PLAIN)
        second = parse_text(PLAIN.replace('09. 28.', '10. 05.').replace('10. 04.', '10. 11.').replace('김천 경매 물건','새 검색어').replace('보증수표 양식 14.29%','보증수표 양식 10%'))
        result = comparisons([first,second])
        current = {r['raw_text']:r for r in result if r['start']=='2026-10-05'}
        self.assertEqual(current['새 검색어']['status'], '신규(목록 기준)')
        self.assertIsNone(current['새 검색어']['difference_pp'])
        self.assertEqual(current['김천 경매 물건']['status'], '사라짐(목록 기준)')
        self.assertEqual(current['보증수표 양식']['difference_pp'], -4.29)
        self.assertTrue(all(r['status']=='이전 주 미등록' for r in comparisons([second])))

    def test_export_accumulated_weekly_separate_from_daily_without_api(self):
        self.store.save(SAMPLE,None,True)
        content, missing = build_export(ROOT/'data/analytics.sqlite3','2026-10-18',[],weekly=self.store.list())
        self.assertEqual(len(rows(content,1)),8)
        self.assertEqual(len(rows(content,4)),5)
        self.assertEqual(rows(content,4)[1][4:8], ['7','57.14','경매 보증금 수표 준비','14.29'])
        self.assertEqual(rows(content,6)[1][5], '이전 주 미등록')
        self.assertEqual(missing,12)


class WeeklyHTTPTests(unittest.TestCase):
    setUp = analysis_tests.HTTPTests.setUp
    tearDown = analysis_tests.HTTPTests.tearDown
    call = analysis_tests.HTTPTests.call

    def test_preview_save_conflict_csrf_and_export_no_naver_requests(self):
        self.assertEqual(self.call('/api/weekly')[1]['reports'], [])
        self.assertFalse(self.server.weekly.path.exists())
        status, preview, _ = self.call('/api/weekly/preview', {'text': SAMPLE})
        self.assertEqual(status, 200)
        self.assertEqual(len(preview['report']['rows']), 4)
        self.assertFalse(self.server.weekly.path.exists())
        payload = {'text':SAMPLE, 'expected_revision':None, 'acknowledge':True}
        self.assertEqual(self.call('/api/weekly/save', payload, token=False)[0], 403)
        self.assertEqual(self.call('/api/weekly/save', payload)[0], 200)
        self.assertEqual(self.call('/api/weekly/save', payload)[0], 409)
        self.assertEqual(len(self.call('/api/weekly')[1]['reports']),1)
        import urllib.request
        request = urllib.request.Request(self.origin+'/api/export',data=json.dumps({'end':'2026-10-18'}).encode(),
            headers={'Content-Type':'application/json','X-Analysis-Token':self.server.token})
        with self.opener.open(request) as response:
            self.assertEqual(len(rows(response.read(),4)),5)
        self.assertEqual(self.client.calls,[])


if __name__ == '__main__':
    unittest.main()
