from __future__ import annotations

import datetime as dt
import os
import re
import time
from pathlib import Path


class AccessRestricted(RuntimeError):
    def __init__(self, message='Access restricted', reason='access'):
        super().__init__(message)
        self.reason = reason  # Safe category only; never retain matched page text.


def guard(page, frame=None):
    from .login import screen_state
    for surface in (page, frame):
        if surface is not None:
            state = screen_state(surface)
            if state in ('blocked', 'challenge'):
                raise AccessRestricted('Security screen detected; stopped', reason='captcha' if state == 'challenge' else 'access')
            if state == 'unknown':
                raise RuntimeError('Security state could not be established')


def chromium_path():
    # Read installed browser locations; never download a browser implicitly.
    root = Path(os.environ.get('USERPROFILE', str(Path.home()))) / 'AppData/Local/ms-playwright'
    candidates = list(root.glob('chromium-*/chrome-win64/chrome.exe'))
    if not candidates:
        raise FileNotFoundError('No existing Chromium found; specify --chromium')
    return max(candidates, key=lambda p: int(p.parents[1].name.split('-')[-1]))


class OfficialDownloader:
    def __init__(self, page, frame):
        self.page = page
        self.frame = frame
        self.restricted = False
        self.unexpected_dialog = False
        self.page.on('response', self._response)
        self.page.on('dialog', self._dialog)

    def _response(self, response):
        from .login import relevant_response
        if relevant_response(response, self.page) and response.status in (401, 403, 429):
            self.restricted = True

    def _dialog(self, dialog):
        # Never log dialog text. Unexpected dialogs stop collection.
        self.unexpected_dialog = True
        dialog.dismiss()

    def check(self, allow_navigation=False):
        if self.restricted:
            raise AccessRestricted('Access restriction or unexpected dialog; stopped')
        if self.unexpected_dialog:
            raise RuntimeError('Unexpected collection dialog; stopped')
        try:
            guard(self.page, self.frame)
        except AccessRestricted:
            raise
        except RuntimeError:
            if not allow_navigation:
                raise
            # During the outstanding download navigation, observe only. Never
            # perform another click/request while the document is inconclusive.

    def choose_date(self, date):
        self.check()
        self.frame.get_by_role('button', name='달력보기', exact=True).click()
        calendar = self.frame.locator('.react-datepicker:visible')
        for _ in range(120):
            month = int(calendar.locator('.react-datepicker__current-month').inner_text())
            year = int(calendar.locator('select').input_value())
            if (year, month) == (date.year, date.month):
                break
            earlier = (date.year, date.month) < (year, month)
            calendar.get_by_text('이전 달' if earlier else '다음 달', exact=True).click()
        else:
            raise ValueError('Date outside supported calendar range')
        # Exclude adjacent months and disabled dates explicitly.
        day = calendar.locator('.react-datepicker__day:not(.react-datepicker__day--outside-month):not(.react-datepicker__day--disabled)').filter(has_text=re.compile(r'^' + str(date.day) + r'$'))
        day.click()
        self.frame.get_by_text(date.strftime('%Y.%m.%d.'), exact=True).wait_for()
        self.check()

    def download(self, date: dt.date, directory: Path):
        self.check()
        self.frame.locator('#REFERER').check()
        self.frame.locator('a').filter(has_text=re.compile(r'^일간$')).click()
        self.choose_date(date)
        selection = self.frame.locator('a[class*="select_box"]')
        if selection.inner_text() != '검색 유입':
            selection.click()
            self.frame.get_by_text('검색 유입', exact=True).click()
        self.check()
        pending = []
        def received(download):
            pending.append(download)
        self.page.on('download', received)
        try:
            self.frame.get_by_role('link', name='지표 다운로드', exact=True).click()
            deadline = time.monotonic() + 60
            while not pending:
                self.check(allow_navigation=True)
                if time.monotonic() >= deadline:
                    raise TimeoutError('Official download timed out')
                self.page.wait_for_timeout(150)
            self.check()
            download = pending[0]
        finally:
            self.page.remove_listener('download', received)
        if download.failure():
            self.check()
            raise RuntimeError('Official download failed')
        self.check()
        directory.mkdir(parents=True, exist_ok=True)
        # Keep the previous valid file until this download has passed metadata validation.
        staged = directory / f'.search-inflow-{date}.pending.xlsx'
        download.save_as(staged)
        from .core import parse_xlsx
        parse_xlsx(staged, expected_date=date)
        path = directory / f'search-inflow-{date}.xlsx'
        staged.replace(path)
        return path
