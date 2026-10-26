"""Loopback-only dashboard. All Playwright operations stay on their owning thread."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .core import CollectionCancelled, Store, collect_dates, date_range
from .dashboard_data import csv_bytes, query, xlsx_bytes

ROOT = Path(__file__).resolve().parents[1]
APP_ID = 'naver-blog-analytics-dashboard-v1'
DEFAULT_BLOG_ID = 'nekkorangkkerong'


def korea_today():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date()


class Jobs:
    TERMINAL = {'completed', 'cancelled', 'blocked', 'failed'}
    AUTH_WAIT = {'waiting_login', 'authenticating'}

    def __init__(self, root, blog_id, runner=None):
        self.root, self.blog_id = Path(root), blog_id
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.login = threading.Event()
        self.thread = None
        self.runner = runner or self._run
        self.shutting_down = False
        self.start_requests = {}
        self.login_requests = set()
        self.revision = 0
        self.state = {'job_id': None, 'phase': 'idle', 'message': '저장된 통계는 로그인 없이 조회할 수 있습니다.', 'dates': [], 'completed': 0, 'total': 0, 'auth_verified': False}

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(self.state)
            result['active'] = self.thread is not None and self.thread.is_alive()
            result['revision'] = self.revision
            return result

    def update(self, job_id=None, **fields):
        with self.lock:
            if job_id is not None and job_id != self.state['job_id']:
                return False
            if self.state['phase'] in self.TERMINAL:
                return False
            phase = fields.get('phase')
            if self.cancel.is_set() and phase != 'cancelled':
                return False
            self.state.update(fields)
            self.revision += 1
            return True

    def start(self, dates, request_id=None):
        request_id = request_id or secrets.token_hex(16)
        with self.lock:
            if self.shutting_down:
                raise ValueError('프로그램 종료 중입니다.')
            if request_id in self.start_requests:
                return self.start_requests[request_id]
            if self.thread is not None and self.thread.is_alive():
                raise ValueError('수집이 이미 진행 중입니다.')
            # Each run owns fresh events; a late request cannot wake a new run.
            self.cancel = threading.Event()
            self.login = threading.Event()
            self.login_requests = set()
            job_id = secrets.token_hex(16)
            self.state = {'job_id': job_id, 'phase': 'starting', 'message': '로그인용 Chromium을 열고 있습니다.', 'dates': [{'date': str(d), 'status': 'waiting', 'keywords': 0, 'routes': 0} for d in dates], 'completed': 0, 'total': len(dates), 'auth_verified': False}
            self.revision += 1
            self.start_requests[request_id] = job_id
            if len(self.start_requests) > 128:
                del self.start_requests[next(iter(self.start_requests))]
            self.thread = threading.Thread(target=self._entry, args=(dates,job_id), name='naver-collection', daemon=False)
            self.thread.start()
            return job_id

    def _entry(self, dates, job_id):
        from .browser import AccessRestricted
        try:
            self.runner(dates)
        except CollectionCancelled:
            self.update(job_id, phase='cancelled', message='사용자가 인증 또는 수집을 취소했습니다. 기존 데이터는 유지됩니다.')
        except AccessRestricted as exc:
            reason = '새 보안 인증 화면' if exc.reason == 'captcha' else '명시적인 접근 제한'
            self.update(job_id, phase='blocked', message=f'{reason}이 감지되어 중단했습니다. 자동 재시도하지 않습니다. 기존 데이터는 유지됩니다.')
        except Exception as exc:
            if self.cancel.is_set():
                self.update(job_id, phase='cancelled', message='작업을 취소하고 브라우저를 닫았습니다. 기존 데이터는 유지됩니다.')
            else:
                messages = {'FileNotFoundError': '기존 Chromium을 찾을 수 없습니다.', 'TimeoutError': '화면 또는 다운로드 대기 시간이 초과됐습니다.', 'ValueError': '공식 파일 또는 선택 날짜 검증에 실패했습니다.'}
                self.update(job_id, phase='failed', message=messages.get(type(exc).__name__, '수집 중 오류가 발생했습니다.') + ' 기존 데이터는 유지됩니다.')
        finally:
            with self.lock:
                if self.state['job_id'] == job_id:
                    if self.cancel.is_set():
                        self.state.update(phase='cancelled', message='작업을 취소했습니다. 기존 데이터는 유지됩니다.')
                    for item in self.state['dates']:
                        if item['status'] in ('waiting', 'collecting'):
                            item['status'] = 'cancelled' if self.cancel.is_set() else 'skipped'
                    self.revision += 1

    def progress(self, date, status, keywords, routes, job_id=None):
        with self.lock:
            if (job_id is not None and job_id != self.state['job_id']) or self.cancel.is_set() or self.state['phase'] in self.TERMINAL:
                return
            for item in self.state['dates']:
                if item['date'] == str(date):
                    item.update(status=status, keywords=keywords, routes=routes)
            self.state['completed'] = sum(i['status'] == 'success' for i in self.state['dates'])
            self.state['message'] = f'{date} ' + ('저장 완료' if status == 'success' else '로그인 확인 완료 · 공식 XLSX 수집 중' if status == 'collecting' else '처리 중단')
            self.revision += 1

    def confirm_login(self, job_id, request_id):
        with self.lock:
            if job_id != self.state['job_id']:
                raise ValueError('이전 작업의 요청입니다. 현재 상태를 새로 확인하세요.')
            if self.shutting_down or self.cancel.is_set() or not self.thread or not self.thread.is_alive():
                raise ValueError('이미 종료되었거나 취소 중인 작업입니다.')
            if request_id in self.login_requests or self.state['phase'] == 'verifying':
                return False
            if self.state['phase'] not in self.AUTH_WAIT:
                raise ValueError('로그인 확인을 기다리는 상태가 아닙니다.')
            self.login_requests.add(request_id)
            self.state.update(phase='verifying', message='로그인 상태와 공식 통계 화면 접근을 확인하고 있습니다.')
            self.revision += 1
            self.login.set()
            return True

    def stop(self, job_id=None, shutdown=False):
        with self.lock:
            if job_id is not None and job_id != self.state['job_id']:
                raise ValueError('이전 작업의 중단 요청입니다.')
            if shutdown:
                self.shutting_down = True
            if self.thread is not None and self.thread.is_alive() and self.state['phase'] not in self.TERMINAL:
                self.cancel.set()
                self.login.set()
                self.state.update(phase='cancelling', message='중단 요청을 받았습니다. 현재 작업을 정리한 뒤 브라우저를 닫습니다.')
                self.revision += 1

    def join(self):
        if self.thread:
            self.thread.join()

    def _run(self, dates):
        from playwright.sync_api import sync_playwright
        from .browser import OfficialDownloader, chromium_path
        from .login import LoginSession
        job_id = self.state['job_id']
        cancel, login = self.cancel, self.login
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=str(chromium_path()), headless=False)
            context = browser.new_context(accept_downloads=True)
            try:
                page = context.new_page()
                page.set_default_timeout(15000)
                page.set_default_navigation_timeout(30000)
                auth = LoginSession(page, self.blog_id)
                page.goto('https://nid.naver.com/nidlogin.login', wait_until='domcontentloaded')
                self.update(job_id, phase='waiting_login', message='Chromium에서 직접 로그인하세요. 완료 후 로그인 확인 버튼을 누르세요.')
                while True:
                    if cancel.is_set():
                        raise CollectionCancelled()
                    observed = auth.observe()
                    with self.lock:
                        requested = login.is_set()
                        if requested:
                            login.clear()
                        elif self.state['phase'] != observed:
                            message = 'CAPTCHA·추가 인증을 직접 완료하세요. 인증 중에는 창을 닫지 않습니다.' if observed == 'authenticating' else '로그인 완료 후 로그인 확인 버튼을 누르세요.'
                            self.update(job_id, phase=observed, message=message)
                    if requested:
                        if auth.verify(cancel):
                            if cancel.is_set():
                                raise CollectionCancelled()
                            self.update(job_id, phase='authenticated', auth_verified=True, message='정상 로그인과 공식 통계 화면 접근을 확인했습니다.')
                            break
                        observed = auth.observe()
                        self.update(job_id, phase=observed, message='아직 인증 또는 통계 화면 접근이 확인되지 않았습니다. 열린 Chromium에서 인증을 완료한 후 다시 로그인 확인을 눌러주세요.')
                    page.wait_for_timeout(700)
                if cancel.is_set():
                    raise CollectionCancelled()
                auth.detach()
                downloader = OfficialDownloader(page, page.frame(name='statmain'))
                self.update(job_id, phase='collecting')
                store = Store(self.root / 'data/analytics.sqlite3')
                try:
                    jobs = self
                    class CommitGate:
                        def save(self, report):
                            # A cancellation acknowledgement and a commit cannot cross.
                            # An in-flight commit finishes before cancellation is accepted.
                            with jobs.lock:
                                if cancel.is_set() or jobs.state['job_id'] != job_id:
                                    raise CollectionCancelled()
                                store.save(report)
                    results = collect_dates(dates, downloader, CommitGate(), self.root / 'downloads', progress=lambda *args: self.progress(*args,job_id=job_id), cancel_event=cancel)
                    if cancel.is_set():
                        raise CollectionCancelled()
                    if any(r[1] != 'success' for r in results):
                        self.update(job_id, phase='failed', message='다운로드 또는 파싱에 실패하여 후속 날짜 수집을 멈췄습니다. 기존 데이터는 유지됩니다.')
                    else:
                        self.update(job_id, phase='completed', message='선택한 날짜의 수집과 저장을 완료했습니다.')
                finally:
                    store.close()
            finally:
                context.close()
                browser.close()


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    block_on_close = False

    def __init__(self, root=ROOT, port=8765, blog_id=DEFAULT_BLOG_ID, jobs=None):
        self.root = Path(root)
        self.identity = hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()[:16]
        self.token = secrets.token_urlsafe(32)
        self.jobs = jobs or Jobs(self.root, blog_id)
        self.stopping = threading.Event()
        super().__init__(('127.0.0.1', port), Handler)
        self.origin = f'http://127.0.0.1:{self.server_port}'

    def stop_cleanly(self):
        if self.stopping.is_set():
            return
        self.stopping.set()
        self.jobs.stop(shutdown=True)
        def finish():
            self.jobs.join()  # Playwright cleanup occurs on its owning thread.
            self.shutdown()
        threading.Thread(target=finish, name='dashboard-shutdown', daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server: DashboardServer

    def log_message(self, *args):
        pass  # Do not persist URLs, headers, page contents, or auth material.

    def send_data(self, status, data, content_type='application/json; charset=utf-8', filename=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def allowed(self, mutation=False):
        if self.headers.get('Host') != urllib.parse.urlsplit(self.server.origin).netloc:
            self.send_data(403, {'error': '허용되지 않은 호스트입니다.'})
            return False
        origin = self.headers.get('Origin')
        if origin and origin != self.server.origin:
            self.send_data(403, {'error': '외부 페이지 요청은 허용하지 않습니다.'})
            return False
        if mutation and (origin != self.server.origin or not secrets.compare_digest(self.headers.get('X-Dashboard-Token', ''), self.server.token)):
            self.send_data(403, {'error': '페이지를 새로 열고 다시 시도하세요.'})
            return False
        return True

    def do_GET(self):
        if not self.allowed():
            return
        parsed = urllib.parse.urlsplit(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        try:
            if parsed.path == '/health':
                self.send_data(200, {'app': APP_ID, 'identity': self.server.identity, 'stopping': self.server.stopping.is_set()})
            elif parsed.path == '/':
                html = (ROOT / 'analytics/web/index.html').read_text(encoding='utf-8').replace('__DASHBOARD_TOKEN__', self.server.token)
                self.send_data(200, html.encode(), 'text/html; charset=utf-8')
            elif parsed.path in ('/app.js', '/style.css'):
                path = ROOT / 'analytics/web' / parsed.path[1:]
                self.send_data(200, path.read_bytes(), 'text/javascript; charset=utf-8' if path.suffix == '.js' else 'text/css; charset=utf-8')
            elif parsed.path == '/api/status':
                state = self.server.jobs.snapshot()
                self.send_data(200, {'job': state, 'stopping': self.server.stopping.is_set(), 'blog_id': self.server.jobs.blog_id, 'today': str(korea_today())})
            elif parsed.path in ('/api/data', '/api/export'):
                data = query(self.server.root / 'data/analytics.sqlite3', params.get('start'), params.get('end'), params.get('q', ''))
                if parsed.path == '/api/data':
                    self.send_data(200, data)
                elif params.get('format') == 'xlsx':
                    self.send_data(200, xlsx_bytes(data), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'blog-statistics.xlsx')
                elif params.get('format') == 'csv':
                    category = params.get('category', 'keywords')
                    self.send_data(200, csv_bytes(data, category), 'text/csv; charset=utf-8', f'{category}.csv')
                else:
                    raise ValueError('CSV 또는 XLSX를 선택하세요.')
            else:
                self.send_data(404, {'error': '페이지를 찾을 수 없습니다.'})
        except ValueError as exc:
            self.send_data(400, {'error': str(exc)})
        except Exception:
            self.send_data(500, {'error': '데이터를 읽지 못했습니다. 기존 DB는 변경하지 않았습니다.'})

    @staticmethod
    def request_id(payload):
        value = payload.get('request_id')
        if not isinstance(value, str) or not 16 <= len(value) <= 128:
            raise ValueError('페이지를 새로 열고 다시 요청하세요.')
        return value

    @staticmethod
    def job_id(payload):
        value = payload.get('job_id')
        if not isinstance(value, str) or len(value) != 32:
            raise ValueError('현재 작업을 확인할 수 없습니다. 페이지를 새로 열어주세요.')
        return value

    def do_POST(self):
        if not self.allowed(mutation=True):
            return
        try:
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise ValueError('JSON 요청이 필요합니다.')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 8192:
                raise ValueError('요청 크기가 올바르지 않습니다.')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError('요청이 올바르지 않습니다.')
            if self.path == '/api/shutdown':
                self.send_data(200, {'ok': True})
                self.server.stop_cleanly()
                return
            if self.server.stopping.is_set():
                raise ValueError('프로그램 종료 중입니다.')
            if self.path == '/api/collect':
                start, end = dt.date.fromisoformat(payload['start']), dt.date.fromisoformat(payload['end'])
                if end >= korea_today() or start < dt.date(2000, 1, 1):
                    raise ValueError('2000년 이후, 어제까지의 날짜를 선택하세요.')
                if (end - start).days > 365:
                    raise ValueError('한 번에 최대 366일을 선택하세요.')
                dates = date_range(start, end)
                if payload.get('existing_only') is True:
                    data = query(self.server.root / 'data/analytics.sqlite3', str(start), str(end))
                    dates = [dt.date.fromisoformat(r['date']) for r in data['daily'] if r['status'] != 'uncollected']
                if not dates:
                    raise ValueError('수집할 날짜가 없습니다.')
                self.server.jobs.start(dates, request_id=self.request_id(payload))
            elif self.path == '/api/login-complete':
                self.server.jobs.confirm_login(self.job_id(payload), self.request_id(payload))
            elif self.path == '/api/cancel':
                self.server.jobs.stop(self.job_id(payload))
            else:
                self.send_data(404, {'error': '기능을 찾을 수 없습니다.'})
                return
            self.send_data(200, {'ok': True})
        except (ValueError, KeyError, TypeError) as exc:
            self.send_data(400, {'error': str(exc) if isinstance(exc, ValueError) else '날짜 선택을 확인하세요.'})
        except Exception:
            self.send_data(500, {'error': '요청을 처리하지 못했습니다. 기존 데이터는 유지됩니다.'})


def prepare_database(root):
    db_path = Path(root) / 'data/analytics.sqlite3'
    if db_path.exists():
        backup = db_path.parent / 'backups' / f'{korea_today()}-before-dashboard.sqlite3'
        backup.parent.mkdir(exist_ok=True)
        if not backup.exists():
            source, target = sqlite3.connect(db_path), sqlite3.connect(backup)
            try:
                source.backup(target)
            finally:
                source.close()
                target.close()
    store = Store(db_path)
    store.close()


def launch(port=8765, open_browser=True, root=ROOT, blog_id=DEFAULT_BLOG_ID):
    origin = f'http://127.0.0.1:{port}'
    identity = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:16]
    try:
        server = DashboardServer(root, port, blog_id)
    except OSError:
        # Existing same-project server is reused. An unrelated process is untouched.
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(origin + '/health', timeout=3) as response:
                info = json.load(response)
            if info.get('app') != APP_ID or info.get('identity') != identity:
                raise RuntimeError()
            if info.get('stopping'):
                raise ValueError('종료 중입니다. 잠시 후 다시 실행하세요.')
        except ValueError:
            raise
        except Exception:
            raise RuntimeError(f'{port} 포트를 다른 프로그램이 사용 중입니다. 해당 프로그램을 확인하거나 --port로 다른 포트를 지정하세요.') from None
        if open_browser:
            webbrowser.open(origin)
        return 'reused'
    try:
        prepare_database(root)
        if open_browser:
            threading.Timer(0.3, webbrowser.open, args=(server.origin,)).start()
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        server.jobs.stop(shutdown=True)
    finally:
        server.jobs.stop(shutdown=True)
        server.jobs.join()
        server.server_close()
    return 'closed'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-open', action='store_true')
    args = parser.parse_args()
    try:
        launch(args.port, not args.no_open)
        return 0
    except Exception as exc:
        message = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else '프로그램을 시작하지 못했습니다. 프로젝트 경로와 Python 설치 상태를 확인하세요.'
        if os.name == 'nt':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, '블로그 통계 실행 오류', 0x10)
        else:
            print(message)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
