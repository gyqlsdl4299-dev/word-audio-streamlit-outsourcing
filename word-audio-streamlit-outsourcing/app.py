from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import pandas as pd
import streamlit as st


APP_ROOT = Path(__file__).resolve().parent
DEFAULT_JOB_ID = "baa4b56a8fb4"
PAGE_SIZE = 50
WORKERS = {
    "1": {"label": "작업자 1", "start": 101, "end": 353},
    "2": {"label": "작업자 2", "start": 354, "end": 606},
}


def secret_value(name: str, default: str = "") -> str:
    try:
        return str(st.secrets.get(name, default))
    except Exception:
        return os.environ.get(name, default)


def elevenlabs_key() -> str:
    return secret_value("ELEVENLABS_API_KEY") or secret_value("elevenlabs_key")


def clean_text(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def voice_label(voice: dict) -> str:
    gender_map = {
        "female": "여성",
        "woman": "여성",
        "male": "남성",
        "man": "남성",
    }
    gender_raw = str(voice.get("gender") or "").lower()
    gender = next((label for key, label in gender_map.items() if key in gender_raw), "성별 미표시")
    accent = voice.get("accent") or "accent 미표시"
    age = voice.get("age") or ""
    name = voice.get("name") or voice.get("voice_id") or "Voice"
    parts = [f"[{gender}]", name, f"· {accent}"]
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
    except Exception as exc:
        raise RuntimeError(f"ElevenLabs 보이스를 불러오지 못했습니다: {exc}") from exc

    voices: list[dict] = []
    for item in payload.get("voices", []):
        labels = item.get("labels") or {}
        voices.append(
            {
                "voice_id": item.get("voice_id") or "",
                "name": item.get("name") or "",
                "accent": labels.get("accent") or labels.get("descriptive") or "",
                "gender": labels.get("gender") or "",
                "age": labels.get("age") or "",
                "category": item.get("category") or "",
            }
        )
    voices = [voice for voice in voices if voice["voice_id"]]
    voices.sort(key=lambda voice: (voice["accent"].lower(), voice["gender"].lower(), voice["name"].lower()))
    return voices


def elevenlabs_preview(api_key: str, voice_id: str, text: str, model_id: str) -> bytes:
    text = (text or "adder").strip()
    if not api_key or not voice_id:
        raise RuntimeError("API Key와 보이스를 먼저 선택해 주세요.")
    url = (
        "https://api.elevenlabs.io/v1/text-to-speech/"
        f"{urllib.parse.quote(voice_id)}?output_format=mp3_44100_128"
    )
    body = json.dumps(
        {
            "text": text,
            "model_id": model_id or "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.95,
                "similarity_boost": 0.65,
                "style": 0.0,
                "use_speaker_boost": False,
            },
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "xi-api-key": api_key,
            "accept": "audio/mpeg",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ElevenLabs {exc.code}: {detail[:500]}") from exc
    except Exception as exc:
        raise RuntimeError(f"미리듣기 생성에 실패했습니다: {exc}") from exc


def render_voice_selector(worker_id: str) -> None:
    api_key = elevenlabs_key()
    if not api_key:
        api_key = st.text_input(
            "ElevenLabs API Key",
            type="password",
            key=f"elevenlabs_key_{worker_id}",
            help="Streamlit Cloud에서는 Secrets에 ELEVENLABS_API_KEY로 넣으면 자동 적용됩니다.",
        )
    model_id = st.text_input("Model ID", value="eleven_multilingual_v2", key=f"model_id_{worker_id}")

    if not api_key:
        st.info("성우 목록과 미리듣기를 쓰려면 ElevenLabs API Key가 필요합니다.")
        return

    col_load, col_hint = st.columns([1, 3])
    with col_load:
        load_clicked = st.button("ElevenLabs 보이스 불러오기", key=f"load_voices_{worker_id}")
    with col_hint:
        st.caption("작업자별 선택값은 각각 따로 유지됩니다. 다른 사람이 다른 브라우저에서 열면 독립적으로 선택됩니다.")

    voices_key = f"voices_{worker_id}"
    if load_clicked or voices_key not in st.session_state:
        with st.spinner("ElevenLabs 보이스를 불러오는 중입니다..."):
            try:
                st.session_state[voices_key] = list_elevenlabs_voices(api_key)
            except RuntimeError as exc:
                st.error(str(exc))
                st.session_state[voices_key] = []

    voices = st.session_state.get(voices_key, [])
    if not voices:
        st.warning("불러온 보이스가 없습니다. API Key 권한을 확인해 주세요.")
        return

    american = [voice for voice in voices if "american" in str(voice.get("accent") or "").lower()]
    british = [voice for voice in voices if "british" in str(voice.get("accent") or "").lower()]
    american = american or voices
    british = british or voices

    us_options = {voice_label(voice): voice for voice in american}
    uk_options = {voice_label(voice): voice for voice in british}
    select_cols = st.columns(2)
    us_choice = select_cols[0].selectbox("American Voice", list(us_options), key=f"us_voice_label_{worker_id}")
    uk_choice = select_cols[1].selectbox("British Voice", list(uk_options), key=f"uk_voice_label_{worker_id}")
    selected_us = us_options[us_choice]
    selected_uk = uk_options[uk_choice]
    st.session_state[f"us_voice_id_{worker_id}"] = selected_us["voice_id"]
    st.session_state[f"uk_voice_id_{worker_id}"] = selected_uk["voice_id"]

    apply_cols = st.columns([1, 3])
    with apply_cols[0]:
        if st.button("이 성우로 적용", type="primary", key=f"apply_voices_{worker_id}"):
            st.session_state[f"voice_config_{worker_id}"] = {
                "api_key": api_key,
                "model_id": model_id or "eleven_multilingual_v2",
                "voice_us": selected_us["voice_id"],
                "voice_uk": selected_uk["voice_id"],
                "voice_us_label": voice_label(selected_us),
                "voice_uk_label": voice_label(selected_uk),
            }
            st.success("현재 작업자의 생성/재생성 성우로 적용했습니다.")
    config = st.session_state.get(f"voice_config_{worker_id}")
    with apply_cols[1]:
        if config:
            st.caption(f"적용됨: US {config['voice_us_label']} / UK {config['voice_uk_label']}")
        else:
            st.caption("미리듣기 후 '이 성우로 적용'을 눌러야 생성/재생성 버튼을 사용할 수 있습니다.")

    sample = st.text_input("미리듣기 단어", value="adder", key=f"preview_text_{worker_id}")
    preview_cols = st.columns(2)
    with preview_cols[0]:
        if st.button("US 미리듣기", key=f"preview_us_{worker_id}"):
            try:
                audio = elevenlabs_preview(api_key, st.session_state[f"us_voice_id_{worker_id}"], sample, model_id)
                st.audio(audio, format="audio/mp3")
            except RuntimeError as exc:
                st.error(str(exc))
    with preview_cols[1]:
        if st.button("UK 미리듣기", key=f"preview_uk_{worker_id}"):
            try:
                audio = elevenlabs_preview(api_key, st.session_state[f"uk_voice_id_{worker_id}"], sample, model_id)
                st.audio(audio, format="audio/mp3")
            except RuntimeError as exc:
                st.error(str(exc))


def data_dir() -> Path:
    configured = secret_value("DATA_DIR")
    if configured:
        return Path(configured)
    local = APP_ROOT / "data"
    if (local / "word_audio.sqlite3").exists():
        return local
    sibling = APP_ROOT.parent / "word-audio-app" / "data"
    return sibling


DATA_DIR = data_dir()
DB_PATH = DATA_DIR / "word_audio.sqlite3"
AUDIO_DIR = DATA_DIR / "audio"
LOG_DIR = APP_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma busy_timeout=30000")
    return conn


@st.cache_data(show_spinner=False)
def audio_order(job_id: str = DEFAULT_JOB_ID) -> list[int]:
    with connect() as conn:
        rows = conn.execute(
            """
            select audio.id
            from audio join groups on audio.group_id=groups.id
            where audio.job_id=?
            order by groups.word collate nocase, groups.sense_code, audio.id
            """,
            (job_id,),
        ).fetchall()
    return [int(row["id"]) for row in rows]


def page_audio_ids(worker_id: str, local_page: int) -> tuple[dict, int, list[int]]:
    worker = WORKERS[worker_id]
    total_pages = worker["end"] - worker["start"] + 1
    local_page = min(total_pages, max(1, int(local_page)))
    global_page = worker["start"] + local_page - 1
    offset = (global_page - 1) * PAGE_SIZE
    ids = audio_order()[offset : offset + PAGE_SIZE]
    return worker, global_page, ids


def fetch_audio_rows(audio_ids: list[int]) -> list[dict]:
    if not audio_ids:
        return []
    placeholders = ",".join("?" for _ in audio_ids)
    with connect() as conn:
        rows = conn.execute(
            f"""
            select audio.*, groups.word, groups.sense_code, groups.pos, groups.pronunciation_key,
                   groups.ipa_us, groups.ipa_uk
            from audio join groups on audio.group_id=groups.id
            where audio.job_id=? and audio.id in ({placeholders})
            """,
            [DEFAULT_JOB_ID, *audio_ids],
        ).fetchall()
    by_id = {int(row["id"]): dict(row) for row in rows}
    payload = [by_id[item_id] for item_id in audio_ids if item_id in by_id]
    mark_reuse_pending(payload)
    return payload


def mark_reuse_pending(rows: list[dict]) -> None:
    keys = sorted({row.get("pronunciation_key") for row in rows if row.get("pronunciation_key")})
    first_ids: dict[tuple[str, str], int] = {}
    if keys:
        placeholders = ",".join("?" for _ in keys)
        with connect() as conn:
            first_rows = conn.execute(
                f"""
                select audio.accent, groups.pronunciation_key, min(audio.id) first_id
                from audio join groups on audio.group_id=groups.id
                where audio.job_id=? and groups.pronunciation_key in ({placeholders})
                group by audio.accent, groups.pronunciation_key
                """,
                [DEFAULT_JOB_ID, *keys],
            ).fetchall()
        first_ids = {
            (row["accent"], row["pronunciation_key"]): int(row["first_id"])
            for row in first_rows
        }
    for row in rows:
        first_id = first_ids.get((row.get("accent"), row.get("pronunciation_key")))
        row["reuse_pending"] = bool(first_id is not None and int(row["id"]) != first_id)


def audio_path(row: dict) -> Path:
    if row.get("file_path") and Path(row["file_path"]).exists():
        return Path(row["file_path"])
    return AUDIO_DIR / str(row["job_id"]) / str(row["accent"]) / str(row["file_name"])


def dictionary_tts_text(word: str, pos: str = "", accent: str = "", variant_index: int = 0) -> str:
    raw = clean_text(word).rstrip(".!?")
    lower = raw.lower()
    pos_lower = clean_text(pos).lower()
    if lower == "i":
        return "eye."
    if lower == "a":
        if any(token in pos_lower for token in ("article", "determiner", "det")):
            return "uh."
        return "ay."
    if lower == "the":
        return "thuh."
    if lower in {"an", "am", "is", "are", "was", "were"}:
        return f"{lower}."
    return f"{raw}."


def current_voice_config(worker_id: str) -> dict | None:
    config = st.session_state.get(f"voice_config_{worker_id}")
    if not config:
        return None
    required = ("api_key", "voice_us", "voice_uk", "model_id")
    if any(not config.get(key) for key in required):
        return None
    return config


def tts_request(api_key: str, voice_id: str, text: str, model_id: str, seed: int | None = None, variation: int = 0) -> bytes:
    url = (
        "https://api.elevenlabs.io/v1/text-to-speech/"
        f"{urllib.parse.quote(voice_id)}?output_format=mp3_44100_128"
    )
    stability = 0.95 if not variation else max(0.35, 0.65 - (variation % 4) * 0.07)
    body = {
        "text": clean_text(text),
        "model_id": model_id or "eleven_multilingual_v2",
        "voice_settings": {
            "stability": stability,
            "similarity_boost": 0.6,
            "style": 0.0 if not variation else 0.05,
            "use_speaker_boost": False,
        },
    }
    if seed is not None:
        body["seed"] = int(seed)
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "xi-api-key": api_key,
            "accept": "audio/mpeg",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ElevenLabs {exc.code}: {detail[:500]}") from exc


def find_reusable_audio(row: dict) -> Path | None:
    pron_key = row.get("pronunciation_key")
    if not pron_key:
        return None
    with connect() as conn:
        match = conn.execute(
            """
            select audio.file_path
            from audio
            join groups on audio.group_id=groups.id
            where audio.job_id=?
              and audio.id<>?
              and audio.accent=?
              and audio.status in ('done','review')
              and groups.pronunciation_key=?
              and audio.file_path is not null
            order by audio.created_at asc
            limit 1
            """,
            (row["job_id"], row["id"], row["accent"], pron_key),
        ).fetchone()
    if not match:
        return None
    path = Path(match["file_path"])
    return path if path.exists() else None


def save_generated_audio(row: dict, source: bytes | Path, note: str, regen_count: int | None = None) -> Path:
    out_dir = AUDIO_DIR / str(row["job_id"]) / str(row["accent"])
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / str(row["file_name"])
    if isinstance(source, Path):
        shutil.copyfile(source, out_path)
    else:
        out_path.write_bytes(source)
    status = "review"
    params: tuple
    if regen_count is None:
        params = (status, str(out_path), note, time.time(), row["id"])
        sql = "update audio set status=?, file_path=?, error=?, created_at=? where id=?"
    else:
        params = (status, str(out_path), note, time.time(), regen_count, row["id"])
        sql = "update audio set status=?, file_path=?, error=?, created_at=?, regen_count=? where id=?"
    with connect() as conn:
        conn.execute(sql, params)
        conn.commit()
    return out_path


def generate_single_audio(row: dict, config: dict, force: bool = False, page_cache: dict | None = None) -> str:
    accent = str(row.get("accent") or "").upper()
    voice_id = config["voice_us"] if accent == "US" else config["voice_uk"]
    page_cache = page_cache if page_cache is not None else {}
    cache_key = (accent, row.get("pronunciation_key") or f"id:{row['id']}")

    if not force:
        reusable = find_reusable_audio(row)
        if reusable:
            save_generated_audio(row, reusable, f"재사용: {reusable.name}")
            return "재사용"
    elif cache_key in page_cache:
        save_generated_audio(row, page_cache[cache_key], f"현재 페이지 재생성 재사용: {page_cache[cache_key].name}")
        return "재사용"

    regen_count = int(row.get("regen_count") or 0)
    if force:
        regen_count += 1
    seed = None
    variation = 0
    if force:
        variation = regen_count
        seed = (int(time.time() * 1000) + int(row["id"]) * 997 + regen_count * 7919) % 2147483647
    tts_text = dictionary_tts_text(row["word"], row.get("pos") or "", accent, regen_count)
    audio = tts_request(config["api_key"], voice_id, tts_text, config["model_id"], seed, variation)
    old_hash = ""
    old_path = audio_path(row)
    if force and old_path.exists():
        old_hash = hashlib.sha1(old_path.read_bytes()).hexdigest()
    new_hash = hashlib.sha1(audio).hexdigest()
    out_path = save_generated_audio(
        row,
        audio,
        f"생성 입력: {tts_text} / 성우 적용 / {new_hash[:8]}",
        regen_count if force else None,
    )
    page_cache[cache_key] = out_path
    return "재생성" if force and old_hash != new_hash else "생성"


def generate_page_audio(rows: list[dict], worker_id: str, force: bool = False) -> tuple[int, int]:
    config = current_voice_config(worker_id)
    if not config:
        raise RuntimeError("먼저 성우를 선택하고 '이 성우로 적용'을 눌러 주세요.")
    targets = []
    for row in rows:
        path = audio_path(row)
        if force or row.get("status") in {"pending", "failed", "generating"} or not path.exists():
            targets.append(row)
    if not targets:
        return (0, 0)

    progress = st.progress(0)
    status = st.empty()
    ok = 0
    failed = 0
    page_cache: dict[tuple[str, str], Path] = {}
    for index, row in enumerate(targets, start=1):
        status.write(f"{index}/{len(targets)} 생성 중: {row['word']} {row['accent']}")
        with connect() as conn:
            conn.execute("update audio set status='generating', error='' where id=?", (row["id"],))
            conn.commit()
        try:
            generate_single_audio(row, config, force=force, page_cache=page_cache)
            ok += 1
        except Exception as exc:
            failed += 1
            with connect() as conn:
                conn.execute("update audio set status='failed', error=? where id=?", (str(exc)[:1000], row["id"]))
                conn.commit()
        progress.progress(index / len(targets))
        time.sleep(0.05)
    status.write(f"생성 완료: 성공 {ok}개 / 실패 {failed}개")
    audio_order.clear()
    return ok, failed


def status_label(row: dict) -> str:
    if row.get("reuse_pending"):
        return "재사용 예정"
    mapping = {
        "pending": "대기",
        "review": "검수중",
        "done": "완료",
        "failed": "실패",
        "needs_manual": "이상 표시",
        "generating": "생성중",
    }
    return mapping.get(str(row.get("status") or ""), str(row.get("status") or "-"))


def log_issue(row: dict, note: str, worker_id: str, page: int, global_page: int) -> None:
    path = LOG_DIR / "issues.csv"
    exists = path.exists()
    fields = [
        "created_at",
        "worker_id",
        "local_page",
        "global_page",
        "audio_id",
        "word",
        "sense_code",
        "accent",
        "file_name",
        "note",
    ]
    with path.open("a", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow(
            {
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "worker_id": worker_id,
                "local_page": page,
                "global_page": global_page,
                "audio_id": row["id"],
                "word": row["word"],
                "sense_code": row["sense_code"],
                "accent": row["accent"],
                "file_name": row["file_name"],
                "note": note,
            }
        )


def mark_issue(audio_id: int, note: str) -> None:
    with connect() as conn:
        conn.execute(
            "update audio set status='needs_manual', error=?, created_at=? where id=?",
            (note, time.time(), audio_id),
        )
        conn.commit()


def clear_issue(audio_id: int) -> None:
    with connect() as conn:
        row = conn.execute("select file_path from audio where id=?", (audio_id,)).fetchone()
        next_status = "review" if row and row["file_path"] else "pending"
        conn.execute(
            "update audio set status=?, error='', created_at=? where id=?",
            (next_status, time.time(), audio_id),
        )
        conn.commit()


def build_page_zip(rows: list[dict], worker_id: str, page: int, global_page: int) -> bytes:
    issue_rows = []
    log_rows = []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for row in rows:
            src = audio_path(row)
            if src.exists():
                zf.write(src, row["file_name"])
            log_rows.append(
                {
                    "worker_id": worker_id,
                    "local_page": page,
                    "global_page": global_page,
                    "word": row["word"],
                    "sense_code": row["sense_code"],
                    "accent": row["accent"],
                    "file_name": row["file_name"],
                    "status": status_label(row),
                    "note": row.get("error") or "",
                }
            )
            if row.get("status") == "needs_manual" or row.get("error"):
                issue_rows.append(log_rows[-1])
        zf.writestr("page_log.csv", dataframe_csv(log_rows))
        zf.writestr("issues.csv", dataframe_csv(issue_rows))
    return buffer.getvalue()


def dataframe_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    return pd.DataFrame(rows).to_csv(index=False, encoding="utf-8-sig")


def render_rows(rows: list[dict], worker_id: str, page: int, global_page: int, title: str) -> None:
    st.subheader(title)
    if not rows:
        st.info("표시할 음원이 없습니다.")
        return
    for row in rows:
        cols = st.columns([2.2, 1.4, 0.8, 3.0, 1.0, 1.0, 1.2, 1.2])
        cols[0].markdown(f"**{row['word']}**  \n<small>{row.get('pos') or ''}</small>", unsafe_allow_html=True)
        cols[1].write(row.get("sense_code") or "-")
        cols[2].write(row.get("accent") or "-")
        cols[3].caption(row.get("file_name") or "-")
        cols[4].write(status_label(row))
        path = audio_path(row)
        if path.exists():
            with cols[5]:
                if st.button("재생", key=f"play_{row['id']}"):
                    st.audio(path.read_bytes(), format="audio/mp3")
        else:
            cols[5].write("-")
        with cols[6]:
            if row.get("status") == "needs_manual":
                if st.button("이상 해제", key=f"clear_{row['id']}"):
                    clear_issue(int(row["id"]))
                    st.rerun()
            else:
                if st.button("이상 표시", key=f"issue_{row['id']}"):
                    st.session_state["issue_target"] = int(row["id"])
                    st.session_state["issue_worker"] = worker_id
                    st.session_state["issue_page"] = page
                    st.session_state["issue_global_page"] = global_page
                    st.rerun()
        with cols[7]:
            if st.button("재생성", key=f"regen_{row['id']}"):
                config = current_voice_config(worker_id)
                if not config:
                    st.error("먼저 성우를 선택하고 '이 성우로 적용'을 눌러 주세요.")
                else:
                    with st.spinner(f"{row['word']} {row['accent']} 재생성 중..."):
                        try:
                            generate_single_audio(row, config, force=True, page_cache={})
                            st.success("재생성 완료")
                            st.rerun()
                        except Exception as exc:
                            with connect() as conn:
                                conn.execute("update audio set status='failed', error=? where id=?", (str(exc)[:1000], row["id"]))
                                conn.commit()
                            st.error(str(exc))


def issue_box(rows: list[dict]) -> None:
    target = st.session_state.get("issue_target")
    if not target:
        return
    row = next((item for item in rows if int(item["id"]) == int(target)), None)
    if not row:
        return
    with st.form("issue_form", clear_on_submit=True):
        st.warning(f"이상 표시: {row['word']} / {row['sense_code']} / {row['accent']}")
        note = st.text_area("메모", value="발음 이상")
        submitted = st.form_submit_button("이상 표시 저장")
        cancelled = st.form_submit_button("취소")
    if submitted:
        mark_issue(int(row["id"]), note)
        log_issue(
            row,
            note,
            st.session_state.get("issue_worker", ""),
            int(st.session_state.get("issue_page", 1)),
            int(st.session_state.get("issue_global_page", 0)),
        )
        st.session_state.pop("issue_target", None)
        st.rerun()
    if cancelled:
        st.session_state.pop("issue_target", None)
        st.rerun()


def main() -> None:
    st.set_page_config(page_title="0623 단어 음원 외주 검수", layout="wide")
    st.title("0623 단어 음원 외주 검수")

    if not DB_PATH.exists():
        st.error(f"DB 파일을 찾을 수 없습니다: {DB_PATH}")
        st.stop()

    worker_label = st.radio("작업자 선택", ["작업자 1", "작업자 2"], horizontal=True)
    worker_id = "1" if worker_label == "작업자 1" else "2"
    worker = WORKERS[worker_id]
    total_pages = worker["end"] - worker["start"] + 1

    top_cols = st.columns([1, 1, 2])
    page = top_cols[0].number_input("작업자 페이지", min_value=1, max_value=total_pages, value=1, step=1)
    worker, global_page, ids = page_audio_ids(worker_id, int(page))
    top_cols[1].metric("전체 페이지", f"{global_page}")
    top_cols[2].caption(f"{worker['label']} 범위: 전체 {worker['start']}~{worker['end']}페이지")

    with st.expander("성우 선택 / 미리듣기", expanded=True):
        render_voice_selector(worker_id)

    rows = fetch_audio_rows(ids)
    main_rows = [row for row in rows if not row.get("reuse_pending")]
    reuse_rows = [row for row in rows if row.get("reuse_pending")]

    m1, m2, m3 = st.columns(3)
    m1.metric("현재 페이지 검수 대상", len(main_rows))
    m2.metric("재사용 예정", len(reuse_rows))
    m3.metric("현재 페이지 전체", len(rows))

    st.markdown("### 현재 페이지 작업")
    applied_config = current_voice_config(worker_id)
    if applied_config:
        st.caption(f"생성 성우: US {applied_config['voice_us_label']} / UK {applied_config['voice_uk_label']}")
    else:
        st.warning("성우 선택 / 미리듣기에서 '이 성우로 적용'을 먼저 눌러 주세요.")

    action_cols = st.columns([1.2, 1.4, 1.2, 2.2])
    with action_cols[0]:
        if st.button("현재 페이지 생성", type="primary", disabled=not applied_config, use_container_width=True):
            try:
                ok, failed = generate_page_audio(rows, worker_id, force=False)
                st.success(f"현재 페이지 생성 완료: 성공 {ok}개 / 실패 {failed}개")
                st.rerun()
            except RuntimeError as exc:
                st.error(str(exc))
    with action_cols[1]:
        if st.button("현재 페이지 전체 재생성", disabled=not applied_config, use_container_width=True):
            if st.session_state.get(f"confirm_force_regen_{worker_id}_{page}"):
                try:
                    ok, failed = generate_page_audio(rows, worker_id, force=True)
                    st.session_state.pop(f"confirm_force_regen_{worker_id}_{page}", None)
                    st.success(f"전체 재생성 완료: 성공 {ok}개 / 실패 {failed}개")
                    st.rerun()
                except RuntimeError as exc:
                    st.error(str(exc))
            else:
                st.session_state[f"confirm_force_regen_{worker_id}_{page}"] = True
                st.warning("한 번 더 누르면 현재 페이지 음원을 선택 성우로 모두 다시 생성합니다.")
    with action_cols[2]:
        zip_bytes = build_page_zip(rows, worker_id, int(page), global_page)
        st.download_button(
            "현재 페이지 ZIP",
            data=zip_bytes,
            file_name=f"worker_{worker_id}_page_{int(page):03d}_global_{global_page}.zip",
            mime="application/zip",
            use_container_width=True,
        )
    with action_cols[3]:
        st.caption("생성: 대기/실패/파일 없음만 처리합니다. 전체 재생성: 선택한 성우로 페이지 파일을 덮어씁니다.")

    issue_box(rows)
    render_rows(main_rows, worker_id, int(page), global_page, "검수 목록")
    with st.expander(f"재사용 예정 목록 {len(reuse_rows)}개", expanded=False):
        render_rows(reuse_rows, worker_id, int(page), global_page, "재사용 예정")


if __name__ == "__main__":
    main()
