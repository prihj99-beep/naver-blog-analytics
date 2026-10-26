"""Deterministic observations from saved reports. No network, persistence or AI."""
import datetime as dt
from decimal import Decimal
import re

from .weekly import comparisons, effective_coverage
from .briefing import build_briefing, group_key

RULE_VERSION = 'intent-rules-v2'
SMALL_VIEWS = 30  # Display guard, not a statistical confidence threshold.
RULES = (
    ('deposit', '입찰 준비 · 보증금/수표', ('입찰보증', '보증수표', '보증금수표', '입찰수표', '경매수표'), '보증금·수표 준비 과정에서 확인할 질문 정리'),
    ('documents', '입찰 준비 · 서류/절차', ('입찰봉투', '입찰표', '입찰서', '대리입찰'), '입찰 서류와 절차의 확인 항목 정리'),
    ('returns', '경매 수익 계산·엑셀', ('경매엑셀', '엑셀경매', '경매수익', '경매투자수익', '경매수익률'), '경매 수익 계산에 필요한 항목과 엑셀 사용 시 확인할 질문 정리'),
    ('review', '아파트 경매 후기', ('아파트경매후기', '아파트경매경험'), '아파트 경매 경험에서 독자가 궁금해하는 질문 검토'),
    ('visit', '경매 임장', ('경매임장', '임장준비', '임장주의'), '경매 임장 전 확인할 질문과 현장 주의사항 검토'),
    ('local', '지역 투자 관심', (), '검색어에 나온 지역과 투자 목적을 먼저 확인'),
    ('property', '경매 물건 탐색', ('경매물건', '경매일정'), '물건 정보를 찾고 확인할 때의 점검 항목 정리'),
    ('loan', '경매 자금/대출', ('경락잔금대출', '경매대출', '잔금대출'), '대출·잔금 준비 시 확인할 질문 정리'),
    ('rights', '권리관계 확인', ('권리분석', '말소기준권리', '대항력'), '권리관계 확인에 필요한 용어와 질문 정리'),
    ('shares', '지분/공유관계', ('지분경매', '공유물분할', '공유자우선매수'), '지분·공유관계에서 확인할 쟁점 정리'),
)
COVERAGE_LABELS = {'unknown': '목록 범위 미확인', 'full': '전체 목록 · 사용자 확인', 'partial': '일부 자료 · 불완전 가능'}


def normalized(text):
    return re.sub(r'\s+', '', text).casefold()


def rule_matches(word):
    value = normalized(word)
    matches = [(key, label, term, idea) for key, label, terms, idea in RULES for term in terms if term in value]
    # A locality suffix + investment intent; no specific place name or date is encoded.
    if re.fullmatch(r'[가-힣]{2,}(?:동|읍|면|리|구|시)투자', value):
        key, label, _, idea = next(rule for rule in RULES if rule[0]=='local')
        matches.append((key,label,'지역명 형태 + 투자',idea))
    return matches


def classify(row, topics):
    word = row['raw_text']
    matches, reasons, keys, labels = rule_matches(word), [], [], []
    for key, label, term, _ in matches:
        if key not in keys:
            keys.append(key); labels.append(label)
        reasons.append(f'규칙 표현 포함: {term}')
    refs = []
    for topic in topics:
        exact = [k for k in topic['keywords'] if normalized(word) == normalized(k)]
        if exact:
            refs.append({'id': topic['id'], 'name': topic['name'], 'version': topic['version']})
            labels.append(f"저장 주제군: {topic['name']} · v{topic['version']}")
            reasons.append('구성 검색어와 공백 제외 일치: ' + ' · '.join(exact))
    return {**row, 'rule_keys': keys, 'labels': labels or ['미분류'], 'topic_refs': refs,
            'reasons': reasons or ['지원하는 표현 규칙이나 현재 주제군 구성어와 일치하지 않습니다.'],
            'classification': '규칙 기반 분류 후보 · 실제 검색 의도를 확정하지 않습니다.'}


def metric(current, previous, field):
    a = previous.get(field) if previous else None
    b = current.get(field)
    difference = (int(b)-int(a) if field=='channel_views' else float(Decimal(str(b))-Decimal(str(a)))) if a is not None and b is not None else None
    return {'previous': a, 'current': b, 'difference': difference,
            'unit': '회' if field=='channel_views' else '%p'}


def analyze(reports, start, topics=(), trend=None, trend_state='not_queried'):
    current = next((r for r in reports if r['start']==start), None)
    if current is None:
        return {'available': False, 'message': '저장된 주간 자료가 없습니다. 붙여넣고 확인 저장하면 분석이 표시됩니다.'}
    prior = tuple(str(dt.date.fromisoformat(current[k])-dt.timedelta(days=7)) for k in ('start','end'))
    previous = next((r for r in reports if (r['start'],r['end'])==prior), None)
    rows = [classify(row, topics) for row in current['rows']]
    groups = {}
    for row in rows:
        label = group_key(row)
        groups.setdefault(label, []).append(row)
    observed = [r for r in comparisons(reports) if r['start']==current['start']]
    for row in observed:
        if row['status']=='사라짐(목록 기준)':
            row['status'] = '이번 목록에서 미확인'
        elif row['status']=='신규(목록 기준)':
            row['status'] = '신규 관측(목록 기준)'
    scope = effective_coverage(current)
    prior_scope = effective_coverage(previous) if previous else None
    caveats = ['신규 관측은 검색 수요 증가의 증거가 아닙니다. 목록에 없는 검색어의 유입을 0으로 처리하지 않습니다.',
               '비율은 개별 원문 값을 보존합니다. 주제별 비율 합계·유입 횟수·절대 검색량을 계산하지 않습니다.']
    if scope != 'full' or (previous and prior_scope != 'full'):
        caveats.append('일부 또는 범위 미확인 목록이 포함되어 있습니다. 목록 변화는 전체 검색 유입의 증감을 뜻하지 않습니다.')
    if current.get('has_more'):
        caveats.append('복사 원문에 더보기가 남아 있습니다. 전체 선택 여부와 관계없이 불완전 가능성을 표시합니다.')
    if any(r.get('channel_views') is not None and r['channel_views'] < SMALL_VIEWS for r in (current,previous) if r):
        caveats.append(f'채널 조회수가 {SMALL_VIEWS}회 미만인 기간이 있습니다. 작은 자료의 비율 변화를 성과로 단정하지 않습니다. 이 기준은 표시용이며 통계적 유의성 기준이 아닙니다.')
    if previous is None:
        caveats.append('정확히 직전 7일의 저장 자료가 없어 주간 변화와 신규·반복을 판정하지 않습니다.')
    if not current.get('channel') or (previous and not previous.get('channel')):
        caveats.append('채널이 미확인인 자료가 있습니다. 같은 채널의 복사 자료인지 직접 확인하세요.')
    metrics = {field: metric(current,previous,field) for field in ('channel_views','search_rate')}
    explanations = []
    for field, label in [('channel_views','채널 조회수'),('search_rate','검색 유입률')]:
        value = metrics[field]
        if value['difference'] is None:
            explanations.append(f'{label}: 이전 주 또는 현재 값이 미확인되어 차이를 계산하지 않습니다.')
        else:
            delta = value['difference']
            explanations.append(f"{label}: 이전 주보다 {abs(delta):g}{value['unit']} {'증가' if delta>0 else '감소' if delta<0 else '차이 없음'}. 변화 원인이나 콘텐츠 효과는 확인되지 않았습니다.")
    candidates = []
    for key, label, _, idea in RULES:
        evidence = [r['raw_text'] for r in rows if r['rule_keys']==[key]]
        if evidence:
            candidates.append({'topic': label, 'candidate': idea, 'evidence': evidence,
                'checks': '기존 글에서 이미 다룬 내용인지, 실제 질문과 맞는지, 최신 정보인지 직접 확인하세요. 콘텐츠대장·게시물별 유입 연결이 없어 특정 글의 보완·발행을 권고하지 않습니다.'})
    for topic in topics:
        evidence = [r['raw_text'] for r in rows if any(ref['id']==topic['id'] for ref in r['topic_refs'])]
        if evidence and not any(r['rule_keys'] for r in rows if r['raw_text'] in evidence):
            candidates.append({'topic': topic['name'], 'candidate': '이 주제에서 관측된 표현의 질문·설명 범위를 검토', 'evidence': evidence,
                'checks': '기존 글과의 중복 및 검색 의도를 직접 확인하세요. 제목만으로 새 글 발행 필요성을 판단하지 않습니다.'})
    trend_info = {'state': trend_state, 'groups': [], 'notice': '현재 화면에서 조회한 트렌드가 없습니다. 자동 API 호출 없이 기본 분석만 표시합니다.'}
    if trend:
        aligned = (trend['start'],trend['end'],trend['split']) == (prior[0],current['end'],current['start'])
        trend_info.update(state='available', start=trend['start'], end=trend['end'], fetched_at=trend.get('fetched_at'),
                          aligned=aligned, demo=bool(trend.get('demo')),
                          notice='트렌드는 주제군 단위 상대지수, 블로그 값은 실제 유입 비율입니다. 기간이 일치해도 인과관계나 성과를 입증하지 않습니다.' if aligned else '주간 자료와 트렌드의 두 7일 구간이 일치하지 않습니다. 직접 비교를 보류합니다.')
        for group in trend['groups']:
            group_rules = {key for k in group['keywords'] for key,_,_,_ in rule_matches(k)}
            matching = []
            for row in rows:
                exact = any(normalized(row['raw_text'])==normalized(k) for k in group['keywords'])
                shared = sorted(set(row['rule_keys']) & group_rules)
                if exact or shared:
                    matching.append({'raw_text': row['raw_text'], 'ratio': row['ratio'],
                                     'basis': '조회 구성어와 공백 제외 일치' if exact else '공통 분류 규칙이 있는 관련 주제 · 동일 검색어 아님'})
            trend_info['groups'].append({'name':group['name'], 'version':group['version'], 'keywords':group['keywords'],
                'previous_mean': group['previous_mean'], 'recent_mean': group['recent_mean'],
                'matching': matching, 'warnings': group.get('warnings', []),
                'notice': '조회 당시 주제군 구성 기준입니다. 일별 누락이 있으면 평균을 계산하지 않습니다. 관련 표현에 주제군 지수를 배분하지 않습니다.'})
    result = {'available': True, 'start':current['start'], 'end':current['end'], 'revision':current['revision'],
            'coverage':scope, 'declared_coverage':current.get('coverage','unknown'), 'coverage_label':COVERAGE_LABELS[scope],
            'previous_coverage_label':COVERAGE_LABELS[prior_scope] if prior_scope else '이전 주 미등록',
            'previous_start':prior[0], 'previous_end':prior[1], 'has_previous':previous is not None,
            'metrics':metrics, 'explanations':explanations, 'caveats':caveats, 'comparisons':observed,
            'topics':rows, 'topic_groups':[{'name':name, 'rows':members} for name,members in groups.items()],
            'candidates':candidates, 'trend':trend_info, 'rule_version':RULE_VERSION,
            'source':current.get('source'), 'warnings':current.get('warnings',[])}
    result['briefing'] = build_briefing(result, [classify(row,topics) for row in (previous or {}).get('rows',[])], previous, SMALL_VIEWS)
    return result
