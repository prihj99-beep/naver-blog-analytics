"""Official API HUB transport and one-response 14-day analysis. Never archives responses."""
from __future__ import annotations

import datetime as dt
import http.client
import json
import math
import os
import socket
import urllib.error
import urllib.request

ENDPOINT = 'https://naverapihub.apigw.ntruss.com/search-trend/v1/search'
LOW_BASELINE = 1.0  # UI heuristic in relative-index points, not an official confidence threshold.
KST = dt.timezone(dt.timedelta(hours=9))
KEY_ENV = ('NAVER_API_HUB_CLIENT_ID', 'NAVER_API_HUB_CLIENT_SECRET')


class TrendError(Exception):
    def __init__(self, code, message, status=502):
        super().__init__(message)
        self.code, self.status = code, status


def credentials():
    pair = tuple(os.environ.get(name, '') for name in KEY_ENV)
    if not all(pair) or any(len(v) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in v) for v in pair):
        return None
    return pair


def make_request(topics, end, today=None):
    try:
        last = dt.date.fromisoformat(end)
    except (TypeError, ValueError):
        raise ValueError('기준 종료일을 YYYY-MM-DD로 선택하세요.') from None
    today = today or dt.datetime.now(KST).date()
    first = last - dt.timedelta(days=13)
    if last >= today or first < dt.date(2016, 1, 1):
        raise ValueError('14일 전체가 2016-01-01 이후이며 종료일은 어제 이전이어야 합니다.')
    return {'startDate': str(first), 'endDate': str(last), 'timeUnit': 'date',
            'keywordGroups': [{'groupName': t['name'], 'keywords': list(t['keywords'])} for t in topics]}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward secret headers to a redirected host.


class TrendClient:
    def __init__(self, opener=None, credential_reader=credentials):
        # Direct TLS connection: do not inherit a proxy that could receive authentication headers.
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self._credentials = credential_reader()  # Snapshot once; server removes child-process env before browser launch.

    def configured(self):
        return bool(self._credentials)

    def fetch(self, request):
        pair = self._credentials
        if not pair:
            raise TrendError('missing_key', 'API 키가 설정되지 않았습니다. 실행 환경변수를 설정한 뒤 다시 실행하세요.', 503)
        req = urllib.request.Request(ENDPOINT, data=json.dumps(request, ensure_ascii=False).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'Accept': 'application/json',
                     'X-NCP-APIGW-API-KEY-ID': pair[0], 'X-NCP-APIGW-API-KEY': pair[1]}, method='POST')
        try:
            with self.opener.open(req, timeout=20) as response:
                raw = response.read(1024*1024+1)
            if len(raw) > 1024*1024:
                raise TrendError('invalid_response', 'API 응답 크기가 예상 범위를 벗어났습니다.')
            result = json.loads(raw)
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()  # Do not read, store or echo an error body (it can contain input or credentials).
            if status in (401, 403):
                raise TrendError('authentication', 'API 인증 또는 사용 권한을 확인하세요. 자동 재시도하지 않습니다.', 401) from None
            if status == 429:
                raise TrendError('quota', '호출 한도 또는 요청 속도 제한입니다. 콘솔에서 확인하세요. 자동 재시도하지 않습니다.', 429) from None
            raise TrendError('api_error', '공식 API 요청이 거절되었거나 서비스 오류가 발생했습니다. 자동 재시도하지 않습니다.') from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, http.client.HTTPException):
            raise TrendError('network', 'API 연결 실패 또는 시간 초과입니다. 자동 재시도하지 않습니다.') from None
        except (UnicodeError, ValueError):
            raise TrendError('invalid_response', 'API 응답 형식을 확인할 수 없습니다.') from None
        if not isinstance(result, dict):
            raise TrendError('invalid_response', 'API 응답 형식을 확인할 수 없습니다.')
        if any(k in result for k in ('error', 'errorCode', 'errMsg', 'errId')):
            raise TrendError('api_error', 'API가 오류를 반환했습니다. 인증·한도를 콘솔에서 확인하세요.')
        return result


def compare(request, response, topics):
    """Validate provenance, preserve gaps, and refuse incomplete-period averages."""
    def invalid():
        raise TrendError('invalid_response', '응답의 기간·주제군 또는 데이터 형식이 요청과 일치하지 않습니다.')
    if not isinstance(response, dict) or any(response.get(k) != request[k] for k in ('startDate', 'endDate', 'timeUnit')):
        invalid()
    results = response.get('results')
    if not isinstance(results, list):
        raise TrendError('missing_response', 'API 응답에 결과 목록이 없습니다.')
    first = dt.date.fromisoformat(request['startDate'])
    dates = [str(first + dt.timedelta(days=i)) for i in range(14)]
    by_name = {}
    expected = {t['name']: t for t in topics}
    for series in results:
        if not isinstance(series, dict) or not isinstance(series.get('title'), str):
            invalid()
        name = series['title']
        if name not in expected or name in by_name:
            invalid()
        words = series.get('keywords')
        if not isinstance(words, list) or not all(isinstance(w, str) for w in words) or sorted(words) != sorted(expected[name]['keywords']):
            invalid()
        data = series.get('data')
        if data is None:
            data = []
        if not isinstance(data, list):
            invalid()
        values = {}
        for point in data:
            if not isinstance(point, dict) or not isinstance(point.get('period'), str):
                invalid()
            day, ratio = point['period'], point.get('ratio')
            if day not in dates or day in values:
                invalid()
            if ratio is not None and (type(ratio) not in (int, float) or not math.isfinite(ratio) or not 0 <= ratio <= 100):
                invalid()
            values[day] = ratio
        by_name[name] = values
    output = []
    for topic in topics:
        values = by_name.get(topic['name'], {})
        points = [{'date': d, 'ratio': values.get(d)} for d in dates]
        previous, recent = [p['ratio'] for p in points[:7]], [p['ratio'] for p in points[7:]]
        a = sum(previous)/7 if all(v is not None for v in previous) else None
        b = sum(recent)/7 if all(v is not None for v in recent) else None
        warnings = []
        if topic['name'] not in by_name:
            warnings.append('이 주제군의 응답이 없습니다.')
        if a is None or b is None:
            warnings.append('날짜 누락 또는 부분 응답: 완전하지 않은 7일 평균과 변화율은 표시하지 않습니다.')
        if a is not None and a <= LOW_BASELINE:
            warnings.append('이전 평균이 1 이하입니다. 작은 분모의 왜곡을 피하기 위해 변화율을 숨깁니다. 이는 프로그램 표시 기준입니다.')
        output.append({'id': topic['id'], 'version': topic['version'], 'name': topic['name'],
                       'keywords': topic['keywords'], 'points': points,
                       'previous_count': sum(v is not None for v in previous), 'recent_count': sum(v is not None for v in recent),
                       'previous_mean': a, 'recent_mean': b,
                       'delta': b-a if a is not None and b is not None else None,
                       'change_percent': (b/a-1)*100 if a is not None and b is not None and a > LOW_BASELINE else None,
                       'warnings': warnings})
    return {'start': dates[0], 'end': dates[-1], 'split': dates[7], 'groups': output,
            'low_baseline': LOW_BASELINE, 'metric': 'relative_index',
            'notice': '동일한 14일 응답 내 비교입니다. 상대지수이며 절대 검색량·유입 비율이 아닙니다. 최신 날짜의 집계 확정 시점은 보장되지 않습니다.'}
