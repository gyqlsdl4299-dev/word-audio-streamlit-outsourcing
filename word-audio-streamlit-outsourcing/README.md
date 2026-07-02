# 0623 단어 음원 외주 검수 Streamlit 앱

외주자가 URL로 접속해서 작업자 1/2 범위의 음원을 검수하고, 현재 페이지 ZIP을 다운로드하는 Streamlit 배포용 앱입니다.

## 포함 기능

- 작업자 1: 전체 101~353페이지
- 작업자 2: 전체 354~606페이지
- 작업자별 American / British 성우 선택
- ElevenLabs 성우 성별 표시 및 미리듣기
- 선택 성우 적용 후 현재 페이지 생성
- 선택 성우로 현재 페이지 전체 재생성
- 행별 개별 재생성
- 현재 페이지 음원 재생
- 이상 표시 / 이상 해제
- 재사용 예정 목록 분리
- 현재 페이지 ZIP 다운로드
- ZIP 안에 `page_log.csv`, `issues.csv` 포함

## 데이터 배치

앱은 아래 위치 중 하나에서 데이터를 찾습니다.

1. `./data/word_audio.sqlite3`
2. 형제 폴더 `../word-audio-app/data/word_audio.sqlite3`
3. Streamlit Secrets 또는 환경변수의 `DATA_DIR`

Streamlit Cloud에 올릴 때는 repo 안에 다음 구조가 필요합니다.

```text
word-audio-streamlit-outsourcing
├─ app.py
├─ requirements.txt
└─ data
   ├─ word_audio.sqlite3
   └─ audio
      └─ baa4b56a8fb4
         ├─ US
         └─ UK
```

주의: 현재 DB 파일이 100MB를 넘을 수 있어 GitHub 일반 업로드 제한에 걸릴 수 있습니다. 이 경우 Git LFS, S3/R2, Google Drive 다운로드 방식, 또는 VPS 배포가 필요합니다.

## 로컬 실행

```powershell
streamlit run app.py
```

## Streamlit Cloud 배포

1. GitHub repo 생성
2. 이 폴더의 파일 업로드
3. Streamlit Cloud에서 repo 연결
4. Main file path: `app.py`
5. 필요한 경우 App settings > Secrets에 값 추가

실제 API Key는 GitHub에 올리지 마세요.

Secrets 예시는 아래와 같습니다.

```toml
ELEVENLABS_API_KEY = "xi-..."
GEMINI_API_KEY = "..."
# DATA_DIR = "/mount/path/data"
```
