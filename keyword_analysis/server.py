"""Independent loopback dashboard. No imports from or writes to the collector."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import secrets
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .model import Conflict, Topics, inbound_keywords
from .export import MIME, build_export
from .weekly import WeeklyStore, comparisons
from .weekly_analysis import analyze as analyze_weekly
from .trends import KEY_ENV, KST, TrendClient, TrendError, compare, make_request

ROOT = Path(__file__).resolve().parents[1]
WEB = Path(__file__).resolve().parent / 'web'
APP_ID = 'naver-keyword-analysis-v1'


class ServerVersionMismatch(RuntimeError):
    pass


def build_signature():
    """Hash executable source only, never credentials, DBs, or user input."""
    folder = Path(__file__).resolve().parent
    paths = [p for p in folder.glob('*.py') if not p.name.startswith('test_')]
    paths += [p for p in WEB.iterdir() if p.suffix in ('.js', '.html', '.css')]
    signature = hashlib.sha256()
    for path in sorted(paths):
        signature.update(path.relative_to(folder).as_posix().encode())
        signature.update(b'\0' + path.read_bytes() + b'\0')
    return signature.hexdigest()


class DemoClient:
    """Explicit offline mode. No credentials, sockets, or external fallback."""
    def configured(self):
        return True

    def fetch(self, request):
        first = dt.date.fromisoformat(request['startDate'])
        return {**{k: request[k] for k in ('startDate', 'endDate', 'timeUnit')},
                'results': [{'title': g['groupName'], 'keywords': g['keywords'],
                             'data': [{'period': str(first+dt.timedelta(days=i)),
                                       'ratio': (20+i*3+j*5) % 100} for i in range(14)]}
                            for j, g in enumerate(request['keywordGroups'])]}


class AnalysisServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = not hasattr(socket, 'SO_EXCLUSIVEADDRUSE')

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, root=ROOT, port=8766, client=None, demo=False):
        self.root = Path(root).resolve()
        self.instance = hashlib.sha256(str(self.root).casefold().encode()).hexdigest()
        self.build = build_signature()  # Frozen at startup; do not report later disk changes as loaded code.
        self.token = secrets.token_hex(32)
        self.client = client if client is not None else (DemoClient() if demo else TrendClient())
        self.demo = demo
        self.gate = threading.Lock()
        self.stopping = threading.Event()
        self.last_request = 0.0
        self.export_lock = threading.Lock()
        self.export_result = None  # One latest response only; never written to disk.
        self.weekly = WeeklyStore(self.root / 'data/creator-advisor-weekly.sqlite3')
        # Bind first so a duplicate launch never initializes or migrates a DB.
        super().__init__(('127.0.0.1', port), Handler)
        self.origin = f'http://127.0.0.1:{self.server_port}'
        try:
            self.topics = Topics(self.root / 'data/keyword-analysis.sqlite3')
        except Exception:
            self.server_close()
            raise

    def handle_error(self, request, client_address):
        pass  # No HTTP payloads, keys, headers, or response bodies in logs.

    def analyze(self, payload):
        if self.stopping.is_set():
            raise TrendError('stopping', '프로그램 종료 중입니다.', 409)
        if not self.gate.acquire(blocking=False):
            raise TrendError('busy', '이미 조회 중입니다. 현재 요청이 끝날 때까지 기다리세요.', 409)
        try:
            with self.export_lock:
                self.export_result = None
            if time.monotonic() - self.last_request < 2:
                raise TrendError('cooldown', '연속 조회를 방지합니다. 잠시 후 직접 다시 조회하세요.', 429)
            topics = self.topics.selected(payload.get('selection'))
            request = make_request(topics, payload.get('end'))
            self.last_request = time.monotonic()
            response = self.client.fetch(request)  # Exactly one attempt, only from explicit POST.
            if self.stopping.is_set():
                raise TrendError('stopping', '종료 요청으로 결과를 폐기했습니다.', 409)
            result = compare(request, response, topics)
            result['demo'] = self.demo
            result['fetched_at'] = dt.datetime.now(KST).isoformat()
            result['export_id'] = secrets.token_hex(16)
            with self.export_lock:
                self.export_result = result
            return result
        finally:
            self.gate.release()


class Handler(BaseHTTPRequestHandler):
    server: AnalysisServer

    def log_message(self, *args):
        pass

    def send(self, data, status=200, content_type='application/json; charset=utf-8', headers=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def host_ok(self):
        return self.headers.get('Host') == urllib.parse.urlsplit(self.server.origin).netloc

    def do_GET(self):
        if not self.host_ok():
            return self.send({'error': '허용되지 않은 호스트입니다.'}, 403)
        url = urllib.parse.urlsplit(self.path)
        try:
            if url.path == '/api/health':
                return self.send({'app': APP_ID, 'instance': self.server.instance, 'demo': self.server.demo, 'build': self.server.build})
            if url.path == '/api/state':
                return self.send({'topics': self.server.topics.list(), 'configured': self.server.client.configured(),
                                  'demo': self.server.demo,
                                  'yesterday': str(dt.datetime.now(KST).date()-dt.timedelta(days=1))})
            if url.path == '/api/inbound':
                params = urllib.parse.parse_qs(url.query)
                value = lambda k: params.get(k, [''])[0]
                return self.send(inbound_keywords(self.server.root/'data/analytics.sqlite3', value('start'), value('end'), value('term')))
            if url.path == '/api/weekly':
                reports = self.server.weekly.list()
                return self.send({'reports': reports, 'comparisons': comparisons(reports)})
            filename = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}.get(url.path)
            if filename:
                data = (WEB / filename).read_bytes().replace(b'__ANALYSIS_TOKEN__', self.server.token.encode())
                mime = {'index.html': 'text/html; charset=utf-8', 'app.js': 'text/javascript; charset=utf-8', 'style.css': 'text/css; charset=utf-8'}[filename]
                return self.send(data, content_type=mime)
            self.send({'error': '경로를 찾을 수 없습니다.'}, 404)
        except ValueError as error:
            self.send({'error': str(error)}, 400)
        except Exception:
            self.send({'error': '로컬 자료를 읽을 수 없습니다. 파일과 사용 권한을 확인하세요.'}, 500)

    def do_POST(self):
        token = self.headers.get('X-Analysis-Token', '')
        if not self.host_ok() or self.headers.get('Origin') not in (None, self.server.origin) or not token.isascii() or not secrets.compare_digest(token, self.server.token):
            return self.send({'error': '허용되지 않은 요청입니다. 이 프로그램의 화면에서 다시 시도하세요.'}, 403)
        try:
            if self.server.stopping.is_set():
                raise TrendError('stopping', '프로그램 종료 중입니다.', 409)
            size = int(self.headers.get('Content-Length', '0'))
            limit = 1500000 if self.path in ('/api/weekly/preview', '/api/weekly/save') else 16384
            if self.headers.get('Transfer-Encoding') or not 0 < size <= limit or self.headers.get_content_type() != 'application/json':
                return self.send({'error': 'JSON 요청 크기 또는 형식이 올바르지 않습니다.'}, 400)
            self.connection.settimeout(5)
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError('요청 형식이 올바르지 않습니다.')
            if self.path == '/api/topics/save':
                return self.send(self.server.topics.save(payload.get('name'), payload.get('keywords'), payload.get('id'), payload.get('version')))
            if self.path == '/api/topics/delete':
                self.server.topics.delete(payload.get('id'), payload.get('version'))
                return self.send({'ok': True})
            if self.path == '/api/trends':
                return self.send(self.server.analyze(payload))
            if self.path == '/api/weekly/preview':
                return self.send(self.server.weekly.preview(payload.get('text'), payload.get('coverage', 'unknown')))
            if self.path == '/api/weekly/save':
                return self.send(self.server.weekly.save(payload.get('text'), payload.get('expected_revision'), payload.get('acknowledge'), payload.get('coverage', 'unknown')))
            if self.path == '/api/weekly/analysis':
                with self.server.export_lock:
                    trend = self.server.export_result
                    ref = payload.get('export_id')
                    state = 'not_queried'
                    if not ref or not trend or ref != trend['export_id']:
                        trend = None
                        state = 'expired' if ref else 'not_queried'
                return self.send(analyze_weekly(self.server.weekly.list(), payload.get('start'), self.server.topics.list(), trend, state))
            if self.path == '/api/export':
                with self.server.export_lock:
                    trend = self.server.export_result
                    if payload.get('export_id'):
                        if not trend or payload['export_id'] != trend['export_id']:
                            raise Conflict('현재 조회 결과가 만료되었습니다. 새 API 호출 없이 이 결과를 내보낼 수 없습니다.')
                    else:
                        trend = None
                weekly = self.server.weekly.list() if hasattr(self.server, 'weekly') else []
                content, missing = build_export(self.server.root/'data/analytics.sqlite3', payload.get('end'), self.server.topics.list(), trend, weekly=weekly)
                return self.send(content, content_type=MIME, headers={
                    'Content-Disposition': 'attachment; filename="chatgpt-blog-analysis.xlsx"',
                    'X-Uncollected-Days': str(missing)})
            if self.path == '/api/shutdown':
                self.server.stopping.set()
                self.send({'ok': True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            self.send({'error': '경로를 찾을 수 없습니다.'}, 404)
        except TrendError as error:
            self.send({'error': str(error), 'code': error.code}, error.status)
        except Conflict as error:
            self.send({'error': str(error), 'code': 'conflict'}, 409)
        except (ValueError, TypeError, KeyError):
            # Validation messages contain only fixed application text, never arbitrary inputs.
            self.send({'error': '입력값을 확인하세요. 이름·검색어 개수·날짜·버전이 유효해야 합니다.', 'code': 'validation'}, 400)
        except Exception:
            self.send({'error': '분석 요청을 처리하지 못했습니다. 기존 통계 자료에는 영향이 없습니다.', 'code': 'internal'}, 500)


def launch(root=ROOT, port=8766, open_browser=True, demo=False, require_fresh=False):
    origin = f'http://127.0.0.1:{port}'
    try:
        server = AnalysisServer(root=root, port=port, demo=demo)
    except OSError:
        if require_fresh:
            raise RuntimeError('분석 서버가 이미 실행 중이거나 포트를 사용할 수 없습니다. 기존 분석 화면을 종료한 뒤 다시 실행하세요.') from None
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(origin+'/api/health', timeout=2) as response:
                info = json.load(response)
            instance = hashlib.sha256(str(Path(root).resolve()).casefold().encode()).hexdigest()
            if info.get('app') != APP_ID or info.get('instance') != instance or info.get('demo') != demo:
                raise ValueError()
        except Exception:
            raise RuntimeError('분석 화면 포트가 사용 중입니다. 기존 창을 확인하거나 --port로 다른 포트를 선택하세요.') from None
        if info.get('build') != build_signature():
            raise ServerVersionMismatch('이전 코드의 분석 서버가 실행 중입니다. 기존 화면의 분석 화면 종료를 누른 뒤 start_keyword_analysis.bat를 다시 실행하세요. 탭 닫기나 새로고침만으로는 서버가 갱신되지 않습니다.')
        for name in KEY_ENV:
            os.environ.pop(name, None)
        if open_browser:
            webbrowser.open(origin)
        return 'reused'
    try:
        for name in KEY_ENV:
            os.environ.pop(name, None)
        print(f'검색어 분석: {server.origin}'+(' (모의 응답 전용)' if demo else ''))
        if open_browser:
            webbrowser.open(server.origin)
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.stopping.set()
        server.server_close()
    return 'stopped'


def main():
    parser = argparse.ArgumentParser(description='독립 검색어 트렌드 분석 화면')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--demo', action='store_true', help='외부 연결 없이 모의 응답만 사용')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('포트는 1~65535 범위여야 합니다.')
    try:
        launch(port=args.port, open_browser=not args.no_browser, demo=args.demo)
    except ServerVersionMismatch as error:
        print(str(error))  # Fixed application message, no payload or credentials.
        raise SystemExit(1) from None
    except (RuntimeError, OSError, sqlite3.Error):
        print('분석 화면을 시작할 수 없습니다. 포트 사용 여부와 data 폴더의 쓰기 권한을 확인하세요.')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
