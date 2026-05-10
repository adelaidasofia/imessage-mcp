"""transcribe.py — Whisper local-cpp wrapper for iMessage audio messages.

iMessage voice notes are stored as `.caf` (Core Audio Format) attachments at
`~/Library/Messages/Attachments/<two-char>/<guid>/<filename>.caf`. whisper-cli
can read .caf directly when ffmpeg is available; otherwise we transcode first.

Reuses whatsapp-mcp's installed model and binary by default. Caches transcripts
in `~/.claude/imessage-mcp/transcripts.db` so retries don't re-run Whisper.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

# Defaults to the whatsapp-mcp installation when not overridden.
MODEL_PATH = Path(
    os.environ.get(
        "IMESSAGE_WHISPER_MODEL_PATH",
        os.environ.get(
            "WHATSAPP_WHISPER_MODEL_PATH",
            str(Path.home() / ".claude" / "whatsapp-mcp" / "models" / "ggml-large-v3.bin"),
        ),
    )
)
WHISPER_BIN = Path(
    os.environ.get(
        "IMESSAGE_WHISPER_BIN_PATH",
        os.environ.get("WHATSAPP_WHISPER_BIN_PATH", "/opt/homebrew/bin/whisper-cli"),
    )
)
LANGUAGE = os.environ.get("IMESSAGE_WHISPER_LANGUAGE", "en")

CACHE_DB = Path.home() / ".claude" / "imessage-mcp" / "transcripts.db"


def _open_cache() -> sqlite3.Connection:
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CACHE_DB), timeout=5.0)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transcripts (
            attachment_guid TEXT PRIMARY KEY,
            transcript TEXT,
            language TEXT,
            duration_ms INTEGER,
            ts REAL
        )
        """
    )
    return conn


def cached_transcript(attachment_guid: str) -> str | None:
    if not attachment_guid:
        return None
    try:
        with _open_cache() as conn:
            row = conn.execute(
                "SELECT transcript FROM transcripts WHERE attachment_guid = ?",
                (attachment_guid,),
            ).fetchone()
            return row[0] if row else None
    except Exception:
        return None


def store_transcript(
    attachment_guid: str, transcript: str, duration_ms: int, language: str
) -> None:
    try:
        with _open_cache() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO transcripts
                  (attachment_guid, transcript, language, duration_ms, ts)
                VALUES (?, ?, ?, ?, ?)
                """,
                (attachment_guid, transcript, language, duration_ms, time.time()),
            )
            conn.commit()
    except Exception:
        pass


def transcribe_audio(audio_path: Path, attachment_guid: str = "") -> str:
    """Run Whisper on an audio file. Returns the transcript or empty string.

    Caches by attachment_guid when provided. Falls back to re-running each
    time when the GUID is unknown.
    """
    if not audio_path.exists() or audio_path.stat().st_size == 0:
        return ""
    if attachment_guid:
        cached = cached_transcript(attachment_guid)
        if cached is not None:
            return cached
    if not WHISPER_BIN.exists():
        return ""
    if not MODEL_PATH.exists():
        return ""

    # whisper-cli accepts .caf via libsndfile/ffmpeg; if it errors we transcode.
    started = time.time()
    text = _run_whisper(audio_path)
    if not text and audio_path.suffix.lower() == ".caf":
        wav = _transcode_to_wav(audio_path)
        if wav:
            try:
                text = _run_whisper(wav)
            finally:
                try:
                    wav.unlink()
                except Exception:
                    pass

    duration_ms = int((time.time() - started) * 1000)
    if attachment_guid and text:
        store_transcript(attachment_guid, text, duration_ms, LANGUAGE)
    return text


def _run_whisper(audio_path: Path) -> str:
    """Call whisper-cli and capture text output."""
    with tempfile.TemporaryDirectory() as td:
        out_prefix = Path(td) / "out"
        cmd = [
            str(WHISPER_BIN),
            "-m",
            str(MODEL_PATH),
            "-f",
            str(audio_path),
            "-l",
            LANGUAGE,
            "-otxt",
            "-of",
            str(out_prefix),
            "-nt",
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            return ""
        if proc.returncode != 0:
            return ""
        txt_path = Path(str(out_prefix) + ".txt")
        if not txt_path.exists():
            return ""
        try:
            return txt_path.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            return ""


def _transcode_to_wav(audio_path: Path) -> Path | None:
    """Transcode .caf to 16kHz mono WAV via ffmpeg. Returns the temp file path."""
    if not shutil.which("ffmpeg"):
        return None
    out = Path(tempfile.mktemp(suffix=".wav"))
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(audio_path),
        "-ar",
        "16000",
        "-ac",
        "1",
        "-f",
        "wav",
        str(out),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0 or not out.exists():
        return None
    return out


def transcription_status() -> dict:
    """Surface backend, model, and cache stats for healthcheck."""
    return {
        "backend": "local-cpp",
        "binary_present": WHISPER_BIN.exists(),
        "model_present": MODEL_PATH.exists(),
        "model_path": str(MODEL_PATH),
        "language": LANGUAGE,
        "cache_path": str(CACHE_DB),
        "cache_size": _cache_size(),
    }


def _cache_size() -> int:
    try:
        with _open_cache() as conn:
            return conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
    except Exception:
        return 0
