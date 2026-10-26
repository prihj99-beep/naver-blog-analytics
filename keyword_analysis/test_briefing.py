"""Briefing presentation and export audit. No writes to production statistics."""
import copy
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import unittest

from keyword_analysis.briefing import build_briefing
from keyword_analysis.export import build_export
from keyword_analysis.model import Topics
from keyword_analysis.test_export import rows
from keyword_analysis.test_server_version import REAL_SAMPLES
from keyword_analysis.weekly import WeeklyStore, parse_text, set_coverage
from keyword_analysis.weekly_analysis import analyze, classify, SMALL_VIEWS

ROOT=Path(__file__).resolve().parents[1]


def reports():
    result=[]
    for *_,text in REAL_SAMPLES:
        r=set_coverage(parse_text(text),'unknown');r.update(revision=1,saved_at='2026-10-19T00:00:00+00:00')
        result.append(r)
    return result


class BriefingTests(unittest.TestCase):
    def test_latest_real_summary_and_caveats_are_plain_and_accurate(self):
        result=analyze(reports(),'2026-10-12')
        brief=result['briefing']
        self.assertEqual(len(brief['summary']),3)
        self.assertIn('27회',brief['summary'][0]);self.assertIn('22회',brief['summary'][0])
        self.assertIn('80%',brief['summary'][1]);self.assertIn('55.56%',brief['summary'][1])
        for word in ('관측 표현','규칙 기반 분류','목록 범위 미확인'):
            self.assertNotIn(word,str(brief))
        self.assertEqual(result['metrics']['search_rate']['difference'],-24.44)
        self.assertTrue(any('조회수가 적어' in n for n in brief['notices']))

    def test_all_keywords_retained_and_original_ratios_never_merged(self):
        data=reports(); before=copy.deepcopy(data)
        result=analyze(data,'2026-10-12')
        self.assertEqual(data,before)
        words=[w for g in result['briefing']['themes'] for w in g['words']]
        self.assertEqual(sorted(words),sorted(r['raw_text'] for r in data[-1]['rows']))
        counts={g['name']:len(g['words']) for g in result['briefing']['themes']}
        self.assertEqual(counts,{'경매 입찰보증금·수표 준비':4,'입찰표·입찰봉투 준비':3,'경매 수익 계산·엑셀':2,'아파트 경매 후기':1,'경매 임장':1,'지역 투자 관심':1})
        self.assertEqual(len(words),len(set(words)))
        self.assertEqual([r['ratio'] for r in result['topics']],['7.41']*3+['3.70']*9)

    def test_topic_repetition_is_classification_not_demand_growth(self):
        brief=analyze(reports(),'2026-10-12')['briefing']
        labels={g['name']:g['status'] for g in brief['themes']}
        self.assertIn('지난주에도',labels['입찰표·입찰봉투 준비'])
        self.assertIn('지난주에도',labels['경매 입찰보증금·수표 준비'])
        self.assertTrue(any('검색 수요가 늘었다는 뜻은 아니' in c for c in brief['changes']))

    def test_specific_comparisons_separate_identical_words_from_related_topics(self):
        changes=analyze(reports(),'2026-10-12')['briefing']['changes']
        self.assertIn('지난주 5회에서 이번 주 27회로 22회 늘었어요.',changes)
        self.assertTrue(any('지난주 80%에서 이번 주 55.56%' in c and '횟수' in c for c in changes))
        self.assertIn('원문이 같은 검색어는 두 주 사이에 없어요.',changes)
        repeated=[c for c in changes if c.startswith('비슷한 주제의 반복')]
        self.assertTrue(any('경매 수표 준비' in c and '경매 입찰 수표 쓰는법' in c for c in repeated))
        self.assertTrue(any('대구지방법원 입찰봉투' in c and '경매 입찰표 작성' in c for c in repeated))
        self.assertTrue(any('새로 보인 관심사' in c and '엑셀 경매 투자 수익' in c for c in changes))

    def test_future_expressions_ambiguity_and_managed_topics_remain_separate(self):
        from keyword_analysis.briefing import group_key
        examples={'경매 수표 발급 방법':'deposit','입찰표 작성 순서':'documents',
                  '경매 수익률 계산표':'returns','부동산 경매 엑셀 서식':'returns',
                  '아파트 경매 경험 공유':'review','경매 임장 체크리스트':'visit',
                  '수원시 투자':'local'}
        for text,key in examples.items():
            self.assertEqual(classify({'raw_text':text,'ratio':'1.20'},[])['rule_keys'],[key])
        ambiguous=classify({'raw_text':'경매 수표 입찰봉투','ratio':'1.20'},[])
        self.assertEqual(group_key(ambiguous),'복수 분류 후보')
        topic={'id':'custom','name':'마음대로 묶은 주제','version':3,'keywords':['뜻이 불분명한 질문']}
        before=copy.deepcopy(topic)
        unclassified=classify({'raw_text':'뜻이 불분명한 질문','ratio':'1.20'},[topic])
        self.assertTrue(unclassified['topic_refs'])
        self.assertEqual(group_key(unclassified),'미분류')
        self.assertEqual(topic,before)
        data=reports(); data[-1]['rows']=[{'raw_text':'경매 수표 입찰봉투','ratio':'1.20','detail_url':None}]
        result=analyze(data,'2026-10-12')
        self.assertEqual(result['candidates'],[])
        self.assertTrue(result['briefing']['themes'][0]['other'])
        self.assertIn('한 가지 관심사로 묶은 목록이 아니',result['briefing']['themes'][0]['description'])

    def test_actions_limited_and_ordered_for_review_not_predicted_performance(self):
        data=reports(); result=analyze(data,'2026-10-12')
        brief=result['briefing']
        self.assertLessEqual(len(brief['actions']),3)
        self.assertIn(brief['actions'][0]['topic'],('입찰표·입찰봉투 준비','경매 입찰보증금·수표 준비'))
        self.assertIn('성과 예상 순위는 아니',brief['action_note'])
        for item in brief['actions']:
            self.assertTrue(set(item['evidence']) <= {r['raw_text'] for r in data[-1]['rows']})
        expanded=copy.deepcopy(result)
        expanded['candidates']*=4
        self.assertLessEqual(len(build_briefing(expanded,[],data[-2],SMALL_VIEWS)['actions']),3)

    def test_core_changes_and_article_topics_for_all_three_real_weeks(self):
        data=reports()
        for report in data:
            briefing=analyze(data,report['start'])['briefing']
            self.assertLessEqual(len(briefing['highlights']),3)
            self.assertLessEqual(len(briefing['actions']),3)
            self.assertEqual(len({a['topic'] for a in briefing['actions']}),len(briefing['actions']))
            for action in briefing['actions']:
                self.assertNotIn('질문 정리',action['title'])
                self.assertNotIn('항목 정리',action['title'])
                self.assertTrue(action['checks'])
                self.assertTrue(set(action['evidence']) <= {r['raw_text'] for r in report['rows']})
        brief=analyze(data,'2026-10-12')['briefing']
        self.assertEqual([h['title'] for h in brief['highlights']],['조회수 변화','이어지는 관심사','새롭게 눈에 띄는 관심사'])
        self.assertIn('경매 수표 준비',brief['highlights'][1]['description'])
        self.assertIn('경매 입찰 수표 쓰는법',brief['highlights'][1]['description'])
        self.assertIn('엑셀 경매 투자 수익',brief['highlights'][2]['description'])
        self.assertEqual(brief['highlights'][0]['value'],'+22회')
        self.assertEqual(brief['highlights'][0]['caption'],'지난주 5회 → 이번 주 27회')
        self.assertEqual(brief['highlights'][2]['value'],'경매 수익 계산·엑셀')
        self.assertIn('관련 검색어 2개',brief['highlights'][2]['caption'])
        self.assertEqual([a['topic'] for a in brief['actions']],['경매 입찰보증금·수표 준비','입찰표·입찰봉투 준비','경매 수익 계산·엑셀'])
        self.assertIn('지난주',brief['actions'][0]['why_first'])

    def test_one_week_empty_week_and_insufficient_topic_evidence(self):
        data=reports()
        one=analyze(data[:1],data[0]['start'])['briefing']
        self.assertEqual(len(one['highlights']),1)
        self.assertTrue(all('한 주 자료' in a['why_first'] for a in one['actions']))
        data[-1]['rows']=[]
        empty=analyze(data,'2026-10-12')['briefing']
        self.assertEqual(empty['actions'],[])
        self.assertIn('검색어가 없는 주',empty['highlights'][1]['description'])
        self.assertFalse(any('새롭게' in h['title'] for h in empty['highlights']))
        data=reports();data[-2]['rows']=[]
        previous_empty=analyze(data,'2026-10-12')['briefing']
        self.assertTrue(all('비교할 수 없어요' in a['why_first'] for a in previous_empty['actions']))
        data[-1]['rows']=[{'raw_text':'아파트 경매 후기','ratio':'7.41','detail_url':None}]
        self.assertEqual(analyze(data,'2026-10-12')['briefing']['actions'],[])

    def test_recommendation_priority_does_not_follow_keyword_count(self):
        data=reports()
        for n in range(20):
            data[-1]['rows'].append({'raw_text':f'경매 엑셀 질문 {n}','ratio':'1.00','detail_url':None})
        actions=analyze(data,'2026-10-12')['briefing']['actions']
        self.assertEqual(actions[0]['topic'],'경매 입찰보증금·수표 준비')
        self.assertEqual(actions[1]['topic'],'입찰표·입찰봉투 준비')

    def test_first_week_and_missing_metrics_not_filled_with_zero(self):
        data=reports(); data[0]['channel_views']=None
        brief=analyze(data,data[0]['start'])['briefing']
        self.assertIn('없어요',brief['summary'][0]);self.assertIsNone(brief['counts'])
        self.assertTrue(any('지난주 자료가 없어' in n for n in brief['notices']))

    def test_zero_and_unchanged_values_are_not_missing(self):
        data=reports()
        for r in data:r['channel_views']=0;r['search_rate']='0'
        brief=analyze(data,'2026-10-12')['briefing']
        self.assertIn('0회',brief['summary'][0]);self.assertIn('같아요',brief['summary'][0])
        self.assertIn('0%',brief['summary'][1])

    def test_more_warning_and_coverage_warning_not_repeated(self):
        data=reports(); data[-1]['has_more']=True;data[-1]['coverage']='full'
        brief=analyze(data,'2026-10-12')['briefing']
        self.assertEqual(sum('더보기' in n for n in brief['notices']),1)
        self.assertFalse(any('모두 복사했는지는' in n for n in brief['notices']))

    def test_additional_presentation_does_not_mutate_existing_analysis(self):
        data=reports(); result=analyze(data,'2026-10-12');before=copy.deepcopy(result)
        build_briefing(result,[classify(r,[]) for r in data[-2]['rows']],data[-2],SMALL_VIEWS)
        self.assertEqual(result,before)


class SavedExportAuditTests(unittest.TestCase):
    def test_actual_saved_weeks_and_export_are_complete_and_read_only(self):
        path=ROOT/'data/creator-advisor-weekly.sqlite3'
        if not path.is_file(): self.skipTest('User weekly database not available')
        paths=list((ROOT/'data').glob('*.sqlite3'))
        before=[hashlib.sha256(p.read_bytes()).digest() for p in paths]
        saved=WeeklyStore(path).list()
        with closing(sqlite3.connect((ROOT/'data/keyword-analysis.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');topics=Topics.rows(db)
        content,_=build_export(ROOT/'data/analytics.sqlite3','2026-10-18',topics,weekly=saved)
        records=rows(content,4)[1:]
        self.assertEqual(len(records),sum(len(r['rows']) for r in saved))
        self.assertEqual(len({(r[0],r[1],r[6]) for r in records}),len(records))
        for report in saved:
            matched=[r for r in records if r[:2]==[report['start'],report['end']]]
            self.assertEqual({(r[6],r[7]) for r in matched},{(r['raw_text'],r['ratio']) for r in report['rows']})
            self.assertTrue(all(r[3]==str(report['revision']) and r[11]==report['source_hash'] and r[9] for r in matched))
        self.assertIn('목록 범위 사용자 선택',rows(content,5)[0])
        self.assertIn('더보기 표시',rows(content,5)[0])
        self.assertEqual(before,[hashlib.sha256(p.read_bytes()).digest() for p in paths])


if __name__=='__main__': unittest.main()
