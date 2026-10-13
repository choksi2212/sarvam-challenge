"""BhashaBuzzer cache layer — A2 (CONTRACT §3 signatures, §7 sacred budget rules).

The budget shield for the ₹100 Sarvam credit (backend-plan D7):

* **TTS blob cache** — key = sha256 over *every* parameter that can change the
  audio (text, speaker, language_code, pace, sample_rate, codec, dict_id, pitch,
  loudness, + key-format salt carrying the TTS model).  Stored as
  ``cache/tts/<key[:2]>/<key>.wav`` plus a ``<key>.meta.json`` sidecar, written
  atomically (``.tmp`` → ``os.replace``) so a crash can never expose a
  half-written clip.  Byte cap (default 500 MB, env ``BHASHA_CACHE_MAX_BYTES``)
  enforced with strict LRU eviction tracked in ``cache/tts/index.json``.
* **Judge cache** — ``(question_id, normalized_answer)`` → verdict JSON, backed
  by ``cache/judge/judge_cache.json`` so identical answers never re-bill the
  105B judge (CONTRACT §7; never used to skip STT).
* **Spend ledger** — ``logs/spend.jsonl``, one JSON line per synthesis
  ``{ts, chars, what, cumulative}``; ``chars_used()`` re-derives the total from
  the ledger (source of truth for /healthz + the governor).
* **Governor** — warn 30,000 / cached-only 40,000 / hard-stop 45,000 chars.

Standalone by design: **no sibling imports** (config.py is A7's).  Directories
and the byte cap come from env (``BHASHA_CACHE_DIR``, ``BHASHA_LOGS_DIR``,
``BHASHA_CACHE_MAX_BYTES``) and can be re-pointed in-process via
``configure()`` (used by tests and by main.py at boot).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Binding constants (CONTRACT §3 / §7)
# ---------------------------------------------------------------------------

GOVERNOR_LIMITS: dict[str, int] = {"warn": 30_000, "cached_only": 40_000, "hard": 45_000}  # chars

#: Default LRU byte cap for the wav blob store (env: BHASHA_CACHE_MAX_BYTES).
DEFAULT_MAX_CACHE_BYTES = 500 * 1024 * 1024  # 500 MB

# Key-format salts. Bump either one → every old cache file is invalidated
# (backend-plan §5.1). The TTS salt also pins the model: bulbul:v4-flash.
_TTS_KEY_SALT = "bhasha-tts-v1|bulbul:v4-flash"
_JUDGE_KEY_SALT = "bhasha-judge-v1"
_INDEX_VERSION = 1

_REPO_ROOT = Path(__file__).resolve().parent.parent

_lock = threading.Lock()  # serialise index mutations within this process
_seq = 0  # monotonic tie-breaker: same-millisecond touches keep strict LRU order
_index_cache: dict | None = None  # parsed index.json fast path (reset by configure)
_last_index_write = 0.0  # last index.json flush (recency writes are coalesced)
_INDEX_WRITE_INTERVAL = 0.25  # s; cache_put and eviction always flush immediately


# ---------------------------------------------------------------------------
# Directory / cap configuration (env-first, override via configure())
# ---------------------------------------------------------------------------

_cache_dir_override: Path | None = None
_logs_dir_override: Path | None = None
_max_cache_bytes_override: int | None = None


def configure(cache_dir: str | os.PathLike | None = None,
              logs_dir: str | os.PathLike | None = None,
              max_cache_bytes: int | None = None) -> None:
    """Point the cache/logs at runtime (or test) directories.

    Each slot is fully re-applied on every call: a value sets it, ``None``
    reverts that slot to env-var/derived default.  main.py (A7) calls this once
    at boot with config.CACHE_DIR / config.LOGS_DIR; tests call it per-test with
    tmp dirs and call ``configure()`` bare to reset.
    """
    global _cache_dir_override, _logs_dir_override, _max_cache_bytes_override
    global _index_cache, _last_index_write
    with _lock:
        _cache_dir_override = Path(cache_dir) if cache_dir is not None else None
        _logs_dir_override = Path(logs_dir) if logs_dir is not None else None
        _max_cache_bytes_override = int(max_cache_bytes) if max_cache_bytes is not None else None
        _index_cache = None
        _last_index_write = 0.0


def cache_dir() -> Path:
    """Root cache dir (env BHASHA_CACHE_DIR, else <repo>/cache)."""
    if _cache_dir_override is not None:
        return _cache_dir_override
    env = os.environ.get("BHASHA_CACHE_DIR")
    return Path(env) if env else _REPO_ROOT / "cache"


def logs_dir() -> Path:
    """Logs dir holding spend.jsonl (env BHASHA_LOGS_DIR, else <repo>/logs)."""
    if _logs_dir_override is not None:
        return _logs_dir_override
    env = os.environ.get("BHASHA_LOGS_DIR")
    return Path(env) if env else _REPO_ROOT / "logs"


def max_cache_bytes() -> int:
    """LRU byte cap for cached wavs (env BHASHA_CACHE_MAX_BYTES, else 500 MB)."""
    if _max_cache_bytes_override is not None:
        return _max_cache_bytes_override
    env = os.environ.get("BHASHA_CACHE_MAX_BYTES")
    if env:
        try:
            return max(0, int(env))
        except ValueError:
            pass  # garbage in env → fall back to the default
    return DEFAULT_MAX_CACHE_BYTES


def spend_path() -> Path:
    """The spend ledger file (logs/spend.jsonl)."""
    return logs_dir() / "spend.jsonl"


# ---------------------------------------------------------------------------
# Keys (CONTRACT §3)
# ---------------------------------------------------------------------------

def _canon_num(value: float | int | None) -> str:
    """Stable canonical string for a numeric key component.

    ``None`` maps to a control character distinct from every numeric repr so
    "not sent" ≠ "sent as 0.0".  Integral floats collapse with ints (pace 1 ==
    1.0) so caller-side int/float drift never splits the cache.
    """
    if value is None:
        return "\x00"
    f = float(value)
    return str(int(f)) if f.is_integer() and abs(f) < 1e15 else repr(f)


def _hash_parts(parts: list[str]) -> str:
    # JSON list encoding is injective — unlike "|" joins, text containing the
    # delimiter can never make two different tuples hash the same.
    blob = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def tts_key(text: str, speaker: str, language_code: str, pace: float,
            sample_rate: int, codec: str, dict_id: str | None = None,
            pitch: float | None = None, loudness: float | None = None) -> str:
    """sha256 cache key over every TTS parameter that can change the audio."""
    if not isinstance(text, str):
        raise TypeError("text must be a str")
    parts = [
        _TTS_KEY_SALT, text, speaker, language_code,
        _canon_num(pace), _canon_num(sample_rate), codec,
        "\x00" if dict_id is None else str(dict_id),
        _canon_num(pitch), _canon_num(loudness),
    ]
    return _hash_parts(parts)


def judge_key(question_id: str, normalized_answer: str) -> str:
    """sha256 judge-cache key over (question_id, normalized heard answer)."""
    return _hash_parts([_JUDGE_KEY_SALT, question_id, normalized_answer])


# ---------------------------------------------------------------------------
# TTS blob cache (atomic writes + LRU byte-cap eviction)
# ---------------------------------------------------------------------------

def _wav_path(key: str) -> Path:
    return cache_dir() / "tts" / key[:2] / f"{key}.wav"


def _meta_path(key: str) -> Path:
    return cache_dir() / "tts" / key[:2] / f"{key}.meta.json"


def _index_path() -> Path:
    return cache_dir() / "tts" / "index.json"


def _judge_store_path() -> Path:
    return cache_dir() / "judge" / "judge_cache.json"


def cache_get(key: str) -> bytes | None:
    """Cached wav blob for ``key``, or None on miss (CONTRACT §3)."""
    try:
        data = _wav_path(key).read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        return None  # transient read trouble → treat as miss; synth ladder handles it
    _touch(key, len(data))
    return data


def cache_put(key: str, wav_bytes: bytes, meta: dict) -> None:
    """Atomically store ``wav_bytes`` + a ``.meta.json`` sidecar for ``key``.

    Both files are written as ``.tmp`` siblings then moved into place with
    ``os.replace`` (rename), so readers only ever observe a fully written file.
    Afterwards the LRU byte cap is enforced (CONTRACT §3: LRU byte-cap).
    """
    if not isinstance(wav_bytes, (bytes, bytearray, memoryview)):
        raise TypeError("wav_bytes must be bytes-like")
    blob = bytes(wav_bytes)
    entry_dir = _wav_path(key).parent
    entry_dir.mkdir(parents=True, exist_ok=True)

    record = {"key": key, "size": len(blob), "created_ms": int(time.time() * 1000)}
    record.update(meta or {})

    wav_tmp = entry_dir / f"{key}.wav.tmp"
    meta_tmp = entry_dir / f"{key}.meta.json.tmp"
    wav_tmp.write_bytes(blob)
    meta_tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.replace(wav_tmp, _wav_path(key))
        os.replace(meta_tmp, _meta_path(key))
    finally:
        # on a failed rename, clean the tmp siblings — never litter the cache
        for tmp in (wav_tmp, meta_tmp):
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
    _touch(key, len(blob), flush=True)


def _rebuild_index() -> dict:
    """Index from disk scan (used when index.json is missing or corrupt)."""
    entries: dict[str, dict] = {}
    tts_root = cache_dir() / "tts"
    if tts_root.is_dir():
        for wav in tts_root.glob("*/*.wav"):
            try:
                stat = wav.stat()
            except OSError:
                continue
            entries[wav.stem] = {"last_used": stat.st_mtime, "seq": 0, "size": stat.st_size}
    return {"version": _INDEX_VERSION, "entries": entries}


def _load_index() -> dict:
    if _index_cache is not None:
        return _index_cache
    try:
        idx = json.loads(_index_path().read_text(encoding="utf-8"))
        if isinstance(idx, dict) and isinstance(idx.get("entries"), dict):
            return idx
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        pass
    return _rebuild_index()


def _save_index(idx: dict) -> None:
    path = _index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(idx, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def _touch(key: str, size: int, *, flush: bool = False) -> None:
    """Record a use of ``key`` in the LRU index; best-effort (never raises).

    The parsed index is cached in memory; disk writes are coalesced to at most
    ~4/s for read-path touches, while puts and evictions always flush at once.
    If index bookkeeping fails, the in-memory cache is dropped so the next
    touch re-derives truth from disk (``_rebuild_index`` rescans wavs anyway).
    """
    global _seq, _index_cache, _last_index_write
    with _lock:
        _seq += 1
        try:
            idx = _load_index()
            idx["entries"][key] = {"last_used": time.time(), "seq": _seq, "size": size}
            evicted = _evict_over_cap(idx)
            now = time.time()
            if flush or evicted or (now - _last_index_write) >= _INDEX_WRITE_INTERVAL:
                _save_index(idx)
                _last_index_write = now
            _index_cache = idx
        except OSError:
            _index_cache = None
            return


def _evict_over_cap(idx: dict) -> list[str]:
    """Evict least-recently-used entries (wav + sidecar) until under the cap."""
    cap = max_cache_bytes()
    entries: dict[str, dict] = idx["entries"]
    total = sum(e.get("size", 0) for e in entries.values())
    evicted: list[str] = []
    while total > cap and entries:
        key = min(entries, key=lambda k: (entries[k].get("last_used", 0.0),
                                          entries[k].get("seq", 0), k))
        total -= entries.pop(key).get("size", 0)
        for path in (_wav_path(key), _meta_path(key)):
            try:
                path.unlink()
            except OSError:
                pass
        evicted.append(key)
    return evicted


# ---------------------------------------------------------------------------
# Judge cache (JSON-file-backed)
# ---------------------------------------------------------------------------

def judge_get(key: str) -> dict | None:
    """Cached verdict dict for ``key`` (from judge_key), or None on miss."""
    try:
        store = json.loads(_judge_store_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    verdicts = store.get("verdicts", {}) if isinstance(store, dict) else {}
    verdict = verdicts.get(key)
    return verdict if isinstance(verdict, dict) else None


def judge_put(key: str, verdict: dict) -> None:
    """Persist ``verdict`` under ``key`` (atomic tmp→rename JSON write)."""
    if not isinstance(verdict, dict):
        raise TypeError("verdict must be a dict")
    path = _judge_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        store = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(store, dict) or not isinstance(store.get("verdicts"), dict):
            raise ValueError("not a judge store")
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
        store = {"version": 1, "verdicts": {}}
    store["verdicts"][key] = verdict
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Spend ledger + governor (CONTRACT §7)
# ---------------------------------------------------------------------------

def log_spend(chars: int, what: str) -> None:
    """Append one ledger line ``{ts, chars, what, cumulative}`` to spend.jsonl.

    Called on EVERY synthesis (CONTRACT §7); ``cumulative`` is the running total
    including this entry, so the ledger is self-checking.
    """
    if isinstance(chars, bool) or not isinstance(chars, int):
        raise TypeError("chars must be an int")
    ledger = spend_path()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "chars": chars,
            "what": what,
            "cumulative": chars_used() + chars,
        }
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())  # a lost spend line = untraceable rupees


def chars_used() -> int:
    """Total chars spent so far, summed from the spend ledger (CONTRACT §3)."""
    total = 0
    try:
        fh = spend_path().open("r", encoding="utf-8")
    except FileNotFoundError:
        return 0
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn line must never break accounting
            value = record.get("chars", 0) if isinstance(record, dict) else 0
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            total += int(value)
    return total


def governor_level(chars: int | None = None) -> str:
    """"ok" | "warn" | "cached_only" | "hard" at the CONTRACT §7 thresholds.

    Defaults to the live ledger total (chars_used()); pass ``chars`` to test a
    hypothetical spend. main.py maps: warn → log, cached_only → serve from
    cache only, hard → zero new synthesis.
    """
    used = chars_used() if chars is None else int(chars)
    if used >= GOVERNOR_LIMITS["hard"]:
        return "hard"
    if used >= GOVERNOR_LIMITS["cached_only"]:
        return "cached_only"
    if used >= GOVERNOR_LIMITS["warn"]:
        return "warn"
    return "ok"
