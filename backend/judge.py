"""BhashaBuzzer answer judge (A4) — LLM primary, stdlib deterministic fallback.

Primary path: POST https://api.sarvam.ai/v1/chat/completions with model
``sarvam-105b-conversations``, ``reasoning_effort="low"``, temperature 0.2,
max_tokens=300 and NO response_format (JSON-in-prompt) under a HARD 2.5 s
client timeout  [PROBED: ~450 ms, clean JSON]  — CONTRACT.md §4 amendment.
On timeout / HTTP error / parse failure / missing or invalid fields /
missing API key, fall back to :func:`deterministic_verdict` — the game NEVER
blocks on the LLM. Every public call returns a schema-valid verdict dict:

    {"verdict": "correct|wrong|partial|unclear",
     "close_call": bool,
     "template_id": <one of TEMPLATE_IDS>,
     "hype_line": str}          # "" for deterministic results

Standalone module: stdlib + httpx only, no sibling imports at module load.
Tests use httpx.MockTransport exclusively — NO live Sarvam calls, and this
module never reads .env directly (the env var / A7 config module own that).
"""
from __future__ import annotations

import difflib
import json
import os
import re
from typing import Any

import httpx

# ------------------------------------------------------------------ constants

JUDGE_URL = "https://api.sarvam.ai/v1/chat/completions"
JUDGE_MODEL = "sarvam-105b-conversations"
JUDGE_TEMPERATURE = 0.2
JUDGE_MAX_TOKENS = 300
JUDGE_REASONING_EFFORT = "low"
JUDGE_TIMEOUT_S = 2.5  # HARD client cap; the fallback keeps e2e judge ≤ ~2.5 s

_TIMEOUT = httpx.Timeout(JUDGE_TIMEOUT_S)

# Test seam: when set, httpx.AsyncClient uses this transport instead of the
# network. Only tests (httpx.MockTransport) and the orchestrator's stubs set it.
_TRANSPORT: httpx.AsyncBaseTransport | None = None

VERDICTS = ("correct", "wrong", "partial", "unclear")

# Binding enum — CONTRACT §3 personas block (A5 owns personas.py; the judge
# validates against the same frozen list so this module stays standalone).
TEMPLATE_IDS = (
    "correct_fast", "correct_confident", "correct_slow", "wrong_confident",
    "wrong_funny", "wrong_close", "partial", "unclear", "timeout",
    "no_answer", "repeat_request", "streak_3", "streak_5", "final_win",
    "final_lose", "game_over",
)

HYPE_LINE_MAX = 60  # characters, per the rubric

# -------------------------------------------------------------- normalization

_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
_INVISIBLES = re.compile("[\u200b\u200c\u200d\ufeff\u00a0]")  # ZWSP/ZWNJ/ZWJ/BOM/NBSP
_DIGIT_FOLD = re.compile(r"(?<=\d),(?=\d)")  # Indian ↔ western comma grouping → bare digits

# Edge punctuation stripped from each token. Keeps ₹ and any in-token
# characters ("26/1/1950", "co-oper" survive as-is).
_TOKEN_EDGE = " \t.,!?;:।॥…'\"“”‘’()[]{}<>*+·•^~`|\\@$%&#=-–—_+"

# Fillers/honorifics removed as whole tokens (question-bank §6.1.2). Verified
# against the 72-question bank: no expected answer or alias is corrupted —
# जी/ji also appear inside aliases ("गांधी जी") but normalization is symmetric
# on both sides, so exact matching is unaffected.
_FILLERS = frozenset({
    # honorifics
    "ji", "jii", "जी", "sir",
    # self-reference / hedging / hesitation / laughter
    "maine", "मैंने", "mujhe", "मुझे", "lagta", "लगता",
    "shayad", "शायद", "maybe", "perhaps", "i", "think",
    "um", "umm", "uh", "hmm", "mmm", "haan", "हाँ", "हां",
    "na", "ना", "arrey", "arey", "yaar", "bhai", "bro",
    "haha", "hahaha", "hehe", "lol", "matlab", "yaani",
    "obviously", "actually", "is", "was", "ans", "answer",
    # trailing Hindi verb ticks (question-bank §6.1.2)
    "tha", "thi", "hai", "hain", "ho", "hoon",
    "था", "थी", "है", "हैं", "हो", "हूँ",
})

# Common Hindi/English word-numbers 1–10 → digits (ek/aik→1 … das→10).
# Replacement is token-exact and symmetric on alias and heard sides, so the
# word-form aliases in the bank ("छह हज़ार पाँच सौ", "do runs") stay exact-match.
_WORD_NUMBERS = {
    "ek": "1", "aik": "1", "one": "1", "एक": "1",
    "do": "2", "dui": "2", "two": "2", "दो": "2",
    "teen": "3", "tin": "3", "three": "3", "तीन": "3",
    "char": "4", "chaar": "4", "four": "4", "चार": "4",
    "panch": "5", "paanch": "5", "five": "5", "पाँच": "5", "पांच": "5",
    "chah": "6", "chhah": "6", "chhe": "6", "che": "6", "six": "6",
    "छह": "6", "छः": "6", "छे": "6",
    "saat": "7", "sat": "7", "seven": "7", "सात": "7",
    "aath": "8", "ath": "8", "eight": "8", "आठ": "8",
    "nau": "9", "nav": "9", "nine": "9", "नौ": "9",
    "das": "10", "dus": "10", "ten": "10", "दस": "10",
}


def normalize_answer(text: str) -> str:
    """Normalize a spoken/typed answer for matching.

    Pipeline: Devanagari digits→ASCII → lowercase latin → strip invisible
    chars → fold Indian/western comma grouping inside digit runs
    (5,00,000 = 500,000 = 500000) → per-token edge-punctuation strip +
    filler/honorific removal + word-number→digit conversion → collapse
    whitespace. Devanagari is preserved as-is: script equivalence rides the
    alias list + LLM, never fuzzy codepoint matching (question-bank §6.1.4).
    """
    if not text:
        return ""
    s = str(text)
    s = s.translate(_DEVANAGARI_DIGITS)
    s = s.lower()
    s = _INVISIBLES.sub("", s)
    s = _DIGIT_FOLD.sub("", s)
    out: list[str] = []
    for tok in s.split():
        core = tok.strip(_TOKEN_EDGE)
        if not core:  # pure punctuation / hesitation dots
            continue
        if core in _FILLERS:
            continue
        out.append(_WORD_NUMBERS.get(core, core))
    return " ".join(out)


# ------------------------------------------------- deterministic verdict (A4)

def _normalized_candidates(question: dict) -> list[str]:
    """expected_answer + accepted_answer_aliases, normalized and deduped."""
    q = question or {}
    raw: list[Any] = [q.get("expected_answer")]
    aliases = q.get("accepted_answer_aliases")
    if isinstance(aliases, (list, tuple)):
        raw.extend(aliases)
    out: list[str] = []
    for cand in raw:
        if cand is None:
            continue
        norm = normalize_answer(str(cand))
        if norm and norm not in out:
            out.append(norm)
    return out


def deterministic_verdict(question: dict, heard: str) -> dict:
    """Stdlib-only instant verdict — the fallback that never fails.

    Normalize both sides; exact match vs expected_answer + any alias →
    correct; difflib.SequenceMatcher ratio ≥ 0.80 vs any candidate →
    partial with close_call true; ≥ 0.60 → wrong with close_call true;
    blank (or pure filler/punctuation) → unclear; otherwise plain wrong.
    hype_line is always "" — deterministic reactions come from the
    pre-rendered template bank, never from generated text.
    """
    norm_heard = normalize_answer(heard)
    candidates = _normalized_candidates(question)
    if not norm_heard:
        return {"verdict": "unclear", "close_call": False,
                "template_id": "unclear", "hype_line": ""}
    if norm_heard in candidates:
        return {"verdict": "correct", "close_call": False,
                "template_id": "correct_confident", "hype_line": ""}
    if not candidates:
        return {"verdict": "wrong", "close_call": False,
                "template_id": "wrong_confident", "hype_line": ""}
    best = max(difflib.SequenceMatcher(None, norm_heard, c).ratio()
               for c in candidates)
    if best >= 0.80:
        return {"verdict": "partial", "close_call": True,
                "template_id": "partial", "hype_line": ""}
    if best >= 0.60:
        return {"verdict": "wrong", "close_call": True,
                "template_id": "wrong_close", "hype_line": ""}
    return {"verdict": "wrong", "close_call": False,
            "template_id": "wrong_confident", "hype_line": ""}


# ------------------------------------------------------------ judge prompt

_SYSTEM_PROMPT = """You are the judge of BhashaBuzzer, a fast Hinglish voice quiz show. Compare the player's spoken answer ("heard", raw STT text) against the question's expected answer and its accepted aliases.

verdict — exactly one of:
- "correct": the heard answer names the SAME entity as expected_answer or an alias. Transliteration and script variants count: Virat = विराट कोहली = Virat Kohli. Shortened forms and surnames of the same entity count (धोनी, Dhoni, MS Dhoni). Ignore fillers and honorifics (ji, जी, sir, maine, I think, shayad, hesitation sounds). A number that is right without its unit still counts.
- "partial": right track but incomplete — e.g. only the year of a date, or half of a two-part name. Worth half points.
- "wrong": a different entity — NEVER reward a plausible but different answer, however famous (AR Rahman for the first Indian Oscar winner is wrong; the answer is Bhanu Athaiya).
- "unclear": blank, garbled, or not an attempt at this question.

close_call: true if the answer was a near-miss or on the right track (so the host can tease the player), false otherwise.

template_id: choose EXACTLY one from this list:
correct_fast, correct_confident, correct_slow, wrong_confident, wrong_funny, wrong_close, partial, unclear, timeout, no_answer, repeat_request, streak_3, streak_5, final_win, final_lose, game_over
Prefer correct_confident for correct answers, wrong_close when close_call is true, wrong_funny for a hilariously wrong answer, wrong_confident otherwise.

hype_line: ONE line of max 60 characters that the host says right after the verdict, in natural spoken Hinglish using NATIVE scripts — Hindi words in Devanagari, English words in Latin (never Romanized Hindi), matching the given host persona's style. No quotes, no emoji, no stage directions.

Reply with ONE JSON object only and nothing else, with exactly these keys: verdict, close_call, template_id, hype_line."""


def _fewshot_user(question_id: str, question: str, expected: str,
                   aliases: list[str], heard: str, persona: str) -> str:
    return json.dumps(
        {"question_id": question_id, "question": question,
         "expected_answer": expected, "aliases": aliases,
         "heard": heard, "persona_label": persona},
        ensure_ascii=False, separators=(",", ":"))


# Four few-shot examples adapted from research/plans/question-bank.md §6.3
# (cricket-01 ×2, gk-05, bolly-12) — user/assistant turns with real JSON.
_FEWSHOTS: tuple[tuple[str, str], ...] = (
    (
        _fewshot_user(
            "cricket-01",
            "2011 के Cricket World Cup final में India की captain कौन थी?",
            "MS Dhoni",
            ["Dhoni", "MS Dhoni", "महेंद्र सिंह धोनी", "धोनी",
             "Mahendra Singh Dhoni", "Mahi", "Captain Cool", "माही"],
            "धोनी",
            "Meera (warm quizmaster)",
        ),
        '{"verdict":"correct","close_call":false,"template_id":"correct_confident",'
        '"hype_line":"शाबाश! एकदम सही जवाब, full marks!"}',
    ),
    (
        _fewshot_user(
            "cricket-01",
            "2011 के Cricket World Cup final में India की captain कौन थी?",
            "MS Dhoni",
            ["Dhoni", "MS Dhoni", "महेंद्र सिंह धोनी", "धोनी",
             "Mahendra Singh Dhoni", "Mahi", "Captain Cool", "माही"],
            "Dhoni sir tha I think",
            "Meera (warm quizmaster)",
        ),
        '{"verdict":"correct","close_call":false,"template_id":"correct_confident",'
        '"hype_line":"भरोसा रखो जी! Dhoni ही सही — शाबाश!"}',
    ),
    (
        _fewshot_user(
            "gk-05",
            "On which date did the Constitution of India come into force?",
            "26 January 1950",
            ["26 January 1950", "26 January", "26 जनवरी 1950", "26 जनवरी",
             "26th January 1950", "Republic Day", "republic day", "26/1/1950",
             "twenty six January nineteen fifty"],
            "1950",
            "Professor Factly (kind schoolteacher)",
        ),
        '{"verdict":"partial","close_call":true,"template_id":"partial",'
        '"hype_line":"साल तो पकड़ा, पर date छूट गई — आधे marks!"}',
    ),
    (
        _fewshot_user(
            "bolly-12",
            "Oscar award जीतने वाले first Indian कौन थे?",
            "Bhanu Athaiya",
            ["Bhanu Athaiya", "भानु अथैया", "भानू अथैया", "Bhanu Athiya",
             "bhanu athaiya"],
            "AR Rahman",
            "Filmi host (drama king)",
        ),
        '{"verdict":"wrong","close_call":true,"template_id":"wrong_close",'
        '"hype_line":"अरे! Rahman साहब बाद में आए — पहले Athaiya जी!"}',
    ),
)


def _question_payload(question: dict) -> dict:
    """Compact question facts for the judge user message."""
    q = question or {}
    aliases = q.get("accepted_answer_aliases")
    if not isinstance(aliases, (list, tuple)):
        aliases = []
    return {
        "question_id": q.get("id", ""),
        "question": q.get("question_text") or q.get("text") or "",
        "expected_answer": q.get("expected_answer", "") or "",
        "aliases": [str(a) for a in aliases][:12],
    }


def _build_messages(question: dict, heard: str, persona_label: str) -> list[dict]:
    """system rubric + 4 few-shot pairs + compact user message."""
    payload = _question_payload(question)
    payload["heard"] = heard if isinstance(heard, str) else ("" if heard is None else str(heard))
    payload["persona_label"] = persona_label or ""
    messages: list[dict] = [{"role": "system", "content": _SYSTEM_PROMPT}]
    for user_msg, assistant_msg in _FEWSHOTS:
        messages.append({"role": "user", "content": user_msg})
        messages.append({"role": "assistant", "content": assistant_msg})
    messages.append({
        "role": "user",
        "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    })
    return messages


# ------------------------------------------------------------------- LLM call

def _parse_llm_verdict(content: Any) -> dict | None:
    """Tolerant parse of the model reply → schema-valid verdict dict or None.

    Slices from the first ``{`` to the last ``}`` (survives prose/markdown
    fences), json.loads, then strictly validates all four contract fields —
    any failure returns None so the caller falls back deterministically.
    """
    if not isinstance(content, str):  # probed failure mode: content is null
        return None
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(content[start:end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    verdict = obj.get("verdict")
    close_call = obj.get("close_call")
    template_id = obj.get("template_id")
    hype_line = obj.get("hype_line")
    if verdict not in VERDICTS:
        return None
    if not isinstance(close_call, bool):
        return None
    if template_id not in TEMPLATE_IDS:
        return None
    if not isinstance(hype_line, str):
        return None
    return {"verdict": verdict, "close_call": close_call,
            "template_id": template_id, "hype_line": hype_line[:HYPE_LINE_MAX]}


def _get_api_key() -> str:
    """SARVAM_API_KEY from the env var, else from the sibling config module
    (A7) when importable. Never reads .env directly, never logs the key."""
    key = os.environ.get("SARVAM_API_KEY", "")
    if key:
        return key
    cfg = None
    try:  # script style: backend/ itself on sys.path
        import config as cfg  # type: ignore[no-redef]
    except Exception:
        try:  # package style: imported as backend.judge
            from . import config as cfg  # type: ignore[no-redef]
        except Exception:
            cfg = None
    try:
        key = getattr(cfg, "SARVAM_API_KEY", "") if cfg is not None else ""
    except Exception:
        key = ""
    return key if isinstance(key, str) else ""


async def _call_llm(question: dict, heard: str, persona_label: str,
                    api_key: str) -> dict | None:
    """One chat-completions call under the hard 2.5 s timeout. Raises on any
    transport/HTTP/shape error — judge_answer converts that to a fallback."""
    body = {
        "model": JUDGE_MODEL,
        "temperature": JUDGE_TEMPERATURE,
        "max_tokens": JUDGE_MAX_TOKENS,
        "reasoning_effort": JUDGE_REASONING_EFFORT,
        "messages": _build_messages(question, heard, persona_label),
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT, transport=_TRANSPORT) as client:
        resp = await client.post(
            JUDGE_URL,
            json=body,
            headers={"api-subscription-key": api_key,
                     "Content-Type": "application/json"},
        )
    resp.raise_for_status()
    payload = resp.json()
    content = payload["choices"][0]["message"]["content"]
    return _parse_llm_verdict(content)


# ------------------------------------------------------------------ public API

async def judge_answer(question: dict, heard: str, persona_label: str) -> dict:
    """Judge a player's spoken answer. Binding signature, CONTRACT §3.

    LLM primary (sarvam-105b-conversations, reasoning_effort low, 2.5 s hard
    timeout) → on timeout/error/parse-fail/missing-field (or no API key):
    ``deterministic_verdict``. Blank/filler-only transcripts short-circuit to
    the deterministic unclear path with zero latency (CONTRACT §2 repeat
    flow). Never raises; always returns a schema-valid dict whose
    template_id is from the binding 16-id enum.
    """
    if not normalize_answer(heard):
        return deterministic_verdict(question, heard)
    api_key = _get_api_key()
    if api_key:
        try:
            verdict = await _call_llm(question, heard, persona_label, api_key)
        except Exception:  # timeout, HTTP error, malformed body, …
            verdict = None
        if verdict is not None:
            return verdict
    return deterministic_verdict(question, heard)
