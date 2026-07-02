# 단어 음원 외주 검수 Streamlit 앱

DB 파일을 배포하지 않고, 업로드한 엑셀을 기준으로 음원을 생성/검수하는 Streamlit 앱입니다.

## 포함 기능

- 검수 엑셀 업로드
- 업로드 엑셀 기준 50개 단위 페이지 검수
- ElevenLabs American / British 성우 선택 및 미리듣기
- 선택 성우 적용 후 현재 페이지 생성
- 현재 페이지 전체 재생성
- 행별 개별 재생성
- 같은 `pronunciation_key`는 페이지 안에서 같은 음원을 재사용
- Gemini로 현재 페이지 발음 재사용 키 보정
- 현재 페이지 ZIP 다운로드
- Google Drive 설정 시 현재 페이지 음원 자동 업로드
- Google Sheets 설정 시 저장완료 / 이상표시 자동 반영
- 재접속 후 엑셀 재업로드 시 Google Sheet 기록을 읽어 첫 미완료 페이지로 이동

## Streamlit Cloud Secrets

API Key는 GitHub에 올리지 말고 Streamlit Cloud의 App settings > Secrets에 넣으세요.

```toml
ELEVENLABS_API_KEY = "xi-..."
GEMINI_API_KEY = "..."

# Google 자동 저장을 쓸 때만 필요
GOOGLE_SERVICE_ACCOUNT_JSON = "{...}"
GOOGLE_DRIVE_FOLDER_ID = "구글드라이브폴더ID"
GOOGLE_SHEET_ID_WORKER_1 = "작업자1_구글시트ID"
GOOGLE_WORKSHEET_NAME_WORKER_1 = "worker_1_upload"
GOOGLE_SHEET_ID_WORKER_2 = "작업자2_구글시트ID"
GOOGLE_WORKSHEET_NAME_WORKER_2 = "worker_2_upload"
GOOGLE_ISSUE_SHEET_NAME = "Issues"
GOOGLE_PROGRESS_SHEET_NAME = "Progress"
```

Google Drive/Sheets 자동 연동을 쓰려면 서비스 계정 이메일을 저장 폴더와 Google Sheet에 공유 권한으로 추가해야 합니다.

## 로컬 실행

```powershell
streamlit run app.py
```

## 배포

Streamlit Cloud에서 GitHub repo를 연결하고 Main file path에 `app.py`를 지정합니다.

GitHub에 올릴 파일:

- `app.py`
- `requirements.txt`
- `README.md`
- `.streamlit/secrets.toml.example`

올리면 안 되는 파일:

- `.streamlit/secrets.toml`
