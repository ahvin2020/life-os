"""Local voice transcription helpers (whisper + ffmpeg transcode).

Split out of capture_daemon.py: these two functions are self-contained (no daemon
state, no logging) — the pure transcode/transcribe primitives. The daemon's
`_handle_voice` orchestration and `route_voice` (which log + touch the vault) stay
in capture_daemon.

`large-v3` + an explicit language. On the Mac we use Apple-Silicon **mlx-whisper**,
which has the headroom — on short Singaporean-accented clips `medium` mis-heard
("childcare for my kid" → "toolkit for my kit") and even `large-v3-turbo` slipped
("chao kei for my key"); only full large-v3 transcribed it correctly. In the NAS
container (Intel x86, no mlx) we fall back to **faster-whisper** running the same
large-v3 weights on CPU (int8) — slower per clip but the same accuracy. Auto-detect
on short clips mis-fired (English → Malay) and drifted into repetition loops
("first first first…"); pinning language + condition_on_previous_text=False fixes
both on either engine. Model + language are settings-overridable (whisper_model /
voice_language — see _handle_voice); the stored model name is an mlx repo id, which
_fw_model_name() maps to the plain faster-whisper size when we fall back.
"""

from __future__ import annotations

import subprocess

_WHISPER_MODEL = "mlx-community/whisper-large-v3-mlx"
_VOICE_LANGUAGE = "en"

# faster-whisper WhisperModel is expensive to construct (loads weights) — build once
# per model size and reuse across clips. Keyed by the resolved faster-whisper name.
_FW_CACHE: dict = {}


def _fw_model_name(model: str | None) -> str:
    """Map a stored whisper_model value to a faster-whisper model id. The setting
    defaults to an mlx repo id ('mlx-community/whisper-large-v3-mlx'); strip the repo
    prefix / 'whisper-' / '-mlx' so it becomes the plain size faster-whisper wants
    ('large-v3'). A value that's already a bare size passes straight through."""
    m = (model or _WHISPER_MODEL).split("/")[-1]
    m = m.replace("whisper-", "").replace("-mlx", "").strip()
    return m or "large-v3"


def _transcribe_faster(wav_path: str, language: str, model: str | None) -> str:
    """CPU transcription via faster-whisper (the fallback when mlx isn't available,
    i.e. the Intel NAS container). int8 keeps memory/compute modest; weights download
    once into HF_HOME (persisted on the /data volume so a redeploy doesn't re-fetch)."""
    from faster_whisper import WhisperModel
    name = _fw_model_name(model)
    mdl = _FW_CACHE.get(name)
    if mdl is None:
        mdl = WhisperModel(name, device="cpu", compute_type="int8")
        _FW_CACHE[name] = mdl
    segments, _info = mdl.transcribe(
        wav_path, language=language, condition_on_previous_text=False)
    return "".join(seg.text for seg in segments).strip()


def transcribe_wav(wav_path: str, language: str = _VOICE_LANGUAGE, model: str | None = None) -> str:
    """Transcribe a wav locally. `language` is passed explicitly (no auto-detect
    misfire) and condition_on_previous_text=False suppresses repetition-loop
    hallucinations. Prefers mlx-whisper (Apple Silicon); if it isn't installed —
    the Intel NAS container — falls back to faster-whisper on CPU. Weights
    auto-download once per engine."""
    try:
        import mlx_whisper
    except ImportError:
        return _transcribe_faster(wav_path, language, model)
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
