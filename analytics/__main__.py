import argparse
import datetime as dt
import re
from pathlib import Path

from .core import Store, collect_dates, date_range, parse_xlsx


def main():
    parser = argparse.ArgumentParser(description='Naver official XLSX collector (manual login, no saved auth)')
    parser.add_argument('--db', type=Path, default=Path('data/analytics.sqlite3'))
    sub = parser.add_subparsers(dest='command', required=True)
    ingest = sub.add_parser('import-xlsx')
    ingest.add_argument('files', type=Path, nargs='+')
    export = sub.add_parser('export')
    export.add_argument('--output', type=Path, default=Path('exports'))
    collect = sub.add_parser('collect')
    collect.add_argument('--blog-id', required=True)
    collect.add_argument('--dates', type=dt.date.fromisoformat, nargs='+')
    collect.add_argument('--start', type=dt.date.fromisoformat)
    collect.add_argument('--end', type=dt.date.fromisoformat)
    collect.add_argument('--chromium', type=Path)
    collect.add_argument('--downloads', type=Path, default=Path('downloads'))
    collect.add_argument('--delay', type=float, default=3)
    args = parser.parse_args()
    if args.command == 'collect':
        if bool(args.dates) == bool(args.start or args.end) or bool(args.start) != bool(args.end):
            parser.error('Use either --dates or both --start and --end')
        try:
            dates = sorted(set(args.dates or date_range(args.start, args.end)))
        except ValueError as exc:
            parser.error(str(exc))
        if args.delay < 3:
            parser.error('--delay must be at least 3 seconds')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', args.blog_id):
            parser.error('Invalid blog ID')
        if dates[-1] >= dt.date.today():
            parser.error('Select completed dates before today')
        print('Selected dates:', ', '.join(d.isoformat() for d in dates))
        if input('Collect exactly these dates? Type YES: ').strip() != 'YES':
            return 0
    store = Store(args.db)
    try:
        if args.command == 'import-xlsx':
            for file in args.files:
                report = parse_xlsx(file)
                store.save(report)
                print(report.date, 'imported', len(report.keywords), len(report.routes))
        elif args.command == 'export':
            store.export(args.output)
            print('CSV export complete:', args.output)
        else:
            from playwright.sync_api import sync_playwright
            from .browser import OfficialDownloader, chromium_path, guard
            with sync_playwright() as pw:
                browser = pw.chromium.launch(executable_path=str(args.chromium or chromium_path()), headless=False)
                context = browser.new_context(accept_downloads=True)
                try:
                    page = context.new_page()
                    page.goto('https://nid.naver.com/nidlogin.login', wait_until='domcontentloaded')
                    input('Log in manually in Chromium, then press Enter here: ')
                    guard(page)
                    page.goto(f'https://admin.blog.naver.com/{args.blog_id}/stat/download', wait_until='domcontentloaded')
                    guard(page)
                    page.frame_locator('iframe[name="statmain"]').locator('#REFERER').wait_for()
                    downloader = OfficialDownloader(page, page.frame(name='statmain'))
                    results = collect_dates(dates, downloader, store, args.downloads, args.delay)
                    for result in results:
                        print(*result)
                    return 1 if any(r[1] != 'success' for r in results) else 0
                finally:
                    context.close()
                    browser.close()
    except Exception as exc:
        # Avoid disclosing browser URLs, request headers, or arbitrary page contents.
        print('Stopped:', type(exc).__name__, '(existing DB data preserved for failed date)')
        return 1
    finally:
        store.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
