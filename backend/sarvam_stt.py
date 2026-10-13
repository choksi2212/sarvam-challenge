"""Sarvam speech-to-text client for BhashaBuzzer (A3).

Single-purpose wrapper around the synchronous Sarvam STT REST endpoint:

    POST https://api.sarvam.ai/speech-to-text   (multipart/form-data)

Field set is FROZEN by CONTRACT §3 and verified live on 2026-10-13
(CONTRACT §4 "[ALL PROBED 200]"):  model="saaras:v4", mode="codemix",
language_code="unknown" (auto-detect), optional single "keyterms" form
field carrying a JSON-encoded list of expected-answer tokens, plus the
audio as file "answer.wav" (audio/wav).  Auth rides the
"api-subscription-key" header; the key is resolved at call time from
backend.config (A7) or the process environment and is never logged.

No live Sarvam traffic ever happens from this module by itself: production
calls go through the default httpx transport, while tests inject an
httpx.MockTransport via the module-level `_transport` seam.

Public API (CONTRACT §3, binding):

    STTUnavailable                    — the single failure signal
    await transcribe_answer(wav_bytes, keyterms=None) -> dict
        {"text": transcript, "language_code": detected, "raw": full JSON}
"""

from __future__ import annotations

import json
import os

import httpx

__all__ = ["STTUnavailable", "transcribe_answer"]

BASE_URL = "https://api.sarvam.ai"
STT_URL = f"{BASE_URL}/speech-to-text"
STT_MODEL = "saaras:v4"        # [PROBED 200] — never the v3 default
STT_MODE = "codemix"           # [PROBED 200] — Devanagari + Latin Hinglish out
STT_LANGUAGE_CODE = "unknown"  # [PROBED 200] — auto-detect per answer
STT_TIMEOUT_S = 12.0           # CONTRACT §3 binding timeout
WAV_FILENAME = "answer.wav"
WAV_CONTENT_TYPE = "audio/wav"

# Test seam: when set (httpx.MockTransport / any AsyncBaseTransport),
# _build_client() uses it instead of the real HTTP transport. Production
# code never touches this; tests patch it per-case.
_transport: httpx.AsyncBaseTransport | None = None


class STTUnavailable(Exception):
    """Speech-to-text failed or produced nothing usable.

    Raised on HTTP != 200, request timeout, transport failure, non-JSON or
    non-object response, empty/garbage transcript (< 2 chars after strip),
    or a missing API key.  Callers should treat this as a verdict of
    "unclear" (CONTRACT §2: host asks the player to repeat, no penalty).
    """


def _api_key() -> str:
    """Resolve the Sarvam API key without ever printing or logging it.

    Preference: backend.config.SARVAM_API_KEY (A7's module — itself
    "env var SARVAM_API_KEY, else ../.env line"), falling back to the
    process environment when backend.config is not importable or has no
    key.  This module NEVER reads any .env file itself.
    """
    try:
        # Deliberately lazy + defensive: config.py is owned by A7 and may
        # not exist yet in this worktree.
        from . import config  # type: ignore[import-untyped]

        key = getattr(config, "SARVAM_API_KEY", "")
    except Exception:
        key = ""
    if not key:
        key = os.environ.get("SARVAM_API_KEY", "")
    return str(key) if key else ""


def _build_client() -> httpx.AsyncClient:
    """One-shot client for a single STT call (12 s budget, auth header)."""
    key = _api_key()
    if not key:
        raise STTUnavailable(
            "SARVAM_API_KEY is not configured (set the SARVAM_API_KEY "
            "environment variable or backend/config.py)"
        )
    return httpx.AsyncClient(
        timeout=httpx.Timeout(STT_TIMEOUT_S),
        headers={"api-subscription-key": key},
        transport=_transport,
    )


async def transcribe_answer(
    wav_bytes: bytes, keyterms: list[str] | None = None
) -> dict:
    """Transcribe a spoken answer via Sarvam saaras:v4 (codemix mode).

    POSTS multipart/form-data to /speech-to-text with the probed field
    set; when ``keyterms`` is a non-empty list it is sent as ONE form
    field containing the JSON-encoded list (v4-only biasing — not the
    legacy ``keyterm``/``hotwords`` fields).

    Returns ``{"text": str, "language_code": str, "raw": dict}`` where
    ``text`` is the transcript, ``language_code`` the detected code, and
    ``raw`` the full response JSON (request_id, probability, ...).

    Raises STTUnavailable on non-200, timeout or transport failure,
    malformed response, or an empty/garbage transcript (blank or under
    2 characters after stripping).
    """
    if not isinstance(wav_bytes, (bytes, bytearray, memoryview)):
        raise STTUnavailable("wav_bytes must be bytes-like audio data")
    audio = bytes(wav_bytes)
    if not audio:
        raise STTUnavailable("wav_bytes is empty — no audio to transcribe")

    data: dict[str, str] = {
        "model": STT_MODEL,
        "mode": STT_MODE,
        "language_code": STT_LANGUAGE_CODE,
    }
    if keyterms:  # None or [] → omit the field entirely
        data["keyterms"] = json.dumps(keyterms)

    try:
        async with _build_client() as client:
            response = await client.post(
                STT_URL,
                files={"file": (WAV_FILENAME, audio, WAV_CONTENT_TYPE)},
                data=data,
            )
    except httpx.TimeoutException as exc:
        raise STTUnavailable(
            f"speech-to-text timed out after {STT_TIMEOUT_S:.0f}s"
        ) from exc
    except httpx.RequestError as exc:
        raise STTUnavailable(
            f"speech-to-text request failed: {type(exc).__name__}"
        ) from exc

    if response.status_code != 200:
        detail = response.text[:200].strip()
        message = f"speech-to-text returned HTTP {response.status_code}"
        if detail:
            message = f"{message}: {detail}"
        raise STTUnavailable(message)

    try:
        raw = response.json()
    except ValueError as exc:
        raise STTUnavailable(
            "speech-to-text response was not valid JSON"
        ) from exc
    if not isinstance(raw, dict):
        raise STTUnavailable(
            "speech-to-text response was not a JSON object"
        )

    transcript = raw.get("transcript")
    if not isinstance(transcript, str) or len(transcript.strip()) < 2:
        raise STTUnavailable(
            "speech-to-text returned an empty or unusable transcript"
        )

    return {
        "text": transcript,
        "language_code": str(raw.get("language_code") or ""),
        "raw": raw,
    }
