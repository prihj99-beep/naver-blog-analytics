"""Parse user-pasted text locally; no network or collector dependencies."""
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qs, urlsplit

from .model import Conflict

DATE = r'(\d{4})\s*[.\-/]\s*(\d{1,2})\s*[.\-/]\s*(\d{1,2})\s*\.?'
PERIOD = re.compile(DATE + r'\s*[~～–—]\s*' + DATE)
LINK = re.compile(r'\[([^\]\n]+)\]\((https?://[^\s)]+)\)')
RATIO = re.compile(r'^(.*?)\s*(\d+(?:\.\d+)?)\s*%$')
MAX_TEXT = 200000
SOURCE = 'Creator Advisor / 사용자 직접 복사 / 유입검색어 분석'
COVERAGES = {'unknown', 'full', 'partial'}


def effective_coverage(report):
    return 'partial' if report.get('has_more') else report.get('coverage', 'unknown')


def set_coverage(report, coverage):
    if coverage not in COVERAGES:
        raise ValueError('목록 범위를 확인하세요.')
    report['coverage'] = coverage
    report['effective_coverage'] = effective_coverage(report)
    return report


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def percent(text):
    value = Decimal(text)
    if not value.is_finite() or not 0 <= value <= 100:
        raise ValueError('비율은 0~100 범위여야 합니다.')
    return format(value, 'f')


def markdown_label(text):
    """Remove only observed outer emphasis; keep the actual keyword characters."""
    text = text.strip()
    for marker in ('***', '**'):
        if len(text) > len(marker)*2 and text.startswith(marker) and text.endswith(marker):
            return text[len(marker):-len(marker)]
    return text


def markdown_line(text):
    text = re.sub(r'^[-*]\s+', '', text.strip())
    return markdown_label(text).replace('\\~', '~')


def parse_text(raw):
    if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_TEXT:
        raise ValueError('복사 텍스트는 1~200,000자로 입력하세요.')
    errors, warnings, rows, periods, channels = [], [], {}, set(), set()
    # The raw source remains untouched for hashing/archive; normalize a parse view only.
    lines = [markdown_line(line) for line in raw.splitlines() if line.strip() and line.strip() not in ('-', '*')]
    for match in PERIOD.finditer('\n'.join(lines)):
        try:
            first, last = dt.date(*map(int, match.groups()[:3])), dt.date(*map(int, match.groups()[3:]))
            if (last-first).days != 6:
                raise ValueError()
            periods.add((str(first), str(last)))
        except ValueError:
            errors.append('조회 기간이 유효한 7일이 아닙니다.')
    if len(periods) != 1:
        errors.append('주간 시작일·종료일을 하나의 명확한 기간으로 확인할 수 없습니다.')
    start, end = next(iter(periods)) if len(periods) == 1 else (None, None)
    has_more = False
    for line in lines:
        label = re.sub(r'^[-*]\s+', '', line).strip()
        link = LINK.fullmatch(label)
        if re.fullmatch(r'더\s*보기\s*[+＋]?', link.group(1) if link else label):
            has_more = True
    metadata = {}
    for label, field, pattern in [('채널 조회수', 'channel_views', r'(\d[\d,]*)'), ('검색 유입률', 'search_rate', r'(\d+(?:\.\d+)?)\s*%')]:
        metadata[field] = None
        values = []
        for i, line in enumerate(lines):
            if line.startswith(label):
                value = line[len(label):].strip(' :：\t') or (lines[i+1] if i+1 < len(lines) else '')
                match = re.fullmatch(pattern, value)
                if not match:
                    errors.append(f'{label} 값을 해석할 수 없습니다.')
                else:
                    values.append(match.group(1).replace(',', ''))
        if len(set(values)) > 1:
            errors.append(f'{label} 값이 서로 다릅니다.')
        try:
            metadata[field] = (int(values[0]) if field == 'channel_views' else percent(values[0])) if values else None
        except (ValueError, InvalidOperation):
            errors.append(f'{label} 값이 올바르지 않습니다.')
        if not values:
            warnings.append(f'{label} 미확인 — 빈칸으로 저장합니다.')

    def add(label, url=None):
        match = RATIO.fullmatch(markdown_label(label))
        if not match or not match.group(1).strip():
            errors.append('검색어 행의 원문·비율을 분리할 수 없습니다.')
            return
        word = match.group(1).strip()
        try:
            ratio = percent(match.group(2))
        except (ValueError, InvalidOperation):
            errors.append('검색어 비율이 올바르지 않습니다.')
            return
        if len(word) > 1000:
            errors.append('검색어가 너무 깁니다.')
            return
        if url:
            url = url.replace('\\&', '&')
            parts = urlsplit(url)
            query = parse_qs(parts.query, keep_blank_values=True)
            if parts.scheme != 'https' or parts.netloc != 'creator-advisor.naver.com' or parts.path != '/new-windows/query-stats' or parts.fragment:
                errors.append('지원하지 않는 상세 통계 URL입니다.')
                return
            allowed = {'channelId', 'endDate', 'interval', 'metric', 'query', 'service', 'startDate'}
            if set(query)-allowed or any(len(v) != 1 for v in query.values()):
                errors.append('상세 URL에 미지원 또는 중복 매개변수가 있습니다. 저장하지 않습니다.')
                return
            expected = {'startDate': start, 'endDate': end, 'query': word, 'interval': 'week', 'service': 'naver_blog'}
            if any(query.get(k) != [v] for k, v in expected.items()):
                errors.append('상세 URL의 날짜·검색어·주간 조건이 표시 내용과 다릅니다.')
                return
            if not query.get('channelId', [''])[0]:
                errors.append('상세 URL에서 채널을 확인할 수 없습니다.')
                return
            channels.add(query['channelId'][0])
        item = {'raw_text': word, 'ratio': ratio, 'detail_url': url}
        if word in rows:
            previous = rows[word]
            if previous['ratio'] != ratio or (previous['detail_url'] and url and previous['detail_url'] != url):
                errors.append('같은 검색어에 서로 다른 비율 또는 URL이 있습니다.')
            else:
                previous['detail_url'] = previous['detail_url'] or url
                warnings.append('동일 검색어 중복 행을 하나로 정리했습니다.')
        else:
            rows[word] = item

    # Restrict extraction to the keyword section; menus and other metric panels are not rows.
    begin = next((i for i, line in enumerate(lines) if line == '유입검색어 분석'), None)
    if begin is None:
        errors.append('유입검색어 분석 영역 제목을 찾을 수 없습니다.')
    else:
        section = lines[begin+1:]
        row_started = False
        i = 0
        while i < len(section):
            line = section[i]
            content = line
            if row_started and content in ('게시글', '동영상', '클립'):
                break
            link = LINK.fullmatch(content)
            adjacent = list(LINK.finditer(content))
            if re.fullmatch(r'더\s*보기\s*[+＋]?', link.group(1) if link else content):
                i += 1
                continue
            if len(adjacent)==2 and adjacent[0].start()==0 and adjacent[-1].end()==len(content) and not content[adjacent[0].end():adjacent[1].start()].strip():
                word, ratio = markdown_label(adjacent[0].group(1)), markdown_label(adjacent[1].group(1))
                first_url, second_url = (m.group(2).replace('\\&','&') for m in adjacent)
                if first_url != second_url or not re.fullmatch(r'\d+(?:\.\d+)?\s*%',ratio):
                    errors.append('검색어·비율 연속 링크의 URL 또는 비율이 일치하지 않습니다.')
                else:
                    add(word+' '+ratio,first_url)
                row_started = True
            elif link and ('%' in link.group(1) or '/new-windows/query-stats' in link.group(2)):
                add(link.group(1), link.group(2)); row_started = True
            elif '%' in content and not link and not content.startswith('검색 유입률'):
                previous = section[i-1] if i else ''
                if previous != '검색 유입률':
                    # Plain text supports a same-line pair or a keyword followed by a ratio.
                    if re.fullmatch(r'\d+(?:\.\d+)?\s*%', content):
                        if previous and previous not in ('채널 조회수', '검색 유입률') and not re.search(r'\d%$', previous):
                            add(previous + ' ' + content); row_started = True
                        else:
                            errors.append('검색어 없이 비율만 있는 행입니다.')
                    else:
                        add(content); row_started = True
            elif not link and i+1 < len(section) and re.fullmatch(r'\d+(?:\.\d+)?\s*%', section[i+1]) and content != '검색 유입률':
                add(content + ' ' + section[i+1]); row_started = True
                i += 1
            elif row_started and content not in ('유입수순', '경쟁력 지수순'):
                errors.append('검색어 목록에 해석하지 못한 행이 있습니다. 누락된 비율·복사 범위를 확인하세요.')
            elif not link and content not in ('유입수순', '경쟁력 지수순', '게시글', '동영상', '클립', '채널 조회수', '검색 유입률') and not content.startswith(('채널 조회수', '검색 유입률')) and (i == 0 or section[i-1] not in ('채널 조회수', '검색 유입률')):
                errors.append('검색어 영역에 미지원 또는 불완전한 행이 있습니다. 복사 내용을 확인하세요.')
            i += 1
    if not rows:
        errors.append('확인 가능한 검색어가 없습니다. 빈 목록으로 기존 주간 자료를 교체할 수 없습니다.')
    if len(channels) > 1:
        errors.append('서로 다른 채널의 자료가 섞여 있습니다.')
    if sum(Decimal(r['ratio']) for r in rows.values()) > Decimal('100.1'):
        errors.append('검색어 비율 합계가 100%를 초과합니다. 복사 범위를 확인하세요.')
    if '조회한 기간의 데이터가 없습니다' in raw and rows:
        warnings.append('다른 영역일 수 있는 데이터 없음 문구가 함께 있습니다. 검색어 목록은 별도로 확인하세요.')
    warnings.append('복사 목록의 전체성을 프로그램이 검증하지는 않습니다. 목록에서 빠진 검색어를 유입 0으로 해석하지 않습니다.')
    if has_more:
        warnings.append('더보기 표시가 남아 있습니다. 목록이 불완전할 가능성이 있어 분석에서 일부 자료로 취급합니다.')
    return {'start': start, 'end': end, **metadata, 'channel': next(iter(channels)) if len(channels)==1 else None,
            'rows': list(rows.values()), 'errors': list(dict.fromkeys(errors)), 'warnings': list(dict.fromkeys(warnings)),
            'source_hash': digest(raw), 'source': SOURCE, 'has_more': has_more}


class WeeklyStore:
    """Lazily create a separate DB only after explicit save. Preview/list do not write."""
    def __init__(self, path):
        self.path = Path(path).resolve()

    def list(self):
        if not self.path.is_file():
            return []
        db = sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True, timeout=3)
        try:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            return [json.loads(r[0]) for r in db.execute('SELECT report_json FROM weekly_reports ORDER BY start,end')]
        finally:
            db.close()

    def preview(self, raw, coverage='unknown'):
        report = set_coverage(parse_text(raw), coverage)
        old = next((r for r in self.list() if (r['start'], r['end']) == (report['start'], report['end'])), None)
        return {'report': report, 'existing_revision': old['revision'] if old else None,
                'identical': bool(old and old['source_hash'] == report['source_hash'] and old.get('coverage', 'unknown') == coverage and old.get('has_more', False) == report['has_more']),
                'diff': difference(old, report)}

    def save(self, raw, expected_revision, acknowledge=False, coverage='unknown'):
        report = set_coverage(parse_text(raw), coverage)  # Never trust client-supplied parsed rows.
        if report['errors']:
            raise ValueError('파싱 오류가 있어 저장할 수 없습니다.')
        if acknowledge is not True:
            raise ValueError('추출 결과와 미확인 항목을 먼저 확인하세요.')
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
            raise ValueError('기존 버전을 확인하세요.')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.executescript('''CREATE TABLE IF NOT EXISTS weekly_reports (
                start TEXT NOT NULL,end TEXT NOT NULL,revision INTEGER NOT NULL,report_json TEXT NOT NULL,
                PRIMARY KEY(start,end));
                CREATE TABLE IF NOT EXISTS weekly_keywords (
                start TEXT NOT NULL,end TEXT NOT NULL,raw_text TEXT NOT NULL,ratio TEXT NOT NULL,detail_url TEXT,
                PRIMARY KEY(start,end,raw_text),FOREIGN KEY(start,end) REFERENCES weekly_reports(start,end));
                CREATE TABLE IF NOT EXISTS weekly_sources (
                start TEXT NOT NULL,end TEXT NOT NULL,revision INTEGER NOT NULL,raw_text TEXT NOT NULL,
                source_hash TEXT NOT NULL,saved_at TEXT NOT NULL,PRIMARY KEY(start,end,revision));''')
            with db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT report_json FROM weekly_reports WHERE start=? AND end=?', (report['start'], report['end'])).fetchone()
                old = json.loads(row[0]) if row else None
                if (old['revision'] if old else None) != expected_revision:
                    raise Conflict('주간 자료가 변경되었습니다. 다시 미리보기 후 교체 여부를 확인하세요.')
                channels = [json.loads(r[0]).get('channel') for r in db.execute('SELECT report_json FROM weekly_reports')]
                if report['channel'] and any(c and c != report['channel'] for c in channels):
                    raise ValueError('현재 주간 DB와 다른 채널의 자료입니다.')
                if old and old['source_hash'] == report['source_hash'] and old.get('coverage', 'unknown') == coverage and old.get('has_more', False) == report['has_more']:
                    return {'saved': False, 'report': old}
                report['revision'] = (old['revision'] if old else 0) + 1
                report['saved_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
                db.execute('DELETE FROM weekly_keywords WHERE start=? AND end=?', (report['start'], report['end']))
                db.execute('INSERT OR REPLACE INTO weekly_reports VALUES (?,?,?,?)',
                           (report['start'], report['end'], report['revision'], json.dumps(report, ensure_ascii=False)))
                db.executemany('INSERT INTO weekly_keywords VALUES (?,?,?,?,?)',
                               [(report['start'], report['end'], r['raw_text'], r['ratio'], r['detail_url']) for r in report['rows']])
                db.execute('INSERT INTO weekly_sources VALUES (?,?,?,?,?,?)',
                           (report['start'], report['end'], report['revision'], raw, report['source_hash'], report['saved_at']))
            return {'saved': True, 'report': report}
        finally:
            db.close()


def difference(old, new):
    before = {r['raw_text']: r for r in (old or {}).get('rows', [])}
    after = {r['raw_text']: r for r in new['rows']}
    return {'added': sorted(after.keys()-before.keys()), 'removed': sorted(before.keys()-after.keys()),
            'changed': [{'raw_text': k, 'before': before[k], 'after': after[k]} for k in sorted(before.keys() & after.keys()) if before[k] != after[k]],
            'metrics': [{'field': k, 'before': (old or {}).get(k, 'unknown' if k=='coverage' else None), 'after': new.get(k)} for k in ('channel_views', 'search_rate', 'channel', 'coverage') if (old or {}).get(k, 'unknown' if k=='coverage' else None) != new.get(k)]}


def comparisons(reports):
    index = {(r['start'], r['end']): r for r in reports}
    result = []
    for current in reports:
        prior = tuple(str(dt.date.fromisoformat(current[k])-dt.timedelta(days=7)) for k in ('start', 'end'))
        previous = index.get(prior)
        before = {r['raw_text']: r for r in (previous or {}).get('rows', [])}
        after = {r['raw_text']: r for r in current['rows']}
        for word in sorted(before.keys() | after.keys()):
            a, b = before.get(word), after.get(word)
            result.append({'start': current['start'], 'end': current['end'], 'previous_start': prior[0], 'previous_end': prior[1],
                'raw_text': word, 'status': '이전 주 미등록' if not previous else '반복' if a and b else '신규(목록 기준)' if b else '사라짐(목록 기준)',
                'previous_ratio': a['ratio'] if a else None, 'ratio': b['ratio'] if b else None,
                'difference_pp': float(Decimal(b['ratio'])-Decimal(a['ratio'])) if a and b else None})
    return result
