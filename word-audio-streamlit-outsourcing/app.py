from __future__ import annotations

import io
import hashlib
import html
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
PREFERRED_VOICE_OPTIONS = {
    "US": [
        ("Matilda", "여자(US): Matilda - Agent, Professional, Audiobook"),
        ("Will", "남자(US): Will - Relaxed Optimist"),
    ],
    "UK": [
        ("Casey", "여자(UK): Casey - Clean, Crisp and Friendly"),
        ("George", "남자(UK): George - Warm, Captivating Storyteller"),
    ],
}


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


def sheet_value(value):
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, (list, tuple, set, dict)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


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


def dictionary_voice_settings(variation: int = 0) -> dict:
    # Keep isolated headwords flat and dictionary-like. Regeneration changes only
    # the seed, not expressive style, so it does not drift into conversational intonation.
    return {
        "stability": 1.0 if not variation else 0.92,
        "similarity_boost": 0.55,
        "style": 0.0,
        "use_speaker_boost": False,
    }


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


def preferred_voice_options(voices: list[dict], accent: str) -> dict[str, dict]:
    options = {}
    by_name = {clean_text(voice.get("name")).lower(): voice for voice in voices}
    for voice_name, label in PREFERRED_VOICE_OPTIONS[accent]:
        target = voice_name.lower()
        voice = by_name.get(target)
        if not voice:
            voice = next((v for v in voices if clean_text(v.get("name")).lower().startswith(target)), None)
        if voice:
            options[label] = voice
    return options


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
        "voice_settings": dictionary_voice_settings(variation),
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
    us_options = preferred_voice_options(voices, "US")
    uk_options = preferred_voice_options(voices, "UK")
    if not us_options or not uk_options:
        missing = []
        if not us_options:
            missing.append("US: Matilda, Will")
        if not uk_options:
            missing.append("UK: Casey, George")
        st.error("지정 성우를 ElevenLabs 계정에서 찾지 못했습니다: " + " / ".join(missing))
        return
    allowed_voice_ids = {voice["voice_id"] for voice in [*us_options.values(), *uk_options.values()]}
    config = current_voice_config()
    if config and (config.get("voice_us") not in allowed_voice_ids or config.get("voice_uk") not in allowed_voice_ids):
        st.session_state.pop("voice_config", None)
        config = None
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
                    folder = drive.files().get(fileId=folder_id, fields="id,name", supportsAllDrives=True).execute(num_retries=0)
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
    audios = get_audios()
    old_audio = audios.get(key_for_count, b"")
    old_hash = hashlib.sha1(old_audio).hexdigest() if old_audio else ""
    audio = b""
    new_hash = ""
    tts_text = dictionary_tts_text(row["word"], row["pos"])
    for attempt in range(2):
        variation = int(index) + int(time.time()) + (regen_counts[key_for_count] * 1009) + (attempt * 17011)
        audio = tts_request(config["api_key"], voice_id, tts_text, config["model_id"], variation=variation)
        new_hash = hashlib.sha1(audio).hexdigest()
        if not old_hash or new_hash != old_hash:
            break
    matched = page_df[(page_df["accent"] == row["accent"]) & (page_df["pronunciation_key"] == row["pronunciation_key"])].index.tolist()
    for item_index in matched:
        audios[row_key(df.loc[item_index])] = audio
    note = f"regenerated {time.strftime('%Y-%m-%d %H:%M:%S')} / {new_hash[:8]}"
    if old_hash and new_hash == old_hash:
        note += " / provider returned same audio"
    update_rows(matched, status="검수중", source_note=note)


def build_page_zip(page_df: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    audios = get_audios()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        log_rows = []
        for index, row in page_df.iterrows():
            if clean_text(row.get("status")) == "이상표시":
                log_rows.append(row.to_dict())
                continue
            key = row_key(row)
            if key in audios:
                zf.writestr(row["file_name"], audios[key])
            log_rows.append(row.to_dict())
        zf.writestr("page_log.csv", pd.DataFrame(log_rows).to_csv(index=False, encoding="utf-8-sig"))
    return buffer.getvalue()


def page_audio_stats(page_df: pd.DataFrame) -> tuple[int, int, int]:
    audios = get_audios()
    ready = 0
    savable = 0
    issue = 0
    for _, row in page_df.iterrows():
        if clean_text(row.get("status")) == "이상표시":
            issue += 1
            continue
        savable += 1
        if row_key(row) in audios:
            ready += 1
    return ready, savable, issue


def validate_page_zip_ready(page_df: pd.DataFrame) -> None:
    ready, savable, _ = page_audio_stats(page_df)
    if savable <= 0:
        raise RuntimeError("저장할 정상 음원이 없습니다. 모든 행이 이상표시 상태인지 확인해 주세요.")
    if ready < savable:
        raise RuntimeError(f"현재 페이지 음원 {savable}개 중 {ready}개만 생성되어 있습니다. 먼저 '현재 페이지 생성'을 완료해 주세요.")


def page_zip_file_name(page_df: pd.DataFrame, page: int, for_drive: bool = False) -> str:
    worker_id = "unknown"
    if not page_df.empty:
        worker_id = clean_text(page_df.iloc[0].get("worker_id")) or worker_id
    worker_id = re.sub(r"[^0-9A-Za-z_-]+", "_", worker_id).strip("_") or "unknown"
    suffix = "_audio" if for_drive else ""
    return f"worker_{worker_id}_page_{int(page):03d}{suffix}.zip"


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


def google_save_enabled() -> bool:
    return bool(secret_text("GOOGLE_APPS_SCRIPT_UPLOAD_URL") or google_enabled())


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
            body={"values": [[sheet_value(value)]]},
        ).execute()


def append_issue_sheet(row: pd.Series, note: str) -> None:
    spreadsheet_id, _sheet_name = google_sheet_target(row)
    issue_sheet = secret_text("GOOGLE_ISSUE_SHEET_NAME", "Issues")
    if not spreadsheet_id:
        return
    _, sheets = google_clients()
    values = [[sheet_value(item) for item in [time.strftime("%Y-%m-%d %H:%M:%S"), row.get("worker_label", ""), row.get("worker_page", ""), row.get("word", ""), row.get("sense_code", ""), row.get("accent", ""), row.get("file_name", ""), note]]]
    sheets.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{issue_sheet}!A:H",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()


def batch_update_google_sheet(row_updates: list[tuple[pd.Series, dict]]) -> None:
    if not row_updates:
        return
    grouped: dict[tuple[str, str], list[tuple[pd.Series, dict]]] = {}
    required_columns = {"status", "issue_note", "saved_at", "drive_url"}
    for row, updates in row_updates:
        spreadsheet_id, sheet_name = google_sheet_target(row)
        if not spreadsheet_id:
            continue
        required_columns.update(updates.keys())
        grouped.setdefault((spreadsheet_id, sheet_name), []).append((row, updates))

    for (spreadsheet_id, sheet_name), items in grouped.items():
        _, sheets = google_clients()
        headers = ensure_sheet_columns(sheets, spreadsheet_id, sheet_name, list(required_columns))
        values = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_name}!A1:{col_letter(len(headers) - 1)}",
        ).execute().get("values", [])
        audio_col = headers.index("audio_id") if "audio_id" in headers else -1
        file_col = headers.index("file_name") if "file_name" in headers else -1
        rows_by_audio = {}
        rows_by_file = {}
        for pos, sheet_row in enumerate(values[1:], start=2):
            if audio_col >= 0 and audio_col < len(sheet_row):
                rows_by_audio[clean_text(sheet_row[audio_col])] = pos
            if file_col >= 0 and file_col < len(sheet_row):
                rows_by_file[clean_text(sheet_row[file_col])] = pos

        data = []
        for row, updates in items:
            target_row = rows_by_audio.get(clean_text(row.get("audio_id"))) or rows_by_file.get(clean_text(row.get("file_name")))
            if not target_row:
                continue
            for key, value in updates.items():
                if key not in headers:
                    continue
                col = headers.index(key)
                data.append({
                    "range": f"{sheet_name}!{col_letter(col)}{target_row}",
                    "values": [[sheet_value(value)]],
                })
        if data:
            sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"valueInputOption": "RAW", "data": data},
            ).execute()


def append_issue_sheets(issue_rows: list[tuple[pd.Series, str]]) -> None:
    if not issue_rows:
        return
    grouped: dict[str, list[list]] = {}
    for row, note in issue_rows:
        spreadsheet_id, _sheet_name = google_sheet_target(row)
        if not spreadsheet_id:
            continue
        grouped.setdefault(spreadsheet_id, []).append([
            sheet_value(item)
            for item in [
                time.strftime("%Y-%m-%d %H:%M:%S"),
                row.get("worker_label", ""),
                row.get("worker_page", ""),
                row.get("word", ""),
                row.get("sense_code", ""),
                row.get("accent", ""),
                row.get("file_name", ""),
                note,
            ]
        ])
    for spreadsheet_id, values in grouped.items():
        _, sheets = google_clients()
        issue_sheet = secret_text("GOOGLE_ISSUE_SHEET_NAME", "Issues")
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
    values = [[sheet_value(item) for item in [
        time.strftime("%Y-%m-%d %H:%M:%S"),
        first.get("worker_id", ""),
        first.get("worker_label", ""),
        first.get("worker_page", ""),
        first.get("global_page", ""),
        len(page_df),
        saved,
        skipped,
    ]]]
    sheets.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{progress_sheet}!A:H",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()


def upload_file_via_apps_script(file_name: str, file_bytes: bytes, mime_type: str = "audio/mpeg") -> str:
    url = secret_text("GOOGLE_APPS_SCRIPT_UPLOAD_URL")
    token = secret_text("GOOGLE_APPS_SCRIPT_TOKEN")
    if not url:
        raise RuntimeError("GOOGLE_APPS_SCRIPT_UPLOAD_URL이 설정되지 않았습니다.")
    payload = {
        "token": token,
        "file_name": file_name,
        "mime_type": mime_type,
        "content_b64": base64.b64encode(file_bytes).decode("ascii"),
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code == 403 and ("Access Denied" in detail or "DOCTYPE html" in detail):
            raise RuntimeError(
                "Apps Script 접근이 차단되었습니다. Apps Script 배포 설정에서 "
                "'실행 사용자: 나', '액세스 권한: 모든 사용자'로 배포한 뒤 새 웹 앱 URL을 Secrets에 넣어 주세요."
            ) from exc
        raise RuntimeError(f"Apps Script upload {exc.code}: {detail[:500]}") from exc
    if not result.get("ok"):
        raise RuntimeError(f"Apps Script upload failed: {result}")
    return clean_text(result.get("url") or result.get("webViewLink") or "")


def upload_audio_via_apps_script(file_name: str, audio_bytes: bytes) -> str:
    return upload_file_via_apps_script(file_name, audio_bytes, "audio/mpeg")


def save_page_to_google(page_df: pd.DataFrame) -> tuple[int, int]:
    use_apps_script = bool(secret_text("GOOGLE_APPS_SCRIPT_UPLOAD_URL"))
    if not use_apps_script and not google_enabled():
        raise RuntimeError("Google Drive 저장을 쓰려면 GOOGLE_APPS_SCRIPT_UPLOAD_URL 또는 GOOGLE_SERVICE_ACCOUNT_JSON/GOOGLE_DRIVE_FOLDER_ID가 필요합니다.")

    drive = None
    if not use_apps_script:
        from googleapiclient.http import MediaIoBaseUpload
        drive, _ = google_clients()
    folder_id = secret_text("GOOGLE_DRIVE_FOLDER_ID")
    audios = get_audios()
    saved = 0
    skipped = 0
    sheet_updates = []
    issue_rows = []
    for index, row in page_df.iterrows():
        if clean_text(row.get("status")) == "이상표시":
            note = clean_text(row.get("issue_note")) or f"발음 이상 표시 {time.strftime('%Y-%m-%d %H:%M:%S')}"
            sheet_updates.append((row, {"status": "이상표시", "issue_note": note}))
            issue_rows.append((row, note))
            skipped += 1
            continue
        key = row_key(row)
        if key not in audios:
            skipped += 1
            continue
        if use_apps_script:
            drive_url = upload_audio_via_apps_script(row["file_name"], audios[key])
        else:
            media = MediaIoBaseUpload(io.BytesIO(audios[key]), mimetype="audio/mpeg", resumable=False)
            file = drive.files().create(
                body={"name": row["file_name"], "parents": [folder_id]},
                media_body=media,
                fields="id,webViewLink",
                supportsAllDrives=True,
            ).execute()
            drive_url = file.get("webViewLink", "")
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        update_rows([index], status="저장완료", saved_at=now, drive_url=drive_url)
        sheet_updates.append((row, {"status": "저장완료", "saved_at": now, "drive_url": drive_url}))
        saved += 1
    batch_update_google_sheet(sheet_updates)
    append_issue_sheets(issue_rows)
    append_progress_sheet(page_df, saved, skipped)
    return saved, skipped


def submit_page_zip_status(page_df: pd.DataFrame) -> tuple[int, int]:
    validate_page_zip_ready(page_df)
    audios = get_audios()
    saved = 0
    skipped = 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    sheet_updates = []
    issue_rows = []
    for index, row in page_df.iterrows():
        if clean_text(row.get("status")) == "이상표시":
            note = clean_text(row.get("issue_note")) or f"발음 이상 표시 {now}"
            update_rows([index], status="이상표시", issue_note=note)
            sheet_updates.append((row, {"status": "이상표시", "issue_note": note}))
            issue_rows.append((row, note))
            skipped += 1
            continue
        key = row_key(row)
        if key not in audios:
            skipped += 1
            continue
        update_rows([index], status="저장완료", saved_at=now, drive_url="ZIP 다운로드")
        sheet_updates.append((row, {"status": "저장완료", "saved_at": now, "drive_url": "ZIP 다운로드"}))
        saved += 1
    batch_update_google_sheet(sheet_updates)
    append_issue_sheets(issue_rows)
    append_progress_sheet(page_df, saved, skipped)
    return saved, skipped


def submit_page_zip_to_drive(page_df: pd.DataFrame, page: int) -> tuple[int, int, str]:
    if not secret_text("GOOGLE_APPS_SCRIPT_UPLOAD_URL"):
        raise RuntimeError("Google Drive ZIP 저장을 쓰려면 GOOGLE_APPS_SCRIPT_UPLOAD_URL이 필요합니다.")
    validate_page_zip_ready(page_df)
    zip_bytes = build_page_zip(page_df)
    file_name = page_zip_file_name(page_df, page, for_drive=True)
    drive_url = upload_file_via_apps_script(file_name, zip_bytes, "application/zip")

    audios = get_audios()
    saved = 0
    skipped = 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    sheet_updates = []
    issue_rows = []
    for index, row in page_df.iterrows():
        if clean_text(row.get("status")) == "이상표시":
            note = clean_text(row.get("issue_note")) or f"발음 이상 표시 {now}"
            update_rows([index], status="이상표시", issue_note=note)
            sheet_updates.append((row, {"status": "이상표시", "issue_note": note}))
            issue_rows.append((row, note))
            skipped += 1
            continue
        key = row_key(row)
        if key not in audios:
            skipped += 1
            continue
        update_rows([index], status="저장완료", saved_at=now, drive_url=drive_url)
        sheet_updates.append((row, {"status": "저장완료", "saved_at": now, "drive_url": drive_url}))
        saved += 1
    batch_update_google_sheet(sheet_updates)
    append_issue_sheets(issue_rows)
    append_progress_sheet(page_df, saved, skipped)
    return saved, skipped, drive_url


def handle_zip_status_submit(page_df: pd.DataFrame) -> None:
    try:
        saved, skipped = submit_page_zip_status(page_df)
        st.session_state["zip_submit_message"] = f"ZIP 기준 시트 반영 완료: 저장완료 {saved}개 / 이상·미생성 {skipped}개"
        st.session_state.pop("zip_submit_error", None)
    except Exception as exc:
        st.session_state["zip_submit_error"] = str(exc)
        st.session_state.pop("zip_submit_message", None)


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


def badge_html(label: str, bg: str, fg: str, border: str = "transparent") -> str:
    safe_label = html.escape(clean_text(label))
    return (
        f'<span style="display:inline-flex;align-items:center;justify-content:center;'
        f'min-width:54px;padding:5px 9px;border-radius:999px;'
        f'background:{bg};color:{fg};border:1px solid {border};'
        f'font-size:12px;font-weight:700;line-height:1;white-space:nowrap;">{safe_label}</span>'
    )


def status_badge_html(status: str) -> str:
    value = clean_text(status) or "pending"
    if value == "저장완료":
        return badge_html(value, "#dcfce7", "#166534", "#bbf7d0")
    if value == "이상표시":
        return badge_html(value, "#fee2e2", "#991b1b", "#fecaca")
    if value == "검수중":
        return badge_html(value, "#dbeafe", "#1d4ed8", "#bfdbfe")
    if value == "재사용 예정":
        return badge_html(value, "#f1f5f9", "#475569", "#e2e8f0")
    return badge_html(value, "#fff7ed", "#c2410c", "#fed7aa")


def accent_badge_html(accent: str) -> str:
    value = clean_text(accent).upper()
    if value == "US":
        return badge_html("US", "#eff6ff", "#1d4ed8", "#bfdbfe")
    if value == "UK":
        return badge_html("UK", "#f5f3ff", "#6d28d9", "#ddd6fe")
    return badge_html(value or "-", "#f8fafc", "#475569", "#e2e8f0")


def render_rows(page_df: pd.DataFrame) -> None:
    audios = get_audios()
    if page_df.empty:
        st.info("표시할 행이 없습니다.")
        return

    st.markdown(
        """
        <style>
        .review-head {
            color:#475569; font-size:12px; font-weight:800;
            padding:0 6px 6px 6px;
        }
        .review-word {font-size:15px;font-weight:800;color:#0f172a;line-height:1.25;}
        .review-meta {font-size:12px;color:#64748b;line-height:1.35;margin-top:2px;}
        .review-file {font-size:12px;color:#64748b;line-height:1.35;word-break:break-all;}
        .row-number {
            width:34px;height:34px;border-radius:8px;background:#f8fafc;
            border:1px solid #e2e8f0;color:#475569;display:flex;
            align-items:center;justify-content:center;font-size:12px;font-weight:800;
        }
        div[data-testid="stVerticalBlockBorderWrapper"] {
            border-color:#dbe3ef !important;
            box-shadow:0 1px 2px rgba(15,23,42,0.04);
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:hover {
            border-color:#94a3b8 !important;
            background:#fbfdff;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    header = st.columns([0.45, 1.8, 1.25, 0.75, 2.7, 1.05, 0.9, 1.05, 1.05])
    for col, label in zip(header, ["", "표제어", "Sense", "발음", "파일명", "상태", "재생", "검수", "재생성"]):
        col.markdown(f'<div class="review-head">{label}</div>', unsafe_allow_html=True)

    for position, (index, row) in enumerate(page_df.iterrows(), start=1):
        key = row_key(row)
        status = clean_text(row.get("status"))
        with st.container(border=True):
            cols = st.columns([0.45, 1.8, 1.25, 0.75, 2.7, 1.05, 0.9, 1.05, 1.05])
            row_no = clean_text(row.get("page_row")) or str(position)
            cols[0].markdown(f'<div class="row-number">{html.escape(row_no)}</div>', unsafe_allow_html=True)
            cols[1].markdown(
                f'<div class="review-word">{html.escape(clean_text(row.get("word")))}</div>'
                f'<div class="review-meta">{html.escape(clean_text(row.get("pos")))}</div>',
                unsafe_allow_html=True,
            )
            cols[2].markdown(
                f'<div class="review-meta"><b>{html.escape(clean_text(row.get("sense_code")))}</b></div>'
                f'<div class="review-meta">ID {html.escape(clean_text(row.get("audio_id")))}</div>',
                unsafe_allow_html=True,
            )
            cols[3].markdown(accent_badge_html(row.get("accent")), unsafe_allow_html=True)
            cols[4].markdown(f'<div class="review-file">{html.escape(clean_text(row.get("file_name")))}</div>', unsafe_allow_html=True)
            cols[5].markdown(status_badge_html(status), unsafe_allow_html=True)

            with cols[6]:
                if key in audios:
                    audio_hash = str(abs(hash(audios[key])))[-10:]
                    render_inline_play_button(audios[key], f"play_{index}_{key}_{audio_hash}")
                else:
                    st.caption("미생성")

            with cols[7]:
                is_issue = status == "이상표시"
                issue_label = "이상 해제" if is_issue else "이상 표시"
                if st.button(issue_label, key=f"issue_{index}", use_container_width=True):
                    if is_issue:
                        update_rows([index], status="검수중", issue_note="")
                    else:
                        note = f"발음 이상 표시 {time.strftime('%Y-%m-%d %H:%M:%S')}"
                        update_rows([index], status="이상표시", issue_note=note)
                    st.rerun()

            with cols[8]:
                if st.button("재생성", key=f"regen_{index}", use_container_width=True):
                    try:
                        regenerate_row(index, page_df)
                        st.toast("재생성 완료")
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
    ready_audio_count, savable_audio_count, issue_audio_count = page_audio_stats(page_df)
    c2.metric("생성됨", f"{ready_audio_count}/{savable_audio_count}")
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
        zip_ready = savable_audio_count > 0 and ready_audio_count >= savable_audio_count
        if zip_ready:
            st.download_button(
                "로컬 ZIP 저장",
                build_page_zip(page_df),
                file_name=page_zip_file_name(page_df, int(page)),
                mime="application/zip",
                use_container_width=True,
                on_click=handle_zip_status_submit,
                args=(page_df.copy(),),
            )
        else:
            st.button(
                "로컬 ZIP 저장",
                use_container_width=True,
                disabled=True,
                help=f"저장 가능 음원 {savable_audio_count}개 중 {ready_audio_count}개 생성됨",
            )
    google_zip_ready = savable_audio_count > 0 and ready_audio_count >= savable_audio_count
    if action_cols[4].button("Google ZIP 저장", use_container_width=True, disabled=not google_zip_ready):
        try:
            saved, skipped, drive_url = submit_page_zip_to_drive(page_df, int(page))
            st.session_state["google_zip_message"] = f"Google Drive ZIP 저장 완료: 저장완료 {saved}개 / 이상·미생성 {skipped}개"
            st.session_state["google_zip_url"] = drive_url
            st.session_state.pop("google_zip_error", None)
            st.rerun()
        except Exception as exc:
            st.session_state["google_zip_error"] = str(exc)
            st.session_state.pop("google_zip_message", None)
            st.session_state.pop("google_zip_url", None)
            st.rerun()
    if not google_zip_ready:
        action_cols[5].caption(f"ZIP 저장 전 현재 페이지 생성 필요: {ready_audio_count}/{savable_audio_count}개 생성됨")
    if st.session_state.get("zip_submit_message"):
        st.success(st.session_state.pop("zip_submit_message"))
    if st.session_state.get("zip_submit_error"):
        st.error(st.session_state.pop("zip_submit_error"))
    if st.session_state.get("google_zip_message"):
        st.success(st.session_state.pop("google_zip_message"))
    if st.session_state.get("google_zip_url"):
        st.link_button("저장된 ZIP 열기", st.session_state.pop("google_zip_url"))
    if st.session_state.get("google_zip_error"):
        st.error(st.session_state.pop("google_zip_error"))
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
