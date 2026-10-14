"""Language/script guard tests (fix-language, Agent B) — stdlib unittest, no network.

Covers the live "Text must contain at least one character from the allowed
languages" TTS failure:
  * script detection + the language_code remap table (main.script_safe_language_code)
  * the DATA MISMATCH TABLE: every speakable line's script matches the
    language_code its session passes to Sarvam — reactions.json, personas.json,
    questions.json, game.py per-language default lines — and every pace
    (bank hints + cast) sits in the calm band
  * LadderVoice.speak guard behaviour with contract-faithful fakes: a remap
    reconfigures the channel BEFORE synthesis, the cache key stays on the
    persona-configured code, mixed Hinglish never remaps, set_pace reconfigs
    lazily, a failed reconfig never blocks the utterance, and the HTTP rungs
    receive the safe code
  * main.get_session_flavour wiring (per-language reaction bank + cast)

Run from the worktree root:
    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

import json
import re
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend import main  # noqa: E402
import backend.game as game  # noqa: E402

DATA_DIR = Path(main.config.DATA_DIR)
DEVA = re.compile(r"[\u0900-\u097F]")
LATIN = re.compile(r"[A-Za-z]")

# session language -> the language_code its cast/personas pass to TTS
SESSION_TTS_CODE = {"hi": "hi-IN", "enhi": "hi-IN", "en": "en-IN", "mr": "mr-IN"}
# script each session's utterances must carry for that code to be accepted
SESSION_NEEDS_DEVA = {"hi": True, "enhi": True, "en": False, "mr": True}

SPEAKER = "ratan_hi_customer_expressive"


def fake_wav(pcm_len: int = 8000, sample_rate: int = 24000) -> bytes:
    """Minimal well-formed 16-bit mono PCM RIFF/WAVE blob."""
    header = b"".join([
        b"RIFF", struct.pack("<I", 36 + pcm_len), b"WAVE",
        b"fmt ", struct.pack("<IHHIIHH", 16, 1, 1, sample_rate,
                              sample_rate * 2, 2, 16),
        b"data", struct.pack("<I", pcm_len),
    ])
    return header + b"\x01\x00\x02\x00" * (pcm_len // 4)


# ----------------------------------------------------------- pure functions


class TestScriptDetection(unittest.TestCase):
    def test_table(self):
        cases = [
            ("नमस्ते दोस्तों, आज का सवाल", "devanagari"),
            ("What is the capital of India?", "latin"),
            ("वाह! What a shot that was!", "mixed"),
            ("Cricket में 'Master Blaster' किस player को कहा जाता है?", "mixed"),
            ("1234 ?!", "none"),
            ("", "none"),
            ("   ", "none"),
            ("...", "none"),
        ]
        for text, expected in cases:
            self.assertEqual(main.detect_script(text), expected, msg=repr(text))

    def test_non_string(self):
        self.assertEqual(main.detect_script(None), "none")  # type: ignore[arg-type]
        self.assertEqual(main.detect_script(42), "none")    # type: ignore[arg-type]


class TestScriptSafeLanguageCode(unittest.TestCase):
    def test_latin_only_text(self):
        cases = [("hi-IN", "en-IN"), ("mr-IN", "en-IN"), ("en-IN", "en-IN"),
                 ("", "en-IN"), ("hi-IN ", "en-IN")]
        for code, expected in cases:
            self.assertEqual(main.script_safe_language_code("On which date?", code),
                             expected, msg=code)

    def test_devanagari_only_text(self):
        cases = [("en-IN", "hi-IN"), ("hi-IN", "hi-IN"), ("mr-IN", "mr-IN"),
                 (None, "hi-IN"), ("", "hi-IN")]
        for code, expected in cases:
            self.assertEqual(main.script_safe_language_code("नमस्ते, आज का सवाल", code),
                             expected, msg=str(code))

    def test_mixed_hinglish_keeps_session_code(self):
        text = "वाह! बिजली की रफ़्तार — lightning speed!"
        for code in ("hi-IN", "en-IN", "mr-IN"):
            self.assertEqual(main.script_safe_language_code(text, code), code)

    def test_letterless_text_keeps_configured_code(self):
        for code in ("hi-IN", "en-IN", "mr-IN"):
            self.assertEqual(main.script_safe_language_code("123?!", code), code)


# ----------------------------------------------------------- data audit table


class TestDataMismatchTable(unittest.TestCase):
    """Every speakable line's script must match the language_code its session
    passes to Sarvam — and every pace must sit in the calm band."""

    def test_every_reaction_line_matches_its_language(self):
        bank = json.loads((DATA_DIR / "reactions.json").read_text(encoding="utf-8"))
        self.assertEqual(set(bank), set(SESSION_TTS_CODE))
        for lang, templates in bank.items():
            needs_deva = SESSION_NEEDS_DEVA[lang]
            for tid, variants in templates.items():
                self.assertTrue(variants, msg=f"{lang}/{tid} empty")
                for i, v in enumerate(variants):
                    text, hint, role = v["text"], v["pace_hint"], v["role"]
                    ok = bool(DEVA.search(text)) if needs_deva else bool(LATIN.search(text))
                    self.assertTrue(ok, msg=f"{lang}/{tid}[{i}] script mismatch: {text[:60]!r}")
                    self.assertTrue(0.85 <= hint <= 1.05,
                                    msg=f"{lang}/{tid}[{i}] pace {hint} outside calm band")
                    self.assertTrue(role, msg=f"{lang}/{tid}[{i}] missing role")

    def test_reaction_paces_match_calmed_targets(self):
        bank = json.loads((DATA_DIR / "reactions.json").read_text(encoding="utf-8"))
        expected = {
            "celebrate": (1.0, 1.05), "tease": (1.0, 1.0), "explain": (0.95, 0.95),
            "repeat": (0.95, 0.95), "timeout": (0.95, 0.95), "hype": (1.05, 1.05),
            "finale": (0.95, 1.0), "outro": (1.0, 1.0),
        }
        for lang, templates in bank.items():
            for tid, variants in templates.items():
                for i, v in enumerate(variants):
                    lo, hi = expected[v["role"]]
                    self.assertTrue(lo <= v["pace_hint"] <= hi,
                                    msg=f"{lang}/{tid}[{i}] role {v['role']!r} "
                                        f"pace {v['pace_hint']} not in [{lo}, {hi}]")

    def test_every_question_text_matches_its_declared_language(self):
        questions = json.loads((DATA_DIR / "questions.json").read_text(encoding="utf-8"))["questions"]
        for q in questions:
            needs_deva = q["language"] in ("hi-IN", "mr-IN")
            rx = DEVA if needs_deva else LATIN
            for field in ("question_text", "fun_fact_line"):
                self.assertTrue(rx.search(q[field]),
                                msg=f"{q['id']}.{field} carries no {q['language']} script char")

    def test_personas_cast_codes_and_paces(self):
        personas = json.loads((DATA_DIR / "personas.json").read_text(encoding="utf-8"))
        for lang, rounds in personas["rounds"].items():
            for num, entry in rounds.items():
                self.assertEqual(entry["language_code"], SESSION_TTS_CODE[lang],
                                 msg=f"rounds[{lang}][{num}]")
                self.assertTrue(0.85 <= entry["pace"] <= 1.05, msg=entry["pace"])
        for lang, entry in personas["final"].items():
            self.assertEqual(entry["language_code"], SESSION_TTS_CODE[lang],
                             msg=f"final[{lang}]")
            self.assertTrue(0.85 <= entry["pace"] <= 1.05, msg=entry["pace"])

    def test_game_default_lines_match_each_session_language(self):
        for lang, lines in game.SESSION_DEFAULT_LINES.items():
            self.assertEqual(set(lines), set(game.DEFAULT_LINES), msg=lang)
            needs_deva = SESSION_NEEDS_DEVA[lang]
            rx = DEVA if needs_deva else LATIN
            for key, text in lines.items():
                self.assertTrue(rx.search(text),
                                msg=f"{lang} default {key!r} has no native script: {text[:60]!r}")
        # placeholders survive translation
        for lang, lines in game.SESSION_DEFAULT_LINES.items():
            self.assertIn("{name}", lines["greeting"])
            self.assertIn("{score}", lines["game_over"])
            self.assertIn("{score}", lines["scoreboard"])
            self.assertIn("{answer}", lines["reveal"])
        self.assertIn("BhashaBuzzer", game.DEFAULT_LINES_EN["game_over"])
        self.assertIn("BhashaBuzzer", game.DEFAULT_LINES_MR["game_over"])

    def test_default_cast_and_question_pace_are_calm(self):
        self.assertEqual(game.QUESTION_READ_PACE, 1.0)
        for num, entry in game.DEFAULT_CAST.items():
            self.assertTrue(0.85 <= entry["pace"] <= 1.05, msg=f"cast[{num}] {entry['pace']}")
            self.assertIn(entry["language_code"], ("hi-IN", "en-IN", "mr-IN"))
        self.assertEqual(game.clamp_pace(1.15), 1.05)
        self.assertEqual(game.clamp_pace(0.5), 0.85)
        self.assertEqual(game.clamp_pace("bad"), 1.0)


# ----------------------------------------------------------- LadderVoice guard


class FakeChannel:
    """Contract-faithful stand-in for sarvam_tts.VoiceChannel."""

    def __init__(self) -> None:
        self.set_speaker_calls: list = []
        self.speak_calls: list = []
        self.fail_speak = False
        self.fail_config = False

    async def set_speaker(self, speaker, language_code, pace=1.0, *, dict_id=None):
        if self.fail_config:
            raise RuntimeError("config rejected")
        self.set_speaker_calls.append((speaker, language_code, pace, dict_id))

    async def speak(self, text, utterance_id, on_first_chunk=None):
        self.speak_calls.append((text, utterance_id))
        if self.fail_speak:
            raise RuntimeError("sarvam ws down")
        if on_first_chunk is not None:
            on_first_chunk(b"RIFF-header")
        yield b"RIFF-header"
        yield b"pcm-bytes"


class FakeCache:
    def __init__(self) -> None:
        self.store: dict = {}
        self.spend: list = []
        self.put_keys: list = []

    def tts_key(self, text, speaker, language_code, pace, sample_rate, codec,
                dict_id=None, pitch=None, loudness=None):
        return "|".join(["fake-tts", text, str(speaker), str(language_code),
                         str(pace), str(sample_rate), str(codec), str(dict_id)])

    def cache_get(self, key):
        return self.store.get(key)

    def cache_put(self, key, wav_bytes, meta):
        self.store[key] = bytes(wav_bytes)
        self.put_keys.append(key)

    def log_spend(self, chars, what):
        self.spend.append((int(chars), str(what)))

    def chars_used(self):
        return sum(chars for chars, _ in self.spend)

    def governor_level(self, chars=None):
        return "ok"


def make_fake_tts():
    tts = types.ModuleType("fake_sarvam_tts")
    tts.rest_calls = []  # type: ignore[attr-defined]
    tts.fail_stream = False  # type: ignore[attr-defined]

    async def speak_via_stream(text, speaker, language_code, pace=1.0,
                               sample_rate=24000, **kwargs):
        if tts.fail_stream:
            raise RuntimeError("stream endpoint 503")
        return fake_wav(pcm_len=6000)

    async def speak_via_rest(text, speaker, language_code, pace=1.0,
                             sample_rate=24000, **kwargs):
        tts.rest_calls.append((text, speaker, language_code, pace))
        return fake_wav(pcm_len=4000)

    def wav_blob_to_chunks(wav, chunk_pcm_bytes=32768):
        if len(wav) < 44 or wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
            raise ValueError("not a RIFF/WAVE blob")
        pcm = wav[44:]
        return wav[:44], [pcm[i:i + chunk_pcm_bytes]
                          for i in range(0, len(pcm), chunk_pcm_bytes)]

    tts.speak_via_stream = speak_via_stream
    tts.speak_via_rest = speak_via_rest
    tts.wav_blob_to_chunks = wav_blob_to_chunks
    return tts


class TestLadderVoiceGuard(unittest.IsolatedAsyncioTestCase):
    """The script guard inside the transport ladder (fix-language)."""

    def setUp(self):
        self.channel = FakeChannel()
        self.cache = FakeCache()
        self.tts = make_fake_tts()
        for name, value in (("get_cache_module", lambda: self.cache),
                            ("get_tts_module", lambda: self.tts)):
            patcher = mock.patch.object(main, name, new=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def voice(self) -> main.LadderVoice:
        return main.LadderVoice(self.channel, "sess-test")

    @staticmethod
    async def collect(agen):
        return [chunk async for chunk in agen]

    async def test_latin_text_on_hi_in_remaps_to_en_in_before_speak(self):
        voice = self.voice()
        await voice.set_speaker(SPEAKER, "hi-IN", 1.05)
        chunks = await self.collect(voice.speak("What is the capital of India?", "u1"))
        self.assertTrue(chunks)
        # the channel was reconfigured BEFORE the utterance was synthesized
        self.assertEqual(self.channel.set_speaker_calls,
                         [(SPEAKER, "hi-IN", 1.05, None), (SPEAKER, "en-IN", 1.05, None)])
        self.assertEqual(self.channel.speak_calls,
                         [("What is the capital of India?", "u1")])
        self.assertEqual(voice._effective_language_code, "en-IN")
        self.assertEqual(voice.language_code, "hi-IN")   # persona intent preserved
        # cache stays keyed on the persona-configured code (test-pinned semantics)
        self.assertEqual(len(self.cache.put_keys), 1)
        self.assertIn("|hi-IN|", self.cache.put_keys[0])

    async def test_devanagari_text_on_en_in_remaps_to_hi_in(self):
        voice = self.voice()
        await voice.set_speaker("simran_en_customer", "en-IN", 1.0)
        chunks = await self.collect(voice.speak("नमस्ते! आज का सवाल शुरू होता है।", "u1"))
        self.assertTrue(chunks)
        self.assertEqual(self.channel.set_speaker_calls[-1],
                         ("simran_en_customer", "hi-IN", 1.0, None))

    async def test_mixed_hinglish_never_remaps(self):
        voice = self.voice()
        await voice.set_speaker(SPEAKER, "hi-IN", 1.0)
        chunks = await self.collect(voice.speak("वाह! बिजली की रफ़्तार — lightning speed!", "u1"))
        self.assertTrue(chunks)
        self.assertEqual(len(self.channel.set_speaker_calls), 1)   # config only
        self.assertEqual(voice._effective_language_code, "hi-IN")

    async def test_cache_hit_needs_no_reconfig(self):
        voice = self.voice()
        await voice.set_speaker(SPEAKER, "hi-IN", 1.05)
        text = "What is the capital of India?"
        key = self.cache.tts_key(text, SPEAKER, "hi-IN", 1.05, 24000, "wav", None, None, None)
        self.cache.store[key] = fake_wav(pcm_len=9000)
        chunks = await self.collect(voice.speak(text, "u1"))
        self.assertTrue(chunks)
        self.assertEqual(self.channel.speak_calls, [])       # no synthesis
        self.assertEqual(len(self.channel.set_speaker_calls), 1)   # no reconfig
        self.assertEqual(self.cache.spend, [])
        self.assertEqual(self.cache.put_keys, [])

    async def test_set_pace_reconfigs_lazily(self):
        voice = self.voice()
        await voice.set_speaker(SPEAKER, "hi-IN", 1.05)
        voice.set_pace(0.95)
        self.assertEqual(len(self.channel.set_speaker_calls), 1)   # nothing sent yet
        chunks = await self.collect(voice.speak("नमस्ते! आज का सवाल।", "u1"))
        self.assertTrue(chunks)
        self.assertEqual(self.channel.set_speaker_calls[-1],
                         (SPEAKER, "hi-IN", 0.95, None))           # one reconfig
        # an unchanged pace is a no-op
        voice.set_pace(0.95)
        self.assertEqual(voice.pace, 0.95)
        self.assertFalse(voice._reconfig_pending)

    async def test_failed_reconfig_never_blocks_the_utterance(self):
        voice = self.voice()
        await voice.set_speaker(SPEAKER, "hi-IN", 1.0)
        self.channel.fail_config = True
        chunks = await self.collect(voice.speak("What is the capital of India?", "u1"))
        self.assertTrue(chunks)                                   # still spoken
        self.assertEqual(len(self.channel.speak_calls), 1)
        self.assertEqual(voice._effective_language_code, "en-IN")  # rungs still safe

    async def test_http_rungs_receive_the_safe_code(self):
        voice = self.voice()
        await voice.set_speaker(SPEAKER, "hi-IN", 1.05)
        self.channel.fail_speak = True
        self.tts.fail_stream = True
        text = "What is the capital of India?"
        chunks = await self.collect(voice.speak(text, "u1"))
        self.assertTrue(chunks)                                   # served via REST
        self.assertEqual(voice.last_metrics["source"], "rest")
        self.assertEqual(self.tts.rest_calls,
                         [(text, SPEAKER, "en-IN", 1.05)])
        self.assertEqual(voice.language_code, "hi-IN")            # intent preserved
        self.assertIn("|hi-IN|", self.cache.put_keys[0])          # cache key stable


# ----------------------------------------------------------- flavour wiring


class TestSessionFlavourWiring(unittest.TestCase):
    """main.get_session_flavour: per-language reaction bank + cast wiring."""

    def test_merges_language_bank_and_cast(self):
        bank = main.get_session_flavour({"demo": [1]}, "mr")
        self.assertEqual(bank["demo"], [1])                       # caller keys kept
        self.assertEqual(bank["cast"][2]["speaker"], "ritu_mr_narration")
        self.assertEqual(bank["cast"][4]["language_code"], "mr-IN")
        rx = json.loads((DATA_DIR / "reactions.json").read_text(encoding="utf-8"))
        self.assertEqual(bank["correct_fast"][0]["text"], rx["mr"]["correct_fast"][0]["text"])
        self.assertLessEqual(bank["correct_fast"][0]["pace_hint"], 1.05)
        self.assertNotIn("cast", bank["correct_fast"])           # not a template

    def test_unknown_language_falls_back_to_hinglish(self):
        bank = main.get_session_flavour({}, "fr")
        self.assertEqual(bank["cast"][1]["language_code"], "hi-IN")
        self.assertTrue(bank["correct_fast"][0]["text"])

    def test_flavour_never_blocks_a_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(main.config, "DATA_DIR", Path(tmp)):
                broken = types.SimpleNamespace()   # no persona_for_round attr
                with mock.patch.object(main, "get_personas_module", new=lambda: broken):
                    bank = main.get_session_flavour({"keep": 1}, "hi")
        self.assertEqual(bank, {"keep": 1})       # wiring failures degrade silently


if __name__ == "__main__":
    unittest.main()
