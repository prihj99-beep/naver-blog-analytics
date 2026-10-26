from __future__ import annotations

import csv
import datetime as dt
import hashlib
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


@dataclass(frozen=True)
class Item:
    raw_text: str
    ratio: str


@dataclass(frozen=True)
class Report:
    date: dt.date
    kind: str
    unit: str
    selection: str
    keywords: tuple[Item, ...]
    routes: tuple[Item, ...]
    sha256: str


def parse_xlsx(path: Path, expected_date: dt.date | None = None) -> Report:
    """Read physical worksheet rows; deliberately ignore Excel dimension hints."""
    raw = Path(path).read_bytes()
    with zipfile.ZipFile(path) as archive:
        shared = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            shared = [''.join(n.itertext()) for n in ET.fromstring(archive.read('xl/sharedStrings.xml')).findall('m:si', NS)]
        # Locate the sole worksheet via workbook relationships, not sheet dimensions.
        workbook = ET.fromstring(archive.read('xl/workbook.xml'))
        sheets = workbook.findall('m:sheets/m:sheet', NS)
        if len(sheets) != 1:
            raise ValueError('Expected one official worksheet')
        rid = sheets[0].get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
        rels = ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))
        target = next(r.get('Target') for r in rels if r.get('Id') == rid)
        import posixpath
        sheet_path = target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/' + target)
        root = ET.fromstring(archive.read(sheet_path))
        rows = []
        for row in root.findall('m:sheetData/m:row', NS):
            cells = {}
            for cell in row.findall('m:c', NS):
                ref = cell.get('r', '')
                col = ''.join(c for c in ref if c.isalpha())
                value = cell.findtext('m:v', default='', namespaces=NS)
                if cell.get('t') == 's':
                    value = shared[int(value)]
                elif cell.get('t') == 'inlineStr':
                    value = ''.join(cell.find('m:is', NS).itertext())
                cells[col] = value
            rows.append(cells)
    meta = {r.get('A', ''): r.get('B', '') for r in rows[:8]}
    import re
    match = re.fullmatch(r'(\d{4})\.(\d{2})\.(\d{2})\.\s*일간', meta.get('데이터 기간', ''))
    if not match or meta.get('데이터명') != '유입분석' or meta.get('데이터 단위') != '일간' or meta.get('선택1') != '검색 유입':
        raise ValueError('Unsupported or incomplete XLSX metadata')
    date = dt.date(*(int(s) for s in match.groups()))
    if expected_date is not None and date != expected_date:
        raise ValueError(f'Date mismatch: expected {expected_date}, got {date}')
    header = next((i for i, r in enumerate(rows) if [r.get(c, '') for c in 'ABCD'] == ['유입경로', '비율', '상세유입경로', '비율']), None)
    if header is None:
        raise ValueError('Missing expected data columns')
    keywords, routes = [], []
    for row in rows[header + 1:]:
        # The two column pairs are independent lists, never joined by row index.
        for text_col, ratio_col, dest in [('A', 'B', keywords), ('C', 'D', routes)]:
            text, ratio = row.get(text_col, ''), row.get(ratio_col, '')
            if text == '' and ratio == '':
                continue
            if not text or not ratio:
                raise ValueError('Incomplete data row')
            try:
                number = Decimal(ratio.rstrip('%'))
            except InvalidOperation as exc:
                raise ValueError('Invalid ratio') from exc
            if not number.is_finite() or not 0 <= number <= 100:
                raise ValueError('Ratio outside 0..100')
            dest.append(Item(text, ratio))
    # A fully validated official header with no keyword rows represents a collected
    # empty day. Missing metadata/columns and incomplete rows still fail above.
    return Report(date, meta['데이터명'], meta['데이터 단위'], meta['선택1'], tuple(keywords), tuple(routes), hashlib.sha256(raw).hexdigest())


class Store:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS reports (
                date TEXT PRIMARY KEY, kind TEXT NOT NULL, unit TEXT NOT NULL,
                selection TEXT NOT NULL, sha256 TEXT NOT NULL,
                collected_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS entries (
                date TEXT NOT NULL REFERENCES reports(date) ON DELETE CASCADE,
                category TEXT NOT NULL CHECK(category IN ('keyword','route')),
                position INTEGER NOT NULL, raw_text TEXT NOT NULL, ratio TEXT NOT NULL,
                PRIMARY KEY(date,category,position));
        ''')

    def save(self, report: Report):
        with self.db:
            self.db.execute('DELETE FROM reports WHERE date=?', (report.date.isoformat(),))
            self.db.execute('INSERT INTO reports VALUES (?,?,?,?,?,?)', (report.date.isoformat(), report.kind, report.unit, report.selection, report.sha256, dt.datetime.now(dt.timezone.utc).isoformat()))
            for category, items in [('keyword', report.keywords), ('route', report.routes)]:
                self.db.executemany('INSERT INTO entries VALUES (?,?,?,?,?)', [(report.date.isoformat(), category, i, item.raw_text, item.ratio) for i, item in enumerate(items, 1)])

    def export(self, directory: Path):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        for category, filename in [('keyword', 'keywords.csv'), ('route', 'routes.csv')]:
            path = directory / filename
            temporary = path.with_suffix('.csv.tmp')
            with temporary.open('w', encoding='utf-8-sig', newline='') as file:
                writer = csv.writer(file)
                writer.writerow(['date', 'statistics_type', 'unit', 'selection', 'position', 'raw_text', 'ratio_percent'])
                writer.writerows(self.db.execute('SELECT e.date,r.kind,r.unit,r.selection,e.position,e.raw_text,e.ratio FROM entries e JOIN reports r ON e.date=r.date WHERE category=? ORDER BY e.date,e.position', (category,)))
            temporary.replace(path)

    def close(self):
        self.db.close()


def date_range(start: dt.date, end: dt.date):
    if end < start:
        raise ValueError('End date precedes start date')
    return [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]


class CollectionCancelled(RuntimeError):
    pass


def collect_dates(dates, downloader, store: Store, download_dir: Path, delay_seconds=3, sleep=None, progress=None, cancel_event=None):
    """One attempt per selected date; a failed download/parse never changes its DB rows."""
    import time
    sleep = sleep or time.sleep
    results = []
    selected = sorted(set(dates))
    for index, date in enumerate(selected):
        if cancel_event is not None and cancel_event.is_set():
            raise CollectionCancelled()
        if index:
            if cancel_event is not None:
                if cancel_event.wait(delay_seconds):
                    raise CollectionCancelled()
            else:
                sleep(delay_seconds)
        if progress:
            progress(date, 'collecting', 0, 0)
        try:
            path = downloader.download(date, Path(download_dir))
            report = parse_xlsx(path, expected_date=date)
            if cancel_event is not None and cancel_event.is_set():
                raise CollectionCancelled()
            store.save(report)
            results.append((date, 'success', len(report.keywords), len(report.routes)))
            if progress:
                progress(date, 'success', len(report.keywords), len(report.routes))
        except Exception as exc:
            from .browser import AccessRestricted
            if progress:
                progress(date, 'cancelled' if isinstance(exc, CollectionCancelled) else 'failed', 0, 0)
            if isinstance(exc, (AccessRestricted, CollectionCancelled)):
                raise
            # Do not log arbitrary browser exceptions: they can contain URLs or page data.
            results.append((date, 'failed:' + type(exc).__name__, 0, 0))
            break
    return results
