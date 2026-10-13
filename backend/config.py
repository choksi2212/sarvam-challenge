"""BhashaBuzzer backend config — CONTRACT §3 (A7 owns).

Constants only. The Sarvam API key is resolved LAZILY via the module-level
``__getattr__`` (PEP 562): ``config.SARVAM_API_KEY`` first checks the
``SARVAM_API_KEY`` env var and only then parses ``../.env`` (repo root).
Nothing here reads files at import time, so importing this module from
tests never touches ``.env`` (CONTRACT §8: never read .env during build).
The key is never hardcoded and never printed/logged.
"""
from __future__ import annotations

import os
from pathlib import Path

# ------------------------------------------------------------------ paths
BACKEND_DIR = Path(__file__).resolve().parent
REPO_ROOT = BACKEND_DIR.parent
ENV_FILE = REPO_ROOT / ".env"  # "../.env" relative to backend/ (CONTRACT §3)

FRONTEND_DIR = REPO_ROOT / "frontend"
DATA_DIR = BACKEND_DIR / "data"
CACHE_DIR = BACKEND_DIR / "cache"
LOGS_DIR = BACKEND_DIR / "logs"

# ------------------------------------------------------------ Sarvam API
BASE_URL = "https://api.sarvam.ai"
TTS_MODEL = "bulbul:v4-flash"
STT_MODEL = "saaras:v4"
JUDGE_MODEL = "sarvam-105b-conversations"  # per orchestrator instruction (matches judge.py)
TTS_WS_URL = (
    "wss://api.sarvam.ai/text-to-speech/ws"
    "?model=bulbul:v4-flash&send_completion_event=true"  # [PROBED: accepted]
)
TTS_SAMPLE_RATE = 24000  # wav @ 24 kHz mono Int16LE everywhere [CONTRACT §4]
TTS_CODEC = "wav"
TTS_PACE_DEFAULT = 1.0

# ------------------------------------------------------------- guardrails
MAX_SESSIONS = 8               # concurrent game sessions per server (CONTRACT §3)
WS_OPEN_STAGGER_S = 0.3        # Sarvam WS opens staggered >= 300 ms (burst 1003s)
RETRY_BACKOFF_S = (0.5, 1.0, 2.0, 4.0)  # 429/503 exponential backoff, capped 4 s
RETRY_MAX = 2                  # max retries per HTTP request
JUDGE_RATE_PER_MIN = 24        # token bucket: judge calls <= 24/min (Sarvam 40/min)
JUDGE_BUCKET_CAPACITY = 24

# ------------------------------------------------------------- log files
LATENCY_LOG = "latency.jsonl"  # per-utterance latency decomposition (LOGS_DIR/)
SPEND_LOG = "spend.jsonl"      # written by cache.log_spend (A2 owns the format)


# ------------------------------------------------------- Sarvam API key
def load_api_key() -> str:
    """Resolve the Sarvam API key: env var first, else the ../.env line.

    Called lazily — ONLY at runtime when a real client needs it — never at
    import time, never logged or printed. Returns "" when nothing is found
    (callers raise their own clear config error in that case).
    """
    env = os.environ.get("SARVAM_API_KEY")
    if env and env.strip():
        return env.strip()
    try:
        text = ENV_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""  # no ../.env (e.g. CI) — real calls are orchestrator-only anyway
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == "SARVAM_API_KEY":
            value = value.strip().strip('"').strip("'")
            return value
    return ""


def __getattr__(name: str) -> str:
    """PEP 562 lazy attribute: ``config.SARVAM_API_KEY`` resolves on first use."""
    if name == "SARVAM_API_KEY":
        return load_api_key()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return [*globals().keys(), "SARVAM_API_KEY"]
