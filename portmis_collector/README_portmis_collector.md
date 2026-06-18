# Port-MIS Excel Auto Collector v0.1

ULSAN AIS Mobile용 Port-MIS 엑셀 자동 수집기입니다.

현재 버전은 다음 작업을 자동화합니다.

1. Port-MIS 선박입출항현황 페이지 열기
2. 오늘부터 지정 일수 후까지 조회 기간 설정
3. 울산항 기준 조회 시도
4. 50000개씩 보기 설정 시도
5. 조회 실행
6. 엑셀 다운로드
7. 다운로드된 `.xlsx` 파일 확인
8. Render 서버 `/portmis/upload-excel`에 multipart/form-data 업로드
9. 업로드 응답 JSON 출력
10. `/portmis/status` 조회로 업로드 상태 확인

## 주의

- Flutter 앱 코드는 수정하지 않습니다.
- Render 서버 `app.py`도 수정하지 않습니다.
- WebSquare 화면 selector는 바뀔 수 있습니다. 실패하면 `debug` 폴더의 스크린샷과 HTML을 보고 selector를 보정해야 합니다.
- 처음 실행은 사람이 화면을 보면서 확인할 수 있도록 `--headful` 모드를 권장합니다.
- Windows 작업 스케줄러 등록은 다음 버전에서 진행하는 것이 안전합니다.

## 폴더

```text
tools\portmis_collector\
  portmis_auto_collector_v0_1.py
  requirements_portmis_collector.txt
  README_portmis_collector.md
  downloads\   # 실행 시 자동 생성
  debug\       # 실패 시 자동 생성
```

## 1. 가상환경 생성

PowerShell에서 프로젝트 폴더로 이동합니다.

```powershell
cd C:\FlutterProjects\ulsan_ais_mobile
py -m venv .venv_portmis
.\.venv_portmis\Scripts\activate
```

프롬프트 앞에 `(.venv_portmis)`가 보이면 가상환경이 켜진 상태입니다.

## 2. Python 패키지 설치

```powershell
pip install -r tools\portmis_collector\requirements_portmis_collector.txt
```

## 3. Playwright Chromium 설치

```powershell
python -m playwright install chromium
```

## 4. Headful 테스트 실행

브라우저를 보이게 실행합니다.

```powershell
python tools\portmis_collector\portmis_auto_collector_v0_1.py --headful --days 7
```

업로드 없이 엑셀 다운로드까지만 확인하려면 다음 명령을 사용합니다.

```powershell
python tools\portmis_collector\portmis_auto_collector_v0_1.py --headful --days 7 --no-upload
```

## 5. Headless 실행

화면 없이 실행하려면 다음 명령을 사용합니다.

```powershell
python tools\portmis_collector\portmis_auto_collector_v0_1.py --headless --days 7
```

## 6. 서버 옵션

기본값은 현재 Render 서버입니다.

```text
https://ulsan-ais-fcm-server.onrender.com
```

API key 기본값은 다음 값입니다.

```text
ulsan_ais_2026_mobile
```

직접 지정하려면 다음처럼 실행합니다.

```powershell
python tools\portmis_collector\portmis_auto_collector_v0_1.py --headful --days 7 --server-url https://ulsan-ais-fcm-server.onrender.com --api-key ulsan_ais_2026_mobile
```

## 7. 업로드 결과 확인

스크립트는 업로드 후 서버 응답 JSON을 출력하고, 이어서 `/portmis/status` 결과를 출력합니다.

수동으로 확인하려면 다음 명령을 사용할 수 있습니다.

```powershell
$SERVER="https://ulsan-ais-fcm-server.onrender.com"
$API_KEY="ulsan_ais_2026_mobile"
curl.exe "$SERVER/portmis/status?api_key=$API_KEY" -H "X-API-Key: $API_KEY"
```

## 8. 실패 시 확인 위치

다운로드 실패 또는 selector 실패 시 아래 폴더에 파일이 저장됩니다.

```text
tools\portmis_collector\debug
```

확인할 파일:

- `.png`: 실패 당시 화면 스크린샷
- `.html`: 실패 당시 페이지 HTML

다운로드에 성공한 엑셀 파일은 아래 폴더에 저장됩니다.

```text
tools\portmis_collector\downloads
```

## 9. 주요 옵션

```text
--headful              브라우저를 보이게 실행합니다. 기본값입니다.
--headless             브라우저를 숨기고 실행합니다.
--days 7               오늘부터 며칠 후까지 조회할지 지정합니다.
--no-upload            엑셀 다운로드만 하고 서버 업로드는 하지 않습니다.
--server-url URL       Render 서버 URL을 지정합니다.
--api-key KEY          서버 API key를 지정합니다.
--download-dir PATH    엑셀 다운로드 폴더를 지정합니다.
--debug-dir PATH       실패 스크린샷/HTML 저장 폴더를 지정합니다.
```

## 10. 다음 버전에서 할 일

- Port-MIS 실제 화면 selector를 한 번 실행 결과로 보정
- Windows 작업 스케줄러 등록 스크립트 추가
- 다운로드 파일명과 업로드 결과를 별도 로그 파일로 저장
- 실패 시 재시도 횟수 옵션 추가
