"""Offline unit + loopback HTTP tests. Every external API transport is a fake."""
import copy
from contextlib import closing
import datetime as dt
import hashlib
import http.client
import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from keyword_analysis.model import Conflict, Topics, clean_group, inbound_keywords
from keyword_analysis.server import AnalysisServer, DemoClient, launch
from keyword_analysis.trends import ENDPOINT, TrendClient, TrendError, compare, credentials, make_request

ROOT = Path(__file__).resolve().parents[1]
END = '2026-10-18'
TOPICS = [dict(id='topic-one', version=1, name='입찰 준비', keywords=['입찰보증금','입찰 수표'])]


def fixture(request=None):
    request = request or make_request(TOPICS, END, today=dt.date(2026,10,19))
    first = dt.date.fromisoformat(request['startDate'])
    return {**{k:request[k] for k in ('startDate','endDate','timeUnit')},
            'results':[dict(title=g['groupName'], keywords=g['keywords'],
                data=[dict(period=str(first+dt.timedelta(days=i)),ratio=20 if i<7 else 40) for i in range(14)])
                       for g in request['keywordGroups']]}


class FakeOpener:
    def __init__(self, result=None, error=None, raw=None):
        self.raw = raw if raw is not None else json.dumps(result or fixture()).encode()
        self.error, self.calls = error, []

    def open(self, request, timeout):
        self.calls.append((request,timeout))
        if self.error:
            raise self.error
        return io.BytesIO(self.raw)


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT/'.setup-tmp')
        self.root = Path(self.temp.name)
        self.store = Topics(self.root/'groups.sqlite3')

    def tearDown(self):
        self.temp.cleanup()

    def test_create_update_reopen_delete_and_version_history(self):
        a = self.store.save(' 주제 ', [' 원문 A ','B'])
        b = self.store.save('수정', ['C'], a['id'], 1)
        self.assertEqual((a['version'], b['version']), (1,2))
        self.assertEqual(a['keywords'], ['원문 A','B'])
        self.assertEqual(Topics(self.store.path).list()[0]['name'], '수정')
        self.store.delete(a['id'], 2)
        self.assertEqual(self.store.list(), [])
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute('SELECT version FROM topic_versions ORDER BY version').fetchall(), [(1,),(2,)])
            self.assertEqual({r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}, {'topics','topic_versions'})

    def test_stale_update_delete_and_selection_are_rejected(self):
        a=self.store.save('주제',['a'])
        self.store.save('새 주제',['b'],a['id'],1)
        with self.assertRaises(Conflict):self.store.save('이전 요청',['c'],a['id'],1)
        with self.assertRaises(Conflict):self.store.delete(a['id'],1)
        with self.assertRaises(Conflict):self.store.selected([{'id':a['id'],'version':1}])
        self.assertEqual(self.store.list()[0]['keywords'],['b'])

    def test_invalid_keywords_names_selection_and_duplicates(self):
        for name, words in [('', ['a']),('x',[]),('x',['a']*2),('x',list('abcdef')),('x',[None]),('x',[' '*3]),('x',['a'*201])]:
            with self.subTest(name=name,words=words), self.assertRaises(ValueError):clean_group(name,words)
        a=self.store.save('주제',['a'])
        with self.assertRaises(ValueError):self.store.save('주제',['b'])
        for refs in [[],[{'id':a['id'],'version':1}]*2,[a['id']]]:
            with self.assertRaises(ValueError):self.store.selected(refs)

    def test_real_inbound_readonly_seven_rows_hash_and_schema_unchanged(self):
        path=ROOT/'data/analytics.sqlite3'
        before=hashlib.sha256(path.read_bytes()).hexdigest()
        connect=sqlite3.connect
        seen=[]
        def checked(*args,**kwargs):
            seen.append((args,kwargs))
            return connect(*args,**kwargs)
        with patch('keyword_analysis.model.sqlite3.connect',side_effect=checked):
            result=inbound_keywords(path,'2026-10-12','2026-10-15')
        self.assertEqual(len(result['rows']),7)
        self.assertEqual([d['status'] for d in result['days']],['collected','uncollected','uncollected','collected'])
        self.assertTrue(seen[0][0][0].endswith('?mode=ro'))
        self.assertTrue(seen[0][1]['uri'])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)
        self.assertEqual(len(inbound_keywords(path,'2026-10-12','2026-10-15','수표')['rows']),2)

    def test_missing_db_not_created_and_empty_collected_day_distinct(self):
        missing=self.root/'missing.sqlite3'
        self.assertFalse(inbound_keywords(missing,END,END)['available'])
        self.assertFalse(missing.exists())
        path=self.root/'existing.sqlite3'
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript('CREATE TABLE reports(date TEXT,sha256 TEXT); CREATE TABLE entries(date TEXT,category TEXT,position INTEGER,raw_text TEXT,ratio TEXT); INSERT INTO reports VALUES ("2026-10-12","hash");')
        self.assertEqual([r['status'] for r in inbound_keywords(path,'2026-10-12','2026-10-13')['days']],['empty','uncollected'])

    def test_user_topic_copy_update_preserves_original_db(self):
        source=ROOT/'data/keyword-analysis.sqlite3'
        before=source.read_bytes()
        target=self.root/'user-topics-copy.sqlite3'
        shutil.copy2(source,target)
        copied=Topics(target)
        original=next((t for t in copied.list() if t['name']=='경매 입찰 준비'),None)
        if original is None:self.skipTest('User topic not present')
        changed=copied.save(original['name'],['모의 사본에서만 변경'],original['id'],original['version'])
        self.assertEqual(changed['version'],original['version']+1)
        self.assertEqual(source.read_bytes(),before)


class CalculationTests(unittest.TestCase):
    def setUp(self):
        self.request=make_request(TOPICS,END,today=dt.date(2026,10,19))

    def analyze(self, response):
        return compare(self.request,response,TOPICS)['groups'][0]

    def test_one_fourteen_day_response_means_delta_percent(self):
        r=self.analyze(fixture(self.request))
        self.assertEqual((self.request['startDate'],self.request['endDate'],self.request['timeUnit']),('2026-10-05',END,'date'))
        self.assertEqual((r['previous_mean'],r['recent_mean'],r['delta'],r['change_percent']),(20,40,20,100))
        self.assertEqual((r['previous_count'],r['recent_count']),(7,7))

    def test_zero_and_small_denominator_hide_growth(self):
        for value in [0,.01,1]:
            data=fixture(self.request)
            for p in data['results'][0]['data'][:7]:p['ratio']=value
            r=self.analyze(data)
            self.assertIsNone(r['change_percent'])
            self.assertTrue(r['warnings'])
            self.assertAlmostEqual(r['delta'],40-value)

    def test_gap_or_null_not_zero_not_partial_mean(self):
        for is_null in [True,False]:
            data=fixture(self.request)
            if is_null:data['results'][0]['data'][2]['ratio']=None
            else:del data['results'][0]['data'][2]
            r=self.analyze(data)
            self.assertIsNone(r['points'][2]['ratio'])
            self.assertEqual(r['previous_count'],6)
            self.assertIsNone(r['previous_mean'])
            self.assertEqual(r['recent_mean'],40)
            self.assertIsNone(r['delta'])
            self.assertIsNone(r['change_percent'])

    def test_missing_group_or_data_preserves_fourteen_gaps(self):
        for mode in ['group','data','empty']:
            data=fixture(self.request)
            if mode=='group':data['results']=[]
            elif mode=='data':del data['results'][0]['data']
            else:data['results'][0]['data']=[]
            r=self.analyze(data)
            self.assertEqual(len(r['points']),14)
            self.assertTrue(all(p['ratio'] is None for p in r['points']))
            self.assertIsNone(r['change_percent'])

    def test_zero_series_is_distinct_from_missing(self):
        data=fixture(self.request)
        for p in data['results'][0]['data']:p['ratio']=0
        r=self.analyze(data)
        self.assertEqual((r['previous_count'],r['recent_count'],r['delta']),(7,7,0))
        self.assertEqual(r['points'][0]['ratio'],0)

    def test_wrong_provenance_duplicate_day_invalid_ratio_rejected(self):
        for mode in ['date','unit','words','duplicate','range','nan','bool','string','extra_group','missing_results']:
            data=fixture(self.request)
            if mode=='date':data['startDate']='2026-10-04'
            if mode=='unit':data['timeUnit']='week'
            if mode=='words':data['results'][0]['keywords']=['다른 검색어']
            if mode=='duplicate':data['results'][0]['data'].append(data['results'][0]['data'][0])
            if mode=='range':data['results'][0]['data'][0]['ratio']=101
            if mode=='nan':data['results'][0]['data'][0]['ratio']=float('nan')
            if mode=='bool':data['results'][0]['data'][0]['ratio']=True
            if mode=='string':data['results'][0]['data'][0]['ratio']='20'
            if mode=='extra_group':data['results'].append({'title':'unexpected','keywords':[],'data':[]})
            if mode=='missing_results':del data['results']
            with self.subTest(mode=mode),self.assertRaises(TrendError):self.analyze(data)

    def test_future_today_too_early_invalid_dates(self):
        for day in ['2026-10-19','2026-10-20','2016-01-13','not-a-date',None]:
            with self.assertRaises(ValueError):make_request(TOPICS,day,today=dt.date(2026,10,19))

    def test_reordered_groups_matched_by_name_not_position(self):
        topics=TOPICS+[dict(id='two',version=3,name='대출',keywords=['대출'])]
        req=make_request(topics,END,today=dt.date(2026,10,19))
        data=fixture(req);data['results'].reverse()
        self.assertEqual([g['id'] for g in compare(req,data,topics)['groups']],['topic-one','two'])


class ClientTests(unittest.TestCase):
    def test_request_endpoint_headers_and_single_attempt_with_fake_transport(self):
        opener=FakeOpener()
        client=TrendClient(opener,lambda:('fake-id','fake-secret'))
        req=make_request(TOPICS,END,today=dt.date(2026,10,19))
        self.assertEqual(client.fetch(req),fixture(req))
        self.assertEqual(len(opener.calls),1)
        request,timeout=opener.calls[0]
        self.assertEqual(request.full_url,ENDPOINT)
        self.assertEqual(request.method,'POST')
        self.assertEqual(timeout,20)
        self.assertEqual(json.loads(request.data),req)
        self.assertEqual({k.lower():v for k,v in request.header_items()}['x-ncp-apigw-api-key'],'fake-secret')

    def test_missing_key_makes_no_request(self):
        opener=FakeOpener()
        client=TrendClient(opener,lambda:None)
        with self.assertRaises(TrendError) as c:client.fetch({})
        self.assertEqual(c.exception.code,'missing_key')
        self.assertEqual(opener.calls,[])

    def test_credentials_never_return_partial_or_header_injection(self):
        with patch.dict(os.environ,{},clear=True):self.assertIsNone(credentials())
        with patch.dict(os.environ,{'NAVER_API_HUB_CLIENT_ID':'a','NAVER_API_HUB_CLIENT_SECRET':'bad\nsecret'},clear=True):self.assertIsNone(credentials())
        with patch.dict(os.environ,{'NAVER_API_HUB_CLIENT_ID':'a','NAVER_API_HUB_CLIENT_SECRET':'b'},clear=True):self.assertEqual(credentials(),('a','b'))

    def test_http_auth_quota_redirect_and_server_errors_redacted_without_retry(self):
        for status,expected in [(401,'authentication'),(403,'authentication'),(429,'quota'),(302,'api_error'),(500,'api_error')]:
            error=urllib.error.HTTPError(ENDPOINT,status,'sensitive-secret',{},io.BytesIO(b'sensitive-response'))
            opener=FakeOpener(error=error)
            client=TrendClient(opener,lambda:('fake-id','fake-secret'))
            with self.assertRaises(TrendError) as c:client.fetch({})
            self.assertEqual(c.exception.code,expected)
            self.assertNotIn('sensitive',str(c.exception))
            self.assertEqual(len(opener.calls),1)

    def test_transport_malformed_oversized_and_error_envelopes(self):
        cases=[FakeOpener(error=urllib.error.URLError('fake-secret')),FakeOpener(error=http.client.IncompleteRead(b'fake-secret')),FakeOpener(raw=b'not json fake-secret'),FakeOpener(raw=b'x'*(1024*1024+1)),FakeOpener(result={'errMsg':'fake-secret'}),FakeOpener(raw=b'[]')]
        for opener in cases:
            with self.assertRaises(TrendError) as c:TrendClient(opener,lambda:('a','b')).fetch({})
            self.assertNotIn('fake-secret',str(c.exception))
            self.assertEqual(len(opener.calls),1)


class RecordingClient(DemoClient):
    def __init__(self):self.calls=[]
    def fetch(self, request):
        self.calls.append(copy.deepcopy(request))
        return fixture(request)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'.setup-tmp')
        self.root=Path(self.temp.name)
        (self.root/'data').mkdir()
        shutil.copy2(ROOT/'data/analytics.sqlite3',self.root/'data/analytics.sqlite3')
        self.original=(self.root/'data/analytics.sqlite3').read_bytes()
        self.client=RecordingClient()
        self.server=AnalysisServer(self.root,port=0,client=self.client)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.origin=self.server.origin
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(3)
        self.assertEqual((self.root/'data/analytics.sqlite3').read_bytes(),self.original)
        self.temp.cleanup()

    def call(self,path,payload=None,token=True,origin=None):
        headers={}
        if payload is not None:
            headers={'Content-Type':'application/json','Origin':origin or self.origin}
            if token:headers['X-Analysis-Token']=self.server.token
        request=urllib.request.Request(self.origin+path,data=None if payload is None else json.dumps(payload).encode(),headers=headers)
        try:
            with self.opener.open(request,timeout=5) as r:return r.status,json.load(r),dict(r.headers)
        except urllib.error.HTTPError as e:
            with e:return e.code,json.load(e),dict(e.headers)

    def create(self):
        return self.call('/api/topics/save',dict(name='입찰',keywords=['입찰 수표']))[1]

    def test_startup_get_crud_inbound_no_automatic_external_request(self):
        with self.opener.open(self.origin) as r:
            text=r.read().decode();self.assertIn(self.server.token,text);self.assertEqual(r.headers['Cache-Control'],'no-store')
        self.assertEqual(self.call('/api/state')[0],200)
        topic=self.create()
        self.assertEqual(topic['version'],1)
        self.assertEqual(len(self.call('/api/inbound?start=2026-10-12&end=2026-10-15')[1]['rows']),7)
        self.assertEqual(self.client.calls,[])
        result=self.call('/api/trends',{'selection':[topic],'end':END})
        self.assertEqual(result[0],200)
        self.assertEqual(result[1]['groups'][0]['change_percent'],100)
        self.assertEqual(len(self.client.calls),1)
        self.assertEqual(self.call('/api/trends',{'selection':[topic],'end':END})[0],429)
        self.assertEqual(len(self.client.calls),1)
        self.assertEqual(result[2]['Cache-Control'],'no-store')
        with closing(sqlite3.connect(self.server.topics.path)) as db:
            self.assertEqual({r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")},{'topics','topic_versions'})

    def test_csrf_origin_host_and_get_cannot_call_trends(self):
        self.assertEqual(self.call('/api/topics/save',dict(name='x',keywords=['a']),token=False)[0],403)
        self.assertEqual(self.call('/api/topics/save',dict(name='x',keywords=['a']),origin='https://example.invalid')[0],403)
        self.assertEqual(self.call('/api/trends')[0],404)
        req=urllib.request.Request(self.origin+'/api/state',headers={'Host':'untrusted.invalid'})
        with self.assertRaises(urllib.error.HTTPError) as c:self.opener.open(req)
        self.assertEqual(c.exception.code,403)
        self.assertEqual(self.client.calls,[])

    def test_stale_version_and_missing_key_do_not_call_api(self):
        t=self.create()
        self.call('/api/topics/save',dict(id=t['id'],version=1,name='수정',keywords=['새 검색어']))
        self.assertEqual(self.call('/api/trends',dict(selection=[t],end=END))[0],409)
        self.assertEqual(self.client.calls,[])
        opener=FakeOpener()
        self.server.client=TrendClient(opener,lambda:None)
        current=self.call('/api/state')[1]['topics'][0]
        status,body,_=self.call('/api/trends',dict(selection=[current],end=END))
        self.assertEqual((status,body['code']),(503,'missing_key'))
        self.assertEqual(opener.calls,[])

    def test_duplicate_launch_reuses_same_server_and_shutdown_is_clean(self):
        with patch('keyword_analysis.server.webbrowser.open') as open_browser:
            self.assertEqual(launch(self.root,self.server.server_port,True),'reused')
            open_browser.assert_called_once_with(self.origin)
        with self.assertRaises(RuntimeError):
            launch(self.root,self.server.server_port,False,require_fresh=True)
        self.assertEqual(self.call('/api/shutdown',{})[0],200)
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())

    def test_concurrent_requests_busy_and_shutdown_discards_response(self):
        entered,release=threading.Event(),threading.Event()
        class BlockingClient(RecordingClient):
            def fetch(self,req):
                entered.set();release.wait(4);return super().fetch(req)
        self.server.client=BlockingClient()
        topic=self.create();payload=dict(selection=[topic],end=END);result=[]
        worker=threading.Thread(target=lambda:result.append(self.call('/api/trends',payload)));worker.start()
        self.assertTrue(entered.wait(2))
        self.assertEqual(self.call('/api/trends',payload)[0],409)
        self.server.stopping.set();release.set();worker.join(3)
        self.assertEqual(result[0][1]['code'],'stopping')
        self.assertEqual(len(self.server.client.calls),1)


class LauncherTests(unittest.TestCase):
    def test_fresh_launch_opens_default_browser_once_without_api(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'.setup-tmp') as directory:
            ready=threading.Event();opened=[];result=[]
            def opened_browser(url):
                self.assertNotIn('NAVER_API_HUB_CLIENT_SECRET',os.environ)
                self.assertNotIn('NAVER_API_HUB_CLIENT_ID',os.environ)
                opened.append(url);ready.set()
            with patch.dict(os.environ,{'NAVER_API_HUB_CLIENT_ID':'mock-id','NAVER_API_HUB_CLIENT_SECRET':'mock-secret'}), patch('keyword_analysis.server.webbrowser.open',side_effect=opened_browser), patch('keyword_analysis.server.TrendClient',return_value=RecordingClient()):
                worker=threading.Thread(target=lambda:result.append(launch(directory,0,True)),daemon=True)
                worker.start();self.assertTrue(ready.wait(3))
                self.assertEqual(len(opened),1)
                opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(opened[0]) as r:
                    html=r.read().decode()
                token=html.split('name="analysis-token" content="',1)[1].split('"',1)[0]
                request=urllib.request.Request(opened[0]+'/api/shutdown',data=b'{}',headers={'Content-Type':'application/json','X-Analysis-Token':token})
                with opener.open(request) as r:self.assertTrue(json.load(r)['ok'])
                worker.join(3);self.assertFalse(worker.is_alive())
                self.assertEqual(result,['stopped'])

    @unittest.skipUnless(os.name=='nt','Windows BAT only')
    def test_real_bat_demo_start_duplicate_and_shutdown(self):
        self.exercise_bat('start_keyword_analysis.bat', 0)

    @unittest.skipUnless(os.name=='nt','Windows BAT only')
    def test_safe_bat_demo_start_refuses_duplicate_and_shutdown(self):
        self.exercise_bat('start_keyword_analysis_secure.bat', 1)

    def exercise_bat(self, bat, duplicate_exit):
        # Explicit --demo prevents any live API call even if host environment has keys.
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        args=['cmd.exe','/d','/c',bat,'--demo','--no-browser','--port',str(port)]
        process=subprocess.Popen(args,cwd=ROOT,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        origin=f'http://127.0.0.1:{port}'
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            deadline=time.monotonic()+8
            while True:
                try:
                    with opener.open(origin+'/api/health',timeout=.5) as r:info=json.load(r)
                    break
                except urllib.error.URLError:
                    if process.poll() is not None or time.monotonic()>deadline:self.fail('BAT did not start local demo server')
                    time.sleep(.1)
            self.assertTrue(info['demo'])
            second=subprocess.run(args,cwd=ROOT,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=5)
            self.assertEqual(second.returncode,duplicate_exit)
            with opener.open(origin) as r:html=r.read().decode()
            token=html.split('name="analysis-token" content="',1)[1].split('"',1)[0]
            request=urllib.request.Request(origin+'/api/shutdown',data=b'{}',headers={'Content-Type':'application/json','X-Analysis-Token':token})
            with opener.open(request) as r:self.assertTrue(json.load(r)['ok'])
            process.communicate(timeout=5)
            self.assertEqual(process.returncode,0)
        finally:
            if process.poll() is None:
                process.kill();process.communicate(timeout=3)


if __name__=='__main__':
    unittest.main()
