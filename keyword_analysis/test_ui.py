"""Real Chromium, local HTTP server, fake API only; all non-loopback page requests blocked."""
import hashlib
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from analytics.browser import chromium_path
from keyword_analysis.server import AnalysisServer
from keyword_analysis.test_analysis import fixture
from keyword_analysis.trends import TrendError

ROOT=Path(__file__).resolve().parents[1]


class MockClient:
    def __init__(self):
        self.calls=[]
        self.mode='normal'

    def configured(self):return self.mode!='missing_key'

    def fetch(self,request):
        self.calls.append(request)
        if self.mode in ('missing_key','authentication','quota'):
            messages={'missing_key':'API 키가 설정되지 않았습니다.', 'authentication':'API 인증 또는 사용 권한을 확인하세요.', 'quota':'호출 한도 또는 요청 속도 제한입니다.'}
            raise TrendError(self.mode,messages[self.mode],429 if self.mode=='quota' else 401)
        result=fixture(request)
        if self.mode=='gap':del result['results'][0]['data'][2]
        if self.mode=='zero':
            for point in result['results'][0]['data'][:7]:point['ratio']=0
        return result


class AnalysisUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw=sync_playwright().start()
        cls.browser=cls.pw.chromium.launch(executable_path=str(chromium_path()),headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'.setup-tmp')
        self.root=Path(self.temp.name);(self.root/'data').mkdir()
        shutil.copy2(ROOT/'data/analytics.sqlite3',self.root/'data/analytics.sqlite3')
        self.before=hashlib.sha256((ROOT/'data/analytics.sqlite3').read_bytes()).hexdigest()
        self.topics_before=hashlib.sha256((ROOT/'data/keyword-analysis.sqlite3').read_bytes()).hexdigest()
        self.client=MockClient();self.server=AnalysisServer(self.root,0,self.client,demo=True)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.context=self.browser.new_context(viewport={'width':1440,'height':1080})
        self.external=[];self.errors=[]
        def route(r):
            if r.request.url.startswith(self.server.origin+'/'):r.continue_()
            else:self.external.append(r.request.url);r.abort()
        self.context.route('**/*',route)
        self.page=self.context.new_page();self.page.on('pageerror',lambda e:self.errors.append(type(e).__name__))
        self.page.goto(self.server.origin)
        from playwright.sync_api import expect
        expect(self.page.locator('#message')).to_contain_text('주제군을 선택해')

    def tearDown(self):
        self.context.close();self.server.shutdown();self.server.server_close();self.thread.join(3)
        self.assertEqual(self.errors,[]);self.assertEqual(self.external,[])
        self.assertEqual(hashlib.sha256((ROOT/'data/analytics.sqlite3').read_bytes()).hexdigest(),self.before)
        self.assertEqual(hashlib.sha256((ROOT/'data/keyword-analysis.sqlite3').read_bytes()).hexdigest(),self.topics_before)
        self.temp.cleanup()

    def add_topic(self,name='경매 입찰 준비'):
        self.page.get_by_role('tab',name='주제군 관리',exact=True).click()
        self.page.get_by_label('주제군 이름',exact=True).fill(name)
        self.page.get_by_label('검색어 · 한 줄에 하나',exact=True).fill('경매 입찰보증금\n경매 입찰 수표\n법원 입찰봉투')
        self.page.get_by_role('button',name='주제군 저장',exact=True).click()
        from playwright.sync_api import expect
        expect(self.page.locator('#message')).to_contain_text('v1 저장 완료')

    def query(self):
        self.page.get_by_role('tab',name='검색 트렌드',exact=True).click()
        self.page.locator('#trend-end').fill('2026-10-18')
        self.server.last_request=0  # Test control only; production always enforces cooldown.
        self.page.get_by_role('button',name='검색 트렌드 조회',exact=True).click()

    def test_saved_three_weeks_plain_briefing_read_only_copy_and_details(self):
        import sqlite3
        from contextlib import closing
        from playwright.sync_api import expect
        path=ROOT/'data/creator-advisor-weekly.sqlite3'
        if not path.is_file(): self.skipTest('User weekly database not available')
        weekly_before=hashlib.sha256(path.read_bytes()).digest()
        for name in ('creator-advisor-weekly.sqlite3','keyword-analysis.sqlite3'):
            with closing(sqlite3.connect((ROOT/'data'/name).as_uri()+'?mode=ro',uri=True)) as source:
                source.execute('PRAGMA query_only=ON')
                with closing(sqlite3.connect(self.root/'data'/name)) as target: source.backup(target)
        saved=self.server.weekly.list()
        required={'2026-09-28','2026-10-05','2026-10-12'}
        self.assertTrue(required <= {r['start'] for r in saved})
        self.client.mode='missing_key';self.server.demo=False
        self.page.reload()
        self.page.get_by_role('tab',name='주간 통계',exact=True).click()
        for report in saved:
            self.page.locator('#weekly-period').select_option(report['start'])
            expect(self.page.locator('#weekly-analysis-status')).to_contain_text(report['start']+' ~ '+report['end']+' 저장 자료 분석 완료')
            expect(self.page.locator('#weekly-brief-value-channel_views')).to_have_text(str(report['channel_views'])+'회')
            expect(self.page.locator('#weekly-brief-value-search_rate')).to_have_text(report['search_rate']+'%')
            expect(self.page.locator('#weekly-analysis-quality')).to_be_hidden()
            for row in report['rows']: expect(self.page.locator('#weekly-brief-themes')).to_contain_text(row['raw_text'])
            self.assertLessEqual(self.page.locator('#weekly-brief-actions li').count(),3)
            for width in (1440,390):
                self.page.set_viewport_size({'width':width,'height':900})
                cards=self.page.locator('.brief-metric')
                expect(cards).to_have_count(2)
                first,second=cards.nth(0).bounding_box(),cards.nth(1).bounding_box()
                if width==1440: self.assertAlmostEqual(first['y'],second['y'],delta=1)
                else: self.assertGreaterEqual(second['y'],first['y']+first['height'])
                self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),width)
                self.assertNotIn('그 밖의 검색어',self.page.locator('#weekly-brief-themes').inner_text())
            expect(self.page.locator('#weekly-brief-themes')).not_to_contain_text('대표 검색어')
            for disclosure in self.page.locator('#weekly-brief-themes details').all():
                expect(disclosure).not_to_have_attribute('open','')
                disclosure.locator('summary').click()
            for row in report['rows']:
                table_row=self.page.locator('#weekly-brief-themes tr').filter(has=self.page.get_by_role('cell',name=row['raw_text'],exact=True))
                expect(table_row).to_have_count(1)
                expect(table_row).to_be_visible()
                expect(table_row).to_contain_text(row['ratio']+'%')
            for disclosure in self.page.locator('#weekly-brief-themes details').all(): disclosure.locator('summary').click()
            for text in ('관측 표현','규칙 기반 분류','목록 범위 미확인'):
                self.assertNotIn(text,self.page.locator('#weekly-analysis-content').inner_text())
        self.page.locator('#weekly-period').select_option('2026-10-12')
        expect(self.page.locator('#weekly-brief-delta-channel_views')).to_have_text('+22회')
        expect(self.page.locator('#weekly-brief-delta-search_rate')).to_have_text('-24.44%p')
        expect(self.page.locator('#weekly-brief-changes')).to_contain_text('지난주 5회에서 이번 주 27회로 22회 늘었어요.')
        expect(self.page.locator('#weekly-brief-changes')).to_contain_text('경매 수표 준비')
        expect(self.page.locator('#weekly-brief-changes')).to_contain_text('경매 입찰 수표 쓰는법')
        expect(self.page.locator('#weekly-brief-themes tbody tr')).to_have_count(12)
        expect(self.page.locator('#weekly-brief-changes article')).to_have_count(3)
        expect(self.page.locator('#weekly-brief-changes h5')).to_have_text(['조회수 변화','이어지는 관심사','새롭게 눈에 띄는 관심사'])
        for text in ('새로 보인 검색어 12개','원문이 같은 검색어는','검색 유입률은'):
            expect(self.page.locator('#weekly-brief-changes')).not_to_contain_text(text)
        expect(self.page.get_by_role('heading',name='다음 블로그 글 추천',exact=True)).to_be_visible()
        expect(self.page.locator('#weekly-brief-actions li')).to_have_count(3)
        for item in self.page.locator('#weekly-brief-actions li').all():
            expect(item.locator('.brief-label')).to_have_text(['추천 글 제목','추천 이유','확인할 점'])
            self.assertNotIn('질문 정리',item.inner_text())

        expect(self.page.locator('#weekly-brief-notices')).to_contain_text('모두 복사했는지는')
        expect(self.page.locator('#weekly-analysis-details')).not_to_have_attribute('open','')
        for disclosure in self.page.locator('#weekly-brief-changes details, #weekly-brief-actions details').all():
            expect(disclosure).not_to_have_attribute('open','')
            disclosure.locator('summary').click()
            expect(disclosure).to_have_attribute('open','')
            disclosure.locator('summary').click()
        self.page.set_viewport_size({'width':1440,'height':1000})
        self.page.locator('#weekly-history-area').screenshot(path=str(ROOT/'verification/weekly-history-desktop.png'))
        self.page.locator('#weekly-analysis').screenshot(path=str(ROOT/'verification/weekly-briefing-desktop.png'))
        self.page.set_viewport_size({'width':390,'height':844})
        self.page.locator('#weekly-history-area').screenshot(path=str(ROOT/'verification/weekly-history-mobile.png'))
        self.page.locator('#weekly-analysis').screenshot(path=str(ROOT/'verification/weekly-briefing-mobile.png'))
        self.page.locator('#weekly-analysis-details > summary').click()
        expect(self.page.locator('#weekly-analysis-quality')).to_be_visible()
        expect(self.page.locator('#weekly-analysis-changes')).to_contain_text('-24.44%p')
        expect(self.page.locator('#weekly-analysis-changes')).to_contain_text('새로 보인 검색어 12개')
        expect(self.page.locator('#weekly-analysis-changes')).to_contain_text('원문이 같은 검색어는 두 주 사이에 없어요.')
        expect(self.page.locator('#weekly-analysis-topics')).to_contain_text('7.41%')
        expect(self.page.locator('#weekly-analysis-trend')).to_contain_text('자동 API 호출 없이')
        self.page.locator('#weekly-analysis-details > summary').click()
        self.page.set_viewport_size({'width':390,'height':844})
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),390)
        self.assertEqual(self.client.calls,[])
        self.assertEqual(weekly_before,hashlib.sha256(path.read_bytes()).digest())

    def test_dashboard_empty_partial_and_long_content_without_api(self):
        import copy
        from keyword_analysis.test_briefing import reports
        from keyword_analysis.weekly_analysis import analyze
        from playwright.sync_api import expect
        self.page.get_by_role('tab',name='주간 통계',exact=True).click()
        def render(data):
            self.page.evaluate("data => { renderWeeklyBriefing(data.briefing,data); document.querySelector('#weekly-analysis-content').hidden=false; }",data)
        source=reports()
        render(analyze(source[:1],source[0]['start']))
        expect(self.page.locator('#weekly-brief-delta-channel_views')).to_be_hidden()
        expect(self.page.locator('#weekly-brief-changes article')).to_have_count(0)
        empty=copy.deepcopy(source);empty[-1]['rows']=[];empty[-1]['channel_views']=None;empty[-1]['search_rate']=None
        render(analyze(empty,empty[-1]['start']))
        expect(self.page.locator('#weekly-brief-value-channel_views')).to_have_text('자료 없음')
        expect(self.page.locator('#weekly-brief-themes article')).to_have_count(0)
        expect(self.page.locator('#weekly-brief-actions')).to_contain_text('추천할 글 주제가 충분하지')
        partial=copy.deepcopy(source);partial[-1]['coverage']='partial';partial[-1]['has_more']=True
        long_word='경매 수표 준비 '+('긴 검색어 원문 확인 ' * 18)
        partial[-1]['rows'][0]['raw_text']=long_word
        data=analyze(partial,partial[-1]['start'])
        long_title='경매 입찰 수표를 준비하기 전에 '+('실제로 확인할 내용 ' * 15)
        data['briefing']['actions'][0]['title']=long_title
        render(data)
        expect(self.page.locator('#weekly-brief-notices')).to_contain_text('더보기')
        expect(self.page.locator('#weekly-brief-themes')).to_contain_text(long_word)
        expect(self.page.locator('#weekly-brief-actions h5').first).to_have_text(long_title)
        for width in (1440,390,320):
            self.page.set_viewport_size({'width':width,'height':900})
            self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),width)
            heading=self.page.locator('#weekly-brief-actions h5').first
            self.assertTrue(heading.evaluate('(e)=>e.scrollWidth <= e.clientWidth'))
            for disclosure in self.page.locator('#weekly-brief-themes details').all():
                disclosure.locator('summary').click()
                self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),width)
                disclosure.locator('summary').click()
        self.assertEqual(self.client.calls,[])

    def test_weekly_history_paging_year_month_selection_and_latest_save(self):
        import datetime as dt
        from keyword_analysis.test_weekly import PLAIN
        from playwright.sync_api import expect
        anchor=dt.date(2025,12,22)
        starts=[]
        analysis_requests=[]
        self.page.on('request',lambda r:analysis_requests.append(r.url) if r.url.endswith('/api/weekly/analysis') else None)
        def sample(start,views=7):
            end=start+dt.timedelta(days=6)
            return PLAIN.replace('2026. 09. 28. ~ 2026. 10. 04.',f'{start:%Y. %m. %d.} ~ {end:%Y. %m. %d.}').replace('채널 조회수\n7',f'채널 조회수\n{views}')
        self.page.get_by_role('tab',name='주간 통계',exact=True).click()
        expect(self.page.locator('#weekly-history-more')).to_be_hidden()
        expect(self.page.locator('#weekly-period')).to_have_value('')
        for count in (1,5,42):
            for index in range(len(starts),count):
                start=anchor+dt.timedelta(days=index*7)
                self.server.weekly.save(sample(start),None,True)
                starts.append(str(start))
            before=hashlib.sha256(self.server.weekly.path.read_bytes()).digest()
            self.page.reload()
            self.page.get_by_role('tab',name='주간 통계',exact=True).click()
            expect(self.page.locator('#weekly-period')).to_have_value(starts[-1])
            expect(self.page.locator('#weekly-analysis-status')).to_contain_text(starts[-1]+' ~ ')
            expect(self.page.locator('#weekly-analysis-status')).to_contain_text('저장 자료 분석 완료')
            expect(self.page.locator('#weekly-saved-body tr')).to_have_count(min(5,count))
            expect(self.page.locator('#weekly-saved-body tr').first).to_contain_text(starts[-1])
            calls=len(analysis_requests)
            for visible in range(10,count+5,5):
                self.page.locator('#weekly-history-more').click()
                expect(self.page.locator('#weekly-saved-body tr')).to_have_count(min(visible,count))
            expect(self.page.locator('#weekly-history-more')).to_be_hidden()
            self.assertEqual(len(analysis_requests),calls)
            self.assertEqual(len(self.server.weekly.list()),count)
            self.assertEqual(hashlib.sha256(self.server.weekly.path.read_bytes()).digest(),before)
        self.page.locator('#weekly-year').select_option('2025')
        self.page.locator('#weekly-month').select_option('12')
        expect(self.page.locator('#weekly-period option')).to_have_count(2)
        self.page.locator('#weekly-period').select_option('2025-12-29')
        expect(self.page.locator('#weekly-analysis-status')).to_contain_text('2025-12-29 ~ 2026-01-04 저장 자료 분석 완료')
        self.page.locator('#weekly-year').select_option('2026')
        self.page.locator('#weekly-month').select_option('02')
        options=self.page.locator('#weekly-period option').evaluate_all('(nodes)=>nodes.map(n=>n.value)')
        self.assertTrue(options and all(v.startswith('2026-02-') for v in options))
        self.page.locator('#weekly-period').select_option(options[-1])
        expect(self.page.locator('#weekly-analysis-status')).to_contain_text(options[-1])
        self.assertEqual(hashlib.sha256(self.server.weekly.path.read_bytes()).digest(),before)
        for width in (1440,390,320):
            self.page.set_viewport_size({'width':width,'height':900})
            self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),width)
            boxes=[self.page.locator('#'+id).bounding_box() for id in ('weekly-year','weekly-month','weekly-period')]
            if width==1440: self.assertAlmostEqual(boxes[0]['y'],boxes[2]['y'],delta=1)
            else: self.assertGreater(boxes[2]['y'],boxes[0]['y'])
        # Saving an older week last selects that saved report on the next fresh load.
        self.server.weekly.save(sample(anchor,8),1,True)
        self.page.reload()
        self.page.get_by_role('tab',name='주간 통계',exact=True).click()
        expect(self.page.locator('#weekly-period')).to_have_value(str(anchor))
        expect(self.page.locator('#weekly-brief-value-channel_views')).to_have_text('8회')
        expect(self.page.locator('#weekly-saved-body tr')).to_have_count(5)
        self.assertEqual(len(self.server.weekly.list()),42)
        self.assertEqual(self.client.calls,[])

    def test_weekly_paste_preview_save_duplicate_replace_and_export_offline(self):
        from keyword_analysis.test_weekly import SAMPLE
        from keyword_analysis.test_export import rows
        from playwright.sync_api import expect
        self.page.get_by_role('tab',name='주간 통계',exact=True).click()
        self.page.locator('#weekly-text').fill(SAMPLE)
        self.page.locator('#weekly-preview').click()
        expect(self.page.locator('#weekly-preview-body tr')).to_have_count(4)
        expect(self.page.locator('#weekly-preview-meta')).to_contain_text('57.14')
        expect(self.page.locator('#weekly-save')).to_be_enabled()
        self.page.locator('#weekly-save').click()
        expect(self.page.locator('#weekly-status')).to_contain_text('v1 저장 완료')
        expect(self.page.locator('#weekly-saved-body tr')).to_have_count(1)
        self.page.locator('#weekly-preview').click()
        expect(self.page.locator('#weekly-diff')).to_contain_text('같은 원문')
        self.page.locator('#weekly-save').click()
        expect(self.page.locator('#weekly-status')).to_contain_text('중복 저장하지 않았습니다')
        self.page.locator('#weekly-text').fill(SAMPLE.replace('채널 조회수\n7','채널 조회수\n8'))
        self.page.locator('#weekly-preview').click()
        expect(self.page.locator('#weekly-diff')).to_contain_text('7 → 8')
        self.page.on('dialog',lambda dialog:dialog.accept())
        self.page.locator('#weekly-save').click()
        expect(self.page.locator('#weekly-status')).to_contain_text('v2 저장 완료')
        expect(self.page.locator('#weekly-compare-body')).to_contain_text('이전 주 미등록')
        self.assertEqual(self.client.calls,[])
        self.add_topic(); self.query()
        expect(self.page.locator('#series-body tr')).to_have_count(14)
        self.page.get_by_role('tab',name='주간 통계',exact=True).click()
        with self.page.expect_download() as event:
            self.page.locator('#weekly-export').click()
        content = Path(event.value.path()).read_bytes()
        self.assertEqual(len(rows(content,4)),5)
        self.assertEqual(rows(content,4)[1][4],'8')
        self.assertEqual(len(self.client.calls),1)
        self.assertEqual(len(rows(content,2)),21)

    def test_weekly_auto_preview_save_analysis_without_keys_or_extra_button(self):
        from keyword_analysis.test_weekly import SAMPLE
        from playwright.sync_api import expect
        self.client.mode = 'missing_key'; self.server.demo = False
        self.page.reload()
        expect(self.page.locator('#query-trends')).to_be_disabled()
        self.page.get_by_role('tab',name='주간 통계',exact=True).click()
        self.page.locator('#weekly-text').fill(SAMPLE+'\n더보기')
        self.page.locator('#weekly-coverage').select_option('full')
        expect(self.page.locator('#weekly-preview-body tr')).to_have_count(4)
        expect(self.page.locator('#weekly-warnings')).to_contain_text('더보기')
        self.page.locator('#weekly-save').click()
        expect(self.page.locator('#weekly-analysis-status')).to_contain_text('분석 완료')
        expect(self.page.locator('#weekly-analysis-quality')).to_contain_text('일부 자료')
        expect(self.page.locator('#weekly-analysis-quality')).to_contain_text('더보기')
        expect(self.page.locator('#weekly-analysis-changes')).to_contain_text('이전 주 미등록')
        expect(self.page.locator('#weekly-analysis-topics')).to_contain_text('검색어 3개')
        expect(self.page.locator('#weekly-analysis-topics')).to_contain_text('14.29%')
        expect(self.page.locator('#weekly-analysis-candidates')).to_contain_text('실제 검색어')
        expect(self.page.locator('#weekly-analysis-candidates')).to_contain_text('직접 확인')
        expect(self.page.locator('#weekly-analysis-trend')).to_contain_text('자동 API 호출 없이')
        self.assertEqual(self.client.calls,[])
        self.page.screenshot(path=str(ROOT/'verification/keyword-analysis-weekly-analysis.png'),full_page=True)
        self.page.reload()
        self.page.get_by_role('tab',name='주간 통계',exact=True).click()
        expect(self.page.locator('#weekly-analysis-status')).to_contain_text('분석 완료')
        expect(self.page.locator('#weekly-analysis-quality')).to_contain_text('일부 자료')
        self.page.set_viewport_size({'width':390,'height':844})
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),390)

    def test_weekly_404_displays_restart_guidance_and_no_extracted_fields(self):
        from keyword_analysis.test_weekly import SAMPLE
        from playwright.sync_api import expect
        self.page.route('**/api/weekly/preview',lambda route:route.fulfill(status=404,content_type='application/json',body='{"error":"경로를 찾을 수 없습니다."}'))
        self.page.get_by_role('tab',name='주간 통계',exact=True).click()
        self.page.locator('#weekly-text').fill(SAMPLE)
        expect(self.page.locator('#weekly-status')).to_contain_text('404')
        expect(self.page.locator('#weekly-status')).to_contain_text('start_keyword_analysis.bat')
        expect(self.page.locator('#weekly-status')).to_contain_text('새로고침만으로는')
        expect(self.page.locator('#weekly-preview-body tr')).to_have_count(0)
        expect(self.page.locator('#weekly-preview-meta tr')).to_have_count(0)
        expect(self.page.locator('#weekly-save')).to_be_disabled()
        self.assertFalse(self.server.weekly.path.exists())
        self.assertEqual(self.client.calls,[])

    def test_three_real_week_previews_save_and_automatic_analysis(self):
        from keyword_analysis.test_server_version import REAL_SAMPLES
        from playwright.sync_api import expect
        self.page.get_by_role('tab',name='주간 통계',exact=True).click()
        for start,end,views,rate,count,text in REAL_SAMPLES:
            self.page.locator('#weekly-text').fill(text)
            expect(self.page.locator('#weekly-preview-meta')).to_contain_text(start)
            expect(self.page.locator('#weekly-preview-meta')).to_contain_text(end)
            expect(self.page.locator('#weekly-preview-meta')).to_contain_text(rate)
            expect(self.page.locator('#weekly-preview-meta td').nth(2)).to_have_text(str(views))
            expect(self.page.locator('#weekly-preview-body tr')).to_have_count(count)
            self.page.locator('#weekly-save').click()
            expect(self.page.locator('#weekly-analysis-status')).to_contain_text(start+' ~ '+end+' 저장 자료 분석 완료')
        self.assertEqual(len(self.server.weekly.list()),3)
        self.assertEqual(self.client.calls,[])

    def test_weekly_previous_metrics_list_changes_and_period_selection(self):
        from keyword_analysis.test_weekly import PLAIN
        from playwright.sync_api import expect
        self.page.get_by_role('tab',name='주간 통계',exact=True).click()
        self.page.locator('#weekly-text').fill(PLAIN)
        self.page.locator('#weekly-coverage').select_option('full')
        expect(self.page.locator('#weekly-preview-body tr')).to_have_count(4)
        self.page.locator('#weekly-save').click()
        expect(self.page.locator('#weekly-analysis-status')).to_contain_text('2026-09-28')
        second = PLAIN.replace('09. 28.','10. 05.').replace('10. 04.','10. 11.').replace('김천 경매 물건','새 검색 표현').replace('채널 조회수\n7','채널 조회수\n9')
        self.page.locator('#weekly-text').fill(second)
        expect(self.page.locator('#weekly-coverage')).to_have_value('unknown')
        expect(self.page.locator('#weekly-preview-body tr')).to_have_count(4)
        self.page.locator('#weekly-save').click()
        expect(self.page.locator('#weekly-analysis-status')).to_contain_text('2026-10-05')
        expect(self.page.locator('#weekly-analysis-changes')).to_contain_text('+2회')
        expect(self.page.locator('#weekly-analysis-changes')).to_contain_text('이번 목록에서 미확인')
        expect(self.page.locator('#weekly-analysis-changes')).to_contain_text('신규 관측')
        self.page.locator('#weekly-period').select_option('2026-09-28')
        expect(self.page.locator('#weekly-analysis-status')).to_contain_text('2026-09-28')
        expect(self.page.locator('#weekly-analysis-changes')).to_contain_text('이전 주 미등록')
        self.assertEqual(self.client.calls,[])

    def test_weekly_trend_alignment_and_invalidation_without_automatic_fetch(self):
        from keyword_analysis.test_weekly import SAMPLE
        from playwright.sync_api import expect
        self.page.get_by_role('tab',name='주간 통계',exact=True).click()
        self.page.locator('#weekly-text').fill(SAMPLE)
        expect(self.page.locator('#weekly-preview-body tr')).to_have_count(4)
        self.page.locator('#weekly-save').click()
        expect(self.page.locator('#weekly-analysis-status')).to_contain_text('분석 완료')
        self.add_topic()
        self.page.get_by_role('tab',name='검색 트렌드',exact=True).click()
        self.page.locator('#trend-end').fill('2026-10-04')
        self.page.locator('#query-trends').click()
        expect(self.page.locator('#series-body tr')).to_have_count(14)
        self.page.get_by_role('tab',name='주간 통계',exact=True).click()
        expect(self.page.locator('#weekly-analysis-trend')).to_contain_text('데이터랩 상대지수')
        expect(self.page.locator('#weekly-analysis-trend')).to_contain_text('실제 유입 비율')
        expect(self.page.locator('#weekly-analysis-trend')).not_to_contain_text('보류')
        self.assertEqual(len(self.client.calls),1)
        self.page.get_by_role('tab',name='검색 트렌드',exact=True).click()
        self.page.locator('#trend-end').fill('2026-10-18')
        self.page.get_by_role('tab',name='주간 통계',exact=True).click()
        expect(self.page.locator('#weekly-analysis-trend')).to_contain_text('자동 API 호출 없이')
        self.assertEqual(len(self.client.calls),1)

    def test_weekly_analysis_failure_preserves_saved_data_and_text_is_not_html(self):
        from keyword_analysis.test_weekly import PLAIN
        from playwright.sync_api import expect
        self.page.get_by_role('tab',name='주간 통계',exact=True).click()
        self.page.route('**/api/weekly/analysis',lambda route:route.fulfill(status=500,content_type='application/json',body='{"error":"mock analysis unavailable"}'))
        self.page.locator('#weekly-text').fill(PLAIN.replace('김천 경매 물건','<img src=x onerror=alert(1)>'))
        expect(self.page.locator('#weekly-preview-body tr')).to_have_count(4)
        self.page.locator('#weekly-save').click()
        expect(self.page.locator('#weekly-status')).to_contain_text('저장 완료')
        expect(self.page.locator('#weekly-analysis-status')).to_contain_text('저장 자료는 유지')
        self.assertEqual(len(self.server.weekly.list()),1)
        self.page.unroute('**/api/weekly/analysis'); self.page.reload()
        self.page.get_by_role('tab',name='주간 통계',exact=True).click()
        expect(self.page.locator('#weekly-analysis-topics')).to_contain_text('<img src=x onerror=alert(1)>')
        expect(self.page.locator('#weekly-analysis img')).to_have_count(0)
        self.assertEqual(self.client.calls,[])

    def test_weekly_period_race_ignores_old_analysis_response(self):
        from keyword_analysis.test_weekly import PLAIN
        from keyword_analysis import server as server_module
        from unittest.mock import patch
        from playwright.sync_api import expect
        self.server.weekly.save(PLAIN,None,True)
        self.server.weekly.save(PLAIN.replace('09. 28.','10. 05.').replace('10. 04.','10. 11.'),None,True)
        self.page.reload(); self.page.get_by_role('tab',name='주간 통계',exact=True).click()
        expect(self.page.locator('#weekly-analysis-status')).to_contain_text('2026-10-05')
        entered, release = threading.Event(), threading.Event()
        original = server_module.analyze_weekly
        def delayed(reports,start,*args):
            if start=='2026-09-28': entered.set(); release.wait(5)
            return original(reports,start,*args)
        try:
            with patch.object(server_module,'analyze_weekly',side_effect=delayed):
                self.page.locator('#weekly-period').select_option('2026-09-28')
                # Pump Playwright's route callbacks while the local server waits.
                for _ in range(40):
                    if entered.is_set(): break
                    self.page.wait_for_timeout(50)
                self.assertTrue(entered.wait(2))
                self.page.locator('#weekly-period').select_option('2026-10-05')
                expect(self.page.locator('#weekly-analysis-status')).to_contain_text('2026-10-05')
                with self.page.expect_response(lambda response:'/api/weekly/analysis' in response.url and response.request.post_data_json.get('start')=='2026-09-28'):
                    release.set()
                expect(self.page.locator('#weekly-analysis-status')).to_contain_text('2026-10-05')
                expect(self.page.locator('#weekly-analysis-quality')).to_contain_text('2026-10-05 ~ 2026-10-11')
        finally:
            release.set()
        self.assertEqual(self.client.calls,[])

    def test_export_current_result_without_another_api_call(self):
        import zipfile
        from keyword_analysis.test_export import rows
        from playwright.sync_api import expect
        self.add_topic(); self.query()
        expect(self.page.locator('#series-body tr')).to_have_count(14)
        self.assertEqual(len(self.client.calls), 1)
        dialogs = []
        def accept(dialog):
            dialogs.append(dialog.message); dialog.accept()
        self.page.on('dialog', accept)
        with self.page.expect_download() as event:
            self.page.locator('#export-xlsx').click()
        download = event.value
        content = Path(download.path()).read_bytes()
        self.assertEqual(len(rows(content, 1)), 8)
        self.assertEqual(len(rows(content, 2)), 21)
        self.assertIn('12일', dialogs[0])
        self.assertEqual(len(self.client.calls), 1)
        expect(self.page.locator('#series-body tr')).to_have_count(14)

    def test_full_user_flow_inbound_edit_version_chart_mobile_and_delete(self):
        from playwright.sync_api import expect
        self.assertEqual(self.client.calls,[])
        self.page.get_by_role('tab',name='내 블로그 유입어',exact=True).click()
        self.page.locator('#inbound-start').fill('2026-10-12')
        self.page.locator('#inbound-end').fill('2026-10-15')
        self.page.get_by_role('button',name='저장된 유입어 조회').click()
        expect(self.page.locator('#inbound-body tr')).to_have_count(7)
        expect(self.page.locator('#inbound-status')).to_contain_text('미수집 2일')
        self.page.get_by_role('button',name='경매 입찰 수표 만들기 추가',exact=True).click()
        expect(self.page.locator('#topic-words')).to_have_value('경매 입찰 수표 만들기')
        self.add_topic()
        self.assertEqual(self.client.calls,[])
        self.page.get_by_role('button',name='경매 입찰 준비 수정',exact=True).click()
        self.page.locator('#topic-words').fill('경매 입찰보증금\n경매 입찰 수표')
        self.page.get_by_role('button',name='주제군 저장',exact=True).click()
        expect(self.page.locator('#message')).to_contain_text('v2 저장 완료')
        self.query()
        expect(self.page.locator('#summaries')).to_contain_text('+100%')
        expect(self.page.locator('#summaries')).to_contain_text('이전 평균 20 → 최근 평균 40')
        expect(self.page.locator('#chart path')).to_have_count(1)
        expect(self.page.locator('#chart')).to_be_visible()
        self.assertGreater(self.page.locator('#chart').bounding_box()['height'],100)
        expect(self.page.locator('#chart circle')).to_have_count(14)
        expect(self.page.locator('#series-body tr')).to_have_count(14)
        self.assertEqual(len(self.client.calls),1)
        self.assertEqual(self.client.calls[0]['startDate'],'2026-10-05')
        self.assertEqual(self.page.evaluate('Object.keys(localStorage).length'),0)
        self.page.screenshot(path=str(ROOT/'verification/keyword-analysis-tabs-trends.png'),full_page=True)
        self.page.get_by_role('tab',name='내 블로그 유입어',exact=True).click()
        self.page.screenshot(path=str(ROOT/'verification/keyword-analysis-tabs-inbound.png'),full_page=True)
        self.page.get_by_role('tab',name='주제군 관리',exact=True).click()
        self.page.screenshot(path=str(ROOT/'verification/keyword-analysis-tabs-manage.png'),full_page=True)
        self.page.get_by_role('tab',name='검색 트렌드',exact=True).click()
        self.page.set_viewport_size({'width':390,'height':844})
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),390)
        self.page.reload()
        expect(self.page.locator('#chart')).to_be_hidden()
        expect(self.page.locator('#topics')).to_contain_text('v2')
        self.page.get_by_role('tab',name='주제군 관리',exact=True).click()
        self.page.once('dialog',lambda d:d.accept())
        self.page.get_by_role('button',name='경매 입찰 준비 삭제',exact=True).click()
        expect(self.page.locator('#managed-topics')).to_contain_text('아직 주제군이 없습니다')

    def test_gap_zero_and_error_states_no_stale_chart(self):
        from playwright.sync_api import expect
        self.add_topic()
        self.client.mode='gap';self.query()
        expect(self.page.locator('#summaries')).to_contain_text('변화율 표시 안 함')
        expect(self.page.locator('#summaries')).to_contain_text('6/7')
        expect(self.page.locator('#chart circle')).to_have_count(13)
        path=self.page.locator('#chart path').get_attribute('d')
        self.assertEqual(path.count('M'),2)
        self.client.mode='zero';self.query()
        expect(self.page.locator('#summaries')).to_contain_text('이전 평균 0')
        expect(self.page.locator('#summaries')).to_contain_text('변화율 표시 안 함')
        for mode,text in [('authentication','인증 또는'),('quota','호출 한도'),('missing_key','키가 설정되지')]:
            self.client.mode=mode;self.query()
            expect(self.page.locator('#trend-status')).to_contain_text(text)
            expect(self.page.locator('#chart')).to_be_hidden()
            expect(self.page.locator('#summaries')).to_be_empty()
        self.assertEqual(len(self.client.calls),5)

    def test_text_is_not_html_and_shutdown(self):
        from playwright.sync_api import expect
        self.add_topic('<img src=x onerror=alert(1)>')
        expect(self.page.locator('#managed-topics img')).to_have_count(0)
        self.query()
        expect(self.page.locator('#summaries')).to_contain_text('<img src=x onerror=alert(1)>')
        expect(self.page.locator('#summaries img')).to_have_count(0)
        self.page.once('dialog',lambda d:d.accept())
        self.page.get_by_role('button',name='분석 화면 종료').click()
        expect(self.page.locator('#message')).to_contain_text('분석 서버가 종료되었습니다')
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())

    def test_shutdown_during_request_ignores_late_error_and_keeps_controls_disabled(self):
        from playwright.sync_api import expect
        self.add_topic()
        entered,release=threading.Event(),threading.Event()
        original=self.client.fetch
        def delayed(request):
            entered.set();release.wait(4);return original(request)
        self.client.fetch=delayed
        try:
            self.query();self.assertTrue(entered.wait(2))
            expect(self.page.locator('#query-trends')).to_be_disabled()
            self.page.once('dialog',lambda d:d.accept())
            with self.page.expect_response('**/api/trends') as pending:
                self.page.get_by_role('button',name='분석 화면 종료').click()
                expect(self.page.locator('#message')).to_contain_text('분석 서버가 종료되었습니다')
                release.set()
            self.assertEqual(pending.value.status,409)
            expect(self.page.locator('#message')).to_contain_text('분석 서버가 종료되었습니다')
            expect(self.page.locator('#query-trends')).to_be_disabled()
            expect(self.page.locator('#chart')).to_be_hidden()
        finally:
            release.set()

    def test_tabs_keyboard_readiness_and_examples_are_not_input_values(self):
        from playwright.sync_api import expect
        expect(self.page.get_by_role('tab',name='검색 트렌드',exact=True)).to_have_attribute('aria-selected','true')
        expect(self.page.locator('#panel-inbound')).to_be_hidden()
        expect(self.page.locator('#panel-manage')).to_be_hidden()
        expect(self.page.locator('#query-requirements')).to_contain_text('주제군을 먼저 등록하세요')
        expect(self.page.locator('#query-trends')).to_be_disabled()
        self.page.get_by_role('tab',name='검색 트렌드',exact=True).focus()
        self.page.keyboard.press('ArrowRight')
        expect(self.page.get_by_role('tab',name='내 블로그 유입어',exact=True)).to_be_focused()
        expect(self.page.locator('#panel-inbound')).to_be_visible()
        self.page.keyboard.press('End')
        expect(self.page.locator('#panel-weekly')).to_be_visible()
        self.page.keyboard.press('ArrowLeft')
        expect(self.page.locator('#panel-manage')).to_be_visible()
        expect(self.page.locator('#topic-words')).to_have_value('')
        self.assertIsNone(self.page.locator('#topic-words').get_attribute('placeholder'))
        self.page.get_by_text('입력 예시 보기 · 실제 입력값이 아닙니다',exact=True).click()
        expect(self.page.locator('#topic-words')).to_have_value('')
        self.add_topic()
        self.page.get_by_role('tab',name='검색 트렌드',exact=True).click()
        expect(self.page.locator('#query-trends')).to_be_enabled()
        expect(self.page.locator('#selected-topic-words')).to_contain_text('법원 입찰봉투')
        self.page.get_by_label('경매 입찰 준비 선택',exact=True).uncheck()
        expect(self.page.locator('#query-requirements')).to_contain_text('분석할 주제군을 선택하세요')
        expect(self.page.locator('#query-trends')).to_be_disabled()
        self.page.get_by_label('경매 입찰 준비 선택',exact=True).check()
        self.page.locator('#trend-end').fill('')
        expect(self.page.locator('#query-trends')).to_be_disabled()
        self.assertEqual(self.client.calls,[])

    def test_missing_key_blocks_query_but_not_management_or_inbound(self):
        from playwright.sync_api import expect
        self.client.mode='missing_key';self.server.demo=False
        self.page.reload()
        expect(self.page.locator('#query-requirements')).to_contain_text('API 인증정보를 설정하세요')
        expect(self.page.locator('#auth-help')).to_be_visible()
        self.add_topic()
        self.page.get_by_role('tab',name='검색 트렌드',exact=True).click()
        expect(self.page.locator('#query-trends')).to_be_disabled()
        self.page.locator('#trend-form').evaluate('(form) => form.requestSubmit()')
        self.assertEqual(self.client.calls,[])
        self.page.get_by_role('tab',name='내 블로그 유입어',exact=True).click()
        self.page.locator('#inbound-start').fill('2026-10-12');self.page.locator('#inbound-end').fill('2026-10-15')
        self.page.get_by_role('button',name='저장된 유입어 조회',exact=True).click()
        expect(self.page.locator('#inbound-body tr')).to_have_count(7)
        self.assertEqual(self.client.calls,[])

    def test_select_saved_target_inbound_add_is_draft_until_explicit_save(self):
        from playwright.sync_api import expect
        self.add_topic()
        topic=self.server.topics.list()[0]
        self.page.get_by_role('tab',name='내 블로그 유입어',exact=True).click()
        self.page.locator('#inbound-start').fill('2026-10-12');self.page.locator('#inbound-end').fill('2026-10-15')
        self.page.locator('#inbound-target').select_option(topic['id'])
        self.page.get_by_role('button',name='저장된 유입어 조회',exact=True).click()
        self.page.get_by_role('button',name='경매 입찰 수표 만들기 추가',exact=True).click()
        expect(self.page.locator('#panel-manage')).to_be_visible()
        self.assertIn('경매 입찰 수표 만들기',self.page.locator('#topic-words').input_value())
        self.assertEqual(self.server.topics.list()[0]['version'],1)
        self.page.get_by_role('button',name='주제군 저장',exact=True).click()
        expect(self.page.locator('#message')).to_contain_text('v2 저장 완료')
        expect(self.page.locator('#managed-topics')).to_contain_text('경매 입찰 수표 만들기')
        self.page.get_by_role('tab',name='검색 트렌드',exact=True).click()
        expect(self.page.locator('#selected-topic-words')).to_contain_text('경매 입찰 수표 만들기')
        self.assertEqual(self.client.calls,[])

    def test_existing_user_topic_copy_can_select_edit_without_touching_original(self):
        from playwright.sync_api import expect
        # All edits below target a temporary copy, never the user's actual v1.
        shutil.copy2(ROOT/'data/keyword-analysis.sqlite3',self.server.topics.path)
        saved=next((t for t in self.server.topics.list() if t['name']=='경매 입찰 준비'),None)
        if saved is None:self.skipTest('User topic not present')
        self.page.reload()
        self.page.get_by_label('경매 입찰 준비 선택',exact=True).check()
        expect(self.page.locator('#selected-topic-words')).to_contain_text(saved['keywords'][0])
        self.page.get_by_role('tab',name='주제군 관리',exact=True).click()
        self.page.get_by_role('button',name='경매 입찰 준비 수정',exact=True).click()
        expect(self.page.locator('#topic-words')).to_have_value('\n'.join(saved['keywords']))
        self.page.locator('#topic-words').fill('모의 테스트에서만 변경')
        self.page.get_by_role('button',name='주제군 저장',exact=True).click()
        expect(self.page.locator('#message')).to_contain_text(f"v{saved['version']+1} 저장 완료")
        self.assertEqual(next(t for t in self.server.topics.list() if t['id']==saved['id'])['version'],saved['version']+1)
        self.assertEqual(self.client.calls,[])

    def test_duplicate_submit_while_mock_api_pending_makes_one_request(self):
        from playwright.sync_api import expect
        self.add_topic()
        entered,release=threading.Event(),threading.Event();original=self.client.fetch
        def delayed(request):
            entered.set();release.wait(4);return original(request)
        self.client.fetch=delayed
        try:
            self.query();self.assertTrue(entered.wait(2))
            expect(self.page.locator('#query-trends')).to_be_disabled()
            self.page.locator('#trend-form').evaluate('(form) => { form.requestSubmit(); form.requestSubmit(); }')
            self.page.get_by_role('tab',name='주제군 관리',exact=True).click()
            expect(self.page.locator('#save-topic')).to_be_disabled()
            self.page.get_by_role('tab',name='검색 트렌드',exact=True).click()
            release.set()
            expect(self.page.locator('#trend-status')).to_contain_text('비교를 완료했습니다')
            self.assertEqual(len(self.client.calls),1)
        finally:release.set()


if __name__=='__main__':unittest.main()
