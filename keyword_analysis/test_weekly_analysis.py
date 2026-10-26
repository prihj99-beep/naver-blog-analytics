"""Offline observations, coverage compatibility, and local-only endpoint tests."""
import copy
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from keyword_analysis import test_analysis as transport_tests
from keyword_analysis.model import Conflict
from keyword_analysis.test_weekly import SAMPLE, PLAIN
from keyword_analysis.weekly import WeeklyStore, parse_text, set_coverage
from keyword_analysis.weekly_analysis import analyze, classify
from keyword_analysis.trends import compare, make_request

ROOT = Path(__file__).resolve().parents[1]


def report(text=SAMPLE, coverage='unknown'):
    value = set_coverage(parse_text(text), coverage)
    assert not value['errors'], value['errors']
    value['revision'] = 1
    return value


def next_week(text=PLAIN):
    return text.replace('09. 28.', '10. 05.').replace('10. 04.', '10. 11.')


def trend(end='2026-10-04'):
    topics = [{'id':'test','version':1,'name':'입찰 준비','keywords':['입찰보증금','보증수표 양식']}]
    req = make_request(topics,end)
    value = compare(req,transport_tests.fixture(req),topics)
    value.update(export_id='current',fetched_at='2026-10-19T10:00:00+09:00',demo=True)
    return value


class ObservationTests(unittest.TestCase):
    def test_no_reports_and_first_week_do_not_claim_new_demand(self):
        self.assertFalse(analyze([],None)['available'])
        result = analyze([report()],'2026-09-28')
        self.assertFalse(result['has_previous'])
        self.assertIsNone(result['metrics']['channel_views']['difference'])
        self.assertEqual({r['status'] for r in result['comparisons']},{'이전 주 미등록'})
        self.assertTrue(any('작은 자료' in text for text in result['caveats']))

    def test_weekly_metrics_and_list_change_preserve_absent_values(self):
        a = report(PLAIN,'full')
        b = report(next_week().replace('채널 조회수\n7','채널 조회수\n9').replace('57.14%', '60%').replace('김천 경매 물건','새 표현').replace('보증수표 양식 14.29%','보증수표 양식 10%'),'partial')
        result = analyze([a,b], b['start'])
        self.assertEqual(result['metrics']['channel_views']['difference'],2)
        self.assertEqual(result['metrics']['search_rate']['difference'],2.86)
        lookup = {r['raw_text']:r for r in result['comparisons']}
        self.assertEqual(lookup['김천 경매 물건']['status'],'이번 목록에서 미확인')
        self.assertIsNone(lookup['김천 경매 물건']['ratio'])
        self.assertEqual(lookup['새 표현']['status'],'신규 관측(목록 기준)')
        self.assertIsNone(lookup['새 표현']['difference_pp'])
        self.assertEqual(lookup['보증수표 양식']['difference_pp'],-4.29)

    def test_missing_metrics_and_zero_never_invent_percent_growth(self):
        a = report(PLAIN.replace('채널 조회수\n7','채널 조회수\n0'))
        b = report(next_week().replace('검색 유입률\n57.14%',''))
        result = analyze([a,b],b['start'])
        self.assertEqual(result['metrics']['channel_views']['difference'],7)
        self.assertIsNone(result['metrics']['search_rate']['current'])
        self.assertIsNone(result['metrics']['search_rate']['difference'])
        self.assertNotIn('growth_percent',result['metrics']['channel_views'])

    def test_exact_previous_period_only(self):
        a = report(PLAIN)
        b = report(next_week().replace('10. 05.','10. 12.').replace('10. 11.','10. 18.'))
        self.assertFalse(analyze([a,b],b['start'])['has_previous'])

    def test_sample_grouping_reason_and_unchanged_source(self):
        value = report(); before = copy.deepcopy(value)
        result = analyze([value],value['start'])
        self.assertEqual(value,before)
        groups = {g['name']:g['rows'] for g in result['topic_groups']}
        self.assertEqual(len(groups['입찰 준비 · 보증금/수표']),3)
        self.assertEqual(len(groups['경매 물건 탐색']),1)
        self.assertEqual([r['ratio'] for r in result['topics']],['14.29']*4)
        self.assertTrue(all(r['reasons'] for r in result['topics']))
        self.assertNotIn('ratio_sum',result)
        self.assertTrue(all(c['evidence'] and c['checks'] for c in result['candidates']))

    def test_unclassified_and_multiple_intents_are_explicit(self):
        row = classify({'raw_text':'알 수 없는 표현','ratio':'12.30'},[])
        self.assertEqual(row['labels'],['미분류'])
        value = report(PLAIN.replace('경매 보증금 수표 준비','입찰보증금 경매대출'))
        groups = analyze([value],value['start'])['topic_groups']
        self.assertTrue(any(g['name']=='복수 분류 후보' for g in groups))

    def test_topics_exact_whitespace_match_not_generic_substring(self):
        topic = {'id':'x','name':'주제','version':2,'keywords':['보증 수표 양식']}
        row = classify({'raw_text':'보증수표 양식','ratio':'14.29'},[topic])
        self.assertEqual(row['topic_refs'][0]['version'],2)
        self.assertFalse(classify({'raw_text':'경매 수표','ratio':'14.29'},[dict(topic,keywords=['경매'])])['topic_refs'])

    def test_full_is_user_declared_more_overrides_it(self):
        full = report(PLAIN,'full')
        self.assertEqual(analyze([full],full['start'])['coverage'],'full')
        more = report(PLAIN+'\n더보기','full')
        result = analyze([more],more['start'])
        self.assertEqual((result['declared_coverage'],result['coverage']),('full','partial'))
        self.assertTrue(any('더보기' in text for text in result['caveats']))
        self.assertEqual(len(more['rows']),4)

    def test_markdown_more_and_unknown_previous_scope(self):
        value = report(PLAIN+'\n[더 보기](https://creator-advisor.naver.com/)')
        self.assertTrue(value['has_more'])
        old = report(PLAIN); old.pop('coverage'); old.pop('effective_coverage')
        current = report(next_week(),'full')
        result = analyze([old,current],current['start'])
        self.assertEqual(result['previous_coverage_label'],'목록 범위 미확인')
        self.assertTrue(any('일부 또는' in c for c in result['caveats']))

    def test_trend_alignment_related_keywords_separate_metrics_and_no_mutation(self):
        value, series = report(),trend(); before = copy.deepcopy(series)
        result = analyze([value],value['start'],trend=series)['trend']
        self.assertTrue(result['aligned']); self.assertTrue(result['demo'])
        self.assertEqual(series,before)
        group = result['groups'][0]
        self.assertEqual((group['previous_mean'],group['recent_mean']),(20,40))
        self.assertEqual(len(group['matching']),3)
        self.assertTrue(any('동일 검색어 아님' in r['basis'] for r in group['matching']))

    def test_trend_period_mismatch_missing_and_no_result(self):
        value = report()
        result = analyze([value],value['start'],trend=trend('2026-10-18'))
        self.assertFalse(result['trend']['aligned'])
        self.assertIn('보류',result['trend']['notice'])
        series = trend(); series['groups'][0]['previous_mean']=None
        self.assertIsNone(analyze([value],value['start'],trend=series)['trend']['groups'][0]['previous_mean'])
        self.assertEqual(analyze([value],value['start'])['trend']['state'],'not_queried')


class CoverageStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT/'.setup-tmp')
        self.store = WeeklyStore(Path(self.temp.name)/'weekly.sqlite3')

    def tearDown(self):
        self.temp.cleanup()

    def test_no_schema_migration_scope_correction_archived_and_readonly_analysis(self):
        self.store.save(SAMPLE,None,True)
        with closing(sqlite3.connect(self.store.path)) as db:
            old_tables = db.execute('SELECT sql FROM sqlite_master ORDER BY name').fetchall()
        preview = self.store.preview(SAMPLE,'partial')
        self.assertFalse(preview['identical'])
        self.assertEqual(preview['diff']['metrics'][0]['field'],'coverage')
        self.store.save(SAMPLE,1,True,'partial')
        before = hashlib.sha256(self.store.path.read_bytes()).digest()
        analyze(self.store.list(),'2026-09-28')
        self.assertEqual(before,hashlib.sha256(self.store.path.read_bytes()).digest())
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(old_tables,db.execute('SELECT sql FROM sqlite_master ORDER BY name').fetchall())
            self.assertEqual(db.execute('SELECT COUNT(*) FROM weekly_sources').fetchone()[0],2)
        with self.assertRaises(Conflict): self.store.save(SAMPLE,1,True,'full')

    def test_invalid_coverage_cannot_write_or_create_db(self):
        with self.assertRaises(ValueError): self.store.save(SAMPLE,None,True,'wrong')
        self.assertFalse(self.store.path.exists())

    def test_legacy_json_without_new_keys_reads_as_unknown_without_rewriting(self):
        self.store.save(SAMPLE,None,True)
        with closing(sqlite3.connect(self.store.path)) as db:
            value = self.store.list()[0]
            for key in ('coverage','effective_coverage','has_more'): value.pop(key,None)
            db.execute('UPDATE weekly_reports SET report_json=?',(json.dumps(value),)); db.commit()
        before = self.store.path.read_bytes()
        self.assertEqual(analyze(self.store.list(),'2026-09-28')['coverage'],'unknown')
        self.assertEqual(before,self.store.path.read_bytes())


class AnalysisHTTPTests(unittest.TestCase):
    setUp = transport_tests.HTTPTests.setUp
    tearDown = transport_tests.HTTPTests.tearDown
    call = transport_tests.HTTPTests.call

    def test_saved_analysis_without_keys_or_api_and_bad_csrf(self):
        payload = {'start':'2026-09-28'}
        self.assertFalse(self.call('/api/weekly/analysis',payload)[1]['available'])
        self.assertFalse(self.server.weekly.path.exists())
        self.assertEqual(self.call('/api/weekly/analysis',payload,token=False)[0],403)
        self.assertEqual(self.call('/api/weekly/save',{'text':SAMPLE,'expected_revision':None,'acknowledge':True,'coverage':'partial'})[0],200)
        before = self.server.weekly.path.read_bytes()
        result = self.call('/api/weekly/analysis',payload)
        self.assertEqual(result[0],200); self.assertEqual(result[1]['coverage'],'partial')
        self.assertEqual(self.server.weekly.path.read_bytes(),before)
        self.assertEqual(self.client.calls,[])

    def test_trend_snapshot_only_matching_token_is_reused(self):
        self.server.weekly.save(SAMPLE,None,True)
        self.server.export_result = trend()
        self.assertEqual(self.call('/api/weekly/analysis',{'start':'2026-09-28','export_id':'old'})[1]['trend']['state'],'expired')
        self.assertEqual(self.call('/api/weekly/analysis',{'start':'2026-09-28','export_id':'current'})[1]['trend']['state'],'available')
        self.assertEqual(self.client.calls,[])


if __name__ == '__main__':
    unittest.main()
