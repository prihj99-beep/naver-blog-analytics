"""Stage-aware manual authentication. No credentials, cookie reads, or page logging."""
import time
from urllib.parse import urlsplit

from .browser import AccessRestricted
from .core import CollectionCancelled


# Return only a small classification. Never return input values, page text or URLs.
SCREEN_PROBE = r'''() => {
  const visible = el => !!el && el.getClientRects().length > 0 &&
    getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
  const items = selector => [...document.querySelectorAll(selector)].filter(visible);
  const block = /^(?:비정상적인\s*접근(?:입니다|이\s*감지되었습니다)|접근(?:이|이\s*일시적으로)?\s*(?:제한|차단)(?:되었습니다|됩니다|된\s*상태입니다)|서비스\s*이용이\s*제한(?:되었습니다|됩니다)|요청이\s*차단되었습니다)[.!\s]*$/i;
  const notices = items('h1,h2,h3,[role="alert"],[role="alertdialog"],main > p,.error_message,.error_content');
  if (notices.some(el => block.test(el.innerText.trim()))) return 'blocked';
  const challengeInputs = items('input[name*="captcha" i],input[id*="captcha" i],input[autocomplete="one-time-code"],input[name="otp"],input[name="authNo"]');
  const challengeFrames = items('iframe').filter(el => {
    const r=el.getBoundingClientRect();
    return r.width >= 200 && r.height >= 100 && /captcha|challenge|보안\s*확인/i.test((el.title||'')+' '+(el.getAttribute('src')||''));
  });
  const challengePanels = items('[id*="captcha" i],[class*="captcha" i],[role="dialog"],main,form').some(el => {
    const text=el.innerText||'';
    const controls=[...el.querySelectorAll('button,input,[role="button"]')].filter(visible);
    const media=[...el.querySelectorAll('img,canvas')].filter(visible);
    return /CAPTCHA|자동\s*입력\s*방지|보안\s*(?:확인|인증)|추가\s*인증|인증번호|로봇이\s*아닙니다/i.test(text) &&
      ((media.length > 0 && controls.length >= 2) || controls.some(c=>c.tagName==='INPUT' && c.type!=='hidden' && c.type!=='checkbox'));
  });
  if (challengeInputs.length || challengeFrames.length || challengePanels) return 'challenge';
  if (items('input[type="password"]').length) return 'login';
  return 'normal';
}'''


def screen_state(surface):
    # Page.frames includes the main frame. A challenge may be inside a child frame.
    if hasattr(surface, 'frames'):
        surfaces = [f for f in surface.frames if f == surface.main_frame or f.name == 'statmain'
                    or urlsplit(f.url).hostname in ('nid.naver.com', 'blog.stat.naver.com')]
    else:
        surfaces = [surface]
    result = 'normal'
    for frame in surfaces:
        try:
            state = frame.evaluate(SCREEN_PROBE)
        except Exception:
            # Navigation/detachment is inconclusive, never evidence of success.
            state = 'unknown'
        if state == 'blocked':
            return state
        if state == 'challenge':
            result = state
        elif result != 'challenge' and state in ('login', 'unknown'):
            result = state
    return result


def relevant_response(response, page):
    """Only the top document, official statistics frame, and download API matter."""
    parsed = urlsplit(response.url)
    if parsed.scheme not in ('http', 'https'):
        return False
    if parsed.hostname == 'blog.stat.naver.com' and parsed.path == '/api/blog/download':
        return True
    request = response.request
    if request.resource_type == 'document':
        try:
            if response.frame == page.main_frame:
                return True
            return parsed.hostname == 'blog.stat.naver.com' and parsed.path.startswith('/blog/')
        except Exception:
            return False
    return False


class LoginSession:
    def __init__(self, page, blog_id, stats_url=None, ready_check=None):
        self.page = page
        self.blog_id = blog_id
        self.stats_url = stats_url or f'https://admin.blog.naver.com/{blog_id}/stat/download'
        self.ready_check = ready_check or self._official_ready
        self.blocked = False
        self.unauthorized = False
        self.unavailable = False
        self.pending_dialog = False
        page.on('response', self._response)
        page.on('dialog', self._dialog)

    def _dialog(self, dialog):
        # Leave authentication dialogs for the human; never read or submit them.
        self.pending_dialog = True

    def _response(self, response):
        if not relevant_response(response, self.page):
            return
        if response.status in (403, 429):
            self.blocked = True
        elif response.status == 401:
            self.unauthorized = True
        elif response.status >= 500:
            self.unavailable = True

    def observe(self):
        if self.page.is_closed():
            raise CollectionCancelled()
        if self.blocked:
            raise AccessRestricted('Explicit access restriction; stopped')
        if self.pending_dialog:
            return 'authenticating'
        state = screen_state(self.page)
        if self.blocked or state == 'blocked':
            raise AccessRestricted('Explicit access restriction; stopped')
        return 'authenticating' if state == 'challenge' else 'waiting_login'

    def _official_ready(self):
        actual, expected = urlsplit(self.page.url), urlsplit(self.stats_url)
        if (actual.scheme, actual.netloc, actual.path) != (expected.scheme, expected.netloc, expected.path):
            return False
        frame = self.page.frame(name='statmain')
        if frame is None:
            return False
        frame_url = urlsplit(frame.url)
        return (frame_url.hostname == 'blog.stat.naver.com' and frame_url.path == '/blog/download'
                and frame.locator('#REFERER').is_visible()
                and frame.locator('#REFERER').is_enabled()
                and frame.locator('a[class*="btn_download"]').is_visible())

    def verify(self, cancel, timeout=12):
        """One user-requested check; no automatic login, reload or retry loop."""
        from playwright.sync_api import Error as BrowserError
        if cancel.is_set():
            raise CollectionCancelled()
        self.pending_dialog = False
        observed = self.observe()
        state = screen_state(self.page)
        if observed == 'authenticating' or state in ('login', 'unknown') or urlsplit(self.page.url).hostname == 'nid.naver.com':
            return False
        self.unauthorized = self.unavailable = False
        try:
            if self.page.url.split('?')[0] != self.stats_url:
                self.page.goto(self.stats_url, wait_until='domcontentloaded')
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if cancel.is_set():
                    raise CollectionCancelled()
                self.observe()
                state = screen_state(self.page)
                if self.unauthorized or self.unavailable or state in ('challenge', 'login'):
                    return False
                if state == 'normal' and self.ready_check():
                    return True
                self.page.wait_for_timeout(150)  # DOM wait only, no network retries.
        except BrowserError:
            self.observe()
        return False

    def detach(self):
        self.page.remove_listener('response', self._response)
        self.page.remove_listener('dialog', self._dialog)
