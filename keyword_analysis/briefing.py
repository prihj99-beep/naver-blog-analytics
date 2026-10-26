"""Plain-language presentation of existing analysis; no new statistical model."""
from .weekly import effective_coverage

NAMES = {
    '입찰 준비 · 보증금/수표': '경매 입찰보증금·수표 준비',
    '입찰 준비 · 서류/절차': '입찰표·입찰봉투 준비',
    '경매 물건 탐색': '경매 물건 찾기',
    '경매 자금/대출': '경매 자금과 대출',
    '권리관계 확인': '권리관계 확인',
    '지분/공유관계': '지분과 공유관계',
    '미분류': '아직 분류하지 않은 검색어',
    '복수 분류 후보': '여러 주제에 걸친 검색어',
    '복수 주제군 후보': '여러 주제에 걸친 검색어',
}


def friendly_name(name):
    if name.startswith('저장 주제군: '):
        import re
        return re.sub(r' · v\d+$','',name.removeprefix('저장 주제군: '))
    return NAMES.get(name, name)


def group_key(row):
    # User-managed DataLab topics are references only, never automatic intent buckets.
    return ('복수 분류 후보' if len(row['rule_keys'])>1 else
            row['labels'][0] if row['rule_keys'] else '미분류')


DESCRIPTIONS = {
    '입찰 준비 · 보증금/수표':'입찰보증금이나 수표를 준비하는 방법을 찾은 검색어예요.',
    '입찰 준비 · 서류/절차':'입찰표·봉투 등 입찰에 필요한 서류를 찾은 검색어예요.',
    '경매 수익 계산·엑셀':'경매 투자 수익을 계산하거나 엑셀 자료를 찾은 검색어예요.',
    '아파트 경매 후기':'아파트 경매를 경험한 이야기를 찾은 검색어예요.',
    '경매 임장':'경매 현장을 방문할 때의 행동이나 준비를 찾은 검색어예요.',
    '지역 투자 관심':'지역명과 투자가 함께 나왔어요. 어떤 투자 대상을 뜻하는지는 확인이 필요해요.',
}


def quoted(words, limit=2):
    return ' · '.join('“'+word+'”' for word in words[:limit])


# Editorial templates are questions for future articles, never invented experience/results.
ARTICLE_IDEAS = {
    '입찰 준비 · 보증금/수표': (4, '경매 입찰보증금 수표, 준비 전에 무엇을 확인할까?', '은행·법원의 최신 안내를 확인하고, 기존 수표 준비 글과 겹치는지 살펴보세요.'),
    '입찰 준비 · 서류/절차': (3, '경매 입찰표와 입찰봉투, 무엇을 준비해야 할까?', '기존 입찰 서류 글과 중복되는지, 해당 법원의 최신 서식·안내와 맞는지 확인하세요.'),
    '경매 수익 계산·엑셀': (4, '경매 수익 계산 엑셀, 어떤 비용 항목부터 확인할까?', '기존 엑셀 글과 중복되는지 확인하세요. 실제 계산 결과나 무료 파일이 있다고 가정하지 말고, 사용할 자료부터 확인하세요.'),
    '경매 임장': (2, '경매 임장 전, 현장 방문에서 무엇을 확인해야 할까?', '기존 임장 글과 겹치는지 확인하세요. 현장 경험은 실제 기록이 있을 때만 쓰고 방문 시 지켜야 할 사항은 공식 안내로 확인하세요.'),
}
UNRESOLVED = {'미분류', '복수 분류 후보', '복수 주제군 후보'}


def grouped_previous_keywords(rows):
    groups = {}
    for row in rows:
        name = group_key(row)
        if name not in UNRESOLVED:
            groups.setdefault(name, []).append(row['raw_text'])
    return groups


def article_recommendations(data, old_groups, has_previous, previous_has_words=True):
    eligible = [g for g in data['topic_groups'] if g['name'] in ARTICLE_IDEAS]
    # Repetition first, then concrete supported intent. Row count/ratio is not a score.
    eligible.sort(key=lambda g: (g['name'] not in old_groups, -ARTICLE_IDEAS[g['name']][0], g['name']))
    actions = []
    for group in eligible[:3]:
        name = group['name']
        words = [r['raw_text'] for r in group['rows']]
        _, title, check = ARTICLE_IDEAS[name]
        if name == '입찰 준비 · 보증금/수표' and any('쓰는법' in w.replace(' ','') for w in words):
            title = '경매 입찰 수표, 준비부터 작성까지 무엇을 확인할까?'
        if name == '입찰 준비 · 서류/절차' and not any('입찰표' in w.replace(' ','') for w in words):
            title = '경매 입찰봉투, 어디서 준비하고 어떻게 사용할까?'
        if name in old_groups:
            reason = f"지난주 {quoted(old_groups[name],1)}에 이어 이번 주 {quoted(words,1)}가 있어요. 두 주에 이어진 관심사를 한 글에서 다룰 후보예요."
        elif has_previous and not previous_has_words:
            reason = f"이번 주 복사 목록의 {quoted(words)}가 근거예요. 지난주 목록에 검색어가 없어 새 관심사인지는 비교할 수 없어요."
        elif has_previous:
            reason = f"이번 주 복사 목록의 {quoted(words)}가 근거예요. 지난주 목록에는 이 주제로 묶인 검색어가 없었어요."
        else:
            reason = f"이번 주 복사 목록의 {quoted(words)}가 근거예요. 한 주 자료이므로 반복 관심인지는 아직 알 수 없어요."
        actions.append({'title':title, 'topic':friendly_name(name), 'evidence':words, 'why_first':reason, 'checks':check,
                        'short_reason': '두 주에 이어진 관련 검색어가 있어요.' if name in old_groups else '이번 주 복사 목록에서 확인한 관심사예요.'})
    return actions


def core_changes(data, old_groups, previous):
    views = data['metrics']['channel_views']
    if views['difference'] is not None:
        delta = views['difference']
        text = (f"지난주 {views['previous']}회에서 이번 주 {views['current']}회로 {abs(delta)}회 {'늘었어요' if delta>0 else '줄었어요'}." if delta else
                f"지난주와 이번 주 모두 {views['current']}회로 같아요.")
    else:
        text = ('이번 주 조회수도 복사 자료에서 확인되지 않았어요.' if views['current'] is None else f"이번 주는 {views['current']}회예요.") + ' 지난주와 비교할 자료가 부족해 변화는 설명할 수 없어요.'
    highlights = [{'title':'조회수 변화', 'description':text,
                   'value': f"{views['difference']:+}회" if views['difference'] is not None else None,
                   'caption':f"지난주 {views['previous']}회 → 이번 주 {views['current']}회" if views['difference'] is not None else text}]
    if not previous:
        return highlights
    if not previous['rows'] or not data['topics']:
        highlights.append({'title':'관심사 비교', 'description':'검색어가 없는 주가 있어 이어지는 관심사와 새 관심사는 비교하기 어려워요. 복사한 목록이 비어 있다는 뜻이며, 실제 검색 유입이 없었다고 단정하지 않아요.'})
        return highlights
    known = [g for g in data['topic_groups'] if g['name'] not in UNRESOLVED]
    recurring = [g for g in known if g['name'] in old_groups]
    if recurring:
        text = ' '.join(f"{friendly_name(g['name'])}는 지난주 {quoted(old_groups[g['name']],1)}, 이번 주 {quoted([r['raw_text'] for r in g['rows']],1)}처럼 두 주의 복사 목록에 이어서 나왔어요." for g in recurring[:2])
    else:
        text = '두 주의 복사 목록에서 이어지는 주제를 확인하지 못했어요. 실제 관심이 사라졌다는 뜻은 아니에요.'
    highlights.append({'title':'이어지는 관심사', 'description':text,
                       'value':friendly_name(recurring[0]['name']) if recurring else None,
                       'caption':'두 주의 복사 목록에 관련 검색어가 있어요.' if recurring else text})
    new = [g for g in known if g['name'] not in old_groups]
    new.sort(key=lambda g: (-ARTICLE_IDEAS.get(g['name'],(0,))[0],g['name']))
    if new:
        group = new[0]
        text = f"이번 주 복사 목록에는 {quoted([r['raw_text'] for r in group['rows']])}가 있어 {friendly_name(group['name'])}가 눈에 띄어요. 지난주 복사 목록에는 이 주제로 묶인 검색어가 없었지만, 검색 수요가 늘었다는 뜻은 아니에요."
    else:
        text = '지난주 복사 목록과 비교해 새 관심사로 소개할 근거가 충분하지 않아요.'
    highlights.append({'title':'새롭게 눈에 띄는 관심사', 'description':text,
                       'value':friendly_name(new[0]['name']) if new else None,
                       'caption':f"지난주 복사 목록에는 없고 이번 주 관련 검색어 {len(new[0]['rows'])}개가 있어요." if new else text})
    return highlights


def build_briefing(data, previous_rows, previous, small_views):
    metrics = data['metrics']
    views, rate = metrics['channel_views'], metrics['search_rate']
    summary = []
    if views['current'] is None:
        summary.append('이번 주 조회수는 복사한 자료에 없어요.')
    elif views['difference'] is None:
        summary.append(f"이번 주 블로그 조회수는 {views['current']}회예요.")
    else:
        delta = views['difference']
        summary.append(f"이번 주 블로그 조회수는 {views['current']}회로, 지난주보다 {abs(delta)}회 {'늘었어요' if delta>0 else '줄었어요'}." if delta else f"이번 주 블로그 조회수는 {views['current']}회로, 지난주와 같아요.")
    if rate['current'] is None:
        summary.append('검색을 통한 유입 비율은 복사한 자료에 없어요.')
    elif rate['difference'] is None:
        summary.append(f"검색을 통한 유입 비율은 {rate['current']}%예요.")
    else:
        summary.append(f"검색을 통한 유입 비율은 지난주 {rate['previous']}%에서 이번 주 {rate['current']}%로 바뀌었어요.")
    previous_names = {group_key(row) for row in previous_rows}
    themes = []
    for group in data['topic_groups']:
        name = group['name']
        other = name in ('미분류', '복수 분류 후보', '복수 주제군 후보')
        status = ('아직 한 주제로 묶기 어려워요' if other else '지난주 자료가 없어 첫 등장 여부는 알 수 없어요' if not previous
                  else '지난주에도 같은 주제로 묶인 검색어가 있었어요' if name in previous_names
                  else '지난주 목록에는 같은 주제로 묶인 검색어가 없었어요')
        themes.append({'name':friendly_name(name), 'status':status, 'other':other,
                       'description': DESCRIPTIONS.get(name, '한 가지 관심사로 묶은 목록이 아니에요. 검색어마다 뜻을 따로 확인해 주세요.' if other else '표현이 비슷한 검색어를 자동으로 묶었어요. 실제 의도는 확인이 필요해요.'),
                       'words':[r['raw_text'] for r in group['rows']]})
    known = [theme['name'] for theme in themes if not theme['other']]
    if known:
        summary.append('실제 유입 검색어를 자동으로 묶으면 ‘' + '’, ‘'.join(known[:3]) + '’ 등의 주제로 나뉘어요. 이 이름은 검색어 원문이 아닌 분류용 제목이에요.')
    else:
        summary.append('아래에서 검색어를 빠짐없이 살펴볼 수 있어요.')

    notices = []
    if any('더보기' in warning for warning in data['caveats']):
        notices.append('‘더보기’가 남아 있어 일부 검색어가 빠졌을 수 있어요.')
    elif data['coverage'] != 'full' or (previous and effective_coverage(previous) != 'full'):
        notices.append('검색어를 모두 복사했는지는 확인되지 않았어요. 목록 안에서 보이거나 빠진 검색어만 비교해요.')
    else:
        notices.append('전체 목록은 사용자가 확인한 정보예요. 프로그램이 빠진 항목까지 확인한 것은 아니에요.')
    if not previous:
        notices.append('지난주 자료가 없어 이번 주 내용만 보여드려요.')
    for value,label in [(views,'조회수'),(rate,'검색 유입률')]:
        if value['current'] is not None and previous and value['previous'] is None:
            notices.append(f'지난주 {label} 정보가 빠져 있어 이 항목은 비교할 수 없어요.')
    if any(v is not None and v < small_views for v in (views['current'],views['previous'])):
        notices.append('조회수가 적어 작은 차이도 크게 느껴질 수 있어요. 이번 변화만으로 글의 효과나 검색 수요를 판단하기는 어려워요.')
    if any('채널이 미확인' in warning for warning in data['caveats']):
        notices.append('어느 블로그의 자료인지 확인되지 않은 주가 있어요. 같은 블로그 자료인지 확인해 주세요.')
    # Copied auxiliary-area notices remain in detailed warnings, not the main briefing.

    counts = {'new':0, 'repeat':0, 'missing':0}
    for row in data['comparisons']:
        if row['status']=='신규 관측(목록 기준)': counts['new']+=1
        elif row['status']=='반복': counts['repeat']+=1
        elif row['status']=='이번 목록에서 미확인': counts['missing']+=1
    changes = []
    if previous:
        if views['difference'] is not None:
            delta = views['difference']
            changes.append(f"지난주 {views['previous']}회에서 이번 주 {views['current']}회로 {abs(delta)}회 {'늘었어요' if delta>0 else '줄었어요'}." if delta else f"조회수는 지난주와 이번 주 모두 {views['current']}회예요.")
        if rate['difference'] is not None:
            changes.append(f"검색 유입률은 지난주 {rate['previous']}%에서 이번 주 {rate['current']}%로 바뀌었어요. 이 비율만으로 검색 유입 횟수가 늘거나 줄었다고 볼 수는 없어요.")
        changes.append(f"새로 보인 검색어 {counts['new']}개, 두 주 모두 보인 검색어 {counts['repeat']}개, 이번 목록에 없는 검색어 {counts['missing']}개예요.")
        repeated = [r['raw_text'] for r in data['comparisons'] if r['status']=='반복']
        changes.append('원문이 같은 검색어는 ' + (quoted(repeated) + ' 등이 두 주에 모두 있어요.' if repeated else '두 주 사이에 없어요.'))
        previous_groups = {}
        for row in previous_rows:
            previous_groups.setdefault(group_key(row), []).append(row['raw_text'])
        for group in data['topic_groups']:
            name = group['name']
            if name in ('미분류','복수 분류 후보','복수 주제군 후보'): continue
            words = [r['raw_text'] for r in group['rows']]
            if name in previous_groups:
                changes.append(f"비슷한 주제의 반복 — {friendly_name(name)}: 지난주 {quoted(previous_groups[name])}, 이번 주 {quoted(words)}가 있어요. 주제가 비슷하다는 뜻이며 원문이 같은지는 별도로 비교해요.")
            else:
                changes.append(f"이번 주 목록에 새로 보인 관심사 — {friendly_name(name)}: {quoted(words)}. 지난주 목록에는 이 주제로 묶인 검색어가 없었어요.")
        changes.append('검색어가 새로 보였다는 뜻은 검색 수요가 늘었다는 뜻은 아니고, 목록에 없다고 실제 유입이 없었다는 뜻도 아니에요.')
    else:
        changes.append('지난주를 저장하면 조회수·검색 유입률과 검색어 목록을 나란히 비교할 수 있어요.')
    old_groups = grouped_previous_keywords(previous_rows)
    actions = article_recommendations(data, old_groups, previous is not None, bool((previous or {}).get('rows')))
    highlights = core_changes(data, old_groups, previous)
    return {'summary':summary, 'notices':list(dict.fromkeys(notices)), 'themes':themes,
            'changes':changes, 'highlights':highlights, 'counts':counts if previous else None, 'actions':actions,
            'action_note':'모두 발행 검토 후보예요. 콘텐츠대장과 연결되지 않아 기존 글의 유무나 발행 효과는 알 수 없으며, 성과 예상 순위는 아니에요.'}
