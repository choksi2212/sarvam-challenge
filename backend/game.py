"""BhashaBuzzer game engine (A6) — CONTRACT §2 phase machine + §3 GameSession.

Standalone by contract: voice, judge_fn, cast and bank are INJECTED and
duck-typed; this module imports no sibling backend modules and makes no
network calls. All server->client frames of CONTRACT §1 are emitted through
the injected async ``send`` coroutine: state, question, audio_start,
audio_chunk, audio_end, verdict, error.

Phase machine (§2):
    lobby -> round_intro -> question -> listening -> judging -> verdict
           -> next -> (question | round_switch -> round_intro | final)

Modes:
    demo   — fixed 7-question arc (R1 2x GK/Bharat d1-2, R2 2x Bollywood/Cricket
             code-mix d2-3, R3 2x Cricket/Ganit d2-4, FINAL 1x GK/Bharat d3),
             base points 100/150/200/300, deterministic order (fixed seed).
    daily5 — 5 questions (one per category), date-seeded (IST), base points
             100/100/150/150/200, last question hosted by the final persona.

Scoring (§2): base x round + speed bonus (+50/+30/+15 at <=1/2/3 s) +
streak multiplier (x1.5/x2/x2.5 at 2/3/4+ consecutive correct), rounded to the
nearest 5; partial = half base, streak-neutral; never negative.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import math
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

# ---------------------------------------------------------------- constants

DATA_FILE = Path(__file__).resolve().parent / "data" / "questions.json"

SAMPLE_RATE = 24000
CODEC = "wav"
AUDIO_SOURCE = "ws"          # engine speaks through the injected voice channel

# phases (§2 — exact names)
P_LOBBY = "lobby"
P_ROUND_INTRO = "round_intro"
P_QUESTION = "question"
P_LISTENING = "listening"
P_JUDGING = "judging"
P_VERDICT = "verdict"
P_NEXT = "next"
P_ROUND_SWITCH = "round_switch"
P_FINAL = "final"

VERDICTS = {"correct", "wrong", "partial", "unclear"}
LANGUAGES = {"hi", "enhi", "en", "mr"}
# session language -> preferred question language of the bank
LANG_TO_QUESTION_LANG = {"hi": "hi-IN", "enhi": "hi-IN", "en": "en-IN", "mr": "mr-IN"}

# canonical Hinglish cast (game-design §2.1) — used only when no cast is injected
DEFAULT_CAST: dict[int, dict[str, Any]] = {
    1: {"speaker": "ratan_hi_customer_expressive", "label": "Ratan — Quizmaster",
        "language_code": "hi-IN", "pace": 1.0},
    2: {"speaker": "ishita_enhi_customer_expressive", "label": "Ishita — Hinglish Teaser",
        "language_code": "hi-IN", "pace": 1.05},
    3: {"speaker": "ratan_hi_sports", "label": "Ratan — Stadium Hype",
        "language_code": "hi-IN", "pace": 1.1},
    4: {"speaker": "mukul_hi_suspense", "label": "Mukul — Suspense Boss",
        "language_code": "hi-IN", "pace": 0.9},
}

# demo match arc: 2+2+2+1 questions (task spec / design §1.1)
DEMO_PLAN: list[dict[str, Any]] = [
    {"round": 1, "persona_round": 1, "base": 100, "categories": ["GK/Bharat"], "dmin": 1, "dmax": 2},
    {"round": 1, "persona_round": 1, "base": 100, "categories": ["GK/Bharat"], "dmin": 1, "dmax": 2},
    {"round": 2, "persona_round": 2, "base": 150, "categories": ["Bollywood", "Cricket"],
     "dmin": 2, "dmax": 3, "prefer_language": "hi-IN"},                     # code-mix hero zone
    {"round": 2, "persona_round": 2, "base": 150, "categories": ["Bollywood", "Cricket"],
     "dmin": 2, "dmax": 3, "prefer_language": "hi-IN"},
    {"round": 3, "persona_round": 3, "base": 200, "categories": ["Cricket", "Ganit"], "dmin": 2, "dmax": 4},
    {"round": 3, "persona_round": 3, "base": 200, "categories": ["Cricket", "Ganit"], "dmin": 2, "dmax": 4},
    {"round": 4, "persona_round": 4, "base": 300, "categories": ["GK/Bharat"], "dmin": 3, "dmax": 3},
]

# daily 5: one per category, values 100/100/150/150/200, finale persona on Q5
DAILY5_PLAN: list[dict[str, Any]] = [
    {"round": 1, "persona_round": 1, "base": 100, "categories": ["Cricket"], "dmin": 1, "dmax": 5},
    {"round": 1, "persona_round": 1, "base": 100, "categories": ["GK/Bharat"], "dmin": 1, "dmax": 5},
    {"round": 2, "persona_round": 2, "base": 150, "categories": ["Ganit"], "dmin": 1, "dmax": 5},
    {"round": 2, "persona_round": 2, "base": 150, "categories": ["Bollywood"], "dmin": 1, "dmax": 5},
    {"round": 3, "persona_round": 4, "base": 200, "categories": ["Vigyan"], "dmin": 1, "dmax": 5},
]

DEMO_SEED = "bhasha-demo-v1"
DAILY5_SEED_PREFIX = "bhasha-daily5"

QUESTION_FIELDS = ("id", "category", "difficulty", "question_text", "expected_answer",
                   "accepted_answer_aliases", "fun_fact_line", "language")

# ------------------------------------------------------------ scoring (§2)


def speed_bonus(buzz_latency_s: Optional[float]) -> int:
    """+50 at <=1 s, +30 at <=2 s, +15 at <=3 s, else 0."""
    if buzz_latency_s is None or buzz_latency_s < 0:
        return 0
    if buzz_latency_s <= 1.0:
        return 50
    if buzz_latency_s <= 2.0:
        return 30
    if buzz_latency_s <= 3.0:
        return 15
    return 0


def streak_multiplier(streak: int) -> float:
    """x1.5 at 2, x2 at 3, x2.5 at 4+ consecutive correct (applies AFTER increment)."""
    if streak >= 4:
        return 2.5
    if streak == 3:
        return 2.0
    if streak == 2:
        return 1.5
    return 1.0


def round_to_5(x: float) -> int:
    """Round to the nearest 5, half up (design §3)."""
    return int(math.floor(x / 5.0 + 0.5) * 5)


def score_correct(base: int, buzz_latency_s: Optional[float], new_streak: int) -> int:
    """(base + speed bonus) x streak multiplier, rounded to nearest 5."""
    return round_to_5((base + speed_bonus(buzz_latency_s)) * streak_multiplier(new_streak))


def score_partial(base: int) -> int:
    """Half base, no speed bonus, streak-neutral."""
    return base // 2


# ----------------------------------------------------------- question bank


def ist_today() -> str:
    """Current IST date as YYYY-MM-DD (IST has no DST: fixed +05:30)."""
    try:
        from zoneinfo import ZoneInfo  # stdlib; may lack tzdata on some Windows builds
        tz = ZoneInfo("Asia/Kolkata")
    except Exception:
        tz = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(tz).strftime("%Y-%m-%d")


def validate_question(rec: dict[str, Any], where: str = "question") -> None:
    """Raise ValueError unless the record carries every binding field with a sane type."""
    if not isinstance(rec, dict):
        raise ValueError(f"{where}: not an object")
    for field in QUESTION_FIELDS:
        if field not in rec:
            raise ValueError(f"{where}: missing field {field!r}")
    if not isinstance(rec["id"], str) or not rec["id"].strip():
        raise ValueError(f"{where}: bad id")
    for field in ("category", "question_text", "expected_answer", "fun_fact_line", "language"):
        if not isinstance(rec[field], str) or not rec[field].strip():
            raise ValueError(f"{rec['id']}: bad {field}")
    d = rec["difficulty"]
    if not isinstance(d, int) or isinstance(d, bool) or not 1 <= d <= 5:
        raise ValueError(f"{rec['id']}: difficulty must be int 1..5")
    if not isinstance(rec["accepted_answer_aliases"], list):
        raise ValueError(f"{rec['id']}: accepted_answer_aliases must be a list")
    kt = rec.get("keyterms")
    if kt is not None and (not isinstance(kt, list) or len(kt) > 8):
        raise ValueError(f"{rec['id']}: keyterms must be a list of <=8 tokens")


def load_questions(path: str | Path = DATA_FILE) -> list[dict[str, Any]]:
    """Load + validate the question bank (dict with 'questions' list, or a bare list)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    raw = data["questions"] if isinstance(data, dict) else data
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{path}: no questions found")
    questions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, rec in enumerate(raw):
        validate_question(rec, f"question #{i}")
        if rec["id"] in seen:
            raise ValueError(f"duplicate question id {rec['id']!r}")
        seen.add(rec["id"])
        questions.append(rec)
    return questions


def _pool(questions: list[dict[str, Any]], categories: set[str], dmin: int, dmax: int,
          lang: Optional[str], used: set[str]) -> list[dict[str, Any]]:
    cands = [q for q in questions
             if q["category"] in categories
             and dmin <= q["difficulty"] <= dmax
             and (lang is None or q["language"] == lang)
             and q["id"] not in used]
    return sorted(cands, key=lambda q: q["id"])


def select_questions(questions: list[dict[str, Any]], plan: list[dict[str, Any]],
                     language: str, rng: random.Random) -> list[dict[str, Any]]:
    """Deterministic per-plan selection: category/difficulty per slot, language preferred,
    previously-used ids excluded; graceful relaxation so selection can never crash."""
    preferred = LANG_TO_QUESTION_LANG.get(language, "hi-IN")
    used: set[str] = set()
    chosen: list[dict[str, Any]] = []
    for slot in plan:
        cats = set(slot["categories"])
        dmin, dmax = slot["dmin"], slot["dmax"]
        pref = slot.get("prefer_language", preferred)
        wide = (max(1, dmin - 1), min(5, dmax + 1))
        cands = (_pool(questions, cats, dmin, dmax, pref, used)
                 or _pool(questions, cats, dmin, dmax, None, used)
                 or _pool(questions, cats, *wide, pref, used)
                 or _pool(questions, cats, *wide, None, used)
                 or _pool(questions, cats, 1, 5, None, used)
                 or _pool(questions, cats, 1, 5, None, set()))   # last resort: allow reuse
        if not cands:                                            # absolute fallback
            cands = sorted(questions, key=lambda q: q["id"])
        pick = rng.choice(cands)
        used.add(pick["id"])
        chosen.append(pick)
    return chosen


# --------------------------------------------------------- default host copy
# Fallbacks only — the injected bank (A5) wins whenever it carries the line.

DEFAULT_LINES: dict[str, str] = {
    "greeting": "नमस्ते {name}! तैयार हो जाओ — यह है BhashaBuzzer। सिर्फ़ आपकी आवाज़ से खेला जाने वाला quiz show!",
    "rules": "नियम आसान हैं — सवाल सुनो, buzzer दबाओ, जवाब बोलो। जल्दी जवाब पर bonus, लगातार सही जवाब पर multiplier!",
    "round_intro_1": "पहला राउंड — गरमा-गरम शुरूआत! सवाल आसान, जवाब तेज़ चाहिए।",
    "round_intro_2": "राउंड दो — यहाँ चलेगी पूरी तेज़ Hinglish!",
    "round_intro_3": "राउंड तीन — Stadium mode! अब सवाल नहीं, दंगल होगा।",
    "round_intro_4": "और अब... आख़िरी सवाल। Sudden death। सही जवाब — और मैच आपका।",
    "correct": "सही जवाब! शाबाश!",
    "wrong": "ग़लत जवाब! कोई बात नहीं — अगला सवाल आपका है।",
    "partial": "आधा सही! आधी जीत भी जीत होती है।",
    "unclear": "ज़रा साफ़-साफ़ बोलिए, एक बार फिर से!",
    "repeat_request": "माफ़ कीजिए, आवाज़ नहीं पहुँची! इस बार mic के पास बोलिए।",
    "no_answer": "जवाब नहीं आया? चलो, अगले सवाल में धमाका करो!",
    "timeout": "समय समाप्त! घड़ी ने आज buzzer को हरा दिया।",
    "streak_3": "तीन लगातार सही! यह अब luck नहीं — दम है!",
    "streak_5": "पाँच का स्ट्रीक! Host को अब डर लगने लगा है!",
    "final_win": "Sudden death में भी धमाका! आप इस मैच के चैंपियन हैं। बधाई हो!",
    "final_lose": "आख़िरी सवाल पर host आगे निकल गया। रीमैच की तारीख़?",
    "game_over": "यही हुआ आज का BhashaBuzzer! Score {score} अंक — कल फिर आना। बोलो और जीतो!",
    "scoreboard": "आपका final score — {score} अंक!",
    "reveal": "सही जवाब था — {answer}।",
    "skip": "ठीक है, इस सवाल को छोड़ देते हैं!",
}
DEFAULT_TEMPLATE: dict[str, str] = {
    "correct": "correct_confident",
    "wrong": "wrong_confident",
    "partial": "partial",
    "unclear": "repeat_request",
    "no_answer": "no_answer",
}


def _bank_text(value: Any, prefix: str) -> Optional[str]:
    """Extract a speakable line from one bank entry (str | {'text'/'line': str} | [str, ...])."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for field in ("text", "line"):
            if isinstance(value.get(field), str) and value[field].strip():
                return value[field].strip()
        return None
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
        return None
    return None


# ============================================================== GameSession


class GameSession:
    """One game session over the §1 WebSocket protocol (duck-typed dependencies).

    Args:
        send: async fn(dict) emitting one S->C frame (CONTRACT §1).
        voice: duck-typed TTS channel — ``await set_speaker(speaker, language_code,
            pace)``; ``speak(text, utterance_id, on_first_chunk=None)`` async-yields
            bytes chunks shaped per §1 (first = 44-byte RIFF header, rest raw PCM).
        judge_fn: ``(question, heard, persona_label) -> verdict dict`` with keys
            ``verdict`` (correct|wrong|partial|unclear), ``close_call``, ``template_id``,
            ``hype_line``. Sync or async.
        bank: reaction/host-line bank dict (A5). Keys are looked up by template id /
            trigger class; missing keys fall back to built-in Hinglish lines.
        cast: round number -> persona dict (``speaker``, ``label``, ``language_code``,
            ``pace``). Falls back to the canonical Hinglish cast.
        timings: optional overrides {beat, buzz_window, answer_window} in seconds
            (defaults follow CONTRACT §2: 1.2 / 10 / 5).
        stt_fn: optional ``async (wav_bytes, keyterms) -> {"text": str} | str`` for
            audio answers; absent it, audio-only answers take the unclear ladder.
        questions: optional injected question list (defaults to backend/data/questions.json).
    """

    def __init__(self, send: Callable[[dict], Awaitable[None]], voice: Any, judge_fn: Any,
                 bank: Optional[dict] = None, cast: Optional[dict] = None,
                 timings: Optional[dict] = None, stt_fn: Any = None,
                 questions: Optional[list] = None) -> None:
        self.send = send
        self.voice = voice
        self.judge_fn = judge_fn
        self.stt_fn = stt_fn
        self.bank = self._norm_bank(bank)
        self.cast = dict(cast) if cast else {}
        self._timings = {"beat": 1.2, "buzz_window": 10.0, "answer_window": 5.0}
        if timings:
            self._timings.update(timings)
        self.questions = list(questions) if questions is not None else load_questions()
        self._reset_state()

    # ------------------------------------------------------------ lifecycle

    def _reset_state(self) -> None:
        self.phase = P_LOBBY
        self.mode = "demo"
        self.language = "hi"
        self.player_name = ""
        self.plan: list[dict[str, Any]] = []
        self.selected: list[dict[str, Any]] = []   # per-slot chosen questions (tests read this)
        self.q_index = -1
        self.current_question: Optional[dict[str, Any]] = None
        self.current_round = 0
        self.persona_round = 0
        self.score = 0
        self.streak = 0
        self.unclear_count = 0
        self.speaker = ""
        self.persona_label = ""
        self.language_code = "hi-IN"
        self.pace = 1.0
        self.t_go: Optional[float] = None
        self.t_buzz: Optional[float] = None
        self._last_stt_ms = 0.0
        self._last_judge_ms = 0.0
        self._last_verdict = ""
        self._seq = 0
        self._active: Optional[dict[str, Any]] = None
        self._timer: Optional[asyncio.Task] = None
        self._flow_task: Optional[asyncio.Task] = None
        self._started = False
        self._done = False

    def _teardown(self) -> None:
        """Cancel every in-flight task (WS close hook / test teardown)."""
        self._clear_timer()
        if self._flow_task is not None:
            self._flow_task.cancel()
            self._flow_task = None
        if self._active is not None:
            self._active["cancelled"] = True
        self._active = None

    @staticmethod
    def _norm_bank(bank: Optional[dict]) -> dict[str, Any]:
        b = dict(bank) if isinstance(bank, dict) else {}
        lines = b.pop("lines", None)
        if isinstance(lines, dict):
            merged = dict(lines)
            merged.update(b)
            return merged
        return b

    def _bank_line(self, *keys: str) -> str:
        """Look up a host line: exact keys, then prefix matches, then built-in default."""
        for key in keys:
            text = _bank_text(self.bank.get(key), key)
            if text:
                return text
        for key in keys:                                    # prefix fallback: correct_fast -> correct_fast_1
            for bank_key in sorted(self.bank):
                if bank_key.startswith(key):
                    text = _bank_text(self.bank[bank_key], bank_key)
                    if text:
                        return text
        for key in keys:
            if key in DEFAULT_LINES:
                return DEFAULT_LINES[key]
        return ""

    def _fill(self, line: str, **kw: str) -> str:
        for k, v in kw.items():
            line = line.replace("{" + k + "}", str(v))
        return line

    # -------------------------------------------------------------- framing

    async def _emit_state(self, phase: Optional[str] = None) -> None:
        if phase is not None:
            self.phase = phase
        await self.send({
            "type": "state", "phase": self.phase, "round": self.current_round,
            "question_index": self.q_index, "score": self.score, "streak": self.streak,
            "speaker": self.speaker, "persona_label": self.persona_label, "mode": self.mode,
        })

    async def _error(self, code: str, message: str, recoverable: bool = True) -> None:
        await self.send({"type": "error", "phase": self.phase, "code": code,
                         "recoverable": recoverable, "message": message})

    def _next_uid(self) -> str:
        self._seq += 1
        return f"u{self._seq}"

    # ------------------------------------------------------------- speaking

    async def _cancel_active(self) -> None:
        """Barge: mark the in-flight utterance cancelled — pending chunks are dropped."""
        if self._active is not None:
            self._active["cancelled"] = True

    async def _speak(self, text: str, uid: Optional[str] = None,
                     stt_ms: float = 0.0, judge_ms: float = 0.0) -> bool:
        """Speak ``text`` through the injected voice, emitting audio_start -> chunks ->
        audio_end. Returns False when the utterance was barged away."""
        uid = uid or self._next_uid()
        active: dict[str, Any] = {"id": uid, "cancelled": False, "ttfb": None}
        self._active = active
        t0 = time.monotonic()
        await self.send({"type": "audio_start", "utterance_id": uid, "speaker": self.speaker,
                         "sample_rate": SAMPLE_RATE, "codec": CODEC, "caption": text})
        cancelled = False
        try:
            stream = self.voice.speak(text, uid,
                                      lambda *a: active.__setitem__("ttfb", time.monotonic()))
            if hasattr(stream, "__aiter__"):
                async for chunk in stream:
                    if active["cancelled"]:
                        cancelled = True
                        break
                    await self.send({"type": "audio_chunk", "utterance_id": uid,
                                     "b64": base64.b64encode(bytes(chunk)).decode("ascii")})
            else:                                            # tolerate a sync iterable
                for chunk in stream:
                    if active["cancelled"]:
                        cancelled = True
                        break
                    await self.send({"type": "audio_chunk", "utterance_id": uid,
                                     "b64": base64.b64encode(bytes(chunk)).decode("ascii")})
        except asyncio.CancelledError:
            raise
        except Exception as exc:                             # voice blew up mid-utterance
            await self._error("tts_failed", f"TTS error: {exc}")
        t_end = time.monotonic()
        ttfb = active.get("ttfb")
        await self.send({
            "type": "audio_end", "utterance_id": uid,
            "ttfb_ms": round((ttfb - t0) * 1000.0, 1) if ttfb is not None else round((t_end - t0) * 1000.0, 1),
            "total_ms": round((t_end - t0) * 1000.0, 1), "chars": len(text),
            "cache_hit": False, "source": AUDIO_SOURCE,
            "stt_ms": round(stt_ms, 1), "judge_ms": round(judge_ms, 1),
            "e2e_ms": round((t_end - t0) * 1000.0, 1), "cancelled": cancelled,
        })
        if self._active is active:
            self._active = None
        return not cancelled

    # ------------------------------------------------------ background tasks

    def _spawn(self, coro: Any) -> asyncio.Task:
        # Flows are sequential and phase-guarded; only a fresh start/teardown cancels one.
        task = asyncio.create_task(self._guard(coro))
        self._flow_task = task
        return task

    async def _guard(self, coro: Any) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            pass
        except Exception as exc:                             # never let a flow die silently
            import traceback
            traceback.print_exc()
            try:
                await self._error("engine_error", f"game flow error: {exc}")
            except Exception:
                pass

    def _arm_timer(self, delay: float, cb: Callable[[], Any]) -> None:
        self._clear_timer()
        self._timer = asyncio.create_task(self._timer_flow(delay, cb))

    def _clear_timer(self) -> None:
        # Never cancel ourselves: timeout callbacks run inside the timer task.
        if self._timer is not None and self._timer is not asyncio.current_task():
            self._timer.cancel()
        self._timer = None

    async def _timer_flow(self, delay: float, cb: Callable[[], Any]) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        await cb()

    # ------------------------------------------------------------- personas

    async def _apply_persona(self, persona_round: int) -> None:
        persona = self.cast.get(persona_round) or DEFAULT_CAST.get(persona_round) or {}
        self.speaker = str(persona.get("speaker") or "unknown_speaker")
        self.persona_label = str(persona.get("label") or self.speaker)
        self.language_code = str(persona.get("language_code") or "hi-IN")
        self.pace = float(persona.get("pace") or 1.0)
        self.persona_round = persona_round
        try:
            result = self.voice.set_speaker(self.speaker, self.language_code, self.pace)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            await self._error("voice_config_failed", f"set_speaker error: {exc}")

    def _round_intro_text(self, persona_round: int) -> str:
        return self._bank_line(f"round_intro_{persona_round}", f"round_{persona_round}_intro",
                               "round_intro")

    # ---------------------------------------------------------- game flows

    async def _intro_flow(self) -> None:
        """lobby greeting + rules -> round_intro -> first question."""
        await self._apply_persona(1)
        self.current_round = 1
        await self._emit_state(P_LOBBY)
        done = await self._speak(self._fill(self._bank_line("greeting"), name=self.player_name))
        if done:
            done = await self._speak(self._bank_line("rules"))
        await self._emit_state(P_ROUND_INTRO)
        await self._speak(self._round_intro_text(1))
        await self._begin_question()

    async def _begin_question(self) -> None:
        """Emit the question card + host speech; buzz window opens at GO (10 s)."""
        self.q_index += 1
        self.current_question = self.selected[self.q_index]
        self.current_round = self.plan[self.q_index]["round"]
        self.unclear_count = 0
        self.t_go = time.monotonic()
        self.t_buzz = None
        await self._cancel_active()
        await self._emit_state(P_QUESTION)
        q = self.current_question
        await self.send({"type": "question", "id": q["id"], "text": q["question_text"],
                         "category": q["category"], "difficulty": q["difficulty"],
                         "language": q["language"]})
        self._arm_timer(self._timings["buzz_window"], self._on_buzz_timeout)
        await self._speak(q["question_text"])
        # A buzz/timeout may have barged in mid-speech; nothing more to arm here.

    async def _wait_speech_settled(self, timeout_s: float = 1.0) -> None:
        """Let a barged in-flight utterance finish emitting its audio_end."""
        deadline = time.monotonic() + timeout_s
        while self._active is not None and time.monotonic() < deadline:
            await asyncio.sleep(0.002)

    async def _advance_after(self, react: Optional[tuple[str, str]] = None) -> None:
        """verdict/skip -> optional reaction speech -> 1.2 s beat -> next question | final."""
        await self._wait_speech_settled()
        if react is not None:
            text, uid = react
            await self._speak(text, uid, stt_ms=self._last_stt_ms, judge_ms=self._last_judge_ms)
        await self._emit_state(P_NEXT)
        await asyncio.sleep(self._timings["beat"])
        nxt = self.q_index + 1
        if nxt >= len(self.plan):
            await self._final_flow()
            return
        slot = self.plan[nxt]
        if slot["persona_round"] != self.persona_round:
            await self._emit_state(P_ROUND_SWITCH)
            await self._apply_persona(slot["persona_round"])
            await self._emit_state(P_ROUND_INTRO)
            await self._speak(self._round_intro_text(slot["persona_round"]))
        self.q_index = nxt - 1
        await self._begin_question()

    async def _final_flow(self) -> None:
        """Final scoreboard + finale line; then waits for the client's next_round ack."""
        await self._emit_state(P_FINAL)
        keys = ("game_over",)
        if self.mode == "demo":
            if self._last_verdict == "correct":
                keys = ("final_win", "game_over")
            elif self._last_verdict in ("wrong", "no_answer"):
                keys = ("final_lose", "game_over")
        line = self._fill(self._bank_line(*keys), score=self.score)
        scoreboard = self._fill(self._bank_line("scoreboard"), score=self.score)
        await self._speak((line + " " + scoreboard).strip())

    # ------------------------------------------------------- event handlers

    async def handle(self, msg: dict) -> None:
        """Process one client->server frame (CONTRACT §1) and drive the §2 machine."""
        if not isinstance(msg, dict):
            await self._error("bad_frame", "frame must be a JSON object")
            return
        mtype = msg.get("type")
        if mtype == "start":
            await self._on_start(msg)
        elif mtype == "buzz":
            await self._on_buzz(msg)
        elif mtype == "answer":
            await self._on_answer(msg)
        elif mtype == "barge":
            await self._cancel_active()
        elif mtype == "repeat_question":
            await self._on_repeat_question()
        elif mtype == "skip_question":
            await self._on_skip_question()
        elif mtype == "next_round":
            await self._on_next_round()
        elif mtype == "ping":
            pass                                            # app-level liveness; no reply frame
        else:
            await self._error("unknown_frame", f"unknown frame type {mtype!r}")

    async def _on_start(self, msg: dict) -> None:
        name = msg.get("player_name")
        if not isinstance(name, str) or not 2 <= len(name.strip()) <= 20:
            await self._error("bad_player_name", "player_name must be 2-20 characters")
            return
        language = msg.get("language", "hi")
        if language not in LANGUAGES:
            await self._error("bad_language", f"language must be one of {sorted(LANGUAGES)}")
            return
        mode = msg.get("mode", "demo")
        if mode not in ("demo", "daily5"):
            await self._error("bad_mode", "mode must be 'demo' or 'daily5'")
            return
        self._teardown()                                    # fresh game even mid-match
        self._reset_state()
        self._started = True
        self.player_name = name.strip()
        self.language = language
        self.mode = mode
        self.plan = [dict(s) for s in (DEMO_PLAN if mode == "demo" else DAILY5_PLAN)]
        seed = f"{DAILY5_SEED_PREFIX}:{msg.get('date') or ist_today()}" if mode == "daily5" else DEMO_SEED
        self.selected = select_questions(self.questions, self.plan, language, random.Random(seed))
        self._spawn(self._intro_flow())

    async def _on_buzz(self, msg: dict) -> None:
        if not self._started:
            await self._error("invalid_phase", "buzz before start")
            return
        if self.phase != P_QUESTION:
            await self._error("invalid_phase", f"buzz not allowed in phase {self.phase!r}")
            return
        self._clear_timer()
        self.t_buzz = time.monotonic()
        await self._cancel_active()                         # barge the host speech
        await self._emit_state(P_LISTENING)
        self._arm_timer(self._timings["answer_window"], self._on_answer_timeout)

    async def _on_answer(self, msg: dict) -> None:
        if not self._started:
            await self._error("invalid_phase", "answer before start")
            return
        if self.phase != P_LISTENING:
            await self._error("invalid_phase", f"answer not allowed in phase {self.phase!r}")
            return
        audio_b64, text = msg.get("audio_b64"), msg.get("text")
        if (audio_b64 is None) == (text is None):           # exactly one of the two
            await self._error("bad_answer", "answer must carry audio_b64 OR text, never both")
            return
        if text is not None and not isinstance(text, str):
            await self._error("bad_answer", "text must be a string")
            return
        self._clear_timer()
        q = self.current_question
        heard = ""
        stt_ms = 0.0
        if audio_b64 is not None:
            try:
                wav_bytes = base64.b64decode(audio_b64)
            except Exception:
                await self._error("bad_answer", "audio_b64 is not valid base64")
                self._arm_timer(self._timings["answer_window"], self._on_answer_timeout)
                return
            if self.stt_fn is not None:
                t0 = time.monotonic()
                try:
                    res = self.stt_fn(wav_bytes, list(q.get("keyterms") or []))
                    if inspect.isawaitable(res):
                        res = await res
                    heard = res.get("text", "") if isinstance(res, dict) else str(res or "")
                except Exception:
                    heard = ""                              # STT misfire -> unclear ladder
                stt_ms = (time.monotonic() - t0) * 1000.0
                self._last_stt_ms = stt_ms
        else:
            heard = text.strip()
        await self._emit_state(P_JUDGING)
        if not heard or not heard.strip():                  # empty/garbage STT -> unclear (§2)
            verdict_dict = {"verdict": "unclear", "close_call": False,
                            "template_id": "unclear", "hype_line": ""}
            await self._apply_verdict("unclear", heard, verdict_dict)
            return
        t0 = time.monotonic()
        try:
            res = self.judge_fn(q, heard, self.persona_label)
            if inspect.isawaitable(res):
                res = await res
            verdict_dict = dict(res) if isinstance(res, dict) else {}
        except Exception:
            verdict_dict = {"verdict": "unclear", "close_call": False,
                            "template_id": "unclear", "hype_line": ""}
        self._last_judge_ms = (time.monotonic() - t0) * 1000.0
        verdict = verdict_dict.get("verdict") or verdict_dict.get("correct") or "unclear"
        if verdict not in VERDICTS:                          # engine-side anomalies never punish
            verdict = "unclear"
        await self._apply_verdict(verdict, heard, verdict_dict)

    async def _on_repeat_question(self) -> None:
        if not self._started:
            await self._error("invalid_phase", "repeat_question before start")
            return
        if self.phase != P_QUESTION:
            await self._error("invalid_phase", f"repeat_question not allowed in phase {self.phase!r}")
            return
        q = self.current_question
        if q is None:
            return
        await self._cancel_active()
        await self._speak(q["question_text"])               # new utterance_id, same text

    async def _on_skip_question(self) -> None:
        if not self._started:
            await self._error("invalid_phase", "skip_question before start")
            return
        if self.phase not in (P_QUESTION, P_LISTENING):
            await self._error("invalid_phase", f"skip_question not allowed in phase {self.phase!r}")
            return
        self._clear_timer()
        await self._cancel_active()
        self.streak = 0                                     # streak reset, zero penalty
        self._last_verdict = ""
        self._spawn(self._advance_after())                   # silent advance — no verdict frame on skip

    async def _on_next_round(self) -> None:
        if not self._started:
            await self._error("invalid_phase", "next_round before start")
            return
        if self.phase != P_FINAL:
            await self._error("invalid_phase", f"next_round not allowed in phase {self.phase!r}")
            return
        if self._done:
            await self._error("already_over", "game already finished")
            return
        self._done = True
        await self._cancel_active()
        await self._speak(self._fill(self._bank_line("game_over"), score=self.score))

    # ----------------------------------------------------------- timeouts

    async def _on_buzz_timeout(self) -> None:
        """10 s buzz window elapsed with no buzz -> no_answer verdict, move on."""
        if self.phase != P_QUESTION:
            return
        self._clear_timer()
        await self._cancel_active()
        await self._no_answer("timeout")

    async def _on_answer_timeout(self) -> None:
        """5 s answer window elapsed after buzz -> no_answer verdict, move on."""
        if self.phase != P_LISTENING:
            return
        self._clear_timer()
        await self._no_answer("no_answer")

    # ----------------------------------------------------------- verdicts

    async def _no_answer(self, template: str) -> None:
        self.streak = 0
        self._last_verdict = "no_answer"
        uid = self._next_uid()
        await self.send({
            "type": "verdict", "utterance_id": uid, "correct": "no_answer",
            "close_call": False, "heard": "", "template_id": template, "hype_line": "",
            "fact_line": self.current_question["fun_fact_line"] if self.current_question else "",
            "score_delta": 0, "score": self.score, "streak": self.streak,
        })
        await self._emit_state(P_VERDICT)
        text = self._compose_reaction("no_answer", template=template)
        self._spawn(self._advance_after((text, uid)))

    async def _apply_verdict(self, verdict: str, heard: str, jd: dict) -> None:
        """Score the verdict, emit the verdict frame, then react + advance (background)."""
        q = self.current_question
        base = self.plan[self.q_index]["base"]
        score_delta = 0
        buzz_latency = (self.t_buzz - self.t_go) if (self.t_buzz is not None and self.t_go is not None) else None
        if verdict == "correct":
            self.streak += 1
            score_delta = score_correct(base, buzz_latency, self.streak)
        elif verdict == "partial":
            score_delta = score_partial(base)               # streak-neutral
        elif verdict == "wrong":
            self.streak = 0
        # unclear: streak-neutral, handled below
        self.score = max(0, self.score + max(0, score_delta))
        self._last_verdict = verdict
        close_call = bool(jd.get("close_call", False))
        template_id = str(jd.get("template_id") or DEFAULT_TEMPLATE.get(verdict, verdict))
        hype_line = jd.get("hype_line") or ""
        hype_line = hype_line if isinstance(hype_line, str) else ""
        uid = self._next_uid()
        await self.send({
            "type": "verdict", "utterance_id": uid, "correct": verdict,
            "close_call": close_call, "heard": heard, "template_id": template_id,
            "hype_line": hype_line, "fact_line": q["fun_fact_line"],
            "score_delta": score_delta, "score": self.score, "streak": self.streak,
        })
        await self._emit_state(P_VERDICT)
        if verdict == "unclear" and self.unclear_count == 0:
            # 1 grace repeat: no penalty, host asks again, answer window re-opens (§2)
            self.unclear_count = 1
            self._spawn(self._grace_repeat((self._compose_reaction("unclear", jd), uid)))
            return
        if verdict == "unclear":
            # second unclear -> move on with charm + reveal, still no penalty
            pass
        self._spawn(self._advance_after((self._compose_reaction(verdict, jd), uid)))

    async def _grace_repeat(self, react: tuple[str, str]) -> None:
        """Host asks the player to repeat; mic re-opens for the 5 s answer window."""
        text, uid = react
        await self._speak(text, uid)
        if self.phase != P_VERDICT:                         # a timeout raced us — move on
            return
        await self._emit_state(P_LISTENING)
        self._arm_timer(self._timings["answer_window"], self._on_answer_timeout)

    # ------------------------------------------------------ reaction speech

    def _compose_reaction(self, verdict: str, jd: Optional[dict] = None,
                          template: str = "") -> str:
        """Bank line + hype + answer reveal + fun fact + streak shout, as one utterance."""
        jd = jd or {}
        template = template or str(jd.get("template_id") or DEFAULT_TEMPLATE.get(verdict, verdict))
        parts: list[str] = []
        buzz_latency = (self.t_buzz - self.t_go) if (self.t_buzz is not None and self.t_go is not None) else None
        if verdict == "correct":
            tier = ("correct_fast" if speed_bonus(buzz_latency) >= 50
                    else "correct_slow" if speed_bonus(buzz_latency) == 0 else "correct_confident")
            line = self._bank_line(template, tier, "correct")
        else:
            line = self._bank_line(template, verdict)
        parts.append(line)
        hype = jd.get("hype_line") or ""
        if isinstance(hype, str) and hype.strip():
            parts.append(hype.strip())
        q = self.current_question
        if verdict in ("wrong", "partial", "unclear", "no_answer") and q is not None:
            parts.append(self._fill(self._bank_line("reveal"), answer=q["expected_answer"]))
            parts.append(q["fun_fact_line"])
        if verdict == "correct":
            if self.streak == 3:
                parts.append(self._bank_line("streak_3"))
            elif self.streak == 5:
                parts.append(self._bank_line("streak_5"))
        return " ".join(p for p in parts if p and p.strip())
