"""Opt-in local dashboard browser test. Never logs in or contacts Naver."""
import csv
import io
import os
import unittest
import zipfile
from pathlib import Path

from analytics.browser import chromium_path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('BLOG_UI_TEST') == '1', 'Set BLOG_UI_TEST=1 with the local dashboard running')
class DashboardBrowserTests(unittest.TestCase):
    def test_filters_downloads_responsive_and_screenshot(self):
        from playwright.sync_api import sync_playwright, expect
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=str(chromium_path()), headless=True)
            try:
                page = browser.new_page(viewport={'width':1440,'height':1100})
                errors=[]
                page.on('pageerror',lambda error:errors.append(type(error).__name__))
                page.goto('http://127.0.0.1:8765/')
                expect(page.locator('#metric-keywords')).to_have_text('7개')
                expect(page.locator('#table-body tr')).to_have_count(7)
                page.get_by_label('검색어·경로 필터',exact=True).fill('수표')
                page.get_by_role('button',name='조회',exact=True).click()
                expect(page.locator('#table-body tr')).to_have_count(2)
                page.get_by_label('검색어·경로 필터',exact=True).fill('')
                page.get_by_role('button',name='조회',exact=True).click()
                expect(page.locator('#table-body tr')).to_have_count(7)
                page.get_by_role('tab',name='상세 유입경로 7',exact=True).click()
                expect(page.locator('#table-head')).to_contain_text('상세 유입경로 원문')
                with page.expect_download() as event:
                    page.locator('#download-csv').click()
                content=Path(event.value.path()).read_text(encoding='utf-8-sig')
                self.assertEqual(len(list(csv.DictReader(io.StringIO(content)))),7)
                with page.expect_download() as event:
                    page.locator('#download-xlsx').click()
                with zipfile.ZipFile(event.value.path()) as z:
                    self.assertIn('xl/worksheets/sheet2.xml',z.namelist())
                page.get_by_role('tab',name='반복 등장',exact=True).click()
                expect(page.locator('#empty')).to_contain_text('반복 등장한 검색어가 없습니다')
                page.get_by_role('tab',name='검색어 7',exact=True).click()
                page.screenshot(path=str(ROOT/'verification/dashboard.png'),full_page=True)
                page.set_viewport_size({'width':390,'height':844})
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'),390)
                self.assertEqual(errors,[])
            finally:
                browser.close()
