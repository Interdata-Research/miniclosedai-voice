"""upstream.py — borrow voices (and Spanish ASR) from a second voice service.

This service's own TTS is Chatterbox Turbo, which is an English engine. Spanish
comes from a **latina_voice_tts** instance (VoxCPM2 voice cloning, native
Spanish, plus Whisper large-v3 for Spanish speech-to-text):

    VOICE_UPSTREAM_URL=http://127.0.0.1:8088 ./start.sh

With that set, this service advertises the upstream's voices alongside its own
and forwards any request for one of them. From MiniClosedAI's point of view
there is still exactly ONE voice backend to register, and it has both languages
— including in call mode, because `/speak`, `/speak/stream` and the WebRTC call
handler all synthesize through `tts.synthesize_stream()`, which is the single
seam this module plugs into.

Design rules, all of them learned the hard way elsewhere in this repo:

* **The upstream is optional and never fatal.** Unset the env var and every
  function here turns into a no-op; if the upstream is unreachable, its voices
  simply vanish from the catalog and local ones keep working. A voice server
  that 500s because a *different* server is down is worse than one that speaks
  only English.
* **Local voices win a name clash.** Both services ship an id called
  `default`; the local one is what `_wav_for()` and every existing caller mean.
* **The catalog is cached** (`CACHE_TTL`) because `/voices` is polled by the
  MiniClosedAI picker and by our own routing check on every synth call; a
  network round trip per chunk would be absurd.
* **Sync, not async.** `tts.synthesize_stream()` is a blocking generator called
  from a worker thread (see server.py's `/speak/stream`), so this uses
  `requests`, not httpx/async — mirroring how it is already consumed.
"""
from __future__ import annotations

import base64
import json
import os
import struct
import threading
import time
from typing import Iterator

import requests

URL = os.environ.get("VOICE_UPSTREAM_URL", "").strip().rstrip("/")
KEY = os.environ.get("VOICE_UPSTREAM_KEY", "").strip()
# Probe/catalog timeout vs. synth timeout: a long Spanish sentence legitimately
# takes seconds, a catalog fetch never should.
PROBE_TIMEOUT = float(os.environ.get("VOICE_UPSTREAM_PROBE_TIMEOUT", "8"))
SYNTH_TIMEOUT = float(os.environ.get("VOICE_UPSTREAM_TIMEOUT", "300"))
CACHE_TTL = float(os.environ.get("VOICE_UPSTREAM_CACHE_TTL", "30"))

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "catalog": {}, "ids": set(), "error": None}


def enabled() -> bool:
    return bool(URL)


def _headers() -> dict:
    return {"Authorization": f"Bearer {KEY}"} if KEY else {}


def catalog(force: bool = False) -> dict:
    """The upstream's `/voices`, `{lang: [{id, name, gender}, ...]}`, cached.

    Never raises: on any failure it returns the last good catalog (or `{}`) and
    records the error for `/health`.
    """
    if not URL:
        return {}
    now = time.monotonic()
    with _lock:
        fresh = (now - _cache["at"]) < CACHE_TTL
        if fresh and not force:
            return _cache["catalog"]
    try:
        r = requests.get(f"{URL}/voices", headers=_headers(), timeout=PROBE_TIMEOUT)
        r.raise_for_status()
        cat = r.json()
        if not isinstance(cat, dict):
            raise ValueError(f"/voices returned {type(cat).__name__}, expected object")
        ids = {v.get("id") for vs in cat.values() if isinstance(vs, list)
               for v in vs if isinstance(v, dict) and v.get("id")}
        with _lock:
            _cache.update(at=now, catalog=cat, ids=ids, error=None)
        return cat
    except Exception as e:
        with _lock:
            _cache["at"] = now          # don't hammer a dead upstream
            _cache["error"] = f"{type(e).__name__}: {e}"
            return _cache["catalog"]


def owns(voice_id: str | None) -> bool:
    """True when `voice_id` is served by the upstream and NOT by us.

    The caller checks its own voices first (see tts.py), so a shared id like
    `default` resolves locally; this is only consulted for ids we don't have.
    """
    if not URL or not voice_id:
        return False
    catalog()
    with _lock:
        return voice_id in _cache["ids"]


def merge_catalog(local: dict) -> dict:
    """Local catalog + upstream voices, local winning on an id clash."""
    if not URL:
        return local
    merged = {lang: list(vs) for lang, vs in local.items()}
    local_ids = {v.get("id") for vs in local.values() for v in vs if isinstance(v, dict)}
    for lang, voices in (catalog() or {}).items():
        if not isinstance(voices, list):
            continue
        for v in voices:
            if isinstance(v, dict) and v.get("id") and v["id"] not in local_ids:
                # `upstream: true` is advisory — MiniClosedAI ignores unknown
                # keys, but it makes `curl /voices` self-explanatory.
                merged.setdefault(lang, []).append({**v, "upstream": True})
    return merged


def status() -> dict:
    """Summary for `/health`. Cheap: uses the cached catalog."""
    if not URL:
        return {"enabled": False}
    cat = catalog()
    with _lock:
        err, ids = _cache["error"], set(_cache["ids"])
    return {"enabled": True, "url": URL, "ok": bool(cat) and err is None,
            "voices": sorted(ids), "languages": sorted(cat or {}), "error": err}


def synthesize_stream(
    text: str, voice_id: str, language: str | None = None,
    speed: float | None = None,
) -> Iterator[tuple[bytes, int]]:
    """Stream `(pcm16_bytes, sample_rate)` from the upstream's `/speak/stream`.

    Same shape `TTS.synthesize_stream()` yields, so callers cannot tell the
    difference — including call.py, which pushes these straight into WebRTC.

    The upstream's terminal frame carries both `done` and `end`; an error
    arrives as an `{"error": ...}` frame on an HTTP 200, so that is raised
    rather than silently ending the audio.
    """
    payload = {"text": text, "voice": voice_id}
    if language:
        payload["language"] = language
    if speed is not None:
        payload["speed"] = speed
    with requests.post(f"{URL}/speak/stream", json=payload, headers=_headers(),
                       stream=True, timeout=SYNTH_TIMEOUT) as r:
        if r.status_code >= 400:
            raise RuntimeError(f"upstream /speak/stream HTTP {r.status_code}: "
                               f"{r.text[:200]}")
        for raw in r.iter_lines():
            if not raw or not raw.startswith(b"data:"):
                continue
            try:
                ev = json.loads(raw[5:])
            except ValueError:
                continue
            if "error" in ev:
                raise RuntimeError(f"upstream /speak/stream: {ev['error']}")
            if ev.get("done") or ev.get("end"):
                return
            b64 = ev.get("chunk_b64")
            if b64:
                yield base64.b64decode(b64), int(ev.get("sample_rate") or 48000)


def transcribe(audio: bytes, language: str | None = None,
               filename: str = "audio.wav",
               content_type: str = "audio/wav") -> dict:
    """POST the upstream's `/transcribe` → `{text, language, segments, ...}`.

    Used for Spanish when our own Whisper is an English-only checkpoint (see
    asr.py). Raises on failure so the caller can fall back to local ASR.
    """
    files = {"audio": (filename, audio, content_type)}
    data = {"language": language} if language else None
    r = requests.post(f"{URL}/transcribe", files=files, data=data,
                      headers=_headers(), timeout=SYNTH_TIMEOUT)
    r.raise_for_status()
    return r.json()


def wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap raw mono int16 PCM in a WAV header, for posting in-memory call
    audio to the upstream's multipart `/transcribe`."""
    n = len(pcm)
    return (b"RIFF" + struct.pack("<I", 36 + n) + b"WAVE" + b"fmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
            + b"data" + struct.pack("<I", n) + pcm)
