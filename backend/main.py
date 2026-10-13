"""BhashaBuzzer FastAPI relay — CONTRACT §3 main.py block (A7 owns).

GET  /         → frontend/ static if present, else a placeholder page
GET  /healthz  → {"ok", "chars_used", "sessions"}
POST /api/warmup → idempotent background prewarm of the global Sarvam WS
WS   /ws       → per-connection GameSession + VoiceChannel wiring

THE TRANSPORT LADDER lives HERE, between game.py and sarvam_tts.py: the
LadderVoice adapter passed to GameSession is duck-typed per CONTRACT §3
(voice.speak(text, utterance_id, on_first_chunk) yields bytes chunks —
first chunk = 44-byte RIFF header) and resolves each utterance as:

    cache hit  → wav_blob_to_chunks        (source "cache", no spend)
    miss       → channel.speak  (WS)      (source "ws")
    WS failure → speak_via_stream          (source "stream")
    then       → speak_via_rest            (source "rest")
    all failed → VoiceUnavailable → {"type":"error"} frame, text-only

log_spend(chars, what) fires on EVERY real synthesis (never on cache hits).

Latency stamps (CONTRACT §3): t_buzz / t_audio_in / t_stt_done /
t_judge_done are stamped by this module; t_first_chunk / t_tts_end by the
ladder. They flow into every outgoing ``audio_end`` frame (stt_ms,
judge_ms, ttfb_ms, e2e_ms, cache_hit, source) and one JSONL line is
appended to logs/latency.jsonl per utterance.

Guards: session semaphore <= config.MAX_SESSIONS, Sarvam WS-open stagger
>= 300 ms, 429/503 exponential backoff 0.5→4 s with <= 2 retries, judge
token bucket <= 24/min, clean teardown on disconnect.

ALL sibling imports are LAZY inside factory functions so this module
imports standalone (CONTRACT §8); tests override the factories to inject
FakeVoice / FakeJudge / FakeCache / FakeGameSession (DI, no real API).
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import importlib
import inspect
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

try:  # flat import (backend/ on sys.path) or package import (repo root on path)
    import config as config
except ImportError:  # pragma: no cover — package-style
    from backend import config as config  # type: ignore[no-redef]

log = logging.getLogger("bhashabuzzer.main")


# --------------------------------------------------------------------------
# Lazy sibling imports (CONTRACT §8): never at module import, only inside
# factories. Tests replace the factory functions themselves (DI).
# --------------------------------------------------------------------------
def _sibling(name: str) -> Any:
    """Import a sibling backend module lazily: flat first, then package."""
    try:
        return importlib.import_module(name)
    except ImportError:
        return importlib.import_module(f"backend.{name}")


def get_tts_module() -> Any:
    """backend.sarvam_tts (A1): VoiceChannel, speak_via_stream/rest, wav_blob_to_chunks."""
    return _sibling("sarvam_tts")


def get_cache_module() -> Any:
    """backend.cache (A2): tts_key, cache_get/put, log_spend, chars_used, governor_level."""
    return _sibling("cache")


def get_stt_module() -> Any:
    """backend.sarvam_stt (A3): transcribe_answer, STTUnavailable."""
    return _sibling("sarvam_stt")


def get_judge_module() -> Any:
    """backend.judge (A4): judge_answer(question, heard, persona_label) -> dict."""
    return _sibling("judge")


def get_personas_module() -> Any:
    """backend.personas (A5): persona_for_round, persona_label."""
    return _sibling("personas")


def make_voice_channel() -> Any:
    """Construct ONE Sarvam TTS WS channel (unconnected; caller staggers+connects)."""
    tts = get_tts_module()
    channel_cls = getattr(tts, "VoiceChannel", None)
    if channel_cls is None:  # pragma: no cover — defensive
        raise RuntimeError("sarvam_tts.VoiceChannel not found")
    return channel_cls(ws_url=config.TTS_WS_URL, api_key=None)  # key read by A1 itself


def make_game_session(send: Callable[[dict], Any], voice: Any, judge_fn: Any, bank: dict) -> Any:
    """backend.game.GameSession (A6): __init__(send, voice, judge_fn, bank)."""
    game_mod = _sibling("game")
    return game_mod.GameSession(send, voice, judge_fn, bank)


def get_bank() -> dict:
    """Question bank dict (backend/data/questions.json); {} when absent."""
    path = Path(config.DATA_DIR) / "questions.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------
# Time + retry helpers
# --------------------------------------------------------------------------
def _now() -> float:
    return time.monotonic()


def _ms(t0: float | None, t1: float | None) -> int | None:
    if t0 is None or t1 is None:
        return None
    return max(0, int(round((t1 - t0) * 1000.0)))


def _is_rate_limited(exc: BaseException) -> bool:
    """429/503 detection across sibling exception shapes."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in (429, 503):
        return True
    text = str(exc)
    return "429" in text or "503" in text or "rate limit" in text.lower()


async def _call_with_backoff(fn: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Call ``fn`` with exponential backoff (0.5→4 s) on 429/503, <= 2 retries."""
    last_exc: BaseException | None = None
    for attempt in range(config.RETRY_MAX + 1):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — sibling error types are unknown here
            last_exc = exc
            if attempt >= config.RETRY_MAX or not _is_rate_limited(exc):
                raise
            delay = config.RETRY_BACKOFF_S[min(attempt, len(config.RETRY_BACKOFF_S) - 1)]
            log.warning("%s rate-limited (attempt %d) — backing off %.1fs",
                        getattr(fn, "__name__", "call"), attempt + 1, delay)
            await asyncio.sleep(delay)
    raise last_exc  # pragma: no cover — loop always returns or raises


class TokenBucket:
    """Simple continuous-refill token bucket (judge calls <= 24/min)."""

    def __init__(self, capacity: int, per_minute: int) -> None:
        self.capacity = float(capacity)
        self.rate = per_minute / 60.0
        self.tokens = float(capacity)
        self.last = _now()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = _now()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait_s = (1.0 - self.tokens) / self.rate
            await asyncio.sleep(max(wait_s, 0.001))


# --------------------------------------------------------------------------
# Per-process runtime state (reset between tests / fresh loops)
# --------------------------------------------------------------------------
class _RuntimeState:
    def __init__(self) -> None:
        self.session_sem = asyncio.Semaphore(config.MAX_SESSIONS)
        self.sessions_active = 0
        self.prewarm_channel: Any = None
        self.prewarm_adopted = False
        self.prewarm_task: asyncio.Task | None = None
        self.prewarm_lock = asyncio.Lock()
        self.judge_bucket: TokenBucket | None = None
        self.ws_open_lock = asyncio.Lock()
        self.last_ws_open = 0.0


_STATE = _RuntimeState()


def reset_runtime() -> None:
    """Reset all per-process runtime state (called by tests in setUp)."""
    global _STATE
    if _STATE.prewarm_task is not None and not _STATE.prewarm_task.done():
        _STATE.prewarm_task.cancel()
    _STATE = _RuntimeState()


async def _reserve_ws_open_slot() -> None:
    """Stagger Sarvam WS opens >= 300 ms apart (burst-opens get 1003-rejected)."""
    async with _STATE.ws_open_lock:
        wait_s = _STATE.last_ws_open + config.WS_OPEN_STAGGER_S - _now()
        if wait_s > 0:
            await asyncio.sleep(wait_s)
        _STATE.last_ws_open = _now()


def _judge_bucket() -> TokenBucket:
    if _STATE.judge_bucket is None:
        _STATE.judge_bucket = TokenBucket(config.JUDGE_BUCKET_CAPACITY, config.JUDGE_RATE_PER_MIN)
    return _STATE.judge_bucket


# --------------------------------------------------------------------------
# Transport-ladder voice adapter (the CONTRACT §3 "voice" injected into GameSession)
# --------------------------------------------------------------------------
class VoiceUnavailable(Exception):
    """Every transport rung failed — the utterance must be shown text-only."""

    def __init__(self, message: str, text: str = "") -> None:
        super().__init__(message)
        self.text = text


def _wav_blob_to_chunks_fallback(wav: bytes, chunk_pcm_bytes: int = 32768) -> tuple[bytes, list[bytes]]:
    """Defensive chunker used only if sarvam_tts.wav_blob_to_chunks is missing."""
    if len(wav) < 44 or wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE blob")
    pcm = wav[44:]
    return wav[:44], [pcm[i:i + chunk_pcm_bytes] for i in range(0, len(pcm), chunk_pcm_bytes)]


def _blob_to_chunks(tts_mod: Any, blob: bytes) -> tuple[bytes, list[bytes]]:
    fn = getattr(tts_mod, "wav_blob_to_chunks", None) or _wav_blob_to_chunks_fallback
    header, chunks = fn(blob)
    return header, list(chunks)


class LadderVoice:
    """Duck-typed `voice` for GameSession implementing the TRANSPORT LADDER.

    speak() is an async generator yielding CONTRACT §1 chunks (first chunk =
    44-byte RIFF header only). After each utterance, ``last_metrics`` holds
    {utterance_id, chars, cache_hit, source, ttfb_ms, total_ms, t_first, t_end}
    for the audio_end enrichment + latency JSONL.
    """

    def __init__(self, channel: Any, session_id: str) -> None:
        self._channel = channel
        self.session_id = session_id
        self.speaker = ""
        self.language_code = ""
        self.pace = float(config.TTS_PACE_DEFAULT)
        self.dict_id: str | None = None
        self.last_metrics: dict[str, Any] = {}

    # ---------------------------------------------------------------- api
    async def set_speaker(self, speaker: str, language_code: str,
                          pace: float = config.TTS_PACE_DEFAULT, *,
                          dict_id: str | None = None) -> None:
        self.speaker = str(speaker)
        self.language_code = str(language_code)
        self.pace = float(pace)
        self.dict_id = dict_id
        await self._channel.set_speaker(speaker, language_code, pace, dict_id=dict_id)

    async def ping(self) -> None:
        await self._channel.ping()

    async def close(self) -> None:
        await self._channel.close()

    @property
    def connected(self) -> bool:
        return bool(getattr(self._channel, "connected", True))

    # ------------------------------------------------------------- ladder
    def _tts_key(self, text: str) -> str:
        cache = get_cache_module()
        return cache.tts_key(text, self.speaker, self.language_code, self.pace,
                             config.TTS_SAMPLE_RATE, config.TTS_CODEC,
                             self.dict_id, None, None)

    def _governor_level(self, cache: Any) -> str:
        fn = getattr(cache, "governor_level", None)
        if not callable(fn):
            return "ok"
        try:
            return str(fn())
        except Exception:  # noqa: BLE001 — accounting must never kill a turn
            return "ok"

    def _finish(self, utterance_id: str, text: str, source: str, cache_hit: bool,
                t_start: float, t_first: float | None) -> None:
        t_end = _now()
        self.last_metrics = {
            "utterance_id": utterance_id,
            "chars": len(text),
            "cache_hit": cache_hit,
            "source": source,
            "ttfb_ms": _ms(t_start, t_first) or 0,
            "total_ms": _ms(t_start, t_end) or 0,
            "t_first": t_first,
            "t_end": t_end,
        }

    async def _yield_blob(self, blob: bytes, text: str, utterance_id: str, source: str,
                          t_start: float, on_first_chunk: Any) -> Any:
        """Yield a cached/synthesized WAV blob in CONTRACT §1 chunk shape."""
        header, chunks = _blob_to_chunks(get_tts_module(), blob)
        t_first: float | None = None
        try:
            for index, chunk in enumerate([header, *chunks]):
                if index == 0:
                    t_first = _now()
                    if on_first_chunk is not None:
                        result = on_first_chunk(chunk)
                        if inspect.isawaitable(result):
                            await result
                yield chunk
        finally:
            self._finish(utterance_id, text, source, source == "cache", t_start, t_first)

    async def speak(self, text: str, utterance_id: str,
                    on_first_chunk: Any = None) -> Any:
        """Async generator — the ladder. Yields raw bytes chunks per CONTRACT §1."""
        cache = get_cache_module()
        t_start = _now()

        # ---- rung 1: cache-first (no spend) --------------------------------
        key = self._tts_key(text)
        blob: bytes | None = None
        try:
            blob = cache.cache_get(key)
        except Exception as exc:  # noqa: BLE001 — cache trouble degrades to a miss
            log.warning("cache_get failed for %s: %s", utterance_id[:16], exc)
        if blob:
            async for chunk in self._yield_blob(blob, text, utterance_id, "cache",
                                                t_start, on_first_chunk):
                yield chunk
            return

        # ---- budget governor: past cap → cache/text-only (CONTRACT §7) -----
        level = self._governor_level(cache)
        if level in ("cached_only", "hard"):
            raise VoiceUnavailable(
                f"spend governor={level} — synthesis refused, text only", text=text)

        # ---- rung 2: Sarvam WS via the session channel ---------------------
        parts: list[bytes] = []
        ws_first_ts: list[float | None] = [None]

        async def _first(chunk: bytes) -> None:
            if ws_first_ts[0] is None:
                ws_first_ts[0] = _now()
            if on_first_chunk is not None:
                result = on_first_chunk(chunk)
                if inspect.isawaitable(result):
                    await result

        ws_ok = False
        try:
            async for chunk in self._channel.speak(text, utterance_id,
                                                    on_first_chunk=_first):
                parts.append(chunk)
                yield chunk
            ws_ok = True
        except Exception as exc:  # noqa: BLE001 — any WS failure demotes the transport
            log.warning("TTS WS rung failed for %r: %s", utterance_id, exc)
            if parts:  # partial audio already streamed — cannot restart mid-utterance
                raise VoiceUnavailable(
                    f"TTS WS died mid-utterance: {exc}", text=text) from exc
        if ws_ok and parts:
            self._store(cache, key, b"".join(parts), text, "ws")
            self._finish(utterance_id, text, "ws", False, t_start, ws_first_ts[0])
            return

        # ---- rungs 3+4: HTTP stream, then REST ------------------------------
        for rung_name, rung_fn in (("stream", "speak_via_stream"),
                                   ("rest", "speak_via_rest")):
            fn = getattr(get_tts_module(), rung_fn, None)
            if fn is None:
                continue
            try:
                blob = await _call_with_backoff(
                    fn, text, self.speaker, self.language_code,
                    pace=self.pace, sample_rate=config.TTS_SAMPLE_RATE)
            except Exception as exc:  # noqa: BLE001 — demote one rung further
                log.warning("TTS %s rung failed for %r: %s", rung_name, utterance_id, exc)
                continue
            if not blob or len(blob) < 44:
                log.warning("TTS %s rung returned no audio for %r", rung_name, utterance_id)
                continue
            self._store(cache, key, bytes(blob), text, rung_name)
            async for chunk in self._yield_blob(blob, text, utterance_id, rung_name,
                                                t_start, on_first_chunk):
                yield chunk
            return

        raise VoiceUnavailable("all TTS transports failed — showing text only", text=text)

    # ------------------------------------------------------------ helpers
    def _store(self, cache: Any, key: str, wav: bytes, text: str, source: str) -> None:
        try:
            cache.cache_put(key, wav, {"source": source, "chars": len(text),
                                      "speaker": self.speaker})
        except Exception as exc:  # noqa: BLE001 — a failed put must not kill audio
            log.warning("cache_put failed: %s", exc)
        try:
            cache.log_spend(len(text), f"tts:{source}")  # EVERY real synthesis
        except Exception as exc:  # noqa: BLE001
            log.warning("log_spend failed: %s", exc)


# --------------------------------------------------------------------------
# FastAPI app
# --------------------------------------------------------------------------
app = FastAPI(title="BhashaBuzzer relay")


@app.get("/")
async def root() -> Any:
    index = Path(config.FRONTEND_DIR) / "index.html"
    if index.is_file():
        return FileResponse(index)
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>BhashaBuzzer</title></head><body style='font-family:system-ui;"
        "display:grid;place-items:center;height:100vh;margin:0'>"
        "<div style='text-align:center'><h1>🎙️ BhashaBuzzer</h1>"
        "<p>Hinglish voice quiz-show server is running.</p>"
        "<p><small>frontend/ not built yet — connect via ws://…/ws</small></p>"
        "</div></body></html>"
    )


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    try:
        chars = int(get_cache_module().chars_used())
    except Exception:  # noqa: BLE001 — health must never 500
        chars = 0
    return {"ok": True, "chars_used": chars, "sessions": _STATE.sessions_active}


async def _do_prewarm() -> Any:
    """Open the global prewarm channel + configure the round-1 persona."""
    await _reserve_ws_open_slot()
    channel = make_voice_channel()
    await channel.connect()
    try:
        personas = get_personas_module()
        persona = personas.persona_for_round(1, "hi")
        await channel.set_speaker(persona["speaker"],
                                  persona.get("language_code", "hi-IN"),
                                  persona.get("pace", config.TTS_PACE_DEFAULT))
    except Exception as exc:  # noqa: BLE001 — persona config is best-effort
        log.warning("prewarm: persona config skipped (%s)", exc)
    _STATE.prewarm_channel = channel
    return channel


@app.post("/api/warmup")
async def warmup() -> dict[str, Any]:
    """Idempotent background prewarm of the global Sarvam WS channel."""
    async with _STATE.prewarm_lock:
        task = _STATE.prewarm_task
        if task is None or (task.done() and task.exception() is not None):
            task = asyncio.create_task(_do_prewarm())
            _STATE.prewarm_task = task
    try:
        channel = await asyncio.shield(task)
    except asyncio.CancelledError:  # pragma: no cover
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("prewarm failed: %s", exc)
        return {"ok": False, "warm": False, "error": str(exc)}
    return {"ok": True, "warm": bool(getattr(channel, "connected", True))}


# --------------------------------------------------------------------------
# WS /ws — the relay loop
# --------------------------------------------------------------------------
def _make_send(ws: WebSocket, conn: dict) -> Callable[[dict], Any]:
    """Per-connection send(): serializes, tracks phase, enriches audio_end,
    and appends one JSONL line per utterance to logs/latency.jsonl."""

    async def send(frame: dict) -> None:
        if not isinstance(frame, dict):
            raise TypeError(f"send(): expected dict frame, got {type(frame).__name__}")
        ftype = str(frame.get("type", ""))
        if ftype == "state":
            conn["phase"] = frame.get("phase", conn.get("phase", "lobby"))
        if ftype == "audio_end":
            frame = _enrich_audio_end(frame, conn)
            _log_latency(frame, conn)
        await ws.send_text(json.dumps(frame, ensure_ascii=False))

    return send


def _enrich_audio_end(frame: dict, conn: dict) -> dict:
    """Fill the CONTRACT §1 audio_end latency fields (add-only, never clobber)."""
    timeline: dict = conn["timeline"]
    voice: LadderVoice | None = conn.get("voice")
    metrics: dict = voice.last_metrics if voice is not None else {}
    stt_ms = _ms(timeline.get("t_audio_in"), timeline.get("t_stt_done"))
    judge_ms = _ms(timeline.get("t_stt_done"), timeline.get("t_judge_done"))
    t_first = metrics.get("t_first")
    e2e_ms = None
    if t_first is not None:
        e2e_ms = _ms(timeline.get("t_buzz"), t_first)
        if e2e_ms is None:
            e2e_ms = _ms(timeline.get("t_audio_in"), t_first)
    frame.setdefault("chars", metrics.get("chars", 0))
    frame.setdefault("ttfb_ms", metrics.get("ttfb_ms", 0))
    frame.setdefault("total_ms", metrics.get("total_ms", 0))
    frame.setdefault("stt_ms", stt_ms if stt_ms is not None else 0)
    frame.setdefault("judge_ms", judge_ms if judge_ms is not None else 0)
    frame.setdefault("e2e_ms", e2e_ms if e2e_ms is not None else frame.get("total_ms", 0))
    frame.setdefault("cache_hit", bool(metrics.get("cache_hit", False)))
    frame.setdefault("source", metrics.get("source", "unknown"))
    return frame


def _log_latency(frame: dict, conn: dict) -> None:
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "session_id": conn.get("session_id", ""),
        "utterance_id": frame.get("utterance_id", ""),
        "source": frame.get("source", "unknown"),
        "cache_hit": frame.get("cache_hit", False),
        "chars": frame.get("chars", 0),
        "stt_ms": frame.get("stt_ms", 0),
        "judge_ms": frame.get("judge_ms", 0),
        "ttfb_ms": frame.get("ttfb_ms", 0),
        "total_ms": frame.get("total_ms", 0),
        "e2e_ms": frame.get("e2e_ms", 0),
    }
    try:
        path = Path(config.LOGS_DIR) / config.LATENCY_LOG
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.warning("latency log write failed: %s", exc)


def _make_judge_fn(conn: dict) -> Callable:
    """judge_fn for GameSession: token-bucket gated + t_judge_done stamped."""
    timeline: dict = conn["timeline"]

    async def judge_fn(question: dict, heard: str, persona_label: str) -> dict:
        await _judge_bucket().acquire()
        try:
            verdict = await get_judge_module().judge_answer(question, heard, persona_label)
        finally:
            timeline["t_judge_done"] = _now()
        return verdict

    return judge_fn


async def _transcribe_answer(conn: dict, wav: bytes) -> str:
    """STT interception: audio_b64 → text via sarvam_stt (backoff on 429/503)."""
    timeline: dict = conn["timeline"]
    stt = get_stt_module()
    unavailable = getattr(stt, "STTUnavailable", Exception)
    try:
        result = await _call_with_backoff(stt.transcribe_answer, wav)
        return str(result.get("text", ""))
    except unavailable as exc:
        log.info("STT unavailable for session %s: %s", conn.get("session_id"), exc)
        return ""
    except Exception as exc:  # noqa: BLE001 — never let STT kill the turn
        log.warning("STT failed for session %s: %s", conn.get("session_id"), exc)
        return ""
    finally:
        timeline["t_stt_done"] = _now()


async def _acquire_channel(conn: dict) -> bool:
    """Adopt the prewarmed global channel if free, else open a staggered own one."""
    async with _STATE.prewarm_lock:
        channel = _STATE.prewarm_channel
        if (channel is not None and not _STATE.prewarm_adopted
                and bool(getattr(channel, "connected", False))):
            _STATE.prewarm_adopted = True
            conn["channel"] = channel
            conn["owns_channel"] = False
            return True
    await _reserve_ws_open_slot()
    channel = make_voice_channel()
    await channel.connect()
    conn["channel"] = channel
    conn["owns_channel"] = True
    return True


async def _start_session(conn: dict, send: Callable, msg: dict) -> bool:
    """Guard the session slot, wire channel+voice+judge+bank+game. True on success."""
    if _STATE.session_sem.locked():
        await send({"type": "error", "phase": "lobby", "code": "server_full",
                    "recoverable": False,
                    "message": "server is full (8 sessions) — try again shortly"})
        return False
    await _STATE.session_sem.acquire()
    conn["slot"] = True
    try:
        await _acquire_channel(conn)
        voice = LadderVoice(conn["channel"], conn["session_id"])
        conn["voice"] = voice
        judge_fn = _make_judge_fn(conn)
        conn["session"] = make_game_session(send, voice, judge_fn, get_bank())
        _STATE.sessions_active += 1
        return True
    except Exception as exc:  # noqa: BLE001 — wiring failure must free the slot
        log.exception("session wiring failed: %s", exc)
        conn["slot"] = False
        _STATE.session_sem.release()
        await _send_safe(send, {"type": "error", "phase": "lobby",
                                "code": "session_init_failed", "recoverable": True,
                                "message": f"could not start session: {exc}"})
        return False


async def _send_safe(send: Callable, frame: dict) -> None:
    try:
        await send(frame)
    except Exception:  # noqa: BLE001 — socket already dying
        pass


async def _teardown(conn: dict) -> None:
    """Clean teardown on disconnect: free the slot, close owned channel, destroy game."""
    session = conn.get("session")
    if session is not None:
        destroy = getattr(session, "destroy", None)
        if destroy is not None:
            try:
                result = destroy()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # noqa: BLE001
                log.warning("session.destroy failed: %s", exc)
    channel = conn.get("channel")
    if channel is not None:
        if conn.get("owns_channel"):
            close = getattr(channel, "close", None)
            if close is not None:
                try:
                    result = close()
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:  # noqa: BLE001
                    log.warning("voice channel close failed: %s", exc)
        else:  # adopted prewarm channel — release it back, keep it warm
            async with _STATE.prewarm_lock:
                _STATE.prewarm_adopted = False
    if conn.pop("slot", False):
        _STATE.sessions_active = max(0, _STATE.sessions_active - 1)
        _STATE.session_sem.release()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    conn: dict[str, Any] = {
        "session_id": uuid.uuid4().hex[:12],
        "timeline": {},
        "session": None,
        "voice": None,
        "channel": None,
        "owns_channel": False,
        "slot": False,
        "phase": "lobby",
    }
    send = _make_send(ws, conn)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                await _send_safe(send, {"type": "error", "phase": conn["phase"],
                                        "code": "bad_message", "recoverable": True,
                                        "message": "frames must be JSON text"})
                continue
            if not isinstance(msg, dict) or "type" not in msg:
                await _send_safe(send, {"type": "error", "phase": conn["phase"],
                                        "code": "bad_message", "recoverable": True,
                                        "message": "frame must be an object with a 'type'"})
                continue

            mtype = msg.get("type")
            if mtype == "start":
                if conn["session"] is not None:
                    await send({"type": "error", "phase": conn["phase"],
                                "code": "already_started", "recoverable": True,
                                "message": "session already started on this connection"})
                    continue
                if not await _start_session(conn, send, msg):
                    continue
            elif mtype == "buzz":
                conn["timeline"]["t_buzz"] = _now()
            elif mtype == "answer":
                audio_b64 = msg.get("audio_b64")
                if audio_b64:
                    conn["timeline"]["t_audio_in"] = _now()
                    try:
                        wav = base64.b64decode(audio_b64, validate=True)
                    except (binascii.Error, ValueError):
                        await send({"type": "error", "phase": conn["phase"],
                                    "code": "bad_audio", "recoverable": True,
                                    "message": "answer.audio_b64 is not valid base64"})
                        continue
                    heard = await _transcribe_answer(conn, wav)
                    if not heard:
                        await _send_safe(send, {
                            "type": "error", "phase": conn["phase"],
                            "code": "stt_unavailable", "recoverable": True,
                            "message": "could not hear that — please answer again"})
                    msg = {"type": "answer", "text": heard}
                else:
                    conn["timeline"]["t_audio_in"] = _now()
                    conn["timeline"]["t_stt_done"] = _now()

            session = conn["session"]
            if session is None:
                await _send_safe(send, {"type": "error", "phase": conn["phase"],
                                         "code": "not_started", "recoverable": True,
                                         "message": "send {\"type\":\"start\"} first"})
                continue
            try:
                await session.handle(msg)
            except VoiceUnavailable as exc:
                log.warning("session %s: text-only fallback: %s", conn["session_id"], exc)
                await _send_safe(send, {
                    "type": "error", "phase": conn["phase"],
                    "code": "tts_unavailable", "recoverable": True,
                    "message": f"voice unavailable — text shown on screen ({exc})"})
            except WebSocketDisconnect:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the game must never hard-crash
                log.exception("session %s handle(%s) failed: %s",
                              conn["session_id"], mtype, exc)
                await _send_safe(send, {
                    "type": "error", "phase": conn["phase"],
                    "code": "internal", "recoverable": True,
                    "message": f"internal error handling {mtype!r}"})
    except WebSocketDisconnect:
        log.info("session %s disconnected", conn["session_id"])
    finally:
        await _teardown(conn)


# Serve the static frontend from "/" when it exists (added LAST so the API
# and WS routes above always win). Placeholder lives in the GET / handler.
if Path(config.FRONTEND_DIR).is_dir():  # pragma: no cover — integration env only
    app.mount("/", StaticFiles(directory=str(config.FRONTEND_DIR), html=True),
              name="frontend")
