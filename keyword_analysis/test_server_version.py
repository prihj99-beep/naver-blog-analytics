"""Regression for an old in-memory server serving updated static UI files."""
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from keyword_analysis import server
from keyword_analysis import test_analysis as transport_tests
from keyword_analysis.test_weekly import SAMPLE


def date_variant(start, end, display_start, display_end):
    """Synthetic date variant of the one real sample; not real later-week metrics."""
    return (SAMPLE.replace('2026. 09. 28.', display_start).replace('2026. 10. 04.', display_end)
            .replace('2026-09-28', start).replace('2026-10-04', end))


VARIANTS = [
    ('2026-09-28', '2026-10-04', SAMPLE),
    ('2026-10-05', '2026-10-11', date_variant('2026-10-05','2026-10-11','2026. 10. 05.','2026. 10. 11.')),
    ('2026-10-12', '2026-10-18', date_variant('2026-10-12','2026-10-18','2026. 10. 12.','2026. 10. 18.')),
]
FIXTURES=Path(__file__).parent/'fixtures'
REAL_SAMPLES=[
    ('2026-09-28','2026-10-04',7,'57.14',4,VARIANTS[0][2]),
    ('2026-10-05','2026-10-11',5,'80',4,(FIXTURES/'creator_advisor_2026-10-05_2026-10-11.md').read_text(encoding='utf-8')),
    ('2026-10-12','2026-10-18',27,'55.56',12,(FIXTURES/'creator_advisor_2026-10-12_2026-10-18.md').read_text(encoding='utf-8')),
]


class VersionTests(unittest.TestCase):
    def info(self):
        return dict(app=server.APP_ID,instance=hashlib.sha256(str(server.ROOT).casefold().encode()).hexdigest(),demo=False)

    def attempt(self,info):
        opener=SimpleNamespace(open=lambda *args,**kwargs:io.BytesIO(json.dumps(info).encode()))
        return patch.object(server.urllib.request,'build_opener',return_value=opener)

    def test_old_or_changed_server_not_reused_or_browser_opened(self):
        for build in (None,'old-build'):
            info=self.info()
            if build is not None: info['build']=build
            with patch.object(server,'AnalysisServer',side_effect=OSError()),self.attempt(info),patch.object(server.webbrowser,'open') as browser:
                with self.assertRaises(server.ServerVersionMismatch): server.launch(open_browser=True)
                browser.assert_not_called()

    def test_same_build_server_is_reused(self):
        info={**self.info(),'build':server.build_signature()}
        with patch.object(server,'AnalysisServer',side_effect=OSError()),self.attempt(info),patch.object(server.webbrowser,'open') as browser:
            self.assertEqual(server.launch(open_browser=True),'reused')
            browser.assert_called_once()

    def test_console_displays_restart_instead_of_generic_port_error(self):
        output=io.StringIO()
        message='이전 코드의 분석 서버가 실행 중입니다. 다시 실행하세요.'
        with patch('sys.argv',['analysis']),patch.object(server,'launch',side_effect=server.ServerVersionMismatch(message)),patch('sys.stdout',output):
            with self.assertRaises(SystemExit) as error: server.main()
        self.assertEqual(error.exception.code,1)
        self.assertIn(message,output.getvalue())


class RealMarkdownTests(unittest.TestCase):
    def test_all_three_real_samples_exact_metrics_keywords_and_decimal_strings(self):
        from keyword_analysis.weekly import parse_text
        from urllib.parse import parse_qs,urlsplit
        for start,end,views,rate,count,text in REAL_SAMPLES:
            report=parse_text(text)
            self.assertEqual(report['errors'],[])
            self.assertEqual((report['start'],report['end'],report['channel_views'],report['search_rate'],len(report['rows'])),(start,end,views,rate,count))
            for row in report['rows']:
                query=parse_qs(urlsplit(row['detail_url']).query)
                self.assertEqual(query['query'],[row['raw_text']])
                self.assertEqual(query['startDate'],[start]);self.assertEqual(query['endDate'],[end])
            if start=='2026-10-05':
                self.assertEqual([r['ratio'] for r in report['rows']],['20.00']*4)
                self.assertEqual(report['rows'][2]['raw_text'],'김천 부곡동 아파트 매매')
            if start=='2026-10-12':
                self.assertEqual([r['ratio'] for r in report['rows']],['7.41']*3+['3.70']*9)
                self.assertEqual(report['rows'][0]['raw_text'],'경매 입찰 수표 쓰는법')
                self.assertEqual(report['rows'][-1]['raw_text'],'입찰보증금 수표 기간')

    def test_split_links_must_match_and_missing_ratio_still_fails(self):
        from keyword_analysis.weekly import parse_text
        text=REAL_SAMPLES[-1][-1]
        # Only the second link on the first keyword row is changed.
        line=next(line for line in text.splitlines() if line.startswith('- [**경매 입찰 수표 쓰는법'))
        cut=line.index(')[***')+1
        changed=line[:cut]+line[cut:].replace('endDate=2026-10-18','endDate=2026-10-17',1)
        self.assertTrue(parse_text(text.replace(line,changed))['errors'])
        self.assertTrue(parse_text(text.replace('[***7.41%***]','[***비율 누락***]',1))['errors'])

    def test_raw_markdown_source_is_archived_unchanged(self):
        import tempfile,sqlite3
        from contextlib import closing
        from keyword_analysis.weekly import WeeklyStore
        with tempfile.TemporaryDirectory(dir=server.ROOT/'.setup-tmp') as temp:
            store=WeeklyStore(Path(temp)/'weekly.sqlite3')
            text=REAL_SAMPLES[-1][-1]
            store.save(text,None,True)
            with closing(sqlite3.connect(store.path)) as db:
                self.assertEqual(db.execute('SELECT raw_text FROM weekly_sources').fetchone()[0],text)


class PreviewHTTPTests(unittest.TestCase):
    setUp=transport_tests.HTTPTests.setUp
    tearDown=transport_tests.HTTPTests.tearDown
    call=transport_tests.HTTPTests.call

    def test_build_is_startup_snapshot_and_mismatched_process_is_not_reused(self):
        self.assertEqual(self.call('/api/health')[1]['build'],self.server.build)
        before=self.server.build
        with patch.object(server,'build_signature',return_value='updated-on-disk'):
            self.assertEqual(self.call('/api/health')[1]['build'],before)
            with patch.object(server.webbrowser,'open') as browser:
                with self.assertRaises(server.ServerVersionMismatch): server.launch(self.root,self.server.server_port,True)
                browser.assert_not_called()
        self.assertEqual(self.call('/api/health')[0],200)

    def test_real_sample_and_two_synthetic_date_variants_return_200_without_saving(self):
        for start,end,text in VARIANTS:
            status,data,_=self.call('/api/weekly/preview',dict(text=text,coverage='unknown'))
            self.assertEqual(status,200)
            report=data['report']
            self.assertEqual((report['start'],report['end'],report['channel_views'],report['search_rate']),(start,end,7,'57.14'))
            self.assertEqual(len(report['rows']),4)
            self.assertEqual(report['errors'],[])
        self.assertFalse(self.server.weekly.path.exists())
        self.assertEqual(self.client.calls,[])

    def test_all_three_actual_copies_return_200_with_expected_fields(self):
        for start,end,views,rate,count,text in REAL_SAMPLES:
            status,data,_=self.call('/api/weekly/preview',dict(text=text))
            self.assertEqual(status,200)
            report=data['report']
            self.assertEqual(report['errors'],[])
            self.assertEqual((report['start'],report['end'],report['channel_views'],report['search_rate'],len(report['rows'])),(start,end,views,rate,count))
        self.assertFalse(self.server.weekly.path.exists())
        self.assertEqual(self.client.calls,[])


if __name__=='__main__': unittest.main()
