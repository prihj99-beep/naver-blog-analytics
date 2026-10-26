"""Console-only, hidden credential input. No secret arguments, files, logs or child process."""
from __future__ import annotations

import argparse
import getpass
import os
import socket
import sys
import warnings

from .server import launch
from .trends import KEY_ENV


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # Never echo unknown arguments: an operator might accidentally supply a secret.
        self.print_usage(sys.stderr)
        self.exit(2, '실행 옵션을 확인하세요. 인증정보를 명령행 인자로 입력하지 마세요.\n')


def port_available(port):
    with socket.socket() as sock:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(('127.0.0.1', port))
        except OSError:
            return False
    return True


def run(port=8766, open_browser=True, demo=False):
    client_id = client_secret = None
    # Use only this launch's input. Do not retain inherited values after cancellation.
    for name in KEY_ENV:
        os.environ.pop(name, None)
    try:
        if not port_available(port):
            print('분석 서버가 이미 실행 중이거나 포트를 사용할 수 없습니다. 기존 분석 화면의 종료 버튼을 누른 뒤 다시 실행하세요.')
            return 1
        if not demo:
            if not (sys.stdin.isatty() and sys.stdout.isatty()):
                print('숨김 입력을 위해 BAT를 콘솔 창에서 실행하세요. 파일 입력·파이프·출력 리다이렉션은 지원하지 않습니다.')
                return 1
            print('검색어 분석 안전 실행 · 입력값은 표시하거나 저장하지 않습니다.')
            print('API는 화면의 조회 버튼을 누를 때만 호출됩니다. 취소하려면 Ctrl+C를 누르세요.')
            with warnings.catch_warnings():
                warnings.simplefilter('error', getpass.GetPassWarning)
                client_id = getpass.getpass('API HUB Client ID (hidden): ')
                client_secret = getpass.getpass('API HUB Client Secret (hidden): ')
            if any(not value or len(value) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value)
                   for value in (client_id, client_secret)):
                print('인증정보 형식을 확인하세요. 빈 값·공백·줄바꿈은 사용할 수 없습니다.')
                return 1
            os.environ[KEY_ENV[0]] = client_id
            os.environ[KEY_ENV[1]] = client_secret
        # Same Python process: secrets never appear in a spawned command line.
        launch(port=port, open_browser=open_browser, demo=demo, require_fresh=True)
        return 0
    except (KeyboardInterrupt, EOFError):
        print('\n인증정보 입력 또는 실행을 취소했습니다. 인증정보를 저장하지 않았습니다.')
        return 130
    except getpass.GetPassWarning:
        print('안전한 숨김 입력을 사용할 수 없어 중단했습니다. 일반 콘솔에서 BAT를 실행하세요.')
        return 1
    except Exception:
        # Exception messages can include third-party input; emit a fixed message only.
        print('분석 화면을 실행하지 못했습니다. 기존 분석 서버의 종료 여부와 포트·폴더 권한을 확인하세요.')
        return 1
    finally:
        for name in KEY_ENV:
            os.environ.pop(name, None)
        # Drop references, but do not claim immutable Python strings have been zeroed.
        client_id = client_secret = None


def main(argv=None):
    parser = SafeParser(description='숨김 입력으로 검색어 분석 화면 실행')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--demo', action='store_true', help='입력·외부 연결 없이 모의 응답만 사용')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('invalid port')
    return run(args.port, not args.no_browser, args.demo)


if __name__ == '__main__':
    raise SystemExit(main())
