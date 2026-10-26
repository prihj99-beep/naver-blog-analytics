"""Real Chromium with intercepted mock pages only. No request reaches Naver."""
import threading
import tempfile
import datetime as dt
import unittest
from pathlib import Path

from analytics.browser import AccessRestricted, OfficialDownloader, chromium_path, guard
from analytics.core import CollectionCancelled
from analytics.login import LoginSession, screen_state

HOME = 'https://www.naver.com/'
LOGIN = 'https://nid.naver.com/nidlogin.login'
STATS = 'https://admin.blog.naver.com/sample/stat/download'
FRAME = 'https://blog.stat.naver.com/blog/download'
CAPTCHA = '<form><h2>보안 확인</h2><label>자동입력 방지<input name="captcha"></label><button>확인</button></form>'
PASSWORD = '<form><h1>로그인</h1><input type="password"><button>로그인</button></form>'


class LoginBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path=str(chromium_path()),headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.context.route('**/*', self.route)
        self.page = self.context.new_page()
        self.page.set_default_timeout(1500)
        self.routes = {
            HOME:(200,'<h1>모의 로그인 완료</h1>'), LOGIN:(200,PASSWORD),
            STATS:(200,f'<iframe name="statmain" src="{FRAME}"></iframe>'),
            FRAME:(200,'<input type="radio" id="REFERER"><a class="u_ni_btn_download" href="#">지표 다운로드</a>'),
        }
        self.hits=[]
        self.download_status=None
        self.auth=LoginSession(self.page,'sample')

    def route(self,route):
        self.hits.append(route.request.url)
        if route.request.url == 'https://blog.stat.naver.com/api/blog/download' and self.download_status:
            if self.download_status == 200:
                route.fulfill(status=200,body=(Path(__file__).resolve().parents[1]/'verification/search-inflow-2026-10-12.xlsx').read_bytes(),headers={'Content-Type':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','Content-Disposition':'attachment; filename="mock.xlsx"'})
            else:
                route.fulfill(status=self.download_status,body='<p>Mock rejected response</p>')
            return
        if route.request.url not in self.routes:
            route.abort()  # No external fallback.
            return
        status,body=self.routes[route.request.url]
        route.fulfill(status=status,content_type='text/html; charset=utf-8',body=body)

    def tearDown(self):
        self.context.close()

    def test_manual_challenge_wait_then_verified_same_context(self):
        self.page.goto(LOGIN)
        self.assertEqual(self.auth.observe(),'waiting_login')
        self.assertFalse(self.auth.verify(threading.Event(),timeout=.3))
        self.page.set_content(CAPTCHA)
        self.assertEqual(self.auth.observe(),'authenticating')
        self.assertFalse(self.auth.verify(threading.Event(),timeout=.3))
        self.assertFalse(self.page.is_closed())
        self.assertNotIn(STATS,self.hits)
        # Simulate a human completing mock authentication (never a real CAPTCHA).
        self.page.goto(HOME)
        self.assertTrue(self.auth.verify(threading.Event(),timeout=2))
        self.assertEqual(self.page.context,self.context)
        self.assertEqual(self.hits.count(STATS),1)

    def test_plain_notices_and_hidden_challenge_are_not_blocks(self):
        self.page.set_content('<h2>자동입력 방지 안내</h2><p>로봇이 아닙니다 인증을 완료했습니다.</p><input name="captcha" hidden>')
        self.assertEqual(screen_state(self.page),'normal')
        guard(self.page)

    def test_explicit_block_stops_even_during_login(self):
        self.page.set_content('<main><h1>접근이 제한되었습니다.</h1></main>')
        with self.assertRaises(AccessRestricted):self.auth.observe()

    def test_failed_access_keeps_browser_and_no_automatic_retries(self):
        self.routes[FRAME]=(200,'<p>아직 준비되지 않은 모의 화면</p>')
        self.page.goto(HOME)
        self.assertFalse(self.auth.verify(threading.Event(),timeout=.35))
        self.assertFalse(self.page.is_closed())
        self.assertEqual(self.hits.count(STATS),1)
        self.page.wait_for_timeout(200)
        self.assertEqual(self.hits.count(STATS),1)

    def test_only_url_or_http_200_does_not_prove_login(self):
        self.routes[FRAME]=(200,'<input id="REFERER" disabled><a class="u_ni_btn_download">다운로드</a>')
        self.page.goto(HOME)
        self.assertFalse(self.auth.verify(threading.Event(),timeout=.3))

    def test_navigation_failure_keeps_authentication_browser_open(self):
        del self.routes[STATS]  # The mock network aborts this document.
        self.page.goto(HOME)
        self.assertFalse(self.auth.verify(threading.Event(),timeout=.3))
        self.assertFalse(self.page.is_closed())
        self.assertEqual(self.hits.count(STATS),1)

    def test_primary_401_is_auth_incomplete_not_permanent_block(self):
        self.routes[FRAME]=(401,PASSWORD)
        self.page.goto(HOME)
        self.assertFalse(self.auth.verify(threading.Event(),timeout=.5))
        self.assertFalse(self.page.is_closed())
        self.assertFalse(self.auth.blocked)

    def test_primary_403_or_429_is_block(self):
        for status in (403,429):
            with self.subTest(status=status):
                self.auth.blocked=False
                self.routes[HOME]=(status,'<p>모의 제한 응답</p>')
                self.page.goto(HOME)
                with self.assertRaises(AccessRestricted):self.auth.observe()

    def test_auxiliary_403_does_not_block(self):
        url='https://assets.example.test/tracker.png'
        self.routes[url]=(403,'denied')
        self.routes[HOME]=(200,f'<h1>모의 정상 화면</h1><img src="{url}">')
        self.page.goto(HOME,wait_until='load')
        self.assertFalse(self.auth.blocked)
        self.assertTrue(self.auth.verify(threading.Event(),timeout=2))

    def test_new_challenge_and_primary_restriction_stop_collection(self):
        self.page.goto(HOME)
        self.auth.detach()
        downloader=OfficialDownloader(self.page,None)
        self.page.set_content(CAPTCHA)
        with self.assertRaises(AccessRestricted):downloader.check()
        with self.assertRaises(AccessRestricted):downloader.check(allow_navigation=True)
        self.routes[HOME]=(429,'<p>모의 제한</p>')
        self.page.goto(HOME)
        with self.assertRaises(AccessRestricted):downloader.check()

    def test_cancellation_prevents_verification_navigation(self):
        self.page.goto(HOME)
        event=threading.Event();event.set()
        with self.assertRaises(CollectionCancelled):self.auth.verify(event)
        self.assertNotIn(STATS,self.hits)

    def download_fixture(self):
        self.routes[FRAME]=(200,'''<input type="radio" id="REFERER"><a>일간</a>
          <span>2026.10.12.</span><button onclick="document.querySelector('.react-datepicker').hidden=false">달력보기</button>
          <div class="react-datepicker" hidden><span class="react-datepicker__current-month">10</span><select><option>2026</option></select><a class="react-datepicker__day" onclick="this.parentElement.hidden=true">12</a></div>
          <a class="u_ni_select_box">검색 유입</a>
          <a class="u_ni_btn_download" href="https://blog.stat.naver.com/api/blog/download">지표 다운로드</a>''')
        self.page.goto(STATS)
        self.page.frame_locator('[name="statmain"]').locator('#REFERER').wait_for()
        self.auth.detach()
        return OfficialDownloader(self.page,self.page.frame(name='statmain'))

    def test_mock_official_download_parses_without_touching_real_files(self):
        self.download_status=200
        downloader=self.download_fixture()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.setup-tmp') as tmp:
            output=downloader.download(dt.date(2026,10,12),Path(tmp))
            self.assertTrue(output.is_file())
            self.assertEqual(self.hits.count('https://blog.stat.naver.com/api/blog/download'),1)

    def test_download_document_403_stops_without_waiting_or_retry(self):
        self.download_status=403
        downloader=self.download_fixture()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.setup-tmp') as tmp:
            with self.assertRaises(AccessRestricted):downloader.download(dt.date(2026,10,12),Path(tmp))
            self.assertFalse(list(Path(tmp).glob('*.xlsx')))
            self.assertEqual(self.hits.count('https://blog.stat.naver.com/api/blog/download'),1)
