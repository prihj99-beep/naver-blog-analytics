"""Read-only dashboard queries and independent-list exports, without login."""
import csv
import datetime as dt
import io
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .core import date_range


def query(db_path: Path, start=None, end=None, term=''):
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN')  # All panels and exports see one committed DB snapshot.
        bounds = db.execute('SELECT MIN(date),MAX(date) FROM reports').fetchone()
        today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date()
        start = dt.date.fromisoformat(start or bounds[0] or str(today - dt.timedelta(days=7)))
        end = dt.date.fromisoformat(end or bounds[1] or str(today - dt.timedelta(days=1)))
        if end < start:
            raise ValueError('조회 종료일이 시작일보다 빠릅니다.')
        if (end - start).days > 365:
            raise ValueError('조회 기간은 한 번에 최대 366일입니다.')
        dates = date_range(start, end)
        if len(term) > 200:
            raise ValueError('검색어 필터가 너무 깁니다.')
        all_rows = [dict(r) for r in db.execute('SELECT e.*,r.kind,r.unit,r.selection FROM entries e JOIN reports r ON r.date=e.date WHERE e.date BETWEEN ? AND ? ORDER BY e.date,e.category,e.position', (str(start), str(end)))]
        reports = {r['date']: dict(r) for r in db.execute('SELECT date,collected_at FROM reports WHERE date BETWEEN ? AND ?', (str(start), str(end)))}
        daily = []
        for date in dates:
            key = str(date)
            keyword_count = sum(r['category'] == 'keyword' and r['date'] == key for r in all_rows)
            daily.append({'date': key, 'status': ('empty' if keyword_count == 0 else 'collected') if key in reports else 'uncollected', 'keywords': keyword_count, 'routes': sum(r['category'] == 'route' and r['date'] == key for r in all_rows)})
        filtered = [r for r in all_rows if term.casefold() in r['raw_text'].casefold()]
        repeated = {}
        for row in filtered:
            if row['category'] == 'keyword':
                repeated.setdefault(row['raw_text'], set()).add(row['date'])
        return {'start': str(start), 'end': str(end), 'term': term, 'daily': daily,
                'keywords': [r for r in filtered if r['category'] == 'keyword'],
                'routes': [r for r in filtered if r['category'] == 'route'],
                'repeated': [{'raw_text': text, 'days': len(days), 'dates': sorted(days)} for text, days in sorted(repeated.items(), key=lambda pair: (-len(pair[1]), pair[0])) if len(days) >= 2]}
    finally:
        db.close()


HEADERS = ['date', 'statistics_type', 'unit', 'selection', 'position', 'raw_text', 'ratio_percent']


def export_rows(data, category):
    if category not in ('keywords', 'routes'):
        raise ValueError('알 수 없는 데이터 종류입니다.')
    return [[r['date'], r['kind'], r['unit'], r['selection'], r['position'], r['raw_text'], r['ratio']] for r in data[category]]


def csv_bytes(data, category):
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(HEADERS)
    writer.writerows(export_rows(data, category))
    return output.getvalue().encode('utf-8-sig')


def xlsx_bytes(data):
    """Build a plain values-only XLSX with separate sheets; no formulas or counts."""
    stream = io.BytesIO()
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    def xml(root):
        return ET.tostring(root, encoding='utf-8', xml_declaration=True)
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        z.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml', '<workbook xmlns="' + ns + '" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="검색어" sheetId="1" r:id="rId1"/><sheet name="상세 유입경로" sheetId="2" r:id="rId2"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        z.writestr('xl/styles.xml', '<styleSheet xmlns="' + ns + '"><fonts count="1"><font><sz val="11"/><name val="Arial"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="1"><xf fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs></styleSheet>')
        for index, category in enumerate(('keywords', 'routes'), 1):
            root = ET.Element('worksheet', xmlns=ns)
            columns = ET.SubElement(root, 'cols')
            ET.SubElement(columns, 'col', min='1', max='5', width='18', customWidth='1')
            ET.SubElement(columns, 'col', min='6', max='6', width='48', customWidth='1')
            ET.SubElement(columns, 'col', min='7', max='7', width='18', customWidth='1')
            sheet = ET.SubElement(root, 'sheetData')
            for row_index, values in enumerate([HEADERS] + export_rows(data, category), 1):
                row = ET.SubElement(sheet, 'row', r=str(row_index))
                for col, value in enumerate(values):
                    cell = ET.SubElement(row, 'c', r=f'{chr(65 + col)}{row_index}', t='inlineStr')
                    text = ET.SubElement(ET.SubElement(cell, 'is'), 't', {'{http://www.w3.org/XML/1998/namespace}space': 'preserve'})
                    # XML 1.0 forbids control characters; reject rather than alter raw text.
                    if any(ord(c) < 32 and c not in '\t\n\r' for c in str(value)):
                        raise ValueError('XLSX에 기록할 수 없는 문자가 있습니다. CSV를 이용하세요.')
                    text.text = str(value)
            z.writestr(f'xl/worksheets/sheet{index}.xml', xml(root))
    return stream.getvalue()
