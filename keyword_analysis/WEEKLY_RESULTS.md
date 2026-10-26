# 주간 통계 붙여넣기 구현 결과

## 변경 범위

- 추가: `weekly.py`, `test_weekly.py`, `fixtures/creator_advisor_2026-09-28_2026-10-04.md`, 이 보고서.
- 수정: `server.py`, `export.py`, `web/index.html`, `web/app.js`, `test_ui.py`, `README.md`.
- UI 회귀 검증으로 기존 `verification/keyword-analysis-tabs-{trends,inbound,manage}.png`를 갱신했습니다.
- 8765 소스·기존 두 DB·기존 다운로드 XLSX의 SHA256은 작업 전후 동일합니다.
- 새 운영용 주간 DB는 테스트에서 만들지 않았습니다. 첫 사용자 확인 저장 시 별도로 생성합니다.

## 실제 샘플 확인

사용자 제공 2026-09-28~2026-10-04 Markdown 복사 샘플을 별도 fixture로 관리합니다.
채널 조회수 7, 검색 유입률 57.14%, 검색어 4개와 각 14.29% 및 상세 URL을 추출했습니다.
URL의 인코딩된 검색어·기간·주간 조건을 표시 내용과 대조합니다.
뒤의 ‘데이터 없음’ 문구는 하위 영역의 의미를 추측하지 않고 경고로 남깁니다.
유입 횟수는 계산하지 않습니다. 테스트용 샘플은 실제 DB에 입력하지 않았습니다.

## 최종 테스트

아래 명령으로 **101개 통과**했습니다. 모두 모의 API·로컬 화면·임시 DB를 사용했습니다.

```powershell
.\.venv\Scripts\python.exe -B -m unittest keyword_analysis.test_weekly keyword_analysis.test_export keyword_analysis.test_analysis.ModelTests keyword_analysis.test_analysis.CalculationTests keyword_analysis.test_analysis.ClientTests keyword_analysis.test_analysis.HTTPTests keyword_analysis.test_secure_launch keyword_analysis.test_ui tests.test_core tests.test_live_artifacts tests.test_dashboard tests.test_login tests.test_login_jobs tests.test_login_ui
```

- 실제 샘플 및 합성 일반 텍스트 파싱, URL 날짜·검색어 인코딩 대조.
- 누락 메타데이터는 빈칸, 잘린 행·불일치 URL·중복 충돌은 저장 거부.
- 미리보기는 DB 생성 없이 수행, 원문/교체 전 원문 보존, 동일 원문 중복 방지.
- 저장 전 기존 버전 검증과 오류 시 기존 내용 보존.
- 정확히 직전 7일 자료 비교, 신규·반복·사라짐, 비율 차이(%p), 이전 주 미등록 구분.
- 실제 Chromium에서 붙여넣기→미리보기→확인 저장→중복→교체 확인→엑셀 다운로드.
- 현재 모의 트렌드와 주간 자료를 함께 내보내고 추가 API 호출 없음 확인.
- 기존 수집기 파서·저장·로그인 상태 관리의 모의 회귀 테스트.
- JavaScript `node --check` 통과.

초기 회귀 검증에서 빈 주제군 안내를 잘못된 영역에서 찾던 기존 테스트를 관리 목록 참조로 수정했습니다.
일반 권한의 Chromium 실행은 멈춰 해당 테스트 프로세스만 정리했고, 승인된 실행 경로에서 최종 검증을 완료했습니다.
네이버 사이트 접속·로그인·수집·실제 API 호출·Git commit/push는 하지 않았습니다.

## 사용 순서와 제한

1. 열려 있는 8766의 현재 트렌드 결과가 필요하면 먼저 기존 방식으로 보존합니다. 서버 재시작 시 메모리 결과는 사라집니다.
2. 기존 분석 서버를 종료하고 `start_keyword_analysis.bat`로 다시 실행합니다. 주간 기능에는 API 키가 필요 없습니다.
3. 주간 통계 탭에 직접 복사한 텍스트를 붙여넣고 미리보기합니다.
4. 오류/미확인/동일 기간 차이를 확인한 뒤 확인 체크와 저장을 누릅니다.
5. 누적 목록과 이전 주 비교를 확인하고 엑셀을 다운로드합니다.

주간 자료가 있으면 기존 3개 시트에 주간 원자료·요약 및 출처·검색어 비교를 추가합니다(총 6개).
주간은 전체 누적, 일간은 기준 종료일까지 14일, 트렌드는 현재 조회 결과 한 건으로 범위를 구분합니다.
원문 전체 및 이전 버전은 별도 로컬 SQLite에만 보존하며 엑셀에는 최신 추출 행·해시를 넣습니다.

실제 일반 텍스트 복사본, 목록 전체성, 원래 비율의 분모, 데이터 없음 문구의 해당 영역은 미확인입니다.
게시물 상세는 방문·수집하지 않으며 연결 게시물은 필수 입력이 아닙니다.
신규·사라짐은 복사 목록 기준으로만 해석하며, 적은 조회수에서 성과를 단정하지 않습니다.
