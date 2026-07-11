"""Local voice transcription helpers (mlx-whisper + ffmpeg transcode).

Split out of capture_daemon.py: these two functions are self-contained (no daemon
state, no logging) — the pure transcode/transcribe primitives. The daemon's
`_handle_voice` orchestration and `route_voice` (which log + touch the vault) stay
in capture_daemon.

`large-v3` + an explicit language. Runs on the Mac's Apple Silicon (mlx), which has
the headroom — on short Singaporean-accented clips `medium` mis-heard ("childcare
for my kid" → "toolkit for my kit") and even `large-v3-turbo` slipped ("chao kei for
my key"); only full large-v3 transcribed it correctly. A few extra seconds per short
clip is worth the accuracy. Auto-detect on short clips mis-fired (English → Malay)
and drifted into repetition loops ("first first first…"); pinning language +
condition_on_previous_text=False fixes both. Model + language are
settings-overridable (whisper_model / voice_language — see _handle_voice).
"""

from __future__ import annotations

import subprocess

_WHISPER_MODEL = "mlx-community/whisper-large-v3-mlx"
_VOICE_LANGUAGE = "en"


def transcribe_wav(wav_path: str, language: str = _VOICE_LANGUAGE, model: str | None = None) -> str:
    """Transcribe a wav with mlx-whisper. `language` is passed explicitly (no auto-detect
    misfire) and condition_on_previous_text=False suppresses repetition-loop
    hallucinations. Weights auto-download once."""
    import mlx_whisper
    out = mlx_whisper.transcribe(
        wav_path, path_or_hf_repo=model or _WHISPER_MODEL,
        language=language, condition_on_previous_text=False)
    return (out.get("text") or "").strip()


def oga_to_wav(oga_path: str, wav_path: str) -> str:
    """ffmpeg: .oga → 16 kHz mono wav (what whisper wants)."""
    subprocess.run(
        ["ffmpeg", "-y", "-i", oga_path, "-ar", "16000", "-ac", "1", wav_path],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return wav_path
