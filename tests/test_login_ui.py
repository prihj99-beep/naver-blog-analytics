"""Mock local dashboard endpoints in Chromium; never starts a real collection."""
import json
import unittest
from pathlib import Path

from analytics.browser import chromium_path

ROOT=Path(__file__).resolve().parents[1]


class LoginUITests(unittest.TestCase):
    def test_authentication_status_buttons_and_job_ids(self):
        from playwright.sync_api import sync_playwright, expect
        state={'job_id':'a'*32,'phase':'waiting_login','message':'모의 로그인 대기','dates':[{'date':'2026-10-12','status':'waiting'}],'completed':0,'total':1,'active':True,'revision':1,'auth_verified':False}
        posts=[]
        data={'start':'2026-10-12','end':'2026-10-12','daily':[{'date':'2026-10-12','status':'uncollected','keywords':0,'routes':0}],'keywords':[],'routes':[],'repeated':[]}
        def route(request):
            path=request.request.url.split('18765',1)[-1].split('?')[0]
            if request.request.method=='POST':
                posts.append((path,request.request.post_data_json))
                if path=='/api/login-complete':state.update(phase='verifying',revision=state['revision']+1)
                if path=='/api/cancel':state.update(phase='cancelling',revision=state['revision']+1)
                request.fulfill(json={'ok':True});return
            if path=='/api/status':request.fulfill(json={'job':state,'stopping':False,'blog_id':'mock','today':'2026-10-18'});return
            if path=='/api/data':request.fulfill(json=data);return
            name={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}.get(path)
            if name:
                body=(ROOT/'analytics/web'/name).read_text(encoding='utf-8').replace('__DASHBOARD_TOKEN__','mock-page-token')
                request.fulfill(body=body,content_type={'index.html':'text/html','app.js':'text/javascript','style.css':'text/css'}[name]);return
            request.abort()
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path=str(chromium_path()),headless=True)
            try:
                page=browser.new_page(viewport={'width':1360,'height':1000})
                errors=[];page.on('pageerror',lambda e:errors.append(type(e).__name__))
                page.route('**/*',route)
                page.goto('http://127.0.0.1:18765/')
                expect(page.locator('#phase')).to_have_text('로그인 대기')
                expect(page.locator('#login-complete')).to_be_visible()
                page.locator('#login-complete').click()
                expect(page.locator('#phase')).to_have_text('로그인 확인 중')
                self.assertEqual(posts[0][1]['job_id'],'a'*32)
                self.assertGreaterEqual(len(posts[0][1]['request_id']),16)
                state.update(phase='authenticating',revision=3,message='모의 인증 미완료 · 창 유지')
                expect(page.locator('#phase')).to_have_text('사용자 인증 진행')
                expect(page.locator('#login-complete')).to_be_enabled()
                self.assertEqual(len(posts),1)  # No automatic recheck after failure.
                page.locator('#cancel').click()
                expect(page.locator('#phase')).to_have_text('취소 처리 중')
                self.assertEqual(posts[-1][1],{'job_id':'a'*32})
                state.update(phase='cancelled',revision=5,active=False)
                expect(page.locator('#phase')).to_have_text('취소')
                expect(page.locator('#login-complete')).to_be_hidden()
                state.update(phase='blocked',revision=6)
                expect(page.locator('#phase')).to_have_text('접근 제한')
                state.update(phase='collecting',revision=7,active=True,auth_verified=True)
                expect(page.locator('#phase')).to_have_text('수집 중')
                state.update(phase='completed',revision=8,active=False,completed=1)
                expect(page.locator('#phase')).to_have_text('완료')
                self.assertEqual(errors,[])
            finally:
                browser.close()
