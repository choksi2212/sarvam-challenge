"""BhashaBuzzer persona cast + reaction bank accessors (A5 — CONTRACT §3).

Binding exports (CONTRACT §3, Python 3.14, stdlib only):
    TEMPLATE_IDS   binding enum of reaction template ids — the judge returns
                   ids from THIS list only.
    ROUNDS         {language: {round_num: {"speaker", "label", "style_tag",
                   "language_code", "pace"}}} for languages hi/enhi/en/mr,
                   rounds 1-3.
    FINAL          {language: {...}} sudden-death host per language.
    persona_for_round(round_num, language) -> cast-entry dict (a copy);
                   round_num >= FINAL_ROUND (4) resolves to the FINAL host.
    persona_label(speaker) -> headline label for a speaker id; for speakers
                   reused in a finale (e.g. mukul_mr_stories) the finale label
                   wins; unknown ids fall back to the raw speaker string.

Convenience accessors for backend/data/reactions.json (the bank: per language
x TEMPLATE_IDS, each variant {"text", "pace_hint", "role"}):
    load_reactions() -> full bank dict
    reaction_line(language, template_id, variant=0) -> one variant dict
                   (variant index cycles, so callers can pass a plain counter)

Reaction roles (flavour groups over the 16 template ids):
    celebrate  correct_fast, correct_confident, correct_slow
    tease      wrong_confident, wrong_funny, wrong_close, no_answer
    explain    partial
    repeat     unclear, repeat_request
    timeout    timeout
    hype       streak_3, streak_5
    finale     final_win, final_lose
    outro      game_over

The hi/enhi bank is the authoritative 28-line native-script Hinglish bank from
game-design.md §4.5 (D5), extended with lines for the CONTRACT's extra
template ids (correct_confident, wrong_close, unclear); en and mr are
same-trigger adaptations. Code-mixing rules per CONTRACT / game-design §7:
Devanagari frame with danda (।) punctuation, English loanwords in Latin
script, never Romanised Indic.

Every speaker id is verified against research/speaker-ids-v4flash.txt (the
live 224-ID roster) — backend/tests/test_personas.py runs that check whenever
the roster file is reachable and pins the exact cast unconditionally.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

__all__ = [
    "TEMPLATE_IDS",
    "ROUNDS",
    "FINAL",
    "FINAL_ROUND",
    "persona_for_round",
    "persona_label",
    "load_reactions",
    "reaction_line",
    "PERSONAS_PATH",
    "REACTIONS_PATH",
    "DATA_DIR",
]

DATA_DIR = Path(__file__).resolve().parent / "data"
PERSONAS_PATH = DATA_DIR / "personas.json"
REACTIONS_PATH = DATA_DIR / "reactions.json"

#: Binding reaction-template enum (CONTRACT §3) — judge emits ids from this list only.
TEMPLATE_IDS = [
    "correct_fast", "correct_confident", "correct_slow", "wrong_confident",
    "wrong_funny", "wrong_close", "partial", "unclear", "timeout", "no_answer",
    "repeat_request", "streak_3", "streak_5", "final_win", "final_lose",
    "game_over",
]

#: persona_for_round(round_num >= FINAL_ROUND, language) resolves to the FINAL host.
FINAL_ROUND = 4

ROUNDS: dict[str, dict[int, dict]] = {}
FINAL: dict[str, dict] = {}
_SPEAKER_LABELS: dict[str, str] = {}

_SPEAKER_RE = re.compile(r"[a-z][a-z0-9_]*")


def _load_cast(path: Path = PERSONAS_PATH) -> None:
    """Populate ROUNDS/FINAL/_SPEAKER_LABELS from data/personas.json."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    for lang, rounds in data.get("rounds", {}).items():
        ROUNDS[lang] = {int(num): dict(entry) for num, entry in rounds.items()}
    for lang, entry in data.get("final", {}).items():
        FINAL[lang] = dict(entry)
    for lang in data.get("rounds", {}):
        for num in sorted(ROUNDS[lang]):
            entry = ROUNDS[lang][num]
            _SPEAKER_LABELS[entry["speaker"]] = entry["label"]
    for lang, entry in FINAL.items():
        _SPEAKER_LABELS[entry["speaker"]] = entry["label"]


def _validate_cast() -> None:
    """Fail fast on malformed cast entries (bad data must never reach TTS)."""
    entries: list[tuple[str, dict]] = []
    for lang, rounds in ROUNDS.items():
        for num, entry in rounds.items():
            entries.append((f"rounds[{lang}][{num}]", entry))
    for lang, entry in FINAL.items():
        entries.append((f"final[{lang}]", entry))
    for where, entry in entries:
        for key in ("speaker", "label", "style_tag"):
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{where}: cast entry needs non-empty {key!r}, got {value!r}")
        speaker = entry["speaker"]
        if speaker != speaker.lower() or not _SPEAKER_RE.fullmatch(speaker):
            raise ValueError(f"{where}: invalid speaker id {speaker!r} (must be a lowercase <voice>_<lang>_<style> id)")


_load_cast()
_validate_cast()


def persona_for_round(round_num: int, language: str) -> dict:
    """Cast entry for a demo-match segment; returns a copy (safe to mutate).

    round_num 1-3 -> ROUNDS[language][round_num]; round_num >= FINAL_ROUND (4)
    -> FINAL[language] (sudden-death host). Raises KeyError for unknown
    languages or round numbers outside 1-3 (and below FINAL_ROUND).
    """
    lang = str(language).strip().lower()
    if lang not in ROUNDS:
        raise KeyError(f"unknown language {language!r}; expected one of {sorted(ROUNDS)}")
    num = int(round_num)
    if num >= FINAL_ROUND:
        return dict(FINAL[lang])
    if num not in ROUNDS[lang]:
        raise KeyError(f"unknown round {num} for language {lang!r}")
    return dict(ROUNDS[lang][num])


def persona_label(speaker: str) -> str:
    """Headline label for a speaker id (finale label wins for reused speakers).

    Unknown ids fall back to the raw speaker string (the on-screen id is judge
    bait anyway) rather than crashing a live game.
    """
    return _SPEAKER_LABELS.get(str(speaker), str(speaker))


def load_reactions(path: str | Path = REACTIONS_PATH) -> dict[str, dict[str, list[dict]]]:
    """Load the reaction bank: {language: {template_id: [{"text", "pace_hint", "role"}, ...]}}."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def reaction_line(language: str, template_id: str, variant: int = 0) -> dict:
    """One bank line for (language, template_id); `variant` cycles across variants.

    Raises KeyError for unknown language or template_id (fail fast — the judge
    is bound to TEMPLATE_IDS and the session language to the four cast tongues).
    """
    bank = load_reactions()
    lang = str(language).strip().lower()
    if lang not in bank:
        raise KeyError(f"unknown language {language!r}; expected one of {sorted(bank)}")
    lines = bank[lang].get(template_id)
    if not lines:
        raise KeyError(f"unknown or empty template_id {template_id!r} for language {lang!r}")
    return dict(lines[int(variant) % len(lines)])
