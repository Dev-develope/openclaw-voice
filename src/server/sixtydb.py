"""
60db cloud client — TTS (3 surfaces) + STT (2 surfaces).

Why this module is flat and not a package:
    The rest of src/server uses one file per concern (tts.py, stt.py, vad.py).
    60db spans both TTS and STT, but every surface is a thin async function;
    splitting them across files would inflate the import surface for no gain.
    The fallback-chain code in tts.py / stt.py imports just what it needs.

Surfaces exposed:
    synthesize_rest_sync()      → POST /tts-synthesize     (one-shot mp3)
    synthesize_ndjson_stream()  → POST /tts-stream         (NDJSON chunks)
    synthesize_ws_stream()      → wss://.../ws/tts          (realtime LINEAR16 PCM)
    transcribe_rest()           → POST /stt                 (batch wav)
    transcribe_ws_stream()      → wss://.../ws/stt          (realtime telephony)

Reference: https://docs.60db.ai
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
from typing import AsyncGenerator, Iterable, Optional

import httpx
import numpy as np
import soundfile as sf
import websockets
from loguru import logger

DEFAULT_API_BASE = "https://api.60db.ai"
DEFAULT_VOICE_ID = "fbb75ed2-975a-40c7-9e06-38e30524a9a1"  # from docs


# ─────────────────────────── helpers ────────────────────────────────────────


def _config() -> tuple[str, str]:
    """Return (api_base, api_key). Raises if no key."""
    api_key = os.environ.get("SIXTYDB_API_KEY")
    if not api_key:
        raise RuntimeError("SIXTYDB_API_KEY is not set")
    api_base = os.environ.get("SIXTYDB_API_BASE", DEFAULT_API_BASE).rstrip("/")
    return api_base, api_key


def _ws_url(api_base: str, path: str, api_key: str) -> str:
    return api_base.replace("https://", "wss://").replace("http://", "ws://") + path + f"?apiKey={api_key}"


def _voice_id() -> str:
    return os.environ.get("SIXTYDB_TTS_VOICE_ID", DEFAULT_VOICE_ID)


# ─────────────────────────── TTS · sync REST ────────────────────────────────


async def synthesize_rest_sync(
    text: str,
    voice_id: Optional[str] = None,
    output_format: str = "mp3",
) -> bytes:
    """One-shot synthesis: POST /tts-synthesize → audio bytes.

    Used by tts.py's `_synthesize_sync()` 60db branch (matches the
    Chatterbox/XTTS shape: synchronous, returns full audio).
    """
    api_base, api_key = _config()
    payload = {
        "text": text,
        "voice_id": voice_id or _voice_id(),
        "enhance": True,
        "speed": 1,
        "stability": 50,
        "similarity": 75,
        "output_format": output_format,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=60.0)) as client:
        r = await client.post(f"{api_base}/tts-synthesize", json=payload, headers=headers)
        r.raise_for_status()
        body = r.json()
    if not body.get("success") or not body.get("audio_base64"):
        raise RuntimeError(f"60db /tts-synthesize returned no audio: {body.get('message')}")
    return base64.b64decode(body["audio_base64"])


# ─────────────────────────── TTS · NDJSON stream ────────────────────────────


async def synthesize_ndjson_stream(
    text: str,
    voice_id: Optional[str] = None,
) -> AsyncGenerator[bytes, None]:
    """POST /tts-stream — yields decoded audio bytes per chunk.

    The upstream emits NDJSON with `{"type":"chunk","audioContent":"<base64>"}`
    lines terminated by `{"type":"complete"}`. We decode each chunk to raw
    bytes so callers can write them straight to disk or socket.
    """
    api_base, api_key = _config()
    payload = {
        "text": text,
        "voice_id": voice_id or _voice_id(),
        "output_format": "mp3",
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=60.0)) as client:
        async with client.stream("POST", f"{api_base}/tts-stream", json=payload, headers=headers) as r:
            r.raise_for_status()
            async for raw_line in r.aiter_lines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "error":
                    raise RuntimeError(f"60db /tts-stream error: {msg}")
                if msg.get("type") == "complete":
                    return
                content = msg.get("audioContent")
                if content:
                    yield base64.b64decode(content)


# ─────────────────────────── TTS · WebSocket ────────────────────────────────


async def synthesize_ws_stream(
    text: str,
    voice_id: Optional[str] = None,
    sample_rate: int = 24000,
) -> AsyncGenerator[bytes, None]:
    """Realtime WS synthesis — yields LINEAR16 PCM bytes at `sample_rate`.

    This is the surface tts.py's `synthesize_stream()` uses by default
    because LINEAR16 @ 24000 matches the existing pcm_24000 contract the
    browser already plays via Web Audio. Zero playback-code changes.
    """
    api_base, api_key = _config()
    url = _ws_url(api_base, "/ws/tts", api_key)
    context_id = f"ctx-{os.getpid()}-{id(text) & 0xFFFFF:x}"

    async with websockets.connect(url, max_size=None) as ws:
        # 1. Wait for connection_established, then create the context.
        async for raw in ws:
            msg = json.loads(raw)
            if "connection_established" in msg:
                await ws.send(json.dumps({
                    "create_context": {
                        "context_id": context_id,
                        "voice_id": voice_id or _voice_id(),
                        "audio_config": {
                            "audio_encoding": "LINEAR16",
                            "sample_rate_hertz": sample_rate,
                        },
                    },
                }))
                continue
            if "context_created" in msg:
                # 2. Send text + flush; from here on we only receive.
                await ws.send(json.dumps({
                    "send_text": {"context_id": context_id, "text": text},
                }))
                await ws.send(json.dumps({
                    "flush_context": {"context_id": context_id},
                }))
                continue
            chunk_b64 = msg.get("audio_chunk", {}).get("audioContent")
            if chunk_b64:
                yield base64.b64decode(chunk_b64)
                continue
            if "flush_completed" in msg:
                # 3. Synthesis done — close politely.
                try:
                    await ws.send(json.dumps({
                        "close_context": {"context_id": context_id},
                    }))
                except Exception:
                    pass
                return


# ─────────────────────────── STT · REST ─────────────────────────────────────


async def transcribe_rest(
    audio: np.ndarray,
    sample_rate: int = 16000,
    language: Optional[str] = None,
    diarize: bool = False,
) -> str:
    """POST /stt — upload a numpy waveform, get a transcript back.

    Matches the shape Whisper paths use in stt.py: take a np.ndarray of
    audio at `sample_rate`, return a string. The wav is encoded in memory
    via soundfile so we don't touch disk.
    """
    api_base, api_key = _config()
    # Whisper-side numpy arrays are float32 in [-1, 1]; soundfile writes
    # them as PCM_16 by default which is what /stt expects.
    buf = io.BytesIO()
    sf.write(buf, audio, sample_rate, format="WAV", subtype="PCM_16")
    buf.seek(0)

    headers = {"Authorization": f"Bearer {api_key}"}
    files = {"file": ("audio.wav", buf, "audio/wav")}
    data: dict[str, str] = {}
    lang = language or os.environ.get("SIXTYDB_STT_LANGUAGE", "auto")
    if lang and lang.lower() != "auto":
        data["language"] = lang
    if diarize:
        data["diarize"] = "true"

    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=60.0)) as client:
        r = await client.post(f"{api_base}/stt", headers=headers, files=files, data=data)
        r.raise_for_status()
        body = r.json()
    return (body.get("text") or "").strip()


# ─────────────────────────── STT · WebSocket ────────────────────────────────


async def transcribe_ws_stream(
    audio_frames: Iterable[bytes],
    encoding: str = "mulaw",
    sample_rate: int = 8000,
) -> AsyncGenerator[dict, None]:
    """Realtime WS STT — feed audio frames in, yield transcription events.

    Yields raw `{type:"transcription", text, is_final, speech_final, ...}`
    dicts so callers can decide their own turn-detection policy. Not used
    in the default openclaw flow (which is push-to-talk batch), but
    exposed so a future continuous-listening client can use it.
    """
    api_base, api_key = _config()
    url = _ws_url(api_base, "/ws/stt", api_key)
    async with websockets.connect(url, max_size=None) as ws:
        # First inbound is connection_established.
        first = json.loads(await ws.recv())
        if "connection_established" not in first:
            raise RuntimeError(f"60db /ws/stt unexpected first message: {first}")

        await ws.send(json.dumps({
            "type": "start",
            "languages": [os.environ.get("SIXTYDB_STT_LANGUAGE", "en")],
            "config": {
                "encoding": encoding,
                "sample_rate": sample_rate,
                "utterance_end_ms": 500,
                "continuous_mode": True,
                "interim_results_frequency": 300,
                "audio_enhancement": "adaptive",
            },
        }))

        async def _pump_audio():
            # Wait for `connected` confirmation before pushing frames.
            for frame in audio_frames:
                await ws.send(frame)
            await ws.send(json.dumps({"type": "stop"}))

        pump_task: Optional[asyncio.Task] = None
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") == "connected" and pump_task is None:
                pump_task = asyncio.create_task(_pump_audio())
                continue
            if msg.get("type") == "transcription":
                yield msg
            if msg.get("type") == "stopped":
                break
        if pump_task is not None:
            pump_task.cancel()
