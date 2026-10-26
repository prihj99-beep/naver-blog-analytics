# 주간 미리보기 404 및 실제 Markdown 형식 보완

## 실제 원인 확인

운영 중인 8766을 종료하거나 실제 저장 요청을 보내지 않고 조사했습니다.

- GET /api/health: 200. 프로젝트 식별값이 현재 프로젝트 경로와 일치했습니다.
- GET /app.js: 200. /api/weekly/preview와 /api/weekly/analysis를 사용하는 최신 화면 코드가 있었습니다.
- GET /api/weekly: 404, ‘경로를 찾을 수 없습니다.’.
- POST /api/weekly/preview: 실제 제공 09.28~10.04 원문으로 요청했지만 404, 같은 오류였습니다.
- 디스크의 최신 server.py에는 해당 GET/POST 라우트가 모두 있습니다. 프론트엔드 요청 URL과 일치합니다.

같은 프로젝트의 구버전 Python 서버가 실행 중인 상태에서 정적 화면 파일만 최신으로 읽어 제공하는 상태를 확인했습니다.
기존 BAT의 중복 실행 재사용 검사는 프로젝트 식별값·모의 모드만 비교했고 실제 실행 코드 버전을 확인하지 않았습니다.
따라서 파서까지 요청이 도달하기 전에 404가 발생했습니다. 브라우저 새로고침만으로는 Python 모듈이 갱신되지 않습니다.

현재 서버 코드는 요청/예외 본문과 인증정보를 로그로 기록하지 않으며, keyword_analysis 내부에 별도 .log 파일도 없었습니다.
확인 가능한 과거 예외 로그 대신 실제 HTTP 상태·응답 본문·현재 파일 라우트를 근거로 원인을 확인했습니다.
페이지 토큰은 메모리에서만 사용했으며 출력·저장하지 않았습니다.

## 추가로 확인한 파서 원인

나중에 제공받은 두 실제 샘플을 변경 전 파서로 직접 실행했습니다.

- 10.05~10.11: 기간·조회수·검색 유입률은 읽었지만 '- 유입검색어 분석' 제목을 찾지 못해 검색어 0개와 오류가 반환됐습니다.
- 10.12~10.18: 굵은 표기·이스케이프된 물결표·연속 링크를 지원하지 않아 날짜/조회수/유입률이 미확인, 검색어 0개와 오류가 반환됐습니다.

404와 별개로 서버 재실행 후에도 발생할 수 있는 실제 형식 차이를 재현한 뒤 보완했습니다.

## 최소 변경

- server.py: 시작 시 실행 소스·화면 파일의 해시를 기억하고 health에 build로 제공합니다. 중복 실행 시 구버전/다른 빌드는 재사용하지 않고 재실행 안내를 출력합니다. 프로세스를 강제로 종료하지 않습니다.
- web/app.js: 주간 API의 404에 구버전 서버 가능성과 정확한 종료·BAT 재실행 방법을 안내합니다.
- weekly.py: 파싱용 사본에서 목록 기호·외곽 굵은 표기·이스케이프된 물결표와 URL의 앰퍼샌드만 처리합니다. 검색어/비율 연속 링크는 URL 일치 및 기존 날짜/검색어 검증을 통과해야 한 행으로 읽습니다.
- 원문 해시와 weekly_sources의 원본 복사 텍스트는 정규화하지 않습니다. 비율 문자열 20.00·3.70 등도 보존합니다.
- web/index.html, README.md: 실제 검증한 세 주차 형식과 재실행 안내 갱신.
- test_server_version.py 추가, test_ui.py 보완. 실제 샘플 fixture 2개 추가.

## 실제 세 주차 결과

| 기간 | 채널 조회수 | 검색 유입률 | 검색어 수 | 파싱 오류 |
|---|---:|---:|---:|---|
| 2026-09-28~10-04 | 7 | 57.14% | 4 | 없음 |
| 2026-10-05~10-11 | 5 | 80% | 4 | 없음 |
| 2026-10-12~10-18 | 27 | 55.56% | 12 | 없음 |

마지막 주의 비율은 7.41% 3개, 3.70% 9개입니다. 비율로 유입 횟수를 역산하지 않았습니다.
처음 확보한 샘플의 날짜만 바꾼 합성 시험과 새로 받은 실제 자료 시험을 구분하고, 최종 브라우저 검증은 실제 세 자료로 수행했습니다.

## 테스트

최종 133개 통과, node --check keyword_analysis/web/app.js 통과.

```powershell
.\.venv\Scripts\python.exe -B -m unittest keyword_analysis.test_server_version keyword_analysis.test_weekly_analysis keyword_analysis.test_weekly keyword_analysis.test_export keyword_analysis.test_analysis.ModelTests keyword_analysis.test_analysis.CalculationTests keyword_analysis.test_analysis.ClientTests keyword_analysis.test_analysis.HTTPTests keyword_analysis.test_secure_launch keyword_analysis.test_ui tests.test_core tests.test_live_artifacts tests.test_dashboard tests.test_login tests.test_login_jobs tests.test_login_ui
```

- 구버전/다른 빌드 재사용 거부, 동일 빌드 재사용, 고정된 재실행 메시지 확인.
- 최신 임시 서버에서 세 실제 자료의 미리보기 HTTP 200, 날짜·조회수·유입률·검색어와 URL 검증.
- 실제 로컬 Chromium에서 세 실제 자료를 순서대로 붙여넣기→자동 미리보기→임시 DB 저장→분석 자동 표시.
- 404 재현 시 재실행 안내·추출 결과 비움·저장 비활성화 확인.
- 연속 링크 URL 불일치·비율 누락은 계속 거부. 원본 Markdown의 보존 확인.
- 기존 주간 저장·비교·분석·트렌드·엑셀·8765의 모의 회귀 테스트.

테스트는 임시 DB와 모의 API만 사용했으며 운영 DB에 세 자료를 저장하지 않았습니다.
네이버 자동 접속·실제 API 호출·8765 자동 수집·Git commit/push를 하지 않았습니다.
운영 DB·기존 저장 자료·8765 소스·기존 다운로드 XLSX의 SHA256은 조사 전후 동일합니다.

## 사용자가 할 일

현재 운영 중인 구버전 8766 프로세스는 보존했습니다. 아래 재실행이 필요합니다.

1. 필요하면 현재 화면의 트렌드 결과와 미저장 복사 원문을 보존합니다.
2. 기존 8766 화면에서 **분석 화면 종료**를 누릅니다. 탭만 닫거나 새로고침하지 마세요.
3. 프로젝트 폴더의 **start_keyword_analysis.bat**를 다시 실행합니다.
4. 새로 열린 8766의 주간 통계 탭에서 원문을 붙여넣고 추출 결과 확인 후 저장합니다.

DB 삭제·초기화·별도 패키지 설치는 필요 없습니다. 주간 기능에는 API 키가 필요 없습니다.
