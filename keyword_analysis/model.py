"""User-owned topic versions and strictly read-only access to existing statistics."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from pathlib import Path


class Conflict(ValueError):
    pass


def clean_group(name, keywords):
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise ValueError('주제군 이름은 1~100자로 입력하세요.')
    if not isinstance(keywords, list) or not 1 <= len(keywords) <= 5:
        raise ValueError('주제군당 검색어는 1~5개로 입력하세요.')
    if any(not isinstance(k, str) or not 1 <= len(k.strip()) <= 200 for k in keywords):
        raise ValueError('검색어는 각각 1~200자로 입력하세요.')
    keywords = [k.strip() for k in keywords]
    if len(set(keywords)) != len(keywords):
        raise ValueError('같은 주제군 안에 중복 검색어가 있습니다.')
    return name.strip(), keywords


class Topics:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = self.connect()
        try:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS topics (
                    id TEXT PRIMARY KEY, version INTEGER NOT NULL,
                    deleted INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS topic_versions (
                    topic_id TEXT NOT NULL REFERENCES topics(id),
                    version INTEGER NOT NULL, name TEXT NOT NULL,
                    keywords_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(topic_id, version));
            ''')
        finally:
            db.close()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    @staticmethod
    def rows(db):
        rows = db.execute('''SELECT t.id,t.version,v.name,v.keywords_json,v.created_at
            FROM topics t JOIN topic_versions v ON t.id=v.topic_id AND t.version=v.version
            WHERE t.deleted=0 ORDER BY v.name,t.id''').fetchall()
        return [dict(id=r['id'], version=r['version'], name=r['name'],
                     keywords=json.loads(r['keywords_json']), updated_at=r['created_at']) for r in rows]

    def list(self):
        db = self.connect()
        try:
            return self.rows(db)
        finally:
            db.close()

    def save(self, name, keywords, topic_id=None, expected_version=None):
        name, keywords = clean_group(name, keywords)
        db = self.connect()
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                if any(t['name'].casefold() == name.casefold() and t['id'] != topic_id for t in self.rows(db)):
                    raise ValueError('같은 이름의 주제군이 있습니다.')
                if topic_id is None:
                    topic_id, version = uuid.uuid4().hex, 1
                    db.execute('INSERT INTO topics VALUES (?,?,0)', (topic_id, version))
                else:
                    row = db.execute('SELECT * FROM topics WHERE id=? AND deleted=0', (topic_id,)).fetchone()
                    if not row or type(expected_version) is not int or row['version'] != expected_version:
                        raise Conflict('주제군이 변경되었거나 삭제되었습니다. 새로고침 후 다시 선택하세요.')
                    version = row['version'] + 1
                    db.execute('UPDATE topics SET version=? WHERE id=?', (version, topic_id))
                db.execute('INSERT INTO topic_versions VALUES (?,?,?,?,?)',
                           (topic_id, version, name, json.dumps(keywords, ensure_ascii=False),
                            dt.datetime.now(dt.timezone.utc).isoformat()))
            return next(t for t in self.list() if t['id'] == topic_id)
        finally:
            db.close()

    def delete(self, topic_id, expected_version):
        db = self.connect()
        try:
            with db:
                if type(expected_version) is not int:
                    raise Conflict('주제군 버전을 다시 확인하세요.')
                changed = db.execute('UPDATE topics SET deleted=1 WHERE id=? AND version=? AND deleted=0',
                                     (topic_id, expected_version)).rowcount
                if not changed:
                    raise Conflict('주제군이 변경되었거나 삭제되었습니다. 새로고침하세요.')
        finally:
            db.close()

    def selected(self, selection):
        if not isinstance(selection, list) or not 1 <= len(selection) <= 5:
            raise ValueError('조회할 주제군을 1~5개 선택하세요.')
        topics = {t['id']: t for t in self.list()}
        result, seen = [], set()
        for ref in selection:
            if not isinstance(ref, dict) or not isinstance(ref.get('id'), str):
                raise ValueError('주제군 선택 형식이 올바르지 않습니다.')
            topic = topics.get(ref['id'])
            if not topic or type(ref.get('version')) is not int or topic['version'] != ref['version']:
                raise Conflict('선택한 주제군의 버전이 바뀌었습니다. 새로고침하세요.')
            if topic['id'] in seen:
                raise ValueError('주제군을 중복 선택할 수 없습니다.')
            seen.add(topic['id'])
            result.append(topic)
        return result


def inbound_keywords(path, start, end, term=''):
    """No Store construction, schema migration, backup, or write-capable connection."""
    try:
        first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    except (TypeError, ValueError):
        raise ValueError('유입어 조회 날짜를 확인하세요.') from None
    if last < first or (last-first).days > 365:
        raise ValueError('유입어 조회 기간은 1~366일로 선택하세요.')
    if not isinstance(term, str) or len(term) > 200:
        raise ValueError('검색어 필터는 200자 이하로 입력하세요.')
    path = Path(path).resolve()
    if not path.is_file():
        return {'available': False, 'rows': [], 'days': [], 'message': '기존 통계 DB가 없습니다. 새로 만들지 않습니다.'}
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        rows = db.execute('''SELECT e.date,e.position,e.raw_text,e.ratio,r.sha256
            FROM entries e JOIN reports r ON r.date=e.date
            WHERE e.category='keyword' AND e.date BETWEEN ? AND ? ORDER BY e.date,e.position''',
                          (str(first), str(last))).fetchall()
        dates = {r[0] for r in db.execute('SELECT date FROM reports WHERE date BETWEEN ? AND ?', (str(first), str(last)))}
        days = []
        for n in range((last-first).days+1):
            day = str(first + dt.timedelta(days=n))
            count = sum(r[0] == day for r in rows)
            days.append({'date': day, 'status': 'uncollected' if day not in dates else ('collected' if count else 'empty')})
        return {'available': True, 'rows': [dict(date=r[0], position=r[1], raw_text=r[2], ratio=r[3], source_hash=r[4])
                for r in rows if term.casefold() in r[2].casefold()], 'days': days}
    finally:
        db.close()
