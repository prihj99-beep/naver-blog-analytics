"""Memory-only XLSX export; statistics are opened by the read-only model reader."""
import datetime as dt
import io
import math
import re
import zipfile
from xml.etree import ElementTree as ET

from .model import inbound_keywords

MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'


def workbook(sheets):
    """Explicit strings (including formula-looking keywords), numbers, and blanks."""
    output = io.BytesIO()
    def xml(element):
        return ET.tostring(element, encoding='utf-8', xml_declaration=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        types = ET.Element('Types', xmlns='http://schemas.openxmlformats.org/package/2006/content-types')
        ET.SubElement(types, 'Default', Extension='rels', ContentType='application/vnd.openxmlformats-package.relationships+xml')
        ET.SubElement(types, 'Default', Extension='xml', ContentType='application/xml')
        ET.SubElement(types, 'Override', PartName='/xl/workbook.xml', ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml')
        rootrels = ET.Element('Relationships', xmlns='http://schemas.openxmlformats.org/package/2006/relationships')
        ET.SubElement(rootrels, 'Relationship', Id='rId1', Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument', Target='xl/workbook.xml')
        archive.writestr('_rels/.rels', xml(rootrels))
        book = ET.Element('workbook', xmlns=NS)
        sheetlist = ET.SubElement(book, 'sheets')
        rels = ET.Element('Relationships', xmlns='http://schemas.openxmlformats.org/package/2006/relationships')
        for index, (name, rows) in enumerate(sheets, 1):
            ET.SubElement(sheetlist, 'sheet', name=name, sheetId=str(index), attrib={'{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id': f'rId{index}'})
            ET.SubElement(rels, 'Relationship', Id=f'rId{index}', Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet', Target=f'worksheets/sheet{index}.xml')
            ET.SubElement(types, 'Override', PartName=f'/xl/worksheets/sheet{index}.xml', ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml')
            sheet = ET.Element('worksheet', xmlns=NS)
            data = ET.SubElement(sheet, 'sheetData')
            for rownum, values in enumerate(rows, 1):
                row = ET.SubElement(data, 'row', r=str(rownum))
                for colnum, value in enumerate(values, 1):
                    n, col = colnum, ''
                    while n:
                        n, rem = divmod(n-1, 26)
                        col = chr(65+rem)+col
                    cell = ET.SubElement(row, 'c', r=f'{col}{rownum}')
                    if value is None:
                        continue
                    if type(value) in (int, float) and math.isfinite(value):
                        ET.SubElement(cell, 'v').text = str(value)
                    else:
                        cell.set('t', 'inlineStr')
                        text = ET.SubElement(ET.SubElement(cell, 'is'), 't', {'{http://www.w3.org/XML/1998/namespace}space': 'preserve'})
                        text.text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(value))
            archive.writestr(f'xl/worksheets/sheet{index}.xml', xml(sheet))
        archive.writestr('[Content_Types].xml', xml(types))
        archive.writestr('xl/workbook.xml', xml(book))
        archive.writestr('xl/_rels/workbook.xml.rels', xml(rels))
    return output.getvalue()


def build_export(path, end, topics, trend=None, weekly=None):
    last = dt.date.fromisoformat(end)
    start = str(last-dt.timedelta(days=13))
    if trend and (trend['start'] != start or trend['end'] != end):
        raise ValueError('조회 조건이 변경되었습니다. 현재 결과를 확인하세요.')
    inbound = inbound_keywords(path, start, end)
    days = inbound['days'] or [{'date': str(dt.date.fromisoformat(start)+dt.timedelta(days=i)), 'status': 'uncollected'} for i in range(14)]
    keywords = [['날짜', '검색어 원문', '실제 유입 비율(%) 원문', '원본 순서', '원본 XLSX SHA256']]
    keywords.extend([r['date'], r['raw_text'], r['ratio'], r['position'], r['source_hash']] for r in inbound['rows'])
    trends = [['출처', 'NAVER 데이터랩 검색어 트렌드' if trend and not trend.get('demo') else '모의 데이터' if trend else '미조회'],
              ['기간 시작', start], ['기간 종료', end], ['조회 시각', trend.get('fetched_at') if trend else None],
              ['조회 조건', '일간 / 기기·성별·연령 전체'],
              ['지표 설명', '주제군 단위 상대지수. 구성 검색어별 개별 지수나 절대 검색량이 아닙니다. 요청이 다른 지수끼리 직접 비교하지 마세요.'],
              ['날짜', '주제군명', '버전', '구성 검색어(JSON)', '상대지수(0~100)', '상태']]
    import json
    for group in (trend or {}).get('groups', []):
        for point in group['points']:
            trends.append([point['date'], group['name'], group['version'], json.dumps(group['keywords'], ensure_ascii=False), point['ratio'], '누락' if point['ratio'] is None else '조회됨'])
    metadata = [['파일 생성 시각', dt.datetime.now(dt.timezone.utc).isoformat()],
                ['기간', start, end], ['안내', '유입 비율과 상대지수는 다른 지표입니다. 방문자 수·유입 횟수를 추정하지 않습니다.'],
                ['이용 조건', 'API 결과의 XLSX 보관 및 ChatGPT 업로드 허용 범위는 제공자 확인 필요'],
                ['주제군 구분', '주제군명', '버전', '구성 검색어(JSON)']]
    for label, groups in [('내보내기 시점 저장 주제군', topics), ('현재 트렌드 조회 주제군', (trend or {}).get('groups', []))]:
        metadata.extend([label, g['name'], g['version'], json.dumps(g['keywords'], ensure_ascii=False)] for g in groups)
    metadata.extend([[], ['날짜', '통계 수집 상태']])
    labels = {'collected': '수집', 'empty': '수집·검색어 없음', 'uncollected': '미수집'}
    metadata.extend([d['date'], labels[d['status']]] for d in days)
    sheets = [('실제 유입 검색어', keywords), ('검색 트렌드', trends), ('주제군 및 수집상태', metadata)]
    if weekly:
        from .weekly import comparisons, SOURCE, effective_coverage
        from .weekly_analysis import COVERAGE_LABELS
        raw_rows = [['기간 시작', '기간 종료', '채널', '버전', '채널 조회수', '검색 유입률(%)', '검색어 원문', '검색어 유입 비율(%)', '상세 통계 URL', '출처', '저장 시각', '원문 SHA256']]
        summary = [['기간 시작', '기간 종료', '채널 조회수', '검색 유입률(%)', '검색어 행 수', '이전 주 상태', '자료 상태', '목록 범위 사용자 선택', '더보기 표시']]
        comparison = [['기간 시작', '기간 종료', '이전 주 시작', '이전 주 종료', '검색어 원문', '목록 비교', '이전 비율(%)', '최근 비율(%)', '차이(%p)']]
        for report in weekly:
            for row in report['rows']:
                raw_rows.append([report['start'], report['end'], report['channel'], report['revision'], report['channel_views'],
                    report['search_rate'], row['raw_text'], row['ratio'], row['detail_url'], SOURCE, report['saved_at'], report['source_hash']])
            prior = tuple(str(dt.date.fromisoformat(report[k])-dt.timedelta(days=7)) for k in ('start', 'end'))
            exists = any((r['start'], r['end'])==prior for r in weekly)
            summary.append([report['start'], report['end'], report['channel_views'], report['search_rate'], len(report['rows']),
                            '등록' if exists else '미등록', COVERAGE_LABELS[effective_coverage(report)], report.get('coverage','unknown'), '있음' if report.get('has_more') else '없음 또는 구버전 미확인'])
        summary.extend([[], ['안내', '주간 자료는 전체 누적이며 일간 시트의 14일 범위와 다릅니다. 채널 조회수·검색 유입률은 주간 지표입니다.'],
                        ['안내', '신규·사라짐은 복사 목록 기준입니다. 이전 주 미등록과 검색어 부재를 구분하며 유입 횟수를 추정하지 않습니다.'],
                        ['안내', '적은 조회수에서는 비율 변화가 크게 보일 수 있습니다. 증감률과 성과를 단정하지 않습니다.']])
        for row in comparisons(weekly):
            comparison.append([row[k] for k in ('start','end','previous_start','previous_end','raw_text','status','previous_ratio','ratio','difference_pp')])
        sheets.extend([('주간 유입검색어 원자료', raw_rows), ('주간 요약 및 출처', summary), ('주간 검색어 비교', comparison)])
    return workbook(sheets), sum(d['status']=='uncollected' for d in days)
