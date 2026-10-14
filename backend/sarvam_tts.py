"""Sarvam Bulbul v4-flash TTS voice channel — CONTRACT §3 (A1) + §4 probed facts.

Implements the game-server side of the Sarvam streaming TTS WebSocket plus the two
HTTP fallback transports and the cached-blob chunker:

  * ``VoiceChannel`` — long-lived WS to ``wss://api.sarvam.ai/text-to-speech/ws``
    (auth via ``api-subscription-key`` header on the upgrade, ``send_completion_event=true``
    [PROBED]). ``set_speaker()`` re-sends ``{type:"config"}`` mid-connection [PROBED works];
    ``speak()`` sends ``{type:"text"}`` + ``{type:"flush"}`` and yields audio chunks in the
    frozen client-contract shape: FIRST chunk = 44-byte RIFF header only, subsequent chunks =
    raw PCM continuation (Int16LE, mono, 24 kHz) [PROBED]. Termination: the final completion
    event ``{type:"event",data:{event_type:"final"}}`` [PROBED] or a 15 s idle timeout.
    A 20 s ``{type:"ping"}`` keepalive task holds the socket open [PROBED].
  * ``speak_via_stream`` / ``speak_via_rest`` — HTTP fallbacks (httpx). The stream endpoint
    returns raw binary audio; the HTTP status is checked BEFORE the body is treated as audio.
    ``output_audio_codec:"wav"`` is always set explicitly [PROBED: stream default is mp3].
  * ``wav_blob_to_chunks`` — cached WAV blob → (44-byte header chunk, PCM chunk list) in
    exactly the live WS shape, so cached replays are byte-stream-compatible with live speech.

Close-code policy (Sarvam WS guide + backend plan §2.3):
  1000/1001 clean → no backoff, re-open on next use · 1003 rate-limited → 5 s cooldown retry ·
  1006/1005/1011/other abnormal → exponential backoff 0.5→1→2→4 s, max 3 retries ·
  4000–4999 auth/quota → fatal signal (never blind-retried).

No live API calls happen at import time; tests inject a fake WS server / httpx.MockTransport.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import json
import logging
import os
import struct
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

log = logging.getLogger("bhashabuzzer.sarvam_tts")

# ---------------------------------------------------------------------------
# Constants (standalone copy of CONTRACT §3 config values — config.py (A7) mirrors these)
# ---------------------------------------------------------------------------
TTS_MODEL = "bulbul:v4-flash"  # ALWAYS explicit [PROBED: silent v3 fallback otherwise]
BASE_URL = "https://api.sarvam.ai"
TTS_WS_URL = (
    f"wss://api.sarvam.ai/text-to-speech/ws"
    f"?model={TTS_MODEL}&send_completion_event=true"  # send_completion_event accepted [PROBED]
)
TTS_REST_URL = f"{BASE_URL}/text-to-speech"
TTS_STREAM_URL = f"{BASE_URL}/text-to-speech/stream"

SAMPLE_RATE = 24000  # wav @ 24 kHz mono (Int16LE) everywhere [CONTRACT §4]
AUDIO_BITRATE = "128k"
AUDIO_CODEC = "wav"
MIN_BUFFER_SIZE = 30
MAX_CHUNK_LENGTH = 200

WS_TEXT_CHAR_LIMIT = 2500
STREAM_CHAR_LIMIT = 3500

IDLE_TIMEOUT_S = 15.0  # speak() terminates if no frame arrives within this window
PING_INTERVAL_S = 20.0  # app-level {"type":"ping"} keepalive [PROBED keeps socket alive]
RECONNECT_BACKOFF_S = (0.5, 1.0, 2.0, 4.0)  # exponential, capped at 4 s
RECONNECT_MAX_TRIES = 3
COOLDOWN_1003_S = 5.0  # close 1003 = rate-limited connection → cool down before retrying
HTTP_TIMEOUT_S = 15.0


# ---------------------------------------------------------------------------
# Exceptions (main.py's transport ladder keys off these)
# ---------------------------------------------------------------------------
class TTSError(Exception):
    """Base class for all Sarvam TTS channel errors."""


class TTSConfigError(TTSError):
    """Bad local configuration (missing API key, no speaker set, bad arguments)."""


class TTSWSError(TTSError):
    """Sarvam sent a {type:"error"} frame or a frame we could not parse."""


class TTSConnectionLost(TTSError):
    """Transient socket loss — caller should demote transport (stream → REST)."""

    def __init__(self, message: str, *, close_code: int | None = None, reason: str = "") -> None:
        super().__init__(message)
        self.close_code = close_code
        self.reason = reason


class TTSFatalError(TTSError):
    """4xxx auth/quota close — NOT retryable; the caller must stop spending (ladder → cached-only)."""


class TTSHTTPError(TTSError):
    """Non-2xx HTTP response (or transport error) on the REST/stream fallbacks."""

    def __init__(self, status_code: int, message: str, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _request_key(explicit: str | None = None) -> str:
    """API key from the argument or the SARVAM_API_KEY env var (never logged)."""
    key = (explicit or os.environ.get("SARVAM_API_KEY") or "").strip()
    if not key:
        raise TTSConfigError("SARVAM_API_KEY not set (env var or api_key= argument)")
    return key


def canonical_wav_header(sample_rate: int, channels: int, bits: int, data_size: int) -> bytes:
    """Canonical 44-byte PCM WAV header (matches the live first-chunk shape [PROBED])."""
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    return (
        b"RIFF"
        + struct.pack("<I", 36 + data_size)
        + b"WAVE"
        + b"fmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, bits)
        + b"data"
        + struct.pack("<I", data_size)
    )


def build_config(
    speaker: str,
    language_code: str,
    pace: float = 1.0,
    *,
    dict_id: str | None = None,
    sample_rate: int = SAMPLE_RATE,
) -> dict[str, Any]:
    """WS config message ({type:"config", data:{...}}) with the frozen param set."""
    data: dict[str, Any] = {
        "speaker": speaker,
        "language_code": language_code,
        "pace": float(pace),
        "speech_sample_rate": int(sample_rate),
        "output_audio_codec": AUDIO_CODEC,
        "output_audio_bitrate": AUDIO_BITRATE,
        "min_buffer_size": MIN_BUFFER_SIZE,
        "max_chunk_length": MAX_CHUNK_LENGTH,
    }
    if dict_id:
        data["dict_id"] = dict_id
    return {"type": "config", "data": data}


def _decode_frame(raw: str | bytes) -> dict[str, Any]:
    """Parse one WS frame into a dict (Sarvam speaks JSON text frames)."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TTSWSError(f"binary frame from TTS WS (expected JSON): {exc}") from exc
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise TTSWSError(f"non-JSON frame from TTS WS: {str(raw)[:120]!r}") from exc
    if not isinstance(msg, dict):
        raise TTSWSError(f"unexpected frame from TTS WS (not an object): {str(msg)[:120]!r}")
    return msg


def _decode_audio(msg: dict[str, Any]) -> bytes:
    """Extract the base64 audio payload from a {type:"audio"} frame (data.audio [PROBED])."""
    data = msg.get("data")
    b64: Any = None
    if isinstance(data, dict):
        b64 = data.get("audio") or data.get("b64") or data.get("chunk")
    elif isinstance(data, str):
        b64 = data
    if not isinstance(b64, str) or not b64:
        raise TTSWSError("audio frame without base64 payload")
    try:
        return base64.b64decode(b64)
    except (binascii.Error, ValueError) as exc:
        raise TTSWSError(f"audio frame has invalid base64: {exc}") from exc


def _extract_error(msg: dict[str, Any]) -> str:
    data = msg.get("data")
    if isinstance(data, dict):
        for key in ("message", "error", "detail"):
            if data.get(key):
                return str(data[key])
    err = msg.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("code") or err)
    return json.dumps(msg)[:200]


def _validate_text(text: str, limit: int, where: str) -> str:
    text = str(text)
    if not text.strip():
        raise ValueError(f"{where}: text must be non-empty")
    if len(text) > limit:
        raise ValueError(f"{where}: text is {len(text)} chars, limit is {limit}")
    return text


async def _fire_first_chunk(callback: Callable[..., Any], chunk: bytes) -> None:
    """Call on_first_chunk with the first chunk; tolerate 0-arg and async callables."""
    if callback is None:
        return
    try:
        sig = inspect.signature(callback)
        positional = [
            p
            for p in sig.parameters.values()
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        takes_any = any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
        if positional or takes_any:
            result = callback(chunk)
        else:
            result = callback()
    except (TypeError, ValueError):  # non-introspectable callables (builtins, C funcs)
        result = callback(chunk)
    if inspect.isawaitable(result):
        await result


async def _default_connector(url: str, headers: dict[str, str]) -> Any:
    """Real websockets.connect with the PROBED handshake options."""
    return await websockets.connect(
        url,
        additional_headers=headers,  # api-subscription-key rides on the upgrade request
        open_timeout=5.0,
        close_timeout=2.0,
        ping_interval=None,  # keepalive is our 20 s app-level {"type":"ping"} task
        max_size=4 * 1024 * 1024,
    )


def _tts_payload(
    text: str,
    speaker: str,
    language_code: str,
    pace: float,
    sample_rate: int,
    dict_id: str | None,
) -> dict[str, Any]:
    """JSON body for the REST + HTTP-stream endpoints (params per CONTRACT §4)."""
    payload: dict[str, Any] = {
        "text": text,
        "model": TTS_MODEL,  # ALWAYS explicit
        "speaker": speaker,
        "language_code": language_code,
        "pace": float(pace),
        "speech_sample_rate": int(sample_rate),
        "output_audio_codec": AUDIO_CODEC,  # stream default is mp3 — set explicitly [PROBED]
        "output_audio_bitrate": AUDIO_BITRATE,
        "enable_preprocessing": True,
    }
    if dict_id:
        payload["dict_id"] = dict_id
    return payload


def _http_error(resp: httpx.Response, what: str) -> TTSHTTPError:
    body = ""
    try:
        body = resp.text[:500]
    except Exception:  # noqa: BLE001 — error-path best effort
        pass
    message = f"{what}: HTTP {resp.status_code}"
    try:
        err = (resp.json() or {}).get("error")
        if isinstance(err, dict):
            message = f"{what}: HTTP {resp.status_code} {err.get('code', '')}: {err.get('message', '')}".strip()
        elif err:
            message = f"{what}: HTTP {resp.status_code} {err}"
    except Exception:  # noqa: BLE001 — error body is optional
        pass
    return TTSHTTPError(resp.status_code, message, body=body)


# ---------------------------------------------------------------------------
# VoiceChannel — the long-lived Sarvam TTS WebSocket
# ---------------------------------------------------------------------------
class VoiceChannel:
    """One Sarvam TTS WS connection per game session (plus the global prewarm one).

    Usage (main.py transport ladder):
        ch = VoiceChannel(api_key=...)
        await ch.connect()                      # prewarm (optional)
        await ch.set_speaker("ratan_hi_customer_expressive", "hi-IN", 1.05)
        async for chunk in ch.speak(text, uid, on_first_chunk=stamp):
            ...  # first chunk is the 44-byte RIFF header, rest are raw PCM
    """

    def __init__(
        self,
        *,
        ws_url: str = TTS_WS_URL,
        api_key: str | None = None,
        connector: Callable[[str, dict[str, str]], Awaitable[Any]] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        idle_timeout: float = IDLE_TIMEOUT_S,
        ping_interval: float = PING_INTERVAL_S,
        backoff_s: tuple[float, ...] = RECONNECT_BACKOFF_S,
        max_reconnect_tries: int = RECONNECT_MAX_TRIES,
        cooldown_1003_s: float = COOLDOWN_1003_S,
    ) -> None:
        self._ws_url = ws_url
        self._api_key = api_key
        self._connector = connector or _default_connector
        self._sleep = sleep  # injectable for tests (records/shortens backoff waits)
        self._idle_timeout = idle_timeout
        self._ping_interval = ping_interval
        self._backoff_s = tuple(backoff_s)
        self._max_reconnect_tries = max_reconnect_tries
        self._cooldown_1003_s = cooldown_1003_s

        self._conn: Any = None
        self._cfg: dict[str, Any] | None = None
        self._close_code: int | None = None
        self._close_reason: str = ""
        self._fatal: TTSFatalError | None = None
        self._exhausted_at: float | None = None  # reconnect ladder exhausted recently
        self._residue = False  # speak() may have left stale frames on the socket
        self._lock = asyncio.Lock()
        self._ping_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None

        # observability (healthz / tests)
        self.connect_count = 0
        self.ping_count = 0

    # -------------------------------------------------------------- properties
    @property
    def connected(self) -> bool:
        return self._conn is not None and self._close_code is None and self._fatal is None

    @property
    def close_code(self) -> int | None:
        return self._close_code

    @property
    def fatal(self) -> TTSFatalError | None:
        return self._fatal

    # ------------------------------------------------------------ public API
    async def connect(self) -> None:
        """Open the WS with the auth header (idempotent; runs reconnect policy if needed)."""
        async with self._lock:
            await self._ensure_ready_locked()

    async def set_speaker(
        self,
        speaker: str,
        language_code: str,
        pace: float = 1.0,
        *,
        dict_id: str | None = None,
    ) -> None:
        """Switch persona via mid-connection {type:"config"} re-send [PROBED: works].

        On a fresh/dead socket the config is sent as the first message after the
        (re)connect, as the WS protocol requires.
        """
        if not isinstance(speaker, str) or not speaker.strip():
            raise TTSConfigError("set_speaker(): speaker must be a non-empty string")
        self._cfg = build_config(speaker, language_code, pace, dict_id=dict_id)
        async with self._lock:
            opened = await self._ensure_ready_locked()  # sends config on fresh opens
            if not opened:
                await self._send_config_locked()  # PROBED: re-send works mid-connection

    async def speak(
        self,
        text: str,
        utterance_id: str,
        on_first_chunk: Callable[..., Any] | None = None,
    ) -> AsyncIterator[bytes]:
        """Stream one utterance: {type:"text"} → {type:"flush"} → yield audio chunks.

        Yields the CONTRACT §1 chunk shape: first chunk = 44-byte RIFF header only,
        subsequent chunks = raw PCM continuation [PROBED] — passed through with
        defensive normalization. Terminates on the final completion event or after
        ``idle_timeout`` seconds with no frames. One utterance at a time.
        """
        text = _validate_text(text, WS_TEXT_CHAR_LIMIT, "speak()")
        async with self._lock:
            if self._fatal is not None:
                raise self._fatal
            if self._cfg is None:
                raise TTSConfigError("speak(): call set_speaker() first")
            await self._ensure_ready_locked()
            conn = self._conn
            if conn is None:  # pragma: no cover — ensure() either connects or raises
                raise TTSConnectionLost("speak(): no TTS connection")
            if self._residue:  # previous speak ended without a final event (e.g. barge)
                await self._drain_stale(conn)
        try:
            await self._send_on(conn, json.dumps({"type": "text", "data": {"text": text}}))
            await self._send_on(conn, json.dumps({"type": "flush"}))
        except TTSError:
            self._residue = True
            raise
        try:
            first = True
            while True:
                try:
                    raw = await asyncio.wait_for(conn.recv(), timeout=self._idle_timeout)
                except asyncio.TimeoutError:
                    log.warning(
                        "speak(%s): no frame for %.1fs before final event — terminating",
                        utterance_id,
                        self._idle_timeout,
                    )
                    self._residue = True
                    return
                except ConnectionClosed:
                    code, reason = self._register_close(conn)
                    if self._fatal is not None:
                        raise self._fatal
                    raise TTSConnectionLost(
                        f"speak({utterance_id!r}): Sarvam TTS WS closed mid-utterance "
                        f"(code={code}, reason={reason!r})",
                        close_code=code,
                        reason=reason,
                    )
                msg = _decode_frame(raw)
                mtype = msg.get("type")
                if mtype == "audio":
                    chunk = _decode_audio(msg)
                    pieces = self._normalize_first(chunk) if first else self._normalize_pcm(chunk)
                    for piece in pieces:
                        if first:
                            first = False
                            await _fire_first_chunk(on_first_chunk, piece)
                        yield piece
                elif mtype == "event":
                    if (msg.get("data") or {}).get("event_type") == "final":
                        self._residue = False
                        return  # clean termination [PROBED: final event observed]
                    log.debug("speak(%s): ignoring event frame %s", utterance_id, msg)
                elif mtype == "error":
                    raise TTSWSError(f"Sarvam TTS error frame: {_extract_error(msg)}")
                else:
                    log.debug("speak(%s): ignoring frame type=%r", utterance_id, mtype)
        except GeneratorExit:
            self._residue = True  # consumer barge-stopped us mid-stream
            raise

    async def ping(self) -> None:
        """Send one app-level {"type:"ping"} keepalive frame immediately."""
        conn = self._conn
        if conn is None:
            raise TTSConnectionLost("ping(): TTS WS not connected", close_code=self._close_code)
        try:
            await conn.send(json.dumps({"type": "ping"}))
        except ConnectionClosed:
            self._register_close(conn)
            raise self._mapped_error("ping()")
        self.ping_count += 1

    async def close(self) -> None:
        """Close the socket cleanly (code 1000) and stop all background tasks."""
        tasks = [t for t in (self._reconnect_task, self._ping_task) if t is not None]
        self._reconnect_task = None
        self._ping_task = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:  # noqa: PERF203 — teardown best effort
                pass
            except Exception:  # noqa: BLE001
                pass
        async with self._lock:
            conn, self._conn = self._conn, None
            self._close_code = 1000
            self._close_reason = "closed by client"
        if conn is not None:
            try:
                await asyncio.wait_for(conn.close(code=1000), timeout=2.0)
            except Exception:  # noqa: BLE001
                pass

    # --------------------------------------------------------- chunk shaping
    def _normalize_first(self, chunk: bytes) -> list[bytes]:
        """Defensive shaping of the FIRST received audio chunk (PROBED: exactly 44 bytes)."""
        if chunk[:4] == b"RIFF":
            if len(chunk) == 44:
                return [chunk]  # probed live shape — pass through
            if len(chunk) > 44:  # fused header+payload → split
                return [chunk[:44], chunk[44:]]
            log.warning("TTS WS sent a %d-byte first chunk (RIFF but < 44) — passing through", len(chunk))
            return [chunk]
        # no RIFF header at all: prepend a canonical one so the client contract still holds
        rate = SAMPLE_RATE
        if self._cfg and isinstance(self._cfg.get("data"), dict):
            rate = int(self._cfg["data"].get("speech_sample_rate", SAMPLE_RATE))
        log.warning("TTS WS first chunk lacked RIFF header (%d bytes) — prepending canonical header", len(chunk))
        return [canonical_wav_header(rate, 1, 16, 0), chunk]

    @staticmethod
    def _normalize_pcm(chunk: bytes) -> list[bytes]:
        """Continuation chunks: raw PCM passthrough; strip a re-sent RIFF header if fused."""
        if chunk[:4] == b"RIFF":
            if len(chunk) > 44:
                return [chunk[44:]]
            return []  # a bare re-sent header carries no PCM
        return [chunk]

    # ------------------------------------------------------------- internals
    async def _send_config_locked(self) -> None:
        if self._cfg is None:
            return
        await self._send_on(self._conn, json.dumps(self._cfg))
        log.debug("TTS config sent (speaker=%s)", self._cfg["data"]["speaker"])

    async def _send_on(self, conn: Any, payload: str) -> None:
        try:
            await conn.send(payload)
        except ConnectionClosed:
            self._register_close(conn)
            raise self._mapped_error("send")

    async def _send(self, payload: str) -> None:
        conn = self._conn
        if conn is None:
            raise TTSConnectionLost("TTS WS not connected", close_code=self._close_code)
        await self._send_on(conn, payload)

    def _mapped_error(self, action: str) -> TTSError:
        if self._fatal is not None:
            return self._fatal
        return TTSConnectionLost(
            f"{action}: Sarvam TTS WS closed (code={self._close_code}, reason={self._close_reason!r})",
            close_code=self._close_code,
            reason=self._close_reason,
        )

    def _register_close(self, conn: Any = None) -> tuple[int, str]:
        """Record the close of ``conn`` (the conn the failure was observed on)."""
        observed = self._conn if conn is None else conn
        if observed is None:
            return (self._close_code or 1006, self._close_reason)
        code = getattr(observed, "close_code", None)
        reason = getattr(observed, "close_reason", "") or ""
        code = int(code) if code is not None else 1006
        if 4000 <= code < 5000 and self._fatal is None:
            self._fatal = TTSFatalError(
                f"Sarvam TTS WS closed with application code {code} (reason={reason!r}) "
                f"— auth/quota failure, not retrying"
            )
        if observed is self._conn:
            self._close_code = code
            self._close_reason = str(reason)
            if self._ping_task is not None:
                self._ping_task.cancel()
                self._ping_task = None
        log.info("TTS WS closed (code=%s reason=%r) fatal=%s", code, reason, self._fatal is not None)
        return (code, str(reason))

    async def _drain_stale(self, conn: Any, budget_s: float = 0.5, per_frame_s: float = 0.05) -> None:
        """Discard frames left over from an interrupted utterance (barge-in safety)."""
        deadline = time.monotonic() + budget_s
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(conn.recv(), timeout=per_frame_s)
            except asyncio.TimeoutError:
                break
            except ConnectionClosed:
                self._register_close(conn)
                raise self._mapped_error("drain")
            try:
                msg = _decode_frame(raw)
            except TTSWSError:
                break
            if msg.get("type") == "event" and (msg.get("data") or {}).get("event_type") == "final":
                break  # end of the stale utterance — socket is clean now
            log.debug("draining stale frame: %s", str(msg)[:80])

    async def _ensure_ready_locked(self) -> bool:
        """Make the socket open + configured. Returns True if this call opened it.

        Close-code policy: 1000/1001 clean (immediate re-open, no backoff) · 1003 →
        cooldown then backoff ladder · 1006/1005/1011/other abnormal → backoff ladder
        (0.5→1→2→4 s, max ``max_reconnect_tries`` retries) · 4xxx auth/quota → fatal.
        """
        if self._fatal is not None:
            raise self._fatal
        if self._conn is not None and self._close_code is None:
            return False  # healthy
        code = self._close_code
        if code is None:
            if self._cfg is None:
                await self._open_locked()  # prewarm: config arrives via set_speaker()
                return True
            try:
                await self._open_locked()
                return True
            except (TTSConfigError, TTSFatalError):
                raise
            except Exception:  # noqa: BLE001 — first-open failure falls into the ladder below
                code = self._close_code or 1006
        if 4000 <= code < 5000:
            raise self._fatal if self._fatal is not None else TTSFatalError(
                f"Sarvam TTS WS closed with application code {code} (auth/quota failure)"
            )
        if code in (1000, 1001):  # clean close — reconnect immediately, no backoff
            try:
                await self._open_locked()
                return True
            except (TTSConfigError, TTSFatalError):
                raise
            except Exception as exc:  # noqa: BLE001
                raise TTSConnectionLost(
                    f"TTS WS re-open after clean close {code} failed: {exc}",
                    close_code=self._close_code or 1006,
                ) from exc
        # abnormal close (1006 / 1005 / 1011 / 1003 / unknown) → backoff ladder
        if self._exhausted_at is not None:
            if time.monotonic() - self._exhausted_at < self._backoff_s[-1]:
                raise TTSConnectionLost(
                    f"TTS WS reconnect attempts exhausted recently (close={code}) — demote transport",
                    close_code=code,
                )
            self._exhausted_at = None  # enough time has passed — re-arm a fresh ladder
        if code == 1003:  # rate-limited connection: cool down, then escalate
            delays = (self._cooldown_1003_s, *self._backoff_s[1:])
        else:
            delays = self._backoff_s
        await self._open_with_backoff_locked(delays)
        return True

    async def _open_with_backoff_locked(self, delays: tuple[float, ...]) -> None:
        """Retry loop: sleep delays[i], then try to open + re-config."""
        last_exc: Exception | None = None
        for attempt in range(1, self._max_reconnect_tries + 1):
            if self._fatal is not None:
                raise self._fatal
            delay = delays[min(attempt - 1, len(delays) - 1)]
            log.info(
                "TTS WS reconnect attempt %d/%d in %.2fs (close=%s)",
                attempt,
                self._max_reconnect_tries,
                delay,
                self._close_code,
            )
            await self._sleep(delay)
            try:
                await self._open_locked()
                return
            except (TTSConfigError, TTSFatalError):
                raise
            except Exception as exc:  # noqa: BLE001 — any transport failure retries
                last_exc = exc
                if self._fatal is not None:
                    raise self._fatal
                self._close_code = self._close_code or 1006
        self._exhausted_at = time.monotonic()
        raise TTSConnectionLost(
            f"TTS WS reconnect failed after {self._max_reconnect_tries} tries "
            f"(close={self._close_code}): {last_exc}",
            close_code=self._close_code,
        )

    async def _open_locked(self) -> None:
        """Open a fresh socket; send config (if any) as the first message; start pings."""
        headers = {"api-subscription-key": _request_key(self._api_key)}
        conn = await self._connector(self._ws_url, headers)
        self._conn = conn
        self._close_code = None
        self._close_reason = ""
        self._exhausted_at = None
        self._residue = False
        self.connect_count += 1
        try:
            await self._send_config_locked()
        except Exception:
            self._register_close()
            raise
        self._start_ping_locked()

    def _start_ping_locked(self) -> None:
        if self._ping_task is not None and not self._ping_task.done():
            self._ping_task.cancel()
        conn = self._conn
        self._ping_task = asyncio.create_task(self._ping_loop(conn))

    async def _ping_loop(self, conn: Any) -> None:
        try:
            while True:
                await asyncio.sleep(self._ping_interval)
                if conn is not self._conn:
                    return
                try:
                    await conn.send(json.dumps({"type": "ping"}))
                    self.ping_count += 1
                except ConnectionClosed:
                    self._register_close(conn)
                    self._kick_background_reconnect()
                    return
        except asyncio.CancelledError:
            raise

    def _kick_background_reconnect(self) -> None:
        """Re-warm the socket in the background (idle close observed by the ping loop)."""
        if self._fatal is not None:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(self._background_reconnect())

    async def _background_reconnect(self) -> None:
        try:
            async with self._lock:
                await self._ensure_ready_locked()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — next speak() retries the policy
            log.warning("TTS WS background reconnect failed: %s", exc)


# ---------------------------------------------------------------------------
# HTTP fallbacks (transport ladder rungs 2 and 3)
# ---------------------------------------------------------------------------
async def speak_via_stream(
    text: str,
    speaker: str,
    language_code: str,
    pace: float = 1.0,
    sample_rate: int = SAMPLE_RATE,
    *,
    dict_id: str | None = None,
    api_key: str | None = None,
    client: httpx.AsyncClient | None = None,
    timeout: float = HTTP_TIMEOUT_S,
) -> bytes:
    """POST /text-to-speech/stream → full raw WAV bytes (raw binary body, no base64).

    The HTTP status is checked BEFORE the body is treated as audio (non-200 bodies are
    JSON error objects). ``output_audio_codec:"wav"`` is set explicitly — the stream
    endpoint defaults to mp3 [PROBED].
    """
    text = _validate_text(text, STREAM_CHAR_LIMIT, "speak_via_stream()")
    payload = _tts_payload(text, speaker, language_code, pace, sample_rate, dict_id)
    headers = {"api-subscription-key": _request_key(api_key)}
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=timeout)
    try:
        try:
            resp = await client.post(TTS_STREAM_URL, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise TTSHTTPError(0, f"speak_via_stream: transport error: {exc}") from exc
        if resp.status_code != 200:
            raise _http_error(resp, "speak_via_stream")
        return resp.content
    finally:
        if own_client:
            await client.aclose()


async def speak_via_rest(
    text: str,
    speaker: str,
    language_code: str,
    pace: float = 1.0,
    sample_rate: int = SAMPLE_RATE,
    *,
    dict_id: str | None = None,
    api_key: str | None = None,
    client: httpx.AsyncClient | None = None,
    timeout: float = HTTP_TIMEOUT_S,
) -> bytes:
    """POST /text-to-speech → decoded WAV bytes from audios[0] (base64 → bytes)."""
    text = _validate_text(text, WS_TEXT_CHAR_LIMIT, "speak_via_rest()")
    payload = _tts_payload(text, speaker, language_code, pace, sample_rate, dict_id)
    headers = {"api-subscription-key": _request_key(api_key)}
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=timeout)
    try:
        try:
            resp = await client.post(TTS_REST_URL, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise TTSHTTPError(0, f"speak_via_rest: transport error: {exc}") from exc
        if resp.status_code != 200:
            raise _http_error(resp, "speak_via_rest")
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 — non-JSON body on a 200
            raise TTSHTTPError(resp.status_code, f"speak_via_rest: non-JSON response: {exc}", body=resp.text[:200]) from exc
        audios = data.get("audios") if isinstance(data, dict) else None
        if not isinstance(audios, list) or not audios:
            raise TTSHTTPError(resp.status_code, "speak_via_rest: response has no audios[0]", body=str(data)[:200])
        try:
            return base64.b64decode(audios[0])
        except (binascii.Error, ValueError, TypeError) as exc:
            raise TTSHTTPError(resp.status_code, f"speak_via_rest: audios[0] is not valid base64: {exc}") from exc
    finally:
        if own_client:
            await client.aclose()


# ---------------------------------------------------------------------------
# Cached-blob chunker (transport ladder rung 1)
# ---------------------------------------------------------------------------
def wav_blob_to_chunks(wav: bytes, chunk_pcm_bytes: int = 32768) -> tuple[bytes, list[bytes]]:
    """Cached WAV blob → (header_chunk, [pcm_chunks]) matching the live WS shape.

    The header chunk is a canonical 44-byte RIFF/PCM header (rebuilt from the blob's
    own fmt chunk so it is always exactly 44 bytes, even for non-canonical blobs);
    the PCM payload is split into chunks of at most ``chunk_pcm_bytes`` raw bytes —
    the same continuation shape the live WS stream produces [PROBED].
    """
    if not isinstance(chunk_pcm_bytes, int) or chunk_pcm_bytes <= 0:
        raise ValueError(f"chunk_pcm_bytes must be a positive int, got {chunk_pcm_bytes!r}")
    if not isinstance(wav, (bytes, bytearray)) or len(wav) < 44:
        raise ValueError("wav blob too short to be a RIFF/WAVE file")
    if bytes(wav[:4]) != b"RIFF" or bytes(wav[8:12]) != b"WAVE":
        raise ValueError("not a RIFF/WAVE blob (bad magic)")

    wav = bytes(wav)
    fmt: bytes | None = None
    data: bytes | None = None
    offset = 12
    while offset + 8 <= len(wav):
        chunk_id = wav[offset : offset + 4]
        (size,) = struct.unpack_from("<I", wav, offset + 4)
        body = wav[offset + 8 : offset + 8 + size]
        if chunk_id == b"fmt " and fmt is None:
            fmt = body
        elif chunk_id == b"data" and data is None:
            data = body
        offset += 8 + size + (size & 1)  # chunks are word-aligned
    if fmt is None or len(fmt) < 16:
        raise ValueError("WAV blob has no valid fmt chunk")
    if data is None:
        raise ValueError("WAV blob has no data chunk")

    audio_format, channels, sample_rate, _byte_rate, _block_align, bits = struct.unpack_from("<HHIIHH", fmt)
    if audio_format not in (0x0001, 0xFFFE) or bits != 16:
        raise ValueError(f"only 16-bit PCM WAV blobs are supported (format={audio_format}, bits={bits})")

    header = canonical_wav_header(sample_rate, channels, bits, len(data))
    chunks = [data[i : i + chunk_pcm_bytes] for i in range(0, len(data), chunk_pcm_bytes)]
    return header, chunks


__all__ = [
    "AUDIO_BITRATE",
    "AUDIO_CODEC",
    "BASE_URL",
    "COOLDOWN_1003_S",
    "HTTP_TIMEOUT_S",
    "IDLE_TIMEOUT_S",
    "MAX_CHUNK_LENGTH",
    "MIN_BUFFER_SIZE",
    "PING_INTERVAL_S",
    "RECONNECT_BACKOFF_S",
    "RECONNECT_MAX_TRIES",
    "SAMPLE_RATE",
    "TTSConnectionLost",
    "TTSError",
    "TTSFatalError",
    "TTSHTTPError",
    "TTSConfigError",
    "TTSWSError",
    "TTS_MODEL",
    "TTS_REST_URL",
    "TTS_STREAM_URL",
    "TTS_WS_URL",
    "VoiceChannel",
    "build_config",
    "canonical_wav_header",
    "speak_via_rest",
    "speak_via_stream",
    "wav_blob_to_chunks",
]
