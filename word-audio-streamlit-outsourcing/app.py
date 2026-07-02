from __future__ import annotations

import io
import json
import os
import re
import time
import base64
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components


APP_ROOT = Path(__file__).resolve().parent
PAGE_SIZE = 50
REQUIRED_COLUMNS = [
    "worker_id",
    "worker_label",
    "worker_page",
    "global_page",
    "page_row",
    "global_order",
    "audio_id",
    "word",
    "pos",
    "sense_code",
    "accent",
    "file_name",
    "pronunciation_key",
    "status",
    "source_note",
    "issue_note",
    "saved_at",
    "drive_url",
]


def secret_value(name: str, default: str = ""):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return os.environ.get(name, default)


def secret_text(name: str, default: str = "") -> str:
    value = secret_value(name, default)
    if value is None:
        return default
    return str(value)


def elevenlabs_key() -> str:
    return secret_text("ELEVENLABS_API_KEY") or secret_text("elevenlabs_key")


def gemini_key() -> str:
    return secret_text("GEMINI_API_KEY") or secret_text("gemini_key")


def clean_text(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def slug(value: str) -> str:
    value = clean_text(value)
    value = re.sub(r'[\\/:*?"<>|]+', "_", value)
    value = re.sub(r"\s+", "_", value)
    return value[:80] or "word"


def row_key(row: pd.Series) -> str:
    audio_id = clean_text(row.get("audio_id"))
    if audio_id:
        return f"id:{audio_id}"
    return f"file:{clean_text(row.get('file_name'))}"


def audio_file_name(word: str, sense_code: str, accent: str, seq: int) -> str:
    sense = slug(sense_code) if clean_text(sense_code) else f"S{seq:09d}"
    return f"{slug(word)}_{sense}_{clean_text(accent).upper()}.mp3"


def dictionary_tts_text(word: str, pos: str = "") -> str:
    raw = clean_text(word).rstrip(".!?")
    lower = raw.lower()
    pos_lower = clean_text(pos).lower()
    if lower == "i":
        return "eye."
    if lower == "a":
        return "uh." if any(token in pos_lower for token in ("article", "determiner", "det")) else "ay."
    if lower == "the":
        return "thuh."
    return f"{raw}."


def voice_label(voice: dict) -> str:
    raw_gender = str(voice.get("gender") or "").lower()
    if "female" in raw_gender or "woman" in raw_gender:
        gender = "여성"
    elif "male" in raw_gender or "man" in raw_gender:
        gender = "남성"
    else:
        gender = "성별 미표시"
    accent = voice.get("accent") or "accent 미표시"
    age = voice.get("age") or ""
    parts = [f"[{gender}]", voice.get("name") or voice.get("voice_id") or "Voice", f"· {accent}"]
    if age:
        parts.append(f"· {age}")
    return " ".join(parts)


@st.cache_data(ttl=3600, show_spinner=False)
def list_elevenlabs_voices(api_key: str) -> list[dict]:
    if not api_key:
        return []
    request = urllib.request.Request(
        "https://api.elevenlabs.io/v1/voices",
        headers={"xi-api-key": api_key, "accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ElevenLabs {exc.code}: {detail[:500]}") from exc
    voices = []
    for item in payload.get("voices", []):
        labels = item.get("labels") or {}
        voices.append(
            {
                "voice_id": item.get("voice_id") or "",
                "name": item.get("name") or "",
                "accent": labels.get("accent") or labels.get("descriptive") or "",
                "gender": labels.get("gender") or "",
                "age": labels.get("age") or "",
            }
        )
    return sorted([v for v in voices if v["voice_id"]], key=lambda v: (v["accent"].lower(), v["gender"].lower(), v["name"].lower()))


def tts_request(api_key: str, voice_id: str, text: str, model_id: str, variation: int = 0) -> bytes:
    url = "https://api.elevenlabs.io/v1/text-to-speech/" + urllib.parse.quote(voice_id) + "?output_format=mp3_44100_128"
    body = {
        "text": clean_text(text),
        "model_id": model_id or "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.95 if not variation else max(0.35, 0.65 - (variation % 4) * 0.07),
            "similarity_boost": 0.6,
            "style": 0.0 if not variation else 0.05,
            "use_speaker_boost": False,
        },
    }
    if variation:
        body["seed"] = (int(time.time() * 1000) + variation * 7919) % 2147483647
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"xi-api-key": api_key, "accept": "audio/mpeg", "content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ElevenLabs {exc.code}: {detail[:500]}") from exc


def normalize_upload(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [clean_text(col).lstrip("\ufeff").lower() for col in df.columns]
    for column in REQUIRED_COLUMNS:
        if column not in df.columns:
            df[column] = ""
    for column in df.columns:
        df[column] = df[column].fillna("").astype(str).map(clean_text)
    df["accent"] = df["accent"].str.upper()
    df["worker_page"] = pd.to_numeric(df["worker_page"], errors="coerce").fillna(1).astype(int)
    df["page_row"] = pd.to_numeric(df["page_row"], errors="coerce").fillna(0).astype(int)
    if (df["page_row"] <= 0).any():
        df["page_row"] = df.groupby("worker_page").cumcount() + 1
    for index, row in df.iterrows():
        if not clean_text(row["sense_code"]):
            df.at[index, "sense_code"] = f"S{index + 1:09d}"
        if not clean_text(row["file_name"]):
            df.at[index, "file_name"] = audio_file_name(row["word"], df.at[index, "sense_code"], row["accent"], index + 1)
        if not clean_text(row["pronunciation_key"]):
            df.at[index, "pronunciation_key"] = f"same|{clean_text(row['word']).lower()}"
        if not clean_text(row["status"]):
            df.at[index, "status"] = "pending"
    return df[REQUIRED_COLUMNS]


def load_uploaded_excel(uploaded_file) -> pd.DataFrame:
    raw = pd.read_excel(uploaded_file, dtype=str)
    return normalize_upload(raw)


def get_df() -> pd.DataFrame | None:
    return st.session_state.get("work_df")


def set_df(df: pd.DataFrame) -> None:
    st.session_state["work_df"] = df


def get_audios() -> dict[str, bytes]:
    if "audios" not in st.session_state:
        st.session_state["audios"] = {}
    return st.session_state["audios"]


def current_voice_config() -> dict | None:
    return st.session_state.get("voice_config")


def render_voice_selector() -> None:
    api_key = elevenlabs_key()
    model_id = st.text_input("Model ID", value="eleven_multilingual_v2")
    if not api_key:
        st.error("Streamlit Secrets에 ELEVENLABS_API_KEY가 필요합니다.")
        return
    if st.button("ElevenLabs 보이스 불러오기"):
        with st.spinner("보이스 목록을 불러오는 중입니다..."):
            st.session_state["voices"] = list_elevenlabs_voices(api_key)
    if "voices" not in st.session_state:
        st.session_state["voices"] = list_elevenlabs_voices(api_key)
    voices = st.session_state.get("voices", [])
    if not voices:
        st.warning("불러온 보이스가 없습니다.")
        return
    american = [v for v in voices if "american" in str(v.get("accent") or "").lower()] or voices
    british = [v for v in voices if "british" in str(v.get("accent") or "").lower()] or voices
    us_options = {voice_label(v): v for v in american}
    uk_options = {voice_label(v): v for v in british}
    col1, col2 = st.columns(2)
    us_label = col1.selectbox("American Voice", list(us_options))
    uk_label = col2.selectbox("British Voice", list(uk_options))
    sample = st.text_input("미리듣기 단어", value="adder")
    p1, p2, p3 = st.columns([1, 1, 1])
    if p1.button("US 미리듣기"):
        st.audio(tts_request(api_key, us_options[us_label]["voice_id"], dictionary_tts_text(sample), model_id), format="audio/mp3")
    if p2.button("UK 미리듣기"):
        st.audio(tts_request(api_key, uk_options[uk_label]["voice_id"], dictionary_tts_text(sample), model_id), format="audio/mp3")
    if p3.button("이 성우로 적용", type="primary"):
        st.session_state["voice_config"] = {
            "api_key": api_key,
            "model_id": model_id,
            "voice_us": us_options[us_label]["voice_id"],
            "voice_uk": uk_options[uk_label]["voice_id"],
            "voice_us_label": us_label,
            "voice_uk_label": uk_label,
        }
        st.success("현재 성우 설정을 생성/재생성에 적용했습니다.")
    config = current_voice_config()
    if config:
        st.caption(f"적용됨: US {config['voice_us_label']} / UK {config['voice_uk_label']}")


def render_google_diagnostics() -> None:
    with st.expander("Google 연결 확인", expanded=False):
        email = google_service_account_email()
        st.write("앱이 사용하는 서비스 계정")
        st.code(email or "서비스 계정 정보를 읽지 못했습니다.")
        st.caption("이 이메일이 작업자1/2 Google Sheet와 음원 저장 폴더에 편집자로 공유되어 있어야 합니다.")
        if st.button("Google Sheet 권한 테스트"):
            try:
                drive, sheets = google_clients()
                test_rows = []
                for label, spreadsheet_id, sheet_name in [
                    ("작업자1", secret_text("GOOGLE_SHEET_ID_WORKER_1"), secret_text("GOOGLE_WORKSHEET_NAME_WORKER_1", "worker_1_upload")),
                    ("작업자2", secret_text("GOOGLE_SHEET_ID_WORKER_2"), secret_text("GOOGLE_WORKSHEET_NAME_WORKER_2", "worker_2_upload")),
                ]:
                    if not spreadsheet_id:
                        test_rows.append({"대상": label, "결과": "시트 ID 없음"})
                        continue
                    values = sheets.spreadsheets().values().get(
                        spreadsheetId=spreadsheet_id,
                        range=f"{sheet_name}!A1:A1",
                        fields="values",
                    ).execute(num_retries=0)
                    test_rows.append({"대상": label, "결과": "접근 가능", "시트ID": spreadsheet_id, "A1": values.get("values", [[""]])[0][0] if values.get("values") else ""})
                folder_id = secret_text("GOOGLE_DRIVE_FOLDER_ID")
                if folder_id:
                    folder = drive.files().get(fileId=folder_id, fields="id,name").execute(num_retries=0)
                    test_rows.append({"대상": "Drive 폴더", "결과": "접근 가능", "시트ID": folder.get("id", ""), "A1": folder.get("name", "")})
                st.dataframe(pd.DataFrame(test_rows), use_container_width=True)
            except Exception as exc:
                st.error(str(exc))


def page_rows(df: pd.DataFrame, page: int) -> pd.DataFrame:
    page_df = df[df["worker_page"] == page].copy()
    if page_df.empty:
        start = (page - 1) * PAGE_SIZE
        page_df = df.iloc[start : start + PAGE_SIZE].copy()
    return page_df


def split_review_rows(page_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    seen: set[tuple[str, str]] = set()
    main_indexes = []
    reuse_indexes = []
    for index, row in page_df.iterrows():
        pron_key = clean_text(row.get("pronunciation_key")) or f"id:{index}"
        key = (clean_text(row.get("accent")).upper(), pron_key)
        if key in seen:
            reuse_indexes.append(index)
        else:
            seen.add(key)
            main_indexes.append(index)
    return page_df.loc[main_indexes].copy(), page_df.loc[reuse_indexes].copy()


def update_rows(indexes: list[int], **updates) -> None:
    df = get_df()
    if df is None:
        return
    for index in indexes:
        for key, value in updates.items():
            if key in df.columns:
                df.at[index, key] = str(value)
    set_df(df)


def generate_page(page_df: pd.DataFrame, force: bool = False) -> tuple[int, int]:
    config = current_voice_config()
    if not config:
        raise RuntimeError("먼저 성우를 선택하고 '이 성우로 적용'을 눌러 주세요.")
    audios = get_audios()
    page_cache: dict[tuple[str, str], bytes] = {}
    targets = page_df.index.tolist()
    progress = st.progress(0)
    status = st.empty()
    ok = 0
    failed = 0
    for offset, index in enumerate(targets, start=1):
        df = get_df()
        row = df.loc[index]
        key = row_key(row)
        if not force and key in audios and clean_text(row["status"]) in {"검수중", "저장완료"}:
            continue
        cache_key = (row["accent"], row["pronunciation_key"])
        status.write(f"{offset}/{len(targets)} 생성 중: {row['word']} {row['accent']}")
        try:
            if cache_key in page_cache:
                audio = page_cache[cache_key]
            else:
                voice_id = config["voice_us"] if row["accent"] == "US" else config["voice_uk"]
                audio = tts_request(
                    config["api_key"],
                    voice_id,
                    dictionary_tts_text(row["word"], row["pos"]),
                    config["model_id"],
                    variation=(offset if force else 0),
                )
                page_cache[cache_key] = audio
            audios[key] = audio
            update_rows([index], status="검수중", source_note=f"generated {time.strftime('%Y-%m-%d %H:%M:%S')}")
            ok += 1
        except Exception as exc:
            update_rows([index], status="failed", source_note=str(exc)[:500])
            failed += 1
        progress.progress(offset / len(targets))
        time.sleep(0.05)
    status.write(f"생성 완료: 성공 {ok}개 / 실패 {failed}개")
    return ok, failed


def regenerate_row(index: int, page_df: pd.DataFrame) -> None:
    config = current_voice_config()
    if not config:
        raise RuntimeError("먼저 성우를 적용해 주세요.")
    df = get_df()
    row = df.loc[index]
    regen_counts = st.session_state.setdefault("regen_counts", {})
    key_for_count = row_key(row)
    regen_counts[key_for_count] = int(regen_counts.get(key_for_count, 0)) + 1
    voice_id = config["voice_us"] if row["accent"] == "US" else config["voice_uk"]
    variation = int(index) + int(time.time()) + (regen_counts[key_for_count] * 1009)
    audio = tts_request(config["api_key"], voice_id, dictionary_tts_text(row["word"], row["pos"]), config["model_id"], variation=variation)
    matched = page_df[(page_df["accent"] == row["accent"]) & (page_df["pronunciation_key"] == row["pronunciation_key"])].index.tolist()
    audios = get_audios()
    for item_index in matched:
        audios[row_key(df.loc[item_index])] = audio
    update_rows(matched, status="검수중", source_note=f"regenerated {time.strftime('%Y-%m-%d %H:%M:%S')}")


def build_page_zip(page_df: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    audios = get_audios()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        log_rows = []
        for index, row in page_df.iterrows():
            key = row_key(row)
            if key in audios:
                zf.writestr(row["file_name"], audios[key])
            log_rows.append(row.to_dict())
        zf.writestr("page_log.csv", pd.DataFrame(log_rows).to_csv(index=False, encoding="utf-8-sig"))
    return buffer.getvalue()


def render_inline_play_button(audio_bytes: bytes, button_id: str) -> None:
    encoded = base64.b64encode(audio_bytes).decode("ascii")
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", button_id)
    components.html(
        f"""
        <button id="btn_{safe_id}" style="
            height:34px;
            padding:0 12px;
            border:1px solid #cbd5e1;
            border-radius:6px;
            background:#ffffff;
            color:#0f172a;
            font-size:13px;
            cursor:pointer;
        ">재생</button>
        <span id="state_{safe_id}" style="margin-left:6px;font-size:12px;color:#64748b;"></span>
        <audio id="audio_{safe_id}" preload="auto" src="data:audio/mpeg;base64,{encoded}"></audio>
        <script>
        const btn = document.getElementById("btn_{safe_id}");
        const state = document.getElementById("state_{safe_id}");
        const audio = document.getElementById("audio_{safe_id}");
        btn.onclick = async () => {{
            try {{
                audio.currentTime = 0;
                state.textContent = "재생중";
                await audio.play();
            }} catch (e) {{
                state.textContent = "재생 실패";
            }}
        }};
        audio.onended = () => {{ state.textContent = ""; }};
        </script>
        """,
        height=42,
    )


def google_enabled() -> bool:
    return bool(
        (secret_text("GOOGLE_SERVICE_ACCOUNT_JSON_B64") or secret_value("GOOGLE_SERVICE_ACCOUNT_JSON", ""))
        and secret_text("GOOGLE_DRIVE_FOLDER_ID", "")
    )


def google_clients():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    info = google_service_account_info()
    scopes = ["https://www.googleapis.com/auth/drive.file", "https://www.googleapis.com/auth/spreadsheets"]
    creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    return build("drive", "v3", credentials=creds), build("sheets", "v4", credentials=creds)


def google_service_account_info() -> dict:
    raw_b64 = secret_text("GOOGLE_SERVICE_ACCOUNT_JSON_B64")
    if raw_b64:
        return json.loads(base64.b64decode(raw_b64).decode("utf-8"))
    raw = secret_value("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


def google_service_account_email() -> str:
    try:
        return clean_text(google_service_account_info().get("client_email", ""))
    except Exception:
        return ""


def col_letter(index: int) -> str:
    out = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def ensure_sheet_columns(service, spreadsheet_id: str, sheet_name: str, required: list[str]) -> list[str]:
    result = service.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=f"{sheet_name}!1:1").execute()
    headers = result.get("values", [[]])[0]
    changed = False
    for column in required:
        if column not in headers:
            headers.append(column)
            changed = True
    if changed:
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_name}!1:1",
            valueInputOption="RAW",
            body={"values": [headers]},
        ).execute()
    return headers


def google_sheet_target(row: pd.Series) -> tuple[str, str]:
    worker_id = clean_text(row.get("worker_id"))
    spreadsheet_id = ""
    sheet_name = ""
    if worker_id:
        spreadsheet_id = secret_text(f"GOOGLE_SHEET_ID_WORKER_{worker_id}")
        sheet_name = secret_text(f"GOOGLE_WORKSHEET_NAME_WORKER_{worker_id}")
    spreadsheet_id = spreadsheet_id or secret_text("GOOGLE_SHEET_ID")
    sheet_name = sheet_name or secret_text("GOOGLE_WORKSHEET_NAME", "Sheet1")
    return spreadsheet_id, sheet_name


def ensure_sheet_exists(service, spreadsheet_id: str, sheet_name: str, headers: list[str] | None = None) -> None:
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    titles = {sheet["properties"]["title"] for sheet in meta.get("sheets", [])}
    if sheet_name not in titles:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]},
        ).execute()
    if headers:
        result = service.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=f"{sheet_name}!1:1").execute()
        current = result.get("values", [[]])[0]
        if not current:
            service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{sheet_name}!1:1",
                valueInputOption="RAW",
                body={"values": [headers]},
            ).execute()


def sync_status_from_google(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    if df.empty:
        return df, 0
    _drive, sheets = google_clients()
    synced = 0
    next_df = df.copy()
    sync_columns = ["status", "issue_note", "saved_at", "drive_url"]
    for _worker_id, group in next_df.groupby("worker_id", dropna=False):
        spreadsheet_id, sheet_name = google_sheet_target(group.iloc[0])
        if not spreadsheet_id:
            continue
        try:
            result = sheets.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=f"{sheet_name}!A1:Z").execute()
        except Exception:
            continue
        values = result.get("values", [])
        if not values:
            continue
        headers = [clean_text(item).lstrip("\ufeff").lower() for item in values[0]]
        if "audio_id" not in headers and "file_name" not in headers:
            continue
        rows_by_audio = {}
        rows_by_file = {}
        for sheet_row in values[1:]:
            padded = sheet_row + [""] * (len(headers) - len(sheet_row))
            record = {headers[i]: clean_text(padded[i]) for i in range(len(headers))}
            if record.get("audio_id"):
                rows_by_audio[record["audio_id"]] = record
            if record.get("file_name"):
                rows_by_file[record["file_name"]] = record
        for index, row in group.iterrows():
            record = rows_by_audio.get(clean_text(row.get("audio_id"))) or rows_by_file.get(clean_text(row.get("file_name")))
            if not record:
                continue
            changed = False
            for column in sync_columns:
                value = clean_text(record.get(column))
                if value:
                    next_df.at[index, column] = value
                    changed = True
            if changed:
                synced += 1
    return next_df, synced


def first_incomplete_page(df: pd.DataFrame) -> int:
    done_statuses = {"저장완료", "이상표시"}
    for page in sorted(df["worker_page"].astype(int).unique().tolist()):
        page_df = page_rows(df, int(page))
        if not page_df.empty and not page_df["status"].isin(done_statuses).all():
            return int(page)
    pages = sorted(df["worker_page"].astype(int).unique().tolist())
    return int(pages[-1]) if pages else 1


def update_google_sheet(row: pd.Series, updates: dict) -> None:
    spreadsheet_id, sheet_name = google_sheet_target(row)
    if not spreadsheet_id:
        return
    _, sheets = google_clients()
    headers = ensure_sheet_columns(sheets, spreadsheet_id, sheet_name, ["status", "issue_note", "saved_at", "drive_url"])
    values = sheets.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=f"{sheet_name}!A1:{col_letter(len(headers) - 1)}").execute().get("values", [])
    audio_col = headers.index("audio_id") if "audio_id" in headers else -1
    file_col = headers.index("file_name") if "file_name" in headers else -1
    target_row = None
    for pos, sheet_row in enumerate(values[1:], start=2):
        audio_match = audio_col >= 0 and audio_col < len(sheet_row) and clean_text(sheet_row[audio_col]) == clean_text(row.get("audio_id"))
        file_match = file_col >= 0 and file_col < len(sheet_row) and clean_text(sheet_row[file_col]) == clean_text(row.get("file_name"))
        if audio_match or file_match:
            target_row = pos
            break
    if target_row is None:
        return
    for key, value in updates.items():
        if key not in headers:
            continue
        col = headers.index(key)
        sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_name}!{col_letter(col)}{target_row}",
            valueInputOption="RAW",
            body={"values": [[value]]},
        ).execute()


def append_issue_sheet(row: pd.Series, note: str) -> None:
    spreadsheet_id, _sheet_name = google_sheet_target(row)
    issue_sheet = secret_text("GOOGLE_ISSUE_SHEET_NAME", "Issues")
    if not spreadsheet_id:
        return
    _, sheets = google_clients()
    values = [[time.strftime("%Y-%m-%d %H:%M:%S"), row.get("worker_label", ""), row.get("worker_page", ""), row.get("word", ""), row.get("sense_code", ""), row.get("accent", ""), row.get("file_name", ""), note]]
    sheets.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{issue_sheet}!A:H",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()


def append_progress_sheet(page_df: pd.DataFrame, saved: int, skipped: int) -> None:
    if page_df.empty:
        return
    spreadsheet_id, _sheet_name = google_sheet_target(page_df.iloc[0])
    if not spreadsheet_id:
        return
    _drive, sheets = google_clients()
    progress_sheet = secret_text("GOOGLE_PROGRESS_SHEET_NAME", "Progress")
    headers = ["submitted_at", "worker_id", "worker_label", "worker_page", "global_page", "total_rows", "saved", "skipped_or_issue"]
    ensure_sheet_exists(sheets, spreadsheet_id, progress_sheet, headers)
    first = page_df.iloc[0]
    values = [[
        time.strftime("%Y-%m-%d %H:%M:%S"),
        first.get("worker_id", ""),
        first.get("worker_label", ""),
        first.get("worker_page", ""),
        first.get("global_page", ""),
        len(page_df),
        saved,
        skipped,
    ]]
    sheets.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{progress_sheet}!A:H",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()


def save_page_to_google(page_df: pd.DataFrame) -> tuple[int, int]:
    if not google_enabled():
        raise RuntimeError("Google Drive 연동을 쓰려면 GOOGLE_SERVICE_ACCOUNT_JSON, GOOGLE_DRIVE_FOLDER_ID가 필요합니다.")
    from googleapiclient.http import MediaIoBaseUpload

    drive, _ = google_clients()
    folder_id = secret_text("GOOGLE_DRIVE_FOLDER_ID")
    audios = get_audios()
    saved = 0
    skipped = 0
    issues = 0
    for index, row in page_df.iterrows():
        if clean_text(row.get("status")) == "이상표시":
            note = clean_text(row.get("issue_note")) or f"발음 이상 표시 {time.strftime('%Y-%m-%d %H:%M:%S')}"
            update_google_sheet(row, {"status": "이상표시", "issue_note": note})
            append_issue_sheet(row, note)
            skipped += 1
            issues += 1
            continue
        key = row_key(row)
        if key not in audios:
            skipped += 1
            continue
        media = MediaIoBaseUpload(io.BytesIO(audios[key]), mimetype="audio/mpeg", resumable=False)
        file = drive.files().create(
            body={"name": row["file_name"], "parents": [folder_id]},
            media_body=media,
            fields="id,webViewLink",
        ).execute()
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        update_rows([index], status="저장완료", saved_at=now, drive_url=file.get("webViewLink", ""))
        update_google_sheet(row, {"status": "저장완료", "saved_at": now, "drive_url": file.get("webViewLink", "")})
        saved += 1
    append_progress_sheet(page_df, saved, skipped)
    return saved, skipped


def analyze_page_with_gemini(page_df: pd.DataFrame) -> int:
    key = gemini_key()
    if not key:
        raise RuntimeError("GEMINI_API_KEY가 필요합니다.")
    updates = {}
    for word, group in page_df.groupby(page_df["word"].str.lower()):
        if len(group) <= 2:
            continue
        senses = [
            {"idx": int(idx), "word": row["word"], "pos": row["pos"], "sense_code": row["sense_code"]}
            for idx, row in group.iterrows()
        ]
        prompt = (
            "Group English word senses by identical headword pronunciation. "
            "Return JSON only: {\"items\":[{\"idx\":0,\"pronunciation_key\":\"same|word\"}]}. "
            "Use different keys only when pronunciation/stress differs by meaning or part of speech.\n"
            + json.dumps(senses, ensure_ascii=False)
        )
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?" + urllib.parse.urlencode({"key": key})
        body = json.dumps({"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1}}).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers={"content-type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                payload = json.loads(response.read().decode("utf-8"))
            text = payload["candidates"][0]["content"]["parts"][0]["text"]
            for item in json.loads(text).get("items", []):
                updates[int(item["idx"])] = clean_text(item["pronunciation_key"])
        except Exception:
            continue
    df = get_df()
    for idx, value in updates.items():
        if idx in df.index and value:
            df.at[idx, "pronunciation_key"] = value
    set_df(df)
    return len(updates)


def render_rows(page_df: pd.DataFrame) -> None:
    audios = get_audios()
    if page_df.empty:
        st.info("표시할 행이 없습니다.")
        return
    for index, row in page_df.iterrows():
        cols = st.columns([2.0, 1.1, 0.7, 2.8, 1.0, 1.0, 1.0, 1.0])
        cols[0].markdown(f"**{row['word']}**  \n<small>{row['pos']}</small>", unsafe_allow_html=True)
        cols[1].write(row["sense_code"])
        cols[2].write(row["accent"])
        cols[3].caption(row["file_name"])
        cols[4].write(row["status"])
        key = row_key(row)
        with cols[5]:
            if key in audios:
                audio_hash = str(abs(hash(audios[key])))[-10:]
                render_inline_play_button(audios[key], f"play_{index}_{key}_{audio_hash}")
        with cols[6]:
            if st.button("이상 표시", key=f"issue_{index}"):
                note = f"발음 이상 표시 {time.strftime('%Y-%m-%d %H:%M:%S')}"
                update_rows([index], status="이상표시", issue_note=note)
                st.rerun()
        with cols[7]:
            if st.button("재생성", key=f"regen_{index}"):
                try:
                    with st.spinner("재생성 중..."):
                        regenerate_row(index, page_df)
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))


def main() -> None:
    st.set_page_config(page_title="단어 음원 외주 검수", layout="wide")
    st.title("단어 음원 외주 검수")

    with st.expander("1. 엑셀 업로드", expanded=get_df() is None):
        uploaded = st.file_uploader("검수할 엑셀을 업로드해 주세요.", type=["xlsx", "xls"])
        if uploaded and st.button("엑셀 불러오기", type="primary"):
            df = load_uploaded_excel(uploaded)
            synced = 0
            if secret_text("GOOGLE_SERVICE_ACCOUNT_JSON_B64") or secret_value("GOOGLE_SERVICE_ACCOUNT_JSON", ""):
                try:
                    df, synced = sync_status_from_google(df)
                except Exception as exc:
                    st.warning(f"Google Sheet 기존 작업 기록을 불러오지 못했습니다: {exc}")
            set_df(df)
            st.session_state["current_page"] = first_incomplete_page(df)
            st.session_state["audios"] = {}
            st.success(f"{uploaded.name}에서 {len(df):,}개 음원 행을 불러왔습니다. 기존 기록 {synced:,}개를 반영했습니다.")
            st.rerun()
        st.caption("Gemini API Key는 Secrets에서 자동으로 읽습니다. 현재 페이지 단위로 발음 재사용 키 보정을 실행할 수 있습니다.")

    with st.expander("2. 성우 선택 / 미리듣기", expanded=current_voice_config() is None):
        render_voice_selector()

    render_google_diagnostics()

    df = get_df()
    if df is None:
        st.info("먼저 검수할 엑셀을 업로드해 주세요.")
        return

    pages = sorted(df["worker_page"].astype(int).unique().tolist())
    default_page = int(st.session_state.get("current_page", first_incomplete_page(df)))
    default_page = min(max(default_page, min(pages)), max(pages))
    page_cols = st.columns([1.2, 1, 2.8])
    with page_cols[0]:
        page = st.number_input("엑셀 기준 페이지", min_value=min(pages), max_value=max(pages), value=default_page, step=1)
    with page_cols[1]:
        st.metric("전체 페이지", f"{len(pages):,}")
    with page_cols[2]:
        st.caption(f"현재 위치: {int(page):,} / {max(pages):,}페이지")
    st.session_state["current_page"] = int(page)
    page_df = page_rows(df, int(page))
    main_page_df, reuse_page_df = split_review_rows(page_df)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("현재 페이지 행", len(page_df))
    c2.metric("생성됨", sum(row_key(row) in get_audios() for _, row in page_df.iterrows()))
    c3.metric("이상표시", int((df["status"] == "이상표시").sum()))
    c4.metric("저장완료", int((df["status"] == "저장완료").sum()))

    action_cols = st.columns([1.1, 1.2, 1.2, 1.1, 1.2, 2.0])
    if action_cols[0].button("현재 페이지 생성", type="primary", use_container_width=True):
        try:
            ok, failed = generate_page(page_df, force=False)
            st.success(f"생성 완료: 성공 {ok}개 / 실패 {failed}개")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))
    if action_cols[1].button("현재 페이지 전체 재생성", use_container_width=True):
        try:
            ok, failed = generate_page(page_df, force=True)
            st.success(f"재생성 완료: 성공 {ok}개 / 실패 {failed}개")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))
    if action_cols[2].button("Gemini 발음 재사용 분석", use_container_width=True):
        try:
            count = analyze_page_with_gemini(page_df)
            st.success(f"발음 재사용 키 {count}개를 보정했습니다.")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))
    with action_cols[3]:
        st.download_button("현재 페이지 ZIP", build_page_zip(page_df), file_name=f"page_{int(page):03d}.zip", mime="application/zip", use_container_width=True)
    if action_cols[4].button("현재 페이지 저장", use_container_width=True):
        if google_enabled():
            try:
                saved, skipped = save_page_to_google(page_df)
                st.success(f"Google Drive 저장 완료: {saved}개 / 미생성 {skipped}개")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
        else:
            st.warning("Google Drive 설정이 없어서 자동 저장을 못 했습니다. ZIP 다운로드를 사용해 주세요.")
    with action_cols[5]:
        output = io.BytesIO()
        df.to_excel(output, index=False)
        st.download_button("상태 반영 엑셀 다운로드", output.getvalue(), file_name="review_status.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    st.divider()
    st.subheader("검수 목록")
    st.caption("같은 발음으로 묶인 sense는 대표 행만 여기에서 검수합니다.")
    render_rows(main_page_df)
    with st.expander(f"재사용 예정 파일 {len(reuse_page_df)}개", expanded=False):
        st.caption("아래 행들은 위 대표 음원을 같은 발음으로 재사용하되, 저장 시 각 sense_code 파일명으로 따로 저장됩니다.")
        render_rows(reuse_page_df)


if __name__ == "__main__":
    main()
