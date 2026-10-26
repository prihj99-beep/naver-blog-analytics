"""Authentication worker integration and race tests; no network or production DB writes."""
import datetime as dt
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from analytics.browser import AccessRestricted
from analytics.core import Store, parse_xlsx
from analytics.dashboard import Jobs

ROOT=Path(__file__).resolve().parents[1]
DATE=dt.date(2026,10,12)


def until(predicate, timeout=3):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if predicate():return
        time.sleep(.005)
    raise AssertionError('Mock state transition timed out')


class LoginJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'.setup-tmp')
        self.root=Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_duplicate_start_stale_requests_and_shutdown(self):
        release=threading.Event()
        calls=[]
        def runner(dates):
            calls.append(dates)
            jobs.update(phase='waiting_login')
            release.wait(3)
        jobs=Jobs(self.root,'sample',runner=runner)
        first=jobs.start([DATE],'start-request-0001')
        until(lambda:jobs.snapshot()['phase']=='waiting_login')
        self.assertEqual(jobs.start([DATE],'start-request-0001'),first)
        with self.assertRaises(ValueError):jobs.start([DATE],'different-request')
        with self.assertRaises(ValueError):jobs.confirm_login('0'*32,'confirm-request-1')
        jobs.stop(first)
        with self.assertRaises(ValueError):jobs.confirm_login(first,'confirm-request-2')
        release.set();jobs.join()
        self.assertEqual(jobs.snapshot()['phase'],'cancelled')
        release.clear()
        second=jobs.start([DATE],'start-request-0002')
        until(lambda:jobs.snapshot()['phase']=='waiting_login')
        self.assertNotEqual(first,second)
        with self.assertRaises(ValueError):jobs.stop(first)
        self.assertFalse(jobs.update(first,phase='completed'))
        jobs.progress(DATE,'success',99,99,job_id=first)
        self.assertEqual(jobs.snapshot()['completed'],0)
        self.assertEqual(jobs.start([DATE],'start-request-0001'),first)
        jobs.stop(second,shutdown=True);release.set();jobs.join()
        with self.assertRaises(ValueError):jobs.start([DATE],'start-request-0003')
        self.assertEqual(len(calls),2)

    def test_duplicate_confirmation_not_replayed_after_incomplete(self):
        calls=[]
        def runner(dates):
            jobs.update(phase='waiting_login')
            for _ in range(2):
                jobs.login.wait(2);jobs.login.clear();calls.append(1)
                if len(calls)==1:jobs.update(phase='authenticating')
            jobs.update(phase='completed')
        jobs=Jobs(self.root,'sample',runner=runner)
        job=jobs.start([DATE])
        until(lambda:jobs.snapshot()['phase']=='waiting_login')
        self.assertTrue(jobs.confirm_login(job,'confirm-request-01'))
        until(lambda:jobs.snapshot()['phase']=='authenticating')
        self.assertFalse(jobs.confirm_login(job,'confirm-request-01'))
        self.assertEqual(len(calls),1)
        self.assertTrue(jobs.confirm_login(job,'confirm-request-02'))
        jobs.join();self.assertEqual(jobs.snapshot()['phase'],'completed')
        self.assertFalse(jobs.update(job,phase='waiting_login'))

    def worker_mocks(self):
        manager=MagicMock()
        pw=manager.__enter__.return_value
        browser=pw.chromium.launch.return_value
        context=browser.new_context.return_value
        page=context.new_page.return_value
        page.wait_for_timeout.side_effect=lambda _:time.sleep(.005)
        return manager,browser,context,page

    def test_real_worker_incomplete_then_confirmed_download_and_preservation(self):
        store=Store(self.root/'data/analytics.sqlite3')
        store.save(parse_xlsx(ROOT/'verification/search-inflow-2026-10-15.xlsx'))
        store.close()
        manager,browser,context,page=self.worker_mocks()
        verified=threading.Event()
        auth=MagicMock()
        auth.observe.side_effect=lambda:'waiting_login' if verified.is_set() else 'authenticating'
        auth.verify.side_effect=lambda cancel:verified.is_set()
        downloader=MagicMock()
        downloader.download.return_value=ROOT/'verification/search-inflow-2026-10-12.xlsx'
        with patch('playwright.sync_api.sync_playwright',return_value=manager),patch('analytics.login.LoginSession',return_value=auth),patch('analytics.browser.OfficialDownloader',return_value=downloader):
            jobs=Jobs(self.root,'sample')
            job=jobs.start([DATE])
            until(lambda:jobs.snapshot()['phase']=='authenticating')
            jobs.confirm_login(job,'first-check-00001')
            until(lambda:auth.verify.call_count==1 and jobs.snapshot()['phase']=='authenticating')
            context.close.assert_not_called()
            downloader.download.assert_not_called()
            self.assertTrue(jobs.snapshot()['active'])
            verified.set();jobs.confirm_login(job,'second-check-0001');jobs.join()
            self.assertEqual(jobs.snapshot()['phase'],'completed')
            self.assertTrue(jobs.snapshot()['auth_verified'])
            self.assertEqual(jobs.snapshot()['completed'],1)
            downloader.download.assert_called_once()
            context.close.assert_called_once();browser.close.assert_called_once()
        store=Store(self.root/'data/analytics.sqlite3')
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM reports').fetchone()[0],2)
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM entries').fetchone()[0],14)
        store.close()

    def test_cancellation_wins_over_late_verification_success(self):
        manager,browser,context,page=self.worker_mocks()
        entered,release=threading.Event(),threading.Event()
        auth=MagicMock();auth.observe.return_value='waiting_login'
        def verify(cancel):
            entered.set();release.wait(3);return True
        auth.verify.side_effect=verify
        with patch('playwright.sync_api.sync_playwright',return_value=manager),patch('analytics.login.LoginSession',return_value=auth),patch('analytics.browser.OfficialDownloader') as factory:
            jobs=Jobs(self.root,'sample');job=jobs.start([DATE])
            until(lambda:jobs.snapshot()['phase']=='waiting_login')
            jobs.confirm_login(job,'confirm-request-01');self.assertTrue(entered.wait(2))
            jobs.stop(job,shutdown=True);release.set();jobs.join()
            self.assertEqual(jobs.snapshot()['phase'],'cancelled')
            self.assertFalse(jobs.snapshot()['auth_verified'])
            factory.assert_not_called()
            self.assertFalse((self.root/'data/analytics.sqlite3').exists())
            context.close.assert_called_once()

    def test_explicit_block_terminates_login_worker_without_download(self):
        manager,browser,context,page=self.worker_mocks()
        auth=MagicMock();auth.observe.side_effect=AccessRestricted('Mock restriction')
        with patch('playwright.sync_api.sync_playwright',return_value=manager),patch('analytics.login.LoginSession',return_value=auth),patch('analytics.browser.OfficialDownloader') as factory:
            jobs=Jobs(self.root,'sample');jobs.start([DATE]);jobs.join()
            self.assertEqual(jobs.snapshot()['phase'],'blocked')
            factory.assert_not_called();context.close.assert_called_once()
            self.assertFalse((self.root/'data/analytics.sqlite3').exists())
