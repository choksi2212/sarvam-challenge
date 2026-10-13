"""Tests for backend/main.py (A7 relay) — stdlib unittest + starlette TestClient.

NO live Sarvam API calls and NO .env reads anywhere: every sibling module
(cache / sarvam_tts / sarvam_stt / judge / personas / game) is absent from
this worktree (CONTRACT §8), so every factory in main.py is overridden with
contract-faithful fakes — FakeVoiceChannel, FakeTTS module, FakeCache,
FakeJudge, FakeSTT, FakeGameSession (mirrors A6's CONTRACT §3 duck-type:
GameSession(send, voice, judge_fn, bank), voice.speak(text, utterance_id,
on_first_chunk) yielding 44-byte-RIFF-header-first chunks).

Run from the worktree root:
    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

import base64
import json
import struct
import sys
import tempfile
import time
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from backend import main
except ImportError:  # pragma: no cover — flat import fallback
    import main  # type: ignore[no-redef]

from fastapi.testclient import TestClient  # noqa: E402

SPEAKER = "ratan_hi_customer_expressive"
LANG = "hi-IN"
PACE = 1.05
QUESTION_TEXT = "What is the capital of India?"
HYPE_LINE = "Shabash Asha! That is absolutely right!"
QUESTION = {
    "id": "q1", "text": QUESTION_TEXT, "category": "GK", "difficulty": "easy",
    "language": "hi", "expected_answer": "new delhi",
    "keyterms": ["delhi", "new delhi"],
}


def fake_wav(pcm_len: int = 8000, sample_rate: int = 24000) -> bytes:
    """A minimal but well-formed 16-bit mono PCM RIFF/WAVE blob."""
    header = b"".join([
        b"RIFF", struct.pack("<I", 36 + pcm_len), b"WAVE",
        b"fmt ", struct.pack("<IHHIIHH", 16, 1, 1, sample_rate,
                             sample_rate * 2, 2, 16),
        b"data", struct.pack("<I", pcm_len),
    ])
    return header + b"\x01\x00\x02\x00" * (pcm_len // 4)


# --------------------------------------------------------------------------
# Contract-faithful fakes (duck-typed to the CONTRACT §3 sibling signatures)
# --------------------------------------------------------------------------
class FakeVoiceChannel:
    """Mirrors sarvam_tts.VoiceChannel (A1): connect/set_speaker/speak/close."""

    def __init__(self) -> None:
        self.connected = False
        self.connect_count = 0
        self.close_count = 0
        self.speak_count = 0
        self.set_speaker_calls: list = []
        self.utterances: list = []
        self.fail_speak = False

    async def connect(self) -> None:
        self.connect_count += 1
        self.connected = True

    async def set_speaker(self, speaker, language_code, pace=1.0, *, dict_id=None):
        self.set_speaker_calls.append((speaker, language_code, pace, dict_id))
        self.connected = True

    async def speak(self, text, utterance_id, on_first_chunk=None):
        self.speak_count += 1
        self.utterances.append((text, utterance_id))
        if self.fail_speak:
            raise RuntimeError("sarvam ws down")
        wav = fake_wav()
        pieces = [wav[:44], wav[44:74], wav[74:]]  # header-first CONTRACT §1 shape
        for index, piece in enumerate(pieces):
            if index == 0 and on_first_chunk is not None:
                result = on_first_chunk(piece)
                if hasattr(result, "__await__"):
                    await result
            yield piece

    async def ping(self) -> None:  # pragma: no cover — not exercised here
        pass

    async def close(self) -> None:
        self.close_count += 1
        self.connected = False


def make_fake_tts(fail_stream: bool = False, fail_rest: bool = False):
    """A fake sarvam_tts module: speak_via_stream/speak_via_rest/wav_blob_to_chunks."""
    tts = types.ModuleType("fake_sarvam_tts")

    async def speak_via_stream(text, speaker, language_code, pace=1.0,
                               sample_rate=24000, **kwargs):
        if fail_stream:
            raise RuntimeError("stream endpoint 503")
        return fake_wav(pcm_len=6000)

    async def speak_via_rest(text, speaker, language_code, pace=1.0,
                             sample_rate=24000, **kwargs):
        if fail_rest:
            raise RuntimeError("rest endpoint 503")
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
    tts.fail_stream = fail_stream
    tts.fail_rest = fail_rest
    return tts


class FakeCache:
    """Mirrors backend.cache (A2) surface used by main.py, in memory."""

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


class FakeJudge:
    """Mirrors backend.judge (A4): judge_answer(question, heard, persona_label)."""

    def __init__(self) -> None:
        self.calls: list = []

    async def judge_answer(self, question, heard, persona_label):
        self.calls.append((question, heard, persona_label))
        return {"verdict": "correct", "close_call": False,
                "template_id": "correct_fast", "hype_line": HYPE_LINE}


class FakeSTT:
    """Mirrors backend.sarvam_stt (A3): transcribe_answer(wav, keyterms=None)."""

    class STTUnavailable(Exception):
        pass

    def __init__(self) -> None:
        self.calls: list = []

    async def transcribe_answer(self, wav_bytes, keyterms=None):
        self.calls.append(len(wav_bytes))
        return {"text": "new delhi", "language_code": "hi-IN", "raw": {}}


class FakeGameSession:
    """Minimal CONTRACT §3 game.py stand-in: the duck-type A6 must provide.

    handle(msg) drives the CONTRACT §1/§2 flow: start → state frames →
    question → host audio (audio_start/audio_chunk*/audio_end via the
    injected voice ladder), buzz → listening, answer → judge_fn → verdict →
    reaction audio, repeat_question → cached re-speak. Exceptions from
    voice.speak propagate (main.py converts them to error frames).
    """

    def __init__(self, send, voice, judge_fn, bank) -> None:
        self.send = send
        self.voice = voice
        self.judge_fn = judge_fn
        self.bank = bank or {}
        self.phase = "lobby"
        self.handled: list = []
        self.utterance_n = 0
        self.destroyed = False
        self.question = QUESTION
        self.speaker = SPEAKER
        self.language_code = LANG
        self.pace = PACE

    async def handle(self, msg) -> None:
        self.handled.append(msg)
        mtype = msg.get("type")
        if mtype == "start":
            await self.voice.set_speaker(self.speaker, self.language_code, self.pace)
            self.phase = "round_intro"
            await self._state()
            self.phase = "question"
            await self._state()
            await self.send({"type": "question", "id": self.question["id"],
                             "text": self.question["text"],
                             "category": self.question["category"],
                             "difficulty": self.question["difficulty"],
                             "language": self.question["language"]})
            await self._speak(self.question["text"])
        elif mtype == "buzz":
            self.phase = "listening"
            await self._state()
        elif mtype == "answer":
            heard = str(msg.get("text", ""))
            self.phase = "judging"
            await self._state()
            verdict = await self.judge_fn(self.question, heard, "Quizmaster")
            self.phase = "verdict"
            self.utterance_n += 1
            await self.send({"type": "verdict",
                             "utterance_id": f"utt-{self.utterance_n}",
                             "correct": verdict["verdict"],
                             "close_call": verdict["close_call"],
                             "heard": heard,
                             "template_id": verdict["template_id"],
                             "hype_line": verdict["hype_line"],
                             "fact_line": "Delhi is India's capital.",
                             "score_delta": 100, "score": 100, "streak": 1})
            await self._speak(verdict["hype_line"])
        elif mtype == "repeat_question":
            await self._speak(self.question["text"])
        # ping / barge / skip_question / next_round: no frames in this fake

    async def _state(self) -> None:
        await self.send({"type": "state", "phase": self.phase, "round": 1,
                         "question_index": 0, "score": 100 if self.phase == "verdict" else 0,
                         "streak": 1 if self.phase == "verdict" else 0,
                         "speaker": self.speaker,
                         "persona_label": "Quizmaster", "mode": "demo"})

    async def _speak(self, text: str) -> None:
        self.utterance_n += 1
        uid = f"utt-{self.utterance_n}"
        t0 = time.monotonic()
        first = [None]

        def on_first(chunk):  # sync callback — ladder must accept sync or async
            first[0] = time.monotonic()

        await self.send({"type": "audio_start", "utterance_id": uid,
                         "speaker": self.speaker, "sample_rate": 24000,
                         "codec": "wav", "caption": text})
        async for chunk in self.voice.speak(text, uid, on_first_chunk=on_first):
            await self.send({"type": "audio_chunk", "utterance_id": uid,
                             "b64": base64.b64encode(chunk).decode("ascii")})
        ttfb = int((first[0] - t0) * 1000) if first[0] is not None else 0
        total = int((time.monotonic() - t0) * 1000)
        await self.send({"type": "audio_end", "utterance_id": uid,
                         "ttfb_ms": ttfb, "total_ms": total, "chars": len(text)})

    async def destroy(self) -> None:
        self.destroyed = True


def drain(ws, stop_type: str, limit: int = 80):
    """Receive frames until one of ``stop_type`` arrives → (stop_frame, all)."""
    frames = []
    for _ in range(limit):
        frame = ws.receive_json()
        frames.append(frame)
        if frame.get("type") == stop_type:
            return frame, frames
    raise AssertionError(f"never received a {stop_type!r} frame; got "
                         f"{[f.get('type') for f in frames]}")


def frames_of(frames, ftype: str):
    return [f for f in frames if f.get("type") == ftype]


# --------------------------------------------------------------------------
# Test base: fresh runtime state + full DI of every main.py factory
# --------------------------------------------------------------------------
class RelayTestCase(unittest.TestCase):
    def setUp(self) -> None:
        main.reset_runtime()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.logs_dir = Path(tmp.name)
        logs_patch = mock.patch.object(main.config, "LOGS_DIR", self.logs_dir)
        logs_patch.start()
        self.addCleanup(logs_patch.stop)

        self.fake_voice = FakeVoiceChannel()
        self.fake_cache = FakeCache()
        self.fake_judge = FakeJudge()
        self.fake_stt = FakeSTT()
        self.fake_tts = make_fake_tts()
        self.fake_bank = {"demo": [QUESTION]}
        self.game: FakeGameSession | None = None
        self.fake_personas = types.SimpleNamespace(
            persona_for_round=lambda r, l: {"speaker": SPEAKER,
                                            "language_code": LANG, "pace": PACE})
        for name, value in (
            ("make_voice_channel", lambda: self.fake_voice),
            ("get_cache_module", lambda: self.fake_cache),
            ("get_tts_module", lambda: self.fake_tts),
            ("get_judge_module", lambda: self.fake_judge),
            ("get_stt_module", lambda: self.fake_stt),
            ("get_personas_module", lambda: self.fake_personas),
            ("get_bank", lambda: self.fake_bank),
            ("make_game_session", self._make_game),
        ):
            patcher = mock.patch.object(main, name, new=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _make_game(self, send, voice, judge_fn, bank):
        self.game = FakeGameSession(send, voice, judge_fn, bank)
        return self.game

    def client(self) -> TestClient:
        return TestClient(main.app)

    def start_frames(self, ws):
        """start → all frames up to the question's audio_end."""
        ws.send_json({"type": "start", "player_name": "Asha", "language": "hi"})
        return drain(ws, "audio_end")

    @staticmethod
    def latency_lines(path: Path) -> list:
        text = (path / "latency.jsonl").read_text(encoding="utf-8").strip()
        return [json.loads(line) for line in text.splitlines() if line]


# --------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------
class TestHttpEndpoints(RelayTestCase):
    def test_healthz_payload(self):
        self.fake_cache.spend.append((120, "tts:ws"))
        with self.client() as client:
            resp = client.get("/healthz")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json(), {"ok": True, "chars_used": 120, "sessions": 0})

    def test_healthz_counts_active_sessions(self):
        with self.client() as client, client.websocket_connect("/ws") as ws:
            self.start_frames(ws)
            payload = client.get("/healthz").json()
            self.assertEqual(payload["sessions"], 1)
            self.assertTrue(payload["ok"])

    def test_root_placeholder_when_frontend_absent(self):
        with self.client() as client:
            resp = client.get("/")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("BhashaBuzzer", resp.text)
            self.assertIn("html", resp.headers["content-type"])


# --------------------------------------------------------------------------
# Warmup
# --------------------------------------------------------------------------
class TestWarmup(RelayTestCase):
    def test_warmup_idempotent_and_configures_r1_persona(self):
        with self.client() as client:
            first = client.post("/api/warmup")
            second = client.post("/api/warmup")
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json(), {"ok": True, "warm": True})
            self.assertEqual(second.json(), {"ok": True, "warm": True})
            self.assertEqual(self.fake_voice.connect_count, 1)  # idempotent
            self.assertEqual(self.fake_voice.set_speaker_calls,
                             [(SPEAKER, LANG, PACE, None)])

    def test_session_adopts_prewarmed_channel_and_keeps_it_warm(self):
        with self.client() as client:
            self.assertEqual(client.post("/api/warmup").json()["warm"], True)
            with client.websocket_connect("/ws") as ws:
                self.start_frames(ws)
                self.assertEqual(self.fake_voice.connect_count, 1)  # adopted
                self.assertEqual(client.get("/healthz").json()["sessions"], 1)
            # disconnect: global prewarm channel released, NOT closed
            self.assertEqual(self.fake_voice.close_count, 0)
            self.assertTrue(self.fake_voice.connected)
            self.assertEqual(client.get("/healthz").json()["sessions"], 0)
            with client.websocket_connect("/ws") as ws:  # re-adopts, still 1 open
                self.start_frames(ws)
                self.assertEqual(self.fake_voice.connect_count, 1)


# --------------------------------------------------------------------------
# Full round-trip through the relay
# --------------------------------------------------------------------------
class TestFullRoundTrip(RelayTestCase):
    def test_start_buzz_answer_verdict(self):
        with self.client() as client, client.websocket_connect("/ws") as ws:
            audio_end, frames = self.start_frames(ws)

            # protocol frames per CONTRACT §1
            states = frames_of(frames, "state")
            self.assertTrue(any(s["phase"] == "round_intro" for s in states))
            self.assertTrue(any(s["phase"] == "question" for s in states))
            questions = frames_of(frames, "question")
            self.assertEqual(len(questions), 1)
            self.assertEqual(questions[0]["text"], QUESTION_TEXT)

            # FIRST audio_chunk is EXACTLY the 44-byte RIFF header
            starts = frames_of(frames, "audio_start")
            self.assertEqual(len(starts), 1)
            self.assertEqual(starts[0]["sample_rate"], 24000)
            self.assertEqual(starts[0]["codec"], "wav")
            self.assertEqual(starts[0]["caption"], QUESTION_TEXT)
            chunks = frames_of(frames, "audio_chunk")
            self.assertGreaterEqual(len(chunks), 2)
            first = base64.b64decode(chunks[0]["b64"])
            self.assertEqual(len(first), 44)
            self.assertEqual(first[:4], b"RIFF")
            self.assertEqual(first[8:12], b"WAVE")
            for later in chunks[1:]:
                self.assertNotIn(b"RIFF", base64.b64decode(later["b64"])[:4])

            # audio_end carries the CONTRACT latency decomposition
            self.assertEqual(audio_end["utterance_id"], starts[0]["utterance_id"])
            self.assertFalse(audio_end["cache_hit"])
            self.assertEqual(audio_end["source"], "ws")
            self.assertEqual(audio_end["chars"], len(QUESTION_TEXT))
            for field in ("ttfb_ms", "total_ms", "stt_ms", "judge_ms", "e2e_ms"):
                self.assertIsInstance(audio_end[field], int)
                self.assertGreaterEqual(audio_end[field], 0)

            # buzz → listening
            ws.send_json({"type": "buzz"})
            state = ws.receive_json()
            self.assertEqual(state["type"], "state")
            self.assertEqual(state["phase"], "listening")

            # answer with audio → STT interception → judge → verdict + reaction
            wav = fake_wav(pcm_len=2000, sample_rate=16000)
            ws.send_json({"type": "answer",
                          "audio_b64": base64.b64encode(wav).decode("ascii")})
            verdict, vframes = drain(ws, "verdict")
            self.assertEqual(verdict["correct"], "correct")
            self.assertEqual(verdict["heard"], "new delhi")  # via FakeSTT
            self.assertEqual(verdict["score"], 100)
            self.assertEqual(self.fake_stt.calls, [len(wav)])
            self.assertEqual(len(self.fake_judge.calls), 1)
            self.assertEqual(self.fake_judge.calls[0][1], "new delhi")

            reaction_end, _ = drain(ws, "audio_end")
            self.assertEqual(reaction_end["source"], "ws")
            self.assertFalse(reaction_end["cache_hit"])
            self.assertGreaterEqual(reaction_end["stt_ms"], 0)
            self.assertGreaterEqual(reaction_end["judge_ms"], 0)
            self.assertGreaterEqual(reaction_end["e2e_ms"], 0)  # t_buzz → first chunk
            self.assertEqual(reaction_end["chars"], len(HYPE_LINE))

            # spend logged once per real synthesis (question + reaction)
            self.assertEqual(self.fake_cache.spend,
                             [(len(QUESTION_TEXT), "tts:ws"),
                              (len(HYPE_LINE), "tts:ws")])
            # latency JSONL: one line per utterance with the decomposition
            lines = self.latency_lines(self.logs_dir)
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0]["source"], "ws")
            self.assertEqual(lines[0]["utterance_id"], "utt-1")
            self.assertEqual(lines[1]["chars"], len(HYPE_LINE))
            self.assertIn("session_id", lines[0])

            # healthz reflects spend + live session
            self.assertEqual(client.get("/healthz").json(),
                             {"ok": True,
                              "chars_used": len(QUESTION_TEXT) + len(HYPE_LINE),
                              "sessions": 1})

    def test_disconnect_teardown_cleans_session_and_channel(self):
        with self.client() as client, client.websocket_connect("/ws") as ws:
            self.start_frames(ws)
            self.assertIsNotNone(self.game)
        # socket closed: slot freed, OWNED channel closed, session destroyed
        self.assertEqual(client.get("/healthz").json()["sessions"], 0)
        self.assertEqual(self.fake_voice.close_count, 1)
        self.assertFalse(self.fake_voice.connected)
        self.assertTrue(self.game.destroyed)

    def test_session_cap_eight_plus_server_full(self):
        with self.client() as client:
            stack = ExitStack()
            self.addCleanup(stack.close)
            sockets = [stack.enter_context(client.websocket_connect("/ws"))
                       for _ in range(9)]
            firsts = []
            for index, sock in enumerate(sockets):
                sock.send_json({"type": "start", "player_name": f"P{index}",
                                "language": "hi"})
            for sock in sockets:
                firsts.append(sock.receive_json())
            self.assertEqual(sum(1 for f in firsts if f["type"] == "state"), 8)
            full = [f for f in firsts if f.get("code") == "server_full"]
            self.assertEqual(len(full), 1)
            self.assertFalse(full[0]["recoverable"])
            self.assertEqual(client.get("/healthz").json()["sessions"], 8)
            stack.close()
            self.assertEqual(client.get("/healthz").json()["sessions"], 0)


# --------------------------------------------------------------------------
# Cache-first + spend discipline
# --------------------------------------------------------------------------
class TestCacheFirst(RelayTestCase):
    def test_cache_hit_streams_without_spend_or_ws_call(self):
        key = self.fake_cache.tts_key(QUESTION_TEXT, SPEAKER, LANG, PACE,
                                      24000, "wav", None, None, None)
        self.fake_cache.store[key] = fake_wav(pcm_len=9000)

        with self.client() as client, client.websocket_connect("/ws") as ws:
            audio_end, frames = self.start_frames(ws)
            self.assertTrue(audio_end["cache_hit"])
            self.assertEqual(audio_end["source"], "cache")
            self.assertGreaterEqual(audio_end["ttfb_ms"], 0)
            first = base64.b64decode(frames_of(frames, "audio_chunk")[0]["b64"])
            self.assertEqual(len(first), 44)  # cached blob → header chunk too
            self.assertEqual(self.fake_voice.speak_count, 0)   # no WS synth
            self.assertEqual(self.fake_cache.spend, [])          # no spend
            self.assertEqual(self.fake_cache.put_keys, [])       # no re-put

    def test_spend_logged_on_miss_only(self):
        with self.client() as client, client.websocket_connect("/ws") as ws:
            # question utterance: MISS → synthesized, cached, spent
            audio_end, _ = self.start_frames(ws)
            self.assertFalse(audio_end["cache_hit"])
            self.assertEqual(audio_end["source"], "ws")
            self.assertEqual(len(self.fake_cache.spend), 1)
            self.assertEqual(len(self.fake_cache.put_keys), 1)

            # buzz + typed shy-mode answer (no audio → STT never called)
            ws.send_json({"type": "buzz"})
            ws.receive_json()
            ws.send_json({"type": "answer", "text": "new delhi"})
            verdict, _ = drain(ws, "verdict")
            self.assertEqual(verdict["heard"], "new delhi")
            self.assertEqual(self.fake_stt.calls, [])
            reaction_end, _ = drain(ws, "audio_end")
            self.assertFalse(reaction_end["cache_hit"])
            self.assertEqual(len(self.fake_cache.spend), 2)  # one per miss

            # repeat_question: question is now cached → hit, NO new spend
            ws.send_json({"type": "repeat_question"})
            repeat_end, _ = drain(ws, "audio_end")
            self.assertTrue(repeat_end["cache_hit"])
            self.assertEqual(repeat_end["source"], "cache")
            self.assertEqual(len(self.fake_cache.spend), 2)  # unchanged
            self.assertEqual(len(self.fake_cache.put_keys), 2)  # question + hype


# --------------------------------------------------------------------------
# Transport-ladder fallbacks
# --------------------------------------------------------------------------
class TestTransportLadder(RelayTestCase):
    def _collect_question_audio(self, ws):
        audio_end, frames = self.start_frames(ws)
        chunks = frames_of(frames, "audio_chunk")
        self.assertGreaterEqual(len(chunks), 2)
        first = base64.b64decode(chunks[0]["b64"])
        self.assertEqual(len(first), 44)
        self.assertEqual(first[:4], b"RIFF")
        return audio_end

    def test_ws_failure_falls_back_to_stream(self):
        self.fake_voice.fail_speak = True
        with self.client() as client, client.websocket_connect("/ws") as ws:
            audio_end = self._collect_question_audio(ws)
            self.assertEqual(audio_end["source"], "stream")
            self.assertFalse(audio_end["cache_hit"])
            self.assertEqual(self.fake_voice.speak_count, 1)  # WS tried first
            self.assertEqual(self.fake_cache.spend,
                             [(len(QUESTION_TEXT), "tts:stream")])
            self.assertEqual(len(self.fake_cache.put_keys), 1)
            self.assertEqual(self.latency_lines(self.logs_dir)[0]["source"],
                             "stream")

    def test_stream_failure_falls_back_to_rest(self):
        self.fake_voice.fail_speak = True
        self.fake_tts = make_fake_tts(fail_stream=True)
        # re-patch the tts factory with the new fake
        patcher = mock.patch.object(main, "get_tts_module", new=lambda: self.fake_tts)
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.client() as client, client.websocket_connect("/ws") as ws:
            audio_end = self._collect_question_audio(ws)
            self.assertEqual(audio_end["source"], "rest")
            self.assertEqual(self.fake_cache.spend,
                             [(len(QUESTION_TEXT), "tts:rest")])

    def test_all_transports_fail_sends_text_only_error_frame(self):
        self.fake_voice.fail_speak = True
        self.fake_tts = make_fake_tts(fail_stream=True, fail_rest=True)
        patcher = mock.patch.object(main, "get_tts_module", new=lambda: self.fake_tts)
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.client() as client, client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "start", "player_name": "Asha", "language": "hi"})
            error, frames = drain(ws, "error")
            self.assertEqual(error["code"], "tts_unavailable")
            self.assertTrue(error["recoverable"])
            self.assertEqual(error["phase"], "question")
            self.assertEqual(frames_of(frames, "audio_chunk"), [])
            self.assertEqual(frames_of(frames, "audio_end"), [])
            self.assertEqual(self.fake_cache.spend, [])       # nothing synthesized
            self.assertEqual(self.fake_cache.put_keys, [])
            # session survives: barge-safe, next beat still works
            ws.send_json({"type": "buzz"})
            state = ws.receive_json()
            self.assertEqual(state["phase"], "listening")


# --------------------------------------------------------------------------
# config.py (A7 owns) — lazy key resolution, never read at import
# --------------------------------------------------------------------------
class TestConfig(unittest.TestCase):
    def test_api_key_env_var_wins_without_touching_env_file(self):
        with mock.patch.dict("os.environ", {"SARVAM_API_KEY": "env-key-123"}):
            fake_env = Path(tempfile.mkdtemp()) / ".env"
            with mock.patch.object(main.config, "ENV_FILE", fake_env):
                self.assertEqual(main.config.SARVAM_API_KEY, "env-key-123")
                self.assertFalse(fake_env.exists())  # never written/read

    def test_api_key_parses_dotenv_at_runtime_only(self):
        tmp = Path(tempfile.mkdtemp())
        env_file = tmp / ".env"
        env_file.write_text(
            "# comment\nOTHER=x\nSARVAM_API_KEY=\"file-key-456\"\n",
            encoding="utf-8")
        environ = {"SARVAM_API_KEY": ""}
        with mock.patch.dict("os.environ", environ, clear=False):
            import os
            with mock.patch.object(main.config, "ENV_FILE", env_file):
                os.environ.pop("SARVAM_API_KEY", None)
                self.assertEqual(main.config.SARVAM_API_KEY, "file-key-456")

    def test_api_key_empty_when_no_env_and_no_file(self):
        import os
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop("SARVAM_API_KEY", None)
            with mock.patch.object(main.config, "ENV_FILE", tmp / "absent.env"):
                self.assertEqual(main.config.SARVAM_API_KEY, "")

    def test_module_attributes_and_lazy_getattr(self):
        self.assertEqual(main.config.MAX_SESSIONS, 8)
        self.assertEqual(main.config.TTS_MODEL, "bulbul:v4-flash")
        self.assertEqual(main.config.STT_MODEL, "saaras:v4")
        self.assertEqual(main.config.JUDGE_MODEL, "sarvam-105b-conversations")
        self.assertTrue(main.config.TTS_WS_URL.startswith(
            "wss://api.sarvam.ai/text-to-speech/ws?model=bulbul:v4-flash"))
        self.assertIn("send_completion_event=true", main.config.TTS_WS_URL)
        with self.assertRaises(AttributeError):
            main.config.NO_SUCH_THING  # noqa: B018

    def test_constants_match_contract(self):
        self.assertEqual(main.config.BASE_URL, "https://api.sarvam.ai")
        self.assertEqual(main.config.TTS_SAMPLE_RATE, 24000)
        self.assertEqual(main.config.TTS_CODEC, "wav")
        self.assertEqual(main.config.RETRY_MAX, 2)
        self.assertEqual(main.config.RETRY_BACKOFF_S, (0.5, 1.0, 2.0, 4.0))
        self.assertEqual(main.config.JUDGE_RATE_PER_MIN, 24)
        self.assertEqual(main.config.WS_OPEN_STAGGER_S, 0.3)
        self.assertEqual(main.config.LATENCY_LOG, "latency.jsonl")
        for name in ("FRONTEND_DIR", "DATA_DIR", "CACHE_DIR", "LOGS_DIR"):
            self.assertIsInstance(getattr(main.config, name), Path)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
