"""A6 game engine tests — stdlib unittest, inline FakeVoice/FakeJudge (no network).

Run from the worktree root:
    python -m unittest discover -s backend/tests
"""

import asyncio
import base64
import random
import sys
import time as real_time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import backend.game as game  # noqa: E402
from backend.game import GameSession  # noqa: E402

FAST = {"beat": 0.01, "buzz_window": 0.5, "answer_window": 0.2}

# a well-formed 44-byte RIFF/WAVE header (PCM 16-bit mono 24 kHz, zero data)
RIFF_HEADER = (b"RIFF" + (36).to_bytes(4, "little") + b"WAVE" + b"fmt "
               + (16).to_bytes(4, "little") + (1).to_bytes(2, "little")
               + (1).to_bytes(2, "little") + (24000).to_bytes(4, "little")
               + (48000).to_bytes(4, "little") + (2).to_bytes(2, "little")
               + (16).to_bytes(2, "little") + b"data" + (0).to_bytes(4, "little"))
assert len(RIFF_HEADER) == 44

PCM_CHUNK = b"\x01\x00" * 512

TEST_CAST = {
    1: {"speaker": "ratan_hi_customer_expressive", "label": "R1 Host",
        "language_code": "hi-IN", "pace": 1.0},
    2: {"speaker": "ishita_enhi_customer_expressive", "label": "R2 Host",
        "language_code": "hi-IN", "pace": 1.05},
    3: {"speaker": "ratan_hi_sports", "label": "R3 Host",
        "language_code": "hi-IN", "pace": 1.1},
    4: {"speaker": "mukul_hi_suspense", "label": "Final Host",
        "language_code": "hi-IN", "pace": 0.9},
}


class FakeVoice:
    """Duck-typed TTS channel: yields 44-byte RIFF header then PCM chunks."""

    def __init__(self, n_pcm=3, chunk_delay=0.001):
        self.set_calls = []
        self.speak_calls = []
        self.n_pcm = n_pcm
        self.chunk_delay = chunk_delay

    async def set_speaker(self, speaker, language_code, pace=1.0):
        self.set_calls.append((speaker, language_code, pace))

    async def speak(self, text, utterance_id, on_first_chunk=None):
        self.speak_calls.append((text, utterance_id))
        if on_first_chunk:
            on_first_chunk()
        yield RIFF_HEADER
        for _ in range(self.n_pcm):
            await asyncio.sleep(self.chunk_delay)
            yield PCM_CHUNK


class FakeJudge:
    """Duck-typed judge: script maps question id -> verdict dict or callable(heard)."""

    def __init__(self, script=None, default=None):
        self.script = script or {}
        self.default = default or {"verdict": "correct", "close_call": False,
                                   "template_id": "correct_fast", "hype_line": ""}
        self.calls = []

    async def __call__(self, question, heard, persona_label):
        self.calls.append({"id": question["id"], "heard": heard,
                           "persona": persona_label, "question": question})
        verdict = self.script.get(question["id"], self.default)
        if callable(verdict):
            verdict = verdict(heard)
        return dict(verdict)


class FakeTime:
    """Static controllable clock (patched into game.time for buzz-latency control)."""

    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


class Harness:
    """Wires GameSession with fakes and collects every emitted frame."""

    def __init__(self, mode="demo", judge=None, voice=None, cast=None, bank=None,
                 timings=None, stt_fn=None, language="hi", questions=None):
        self.frames = []
        self.voice = voice or FakeVoice()
        self.judge = judge or FakeJudge()
        self.stt_calls = []

        async def send(frame):
            self.frames.append(frame)

        t = dict(FAST)
        t.update(timings or {})
        self.sess = GameSession(send, self.voice, self.judge, bank or {}, cast=cast,
                                timings=t, stt_fn=stt_fn, questions=questions)
        self._mode = mode
        self._language = language

    async def start(self, name="Ashu", mode=None, language=None, date=None):
        msg = {"type": "start", "player_name": name,
               "language": language or self._language, "mode": mode or self._mode}
        if date is not None:
            msg["date"] = date
        await self.sess.handle(msg)

    async def buzz_answer(self, text="jawaab"):
        await self.sess.handle({"type": "buzz"})
        await self.sess.handle({"type": "answer", "text": text})

    # -- frame helpers ------------------------------------------------------

    def of(self, ftype):
        return [f for f in self.frames if f.get("type") == ftype]

    def states(self):
        return self.of("state")

    def phases(self):
        return [f["phase"] for f in self.states()]

    def verdicts(self):
        return self.of("verdict")

    def qids(self):
        return [f["id"] for f in self.of("question")]

    def kill(self):
        self.sess._teardown()

    def last_state(self):
        return self.states()[-1]


def demo_ids(language="hi"):
    qs = game.load_questions()
    plan = [dict(s) for s in game.DEMO_PLAN]
    sel = game.select_questions(qs, plan, language, random.Random(game.DEMO_SEED))
    return [q["id"] for q in sel]


class GameTest(unittest.IsolatedAsyncioTestCase):
    """Base: patched fake clock + harness cleanup."""

    def setUp(self):
        self.ft = FakeTime()
        game.time = self.ft
        self.addCleanup(setattr, game, "time", real_time)

    def harness(self, **kw):
        h = Harness(**kw)
        self.addCleanup(h.kill)
        return h

    async def wait_until(self, cond, timeout=10.0, msg="condition"):
        end = real_time.monotonic() + timeout
        while True:
            val = cond()
            if val:
                return val
            if real_time.monotonic() > end:
                self.fail(f"timeout waiting for {msg}")
            await asyncio.sleep(0.002)

    async def wait_question(self, h, n, timeout=10.0):
        return await self.wait_until(lambda: len(h.of("question")) >= n and h.of("question")[n - 1],
                                     timeout=timeout, msg=f"question #{n}")

    async def wait_verdict(self, h, n, timeout=10.0):
        return await self.wait_until(lambda: len(h.verdicts()) >= n and h.verdicts()[n - 1],
                                     timeout=timeout, msg=f"verdict #{n}")

    async def wait_phase(self, h, phase, timeout=10.0):
        return await self.wait_until(lambda: any(s["phase"] == phase for s in h.states()),
                                     timeout=timeout, msg=f"phase {phase}")

    async def play_question(self, h, i, clock_bump=0.3, text="jawaab"):
        """Wait for question i (1-based), advance the fake clock, buzz, answer."""
        await self.wait_question(h, i)
        self.ft.t += clock_bump
        await h.buzz_answer(text)


# --------------------------------------------------------------- pure math


class TestScoringFunctions(unittest.TestCase):
    def test_speed_bonus_tiers(self):
        cases = [(0.0, 50), (0.5, 50), (0.99, 50), (1.0, 50),
                 (1.01, 30), (1.5, 30), (2.0, 30),
                 (2.01, 15), (2.5, 15), (3.0, 15),
                 (3.01, 0), (4.2, 0), (None, 0), (-1.0, 0)]
        for elapsed, expected in cases:
            self.assertEqual(game.speed_bonus(elapsed), expected, msg=f"elapsed={elapsed}")

    def test_streak_multiplier(self):
        for streak, expected in [(0, 1.0), (1, 1.0), (2, 1.5), (3, 2.0), (4, 2.5),
                                 (5, 2.5), (7, 2.5)]:
            self.assertEqual(game.streak_multiplier(streak), expected, msg=f"streak={streak}")

    def test_round_to_5(self):
        self.assertEqual(game.round_to_5(172.5), 175)
        self.assertEqual(game.round_to_5(225), 225)
        self.assertEqual(game.round_to_5(460), 460)
        self.assertEqual(game.round_to_5(100), 100)

    def test_score_correct_worked_example(self):
        # design §3 worked example: R3, streak 3, buzz at 1.2 s -> (200+30) x 2 = 460
        self.assertEqual(game.score_correct(200, 1.2, 3), 460)
        # max single question: final, streak 4+, fast buzz -> (300+50) x 2.5 = 875
        self.assertEqual(game.score_correct(300, 0.2, 4), 875)
        # rounding case: (100+15) x 1.5 = 172.5 -> 175
        self.assertEqual(game.score_correct(100, 2.5, 2), 175)

    def test_score_partial_halves(self):
        for base, expected in [(100, 50), (150, 75), (200, 100), (300, 150)]:
            self.assertEqual(game.score_partial(base), expected)


# ------------------------------------------------------------ question bank


class TestQuestionBank(unittest.TestCase):
    def test_file_valid(self):
        questions = game.load_questions()
        self.assertEqual(len(questions), 72)
        ids = [q["id"] for q in questions]
        self.assertEqual(len(ids), len(set(ids)))
        for q in questions:
            for field in game.QUESTION_FIELDS:
                self.assertIn(field, q)
            self.assertIn(q["category"], {"Bollywood", "Cricket", "GK/Bharat", "Ganit",
                                          "Pop-culture/Tech", "Vigyan"})
            self.assertIsInstance(q["difficulty"], int)
            self.assertTrue(1 <= q["difficulty"] <= 5)
            self.assertIn(q["language"], {"hi-IN", "en-IN", "mr-IN"})
            self.assertTrue(q["question_text"].strip())
            self.assertTrue(q["expected_answer"].strip())
            self.assertTrue(q["accepted_answer_aliases"])
            self.assertTrue(q["fun_fact_line"].strip())

    def test_keyterms_derived_and_capped(self):
        questions = game.load_questions()
        for q in questions:
            kt = q["keyterms"]
            self.assertTrue(1 <= len(kt) <= 8, msg=f"{q['id']}: {len(kt)} keyterms")
            lower = [k.lower() for k in kt]
            self.assertEqual(len(lower), len(set(lower)))
            # keyterms derive from expected answer + aliases
            self.assertEqual(lower[0], q["expected_answer"].strip().lower())
        dhoni = next(q for q in questions if q["id"] == "cricket-01")
        self.assertEqual(dhoni["keyterms"][0], "MS Dhoni")
        self.assertIn("धोनी", dhoni["keyterms"])

    def test_keyterms_at_most_8_tokens(self):
        questions = game.load_questions()
        worst = max(len(q["keyterms"]) for q in questions)
        self.assertLessEqual(worst, 8)


# --------------------------------------------------------- demo match: full


class TestDemoMatch(GameTest):
    async def test_full_match_scoring_and_arc(self):
        ids = demo_ids()
        script = {
            ids[0]: {"verdict": "correct", "template_id": "correct_fast"},
            ids[1]: {"verdict": "correct", "template_id": "correct_fast"},
            ids[2]: {"verdict": "correct", "template_id": "correct_fast"},
            ids[3]: {"verdict": "partial", "template_id": "partial"},
            ids[4]: {"verdict": "correct", "template_id": "correct_confident"},
            ids[5]: {"verdict": "wrong", "template_id": "wrong_confident"},
            ids[6]: {"verdict": "correct", "template_id": "correct_fast"},
        }
        judge = FakeJudge(script=script)
        h = self.harness(judge=judge, cast=TEST_CAST)
        await h.start()

        # buzz latencies per question: fast, fast, <=2s, -, slow(3.5s), fast, fast
        bumps = [0.3, 0.5, 1.2, 1.0, 3.5, 0.4, 0.2]
        for i in range(1, 8):
            await self.play_question(h, i, clock_bump=bumps[i - 1])
            await self.wait_verdict(h, i)
        await self.wait_phase(h, "final")

        # (base+speed) x multiplier, rounded to nearest 5:
        # Q1 (100+50)x1   Q2 (100+50)x1.5  Q3 (150+30)x2   Q4 partial 150//2
        # Q5 (200+0)x2.5  Q6 wrong 0       Q7 (300+50)x1
        deltas = [v["score_delta"] for v in h.verdicts()]
        self.assertEqual(deltas, [150, 225, 360, 75, 500, 0, 350])
        self.assertEqual([v["correct"] for v in h.verdicts()],
                         ["correct", "correct", "correct", "partial", "correct",
                          "wrong", "correct"])
        self.assertEqual([v["streak"] for v in h.verdicts()], [1, 2, 3, 3, 4, 0, 1])
        running = [v["score"] for v in h.verdicts()]
        self.assertEqual(running, [150, 375, 735, 810, 1310, 1310, 1660])
        self.assertEqual(h.last_state()["score"], 1660)
        self.assertEqual(h.last_state()["phase"], "final")
        self.assertEqual(h.last_state()["mode"], "demo")
        # no errors in a clean match
        self.assertEqual(h.of("error"), [])

        # judge received the full question records + current persona labels
        self.assertEqual([c["id"] for c in judge.calls], ids)
        expected_personas = ["R1 Host", "R1 Host", "R2 Host", "R2 Host",
                              "R3 Host", "R3 Host", "Final Host"]
        self.assertEqual([c["persona"] for c in judge.calls], expected_personas)
        for call in judge.calls:
            self.assertIn("expected_answer", call["question"])
            self.assertIn("keyterms", call["question"])

        # verdict frames carry the fact line of their question
        facts = [v["fact_line"] for v in h.verdicts()]
        bank = {q["id"]: q["fun_fact_line"] for q in game.load_questions()}
        for vid, fact in zip(ids, facts):
            self.assertEqual(fact, bank[vid])

    async def test_phase_transition_sequence(self):
        h = self.harness(cast=TEST_CAST)
        await h.start()
        for i in range(1, 8):
            await self.play_question(h, i)
            await self.wait_verdict(h, i)
        await self.wait_phase(h, "final")
        expected = ["lobby", "round_intro"]
        per_q = ["question", "listening", "judging", "verdict", "next"]
        for i in range(7):
            if i in (2, 4, 6):
                expected += ["round_switch", "round_intro"]
            expected += per_q
        expected.append("final")
        self.assertEqual(h.phases(), expected)

    async def test_arc_selection_and_determinism(self):
        h1 = self.harness(cast=TEST_CAST)
        await h1.start()
        await self.wait_question(h1, 1)
        h2 = self.harness(cast=TEST_CAST)
        await h2.start()
        await self.wait_question(h2, 1)
        ids1 = [q["id"] for q in h1.sess.selected]
        ids2 = [q["id"] for q in h2.sess.selected]
        self.assertEqual(ids1, ids2)                        # fixed arc => same order always
        self.assertEqual(ids1, demo_ids())
        qs = {q["id"]: q for q in game.load_questions()}
        sel = [qs[qid] for qid in ids1]
        # R1: 2x GK/Bharat d1-2; R2: 2x Bollywood/Cricket d2-3; R3: 2x Cricket/Ganit d2-4
        # FINAL: 1x GK/Bharat d3
        self.assertEqual(len(sel), 7)
        self.assertEqual(len(set(ids1)), 7)
        for q in sel[0:2]:
            self.assertEqual(q["category"], "GK/Bharat")
            self.assertTrue(1 <= q["difficulty"] <= 2)
        for q in sel[2:4]:
            self.assertIn(q["category"], {"Bollywood", "Cricket"})
            self.assertTrue(2 <= q["difficulty"] <= 3)
        for q in sel[4:6]:
            self.assertIn(q["category"], {"Cricket", "Ganit"})
            self.assertTrue(2 <= q["difficulty"] <= 4)
        self.assertEqual(sel[6]["category"], "GK/Bharat")
        self.assertEqual(sel[6]["difficulty"], 3)

    async def test_r2_code_mix_zone_is_hinglish_even_in_english_session(self):
        h = self.harness(language="en", cast=TEST_CAST)
        await h.start()
        await self.wait_question(h, 7, timeout=15)
        frames = h.of("question")
        r2 = frames[2:4]
        for q in r2:
            self.assertEqual(q["language"], "hi-IN")   # code-mix hero zone
        self.assertEqual(h.sess.mode, "demo")

    async def test_next_round_ack_ends_game(self):
        h = self.harness(cast=TEST_CAST)
        await h.start()
        for i in range(1, 8):
            await self.play_question(h, i)
            await self.wait_verdict(h, i)
        await self.wait_phase(h, "final")
        ends_before = len(h.of("audio_end"))
        await h.sess.handle({"type": "next_round"})
        await self.wait_until(lambda: len(h.of("audio_end")) > ends_before, msg="outro speech")
        outro = h.of("audio_start")[-1]
        self.assertIn("BhashaBuzzer", outro["caption"])
        self.assertEqual(h.last_state()["phase"], "final")
        await h.sess.handle({"type": "next_round"})
        errors = h.of("error")
        self.assertEqual(errors[-1]["code"], "already_over")


# -------------------------------------------------------- frame conformance


class TestFrameConformance(GameTest):
    def check_audio_utterances(self, h):
        frames = h.frames
        starts = [f for f in frames if f["type"] == "audio_start"]
        start_ids = [f["utterance_id"] for f in starts]
        self.assertEqual(len(start_ids), len(set(start_ids)), "utterance ids must be unique")
        for i, uid in enumerate(start_ids):
            self.assertRegex(uid, r"^u\d+$")
            self.assertEqual(uid, f"u{i + 1}")            # u+sequence from 1
        positions = {id(f): i for i, f in enumerate(frames)}
        for start in starts:
            uid = start["utterance_id"]
            for field in ("utterance_id", "speaker", "sample_rate", "codec", "caption"):
                self.assertIn(field, start)
            self.assertEqual(start["sample_rate"], 24000)
            self.assertEqual(start["codec"], "wav")
            s_idx = positions[id(start)]
            chunk_idxs = [i for i, f in enumerate(frames)
                          if f["type"] == "audio_chunk" and f["utterance_id"] == uid]
            end_idxs = [i for i, f in enumerate(frames)
                        if f["type"] == "audio_end" and f["utterance_id"] == uid]
            self.assertTrue(chunk_idxs, f"no chunks for {uid}")
            self.assertEqual(len(end_idxs), 1, f"expected exactly one audio_end for {uid}")
            e_idx = end_idxs[0]
            self.assertLess(s_idx, chunk_idxs[0], "audio_start must precede chunks")
            self.assertLess(chunk_idxs[-1], e_idx, "audio_end must follow all chunks")
            for field in ("utterance_id", "ttfb_ms", "total_ms", "chars", "cache_hit",
                         "source", "stt_ms", "judge_ms", "e2e_ms"):
                self.assertIn(field, frames[e_idx])
            # first chunk is exactly the 44-byte RIFF header
            first = base64.b64decode(frames[chunk_idxs[0]]["b64"])
            self.assertEqual(len(first), 44, f"{uid} first chunk must be 44 bytes")
            self.assertTrue(first.startswith(b"RIFF"), f"{uid} first chunk must be RIFF")
            for i in chunk_idxs:
                base64.b64decode(frames[i]["b64"])       # every chunk decodes

    async def test_demo_match_frames_conform(self):
        h = self.harness(cast=TEST_CAST)
        await h.start()
        for i in range(1, 8):
            await self.play_question(h, i)
            await self.wait_verdict(h, i)
        await self.wait_phase(h, "final")
        await h.sess.handle({"type": "next_round"})
        await self.wait_until(lambda: any("BhashaBuzzer" in f["caption"]
                                          for f in h.of("audio_start")), msg="outro")

        self.check_audio_utterances(h)

        for q in h.of("question"):
            self.assertEqual(set(q), {"type", "id", "text", "category", "difficulty", "language"})
        for s in h.states():
            self.assertEqual(set(s), {"type", "phase", "round", "question_index", "score",
                                     "streak", "speaker", "persona_label", "mode"})
        for v in h.verdicts():
            self.assertEqual(set(v), {"type", "utterance_id", "correct", "close_call", "heard",
                                      "template_id", "hype_line", "fact_line", "score_delta",
                                      "score", "streak"})
            # the reaction audio for each verdict starts right after, with the same uid
            self.assertIn(v["utterance_id"], [f["utterance_id"] for f in h.of("audio_start")])
        for e in h.of("error"):
            self.assertEqual(set(e), {"type", "phase", "code", "recoverable", "message"})
        # every verdict precedes its reaction audio_start
        frames = h.frames
        for v in h.verdicts():
            v_idx = next(i for i, f in enumerate(frames) if f is v)
            s_idx = next(i for i, f in enumerate(frames)
                         if f["type"] == "audio_start" and f["utterance_id"] == v["utterance_id"])
            self.assertLess(v_idx, s_idx)

    async def test_speaker_ids_in_state_frames_follow_cast(self):
        h = self.harness(cast=TEST_CAST)
        await h.start()
        await self.wait_question(h, 7, timeout=15)
        by_round = {}
        for s in h.states():
            if s["question_index"] >= 0:
                by_round.setdefault(s["round"], s)
        self.assertEqual(by_round[1]["speaker"], "ratan_hi_customer_expressive")
        self.assertEqual(by_round[2]["speaker"], "ishita_enhi_customer_expressive")
        self.assertEqual(by_round[3]["speaker"], "ratan_hi_sports")
        self.assertEqual(by_round[4]["speaker"], "mukul_hi_suspense")
        speakers = [c[0] for c in h.voice.set_calls]
        self.assertEqual(speakers, ["ratan_hi_customer_expressive", "ishita_enhi_customer_expressive",
                                    "ratan_hi_sports", "mukul_hi_suspense"])


# ------------------------------------------------------------ skip / repeat


class TestSkipRepeatBarge(GameTest):
    async def test_skip_resets_streak_no_penalty_no_verdict(self):
        ids = demo_ids()
        script = {ids[0]: {"verdict": "correct", "template_id": "correct_fast"},
                  ids[2]: {"verdict": "correct", "template_id": "correct_fast"}}
        h = self.harness(judge=FakeJudge(script=script), cast=TEST_CAST)
        await h.start()
        await self.play_question(h, 1, clock_bump=0.3)     # correct -> streak 1, score 150
        await self.wait_verdict(h, 1)
        await self.wait_question(h, 2)
        await h.sess.handle({"type": "skip_question"})     # skip Q2
        await self.wait_question(h, 3)
        self.assertEqual(len(h.verdicts()), 1)             # no verdict frame on skip
        # streak reset visible in state frames after the skip
        post_skip = [s for s in h.states()
                     if s["question_index"] == 2]
        self.assertEqual(post_skip[0]["streak"], 0)
        self.assertEqual(post_skip[0]["score"], 150)       # zero penalty
        # Q3 correct: streak 1 -> (150+50) x 1 = 200 (would be 300 if skip kept the streak)
        await self.play_question(h, 3, clock_bump=0.3)
        await self.wait_verdict(h, 2)
        self.assertEqual(h.verdicts()[-1]["score_delta"], 200)
        self.assertEqual(h.verdicts()[-1]["score"], 350)

    async def test_skip_from_listening_phase(self):
        h = self.harness(cast=TEST_CAST)
        await h.start()
        await self.wait_question(h, 1)
        self.ft.t += 0.3
        await h.sess.handle({"type": "buzz"})
        await h.sess.handle({"type": "skip_question"})
        await self.wait_question(h, 2)
        self.assertEqual(len(h.verdicts()), 0)

    async def test_repeat_question_respeaks_with_new_utterance_id(self):
        h = self.harness(cast=TEST_CAST)
        await h.start()
        q1 = await self.wait_question(h, 1)
        await self.wait_until(lambda: [f for f in h.of("audio_start")
                                       if f["caption"] == q1["text"]], msg="question speech")
        original_uid = next(f["utterance_id"] for f in h.of("audio_start")
                            if f["caption"] == q1["text"])
        speaks_before = len(h.voice.speak_calls)
        await h.sess.handle({"type": "repeat_question"})
        await self.wait_until(lambda: len([f for f in h.of("audio_start")
                                          if f["caption"] == q1["text"]]) >= 2,
                              msg="repeat speech")
        repeat_uid = next(f["utterance_id"] for f in h.of("audio_start")
                          if f["caption"] == q1["text"] and f["utterance_id"] != original_uid)
        self.assertNotEqual(repeat_uid, original_uid)      # new utterance id
        # same text re-spoken through the voice channel
        self.assertEqual(len(h.voice.speak_calls), speaks_before + 1)
        self.assertEqual(h.voice.speak_calls[-1][0], q1["text"])
        self.assertEqual(h.voice.speak_calls[-1][1], repeat_uid)
        # still in the question phase
        self.assertEqual(h.last_state()["phase"], "question")

    async def test_barge_drops_pending_chunks(self):
        voice = FakeVoice(n_pcm=50, chunk_delay=0.01)      # ~500 ms of streaming
        h = self.harness(voice=voice, cast=TEST_CAST)
        await h.start()
        q1 = await self.wait_question(h, 1)
        uid = await self.wait_until(lambda: next((f["utterance_id"] for f in h.of("audio_start")
                                                 if f["caption"] == q1["text"]), None),
                                    msg="question audio start")
        await self.wait_until(lambda: len([f for f in h.of("audio_chunk")
                                           if f["utterance_id"] == uid]) >= 2,
                              msg="streaming chunks")
        await h.sess.handle({"type": "barge"})
        count = len([f for f in h.of("audio_chunk") if f["utterance_id"] == uid])
        await asyncio.sleep(0.3)                            # give the stream time to misbehave
        count_after = len([f for f in h.of("audio_chunk") if f["utterance_id"] == uid])
        self.assertLessEqual(count_after, count + 1)        # pending chunks dropped
        self.assertLess(count_after, 51)                    # definitely not the full stream
        end = next(f for f in h.of("audio_end") if f["utterance_id"] == uid)
        self.assertTrue(end["cancelled"])
        self.assertEqual(h.last_state()["phase"], "question")  # barge alone: no phase change

    async def test_buzz_barges_host_speech_and_opens_listening(self):
        voice = FakeVoice(n_pcm=50, chunk_delay=0.01)
        h = self.harness(voice=voice, cast=TEST_CAST)
        await h.start()
        q1 = await self.wait_question(h, 1)
        uid = await self.wait_until(lambda: next((f["utterance_id"] for f in h.of("audio_start")
                                                 if f["caption"] == q1["text"]), None),
                                    msg="question audio start")
        await self.wait_until(lambda: len([f for f in h.of("audio_chunk")
                                           if f["utterance_id"] == uid]) >= 2,
                              msg="streaming chunks")
        self.ft.t += 0.4
        await h.sess.handle({"type": "buzz"})
        self.assertEqual(h.last_state()["phase"], "listening")
        end = await self.wait_until(lambda: next((f for f in h.of("audio_end")
                                                  if f["utterance_id"] == uid and f["cancelled"]),
                                                 None), msg="cancelled audio_end")
        self.assertTrue(end["cancelled"])
        # answering works right after the barge-in
        await h.sess.handle({"type": "answer", "text": "jawaab"})
        await self.wait_verdict(h, 1)
        self.assertEqual(h.verdicts()[0]["score_delta"], 150)   # Q1: (100+50) x 1


# -------------------------------------------------------- unclear grace etc.


class TestUnclearGrace(GameTest):
    async def test_first_unclear_gets_one_grace_repeat_then_moves_on(self):
        ids = demo_ids()
        calls = {"n": 0}

        def q2_unclear(heard):
            calls["n"] += 1
            return {"verdict": "unclear", "close_call": False,
                    "template_id": "repeat_request", "hype_line": ""}

        script = {ids[0]: {"verdict": "correct", "template_id": "correct_fast"},
                  ids[1]: q2_unclear,
                  ids[2]: {"verdict": "correct", "template_id": "correct_fast"}}
        h = self.harness(judge=FakeJudge(script=script), cast=TEST_CAST)
        await h.start()
        await self.play_question(h, 1, clock_bump=0.3)     # correct -> streak 1
        await self.wait_verdict(h, 1)
        await self.wait_question(h, 2)
        self.ft.t += 0.3
        await h.buzz_answer(text="kya?")                    # 1st unclear
        v1 = await self.wait_verdict(h, 2)
        self.assertEqual(v1["correct"], "unclear")
        self.assertEqual(v1["score_delta"], 0)             # no penalty
        self.assertEqual(v1["streak"], 1)                   # streak-neutral
        # host asks to repeat and the answer window re-opens
        await self.wait_until(lambda: h.phases()[-1] == "listening", msg="grace listening")
        repeat_speech = [f for f in h.of("audio_start") if f["utterance_id"] == v1["utterance_id"]]
        self.assertTrue(repeat_speech)
        self.assertIn("बोलिए", repeat_speech[0]["caption"])
        await h.sess.handle({"type": "answer", "text": "phir kya?"})   # 2nd unclear
        v2 = await self.wait_verdict(h, 3)
        self.assertEqual(v2["correct"], "unclear")
        self.assertEqual(v2["score_delta"], 0)
        self.assertEqual(v2["streak"], 1)                    # still streak-neutral
        self.assertEqual(calls["n"], 2)
        await self.wait_question(h, 3)                       # moved on to Q3
        # streak survived both unclears: Q3 correct -> streak 2 -> (150+50) x 1.5 = 300
        await self.play_question(h, 3, clock_bump=0.3)
        v3 = await self.wait_verdict(h, 4)
        self.assertEqual(v3["score_delta"], 300)
        self.assertEqual(v3["streak"], 2)
        self.assertEqual(len(h.judge.calls), 4)

    async def test_grace_repeat_then_correct_answer(self):
        ids = demo_ids()
        state = {"n": 0}

        def unclear_then_correct(heard):
            state["n"] += 1
            if state["n"] == 1:
                return {"verdict": "unclear", "close_call": False, "template_id": "unclear",
                        "hype_line": ""}
            return {"verdict": "correct", "close_call": False, "template_id": "correct_fast",
                    "hype_line": ""}

        h = self.harness(judge=FakeJudge(script={ids[0]: unclear_then_correct}), cast=TEST_CAST)
        await h.start()
        await self.wait_question(h, 1)
        self.ft.t += 0.3
        await h.buzz_answer(text="hmm")
        v1 = await self.wait_verdict(h, 1)
        self.assertEqual(v1["correct"], "unclear")
        await self.wait_until(lambda: h.phases()[-1] == "listening", msg="grace listening")
        self.ft.t += 0.3
        await h.sess.handle({"type": "answer", "text": "Dhoni"})
        v2 = await self.wait_verdict(h, 2)
        self.assertEqual(v2["correct"], "correct")
        self.assertEqual(v2["score_delta"], 150)             # (100+50) x 1, streak 1
        self.assertEqual(v2["streak"], 1)

    async def test_empty_answer_skips_judge_directly(self):
        h = self.harness(cast=TEST_CAST)
        await h.start()
        await self.wait_question(h, 1)
        self.ft.t += 0.3
        await h.buzz_answer(text="   ")                      # empty/garbage -> unclear, no judge call
        v = await self.wait_verdict(h, 1)
        self.assertEqual(v["correct"], "unclear")
        self.assertEqual(len(h.judge.calls), 0)
        await self.wait_until(lambda: h.phases()[-1] == "listening", msg="grace listening")


# ----------------------------------------------------------------- timeouts


class TestTimeouts(GameTest):
    async def test_answer_window_timeout_is_no_answer_verdict(self):
        h = self.harness(timings={"answer_window": 0.05}, cast=TEST_CAST)
        await h.start()
        await self.wait_question(h, 1)
        self.ft.t += 0.3
        await h.sess.handle({"type": "buzz"})
        # no answer arrives; window elapses
        v = await self.wait_verdict(h, 1, timeout=5)
        self.assertEqual(v["correct"], "no_answer")
        self.assertEqual(v["template_id"], "no_answer")
        self.assertEqual(v["score_delta"], 0)
        self.assertEqual(v["heard"], "")
        await self.wait_question(h, 2)                       # moved on

    async def test_answer_timeout_resets_streak(self):
        ids = demo_ids()
        script = {ids[0]: {"verdict": "correct", "template_id": "correct_fast"}}
        h = self.harness(judge=FakeJudge(script=script), timings={"answer_window": 0.05},
                         cast=TEST_CAST)
        await h.start()
        await self.play_question(h, 1, clock_bump=0.3)       # correct -> streak 1
        await self.wait_verdict(h, 1)
        await self.wait_question(h, 2)
        self.ft.t += 0.3
        await h.sess.handle({"type": "buzz"})
        v = await self.wait_verdict(h, 2, timeout=5)
        self.assertEqual(v["correct"], "no_answer")
        self.assertEqual(v["streak"], 0)

    async def test_buzz_window_timeout_is_no_answer_verdict(self):
        h = self.harness(timings={"buzz_window": 0.05}, cast=TEST_CAST)
        await h.start()
        await self.wait_question(h, 1)
        v = await self.wait_verdict(h, 1, timeout=5)         # no buzz ever arrives
        self.assertEqual(v["correct"], "no_answer")
        self.assertEqual(v["template_id"], "timeout")
        self.assertEqual(v["score_delta"], 0)
        self.assertEqual(v["streak"], 0)
        await self.wait_question(h, 2)


# ---------------------------------------------------------------- daily 5


class TestDaily5(GameTest):
    async def play_daily5(self, h):
        for i in range(1, 6):
            await self.play_question(h, i, clock_bump=0.3)
            await self.wait_verdict(h, i)
        await self.wait_phase(h, "final")

    async def test_daily5_shape_categories_bases_and_persona(self):
        h = self.harness(mode="daily5", cast=TEST_CAST, language="hi")
        await h.start()
        await self.play_daily5(h)
        self.assertEqual(h.qids(), [f["id"] for f in h.sess.selected])
        self.assertEqual([q["category"] for q in h.sess.selected],
                         ["Cricket", "GK/Bharat", "Ganit", "Bollywood", "Vigyan"])
        self.assertEqual(len(set(h.qids())), 5)
        # bases 100/100/150/150/200, all correct fast: streaks 1..5
        self.assertEqual([v["score_delta"] for v in h.verdicts()],
                         [150, 225, 400, 500, 625])         # (100+50)x1, x1.5, (150+50)x2, x2.5, (200+50)x2.5
        self.assertEqual([v["streak"] for v in h.verdicts()], [1, 2, 3, 4, 5])
        self.assertEqual(h.last_state()["score"], 1900)
        self.assertEqual(h.last_state()["mode"], "daily5")
        # Q5 is hosted by the final (suspense) persona
        q5_state = [s for s in h.states() if s["question_index"] == 4][0]
        self.assertEqual(q5_state["speaker"], "mukul_hi_suspense")
        self.assertIn(("mukul_hi_suspense", "hi-IN", 0.9), h.voice.set_calls)

    async def test_daily5_date_seeded_determinism(self):
        pairs = []
        for date in ["2026-10-13", "2026-10-13", "2026-10-14", "2026-10-15",
                     "2026-10-16", "2026-10-17", "2026-10-18", "2026-10-19"]:
            h = self.harness(mode="daily5", cast=TEST_CAST)
            await h.start(date=date)
            await self.wait_question(h, 1)
            pairs.append((date, [q["id"] for q in h.sess.selected]))
        same_date = [sel for date, sel in pairs if date == "2026-10-13"]
        self.assertEqual(same_date[0], same_date[1])         # same date => identical set
        bank_ids = {q["id"] for q in game.load_questions()}
        for _, sel in pairs:
            self.assertEqual(len(sel), 5)
            self.assertTrue(set(sel) <= bank_ids)
        distinct = {tuple(sel) for _, sel in pairs}
        self.assertGreater(len(distinct), 1, "different dates must yield different sets")

    async def test_same_date_two_sessions_identical(self):
        h1 = self.harness(mode="daily5", cast=TEST_CAST)
        await h1.start(date="2026-10-13")
        await self.wait_question(h1, 1)
        h2 = self.harness(mode="daily5", cast=TEST_CAST)
        await h2.start(date="2026-10-13")
        await self.wait_question(h2, 1)
        self.assertEqual([q["id"] for q in h1.sess.selected],
                         [q["id"] for q in h2.sess.selected])
        # and the selection actually comes from the bank
        bank_ids = {q["id"] for q in game.load_questions()}
        self.assertTrue({q["id"] for q in h1.sess.selected} <= bank_ids)


# -------------------------------------------------------------- audio answers


class TestAudioAnswers(GameTest):
    async def test_audio_answer_flows_through_stt_to_judge(self):
        keyterms_seen = []

        async def stt_fn(wav_bytes, keyterms):
            keyterms_seen.append((wav_bytes, list(keyterms)))
            return {"text": "Mahi"}

        judge = FakeJudge()
        h = self.harness(judge=judge, stt_fn=stt_fn, cast=TEST_CAST)
        await h.start()
        await self.wait_question(h, 1)
        self.ft.t += 0.3
        wav = RIFF_HEADER + PCM_CHUNK * 10
        await h.sess.handle({"type": "buzz"})
        await h.sess.handle({"type": "answer", "audio_b64": base64.b64encode(wav).decode()})
        v = await self.wait_verdict(h, 1)
        self.assertEqual(judge.calls[0]["heard"], "Mahi")
        self.assertEqual(v["heard"], "Mahi")
        self.assertEqual(keyterms_seen[0][0], wav)
        self.assertEqual(keyterms_seen[0][1], judge.calls[0]["question"]["keyterms"])

    async def test_stt_failure_takes_unclear_ladder(self):
        async def stt_fn(wav_bytes, keyterms):
            raise RuntimeError("stt down")

        h = self.harness(stt_fn=stt_fn, cast=TEST_CAST)
        await h.start()
        await self.wait_question(h, 1)
        self.ft.t += 0.3
        await h.sess.handle({"type": "buzz"})
        await h.sess.handle({"type": "answer",
                             "audio_b64": base64.b64encode(RIFF_HEADER).decode()})
        v = await self.wait_verdict(h, 1)
        self.assertEqual(v["correct"], "unclear")
        self.assertEqual(len(h.judge.calls), 0)

    async def test_answer_with_both_fields_is_rejected(self):
        h = self.harness(cast=TEST_CAST)
        await h.start()
        await self.wait_question(h, 1)
        self.ft.t += 0.3
        await h.sess.handle({"type": "buzz"})
        await h.sess.handle({"type": "answer", "text": "x",
                             "audio_b64": base64.b64encode(RIFF_HEADER).decode()})
        err = h.of("error")[-1]
        self.assertEqual(err["code"], "bad_answer")
        self.assertTrue(err["recoverable"])
        self.assertEqual(h.phases()[-1], "listening")        # window stays open

    async def test_answer_with_bad_base64_is_rejected(self):
        h = self.harness(cast=TEST_CAST)
        await h.start()
        await self.wait_question(h, 1)
        self.ft.t += 0.3
        await h.sess.handle({"type": "buzz"})
        await h.sess.handle({"type": "answer", "audio_b64": "!!!not-base64!!!"})
        self.assertEqual(h.of("error")[-1]["code"], "bad_answer")
        self.assertEqual(h.phases()[-1], "listening")


# --------------------------------------------------------------- error paths


class TestErrorPaths(GameTest):
    async def test_frames_before_start_are_rejected(self):
        h = self.harness(cast=TEST_CAST)
        for msg in [{"type": "buzz"}, {"type": "answer", "text": "x"},
                    {"type": "skip_question"}, {"type": "repeat_question"},
                    {"type": "next_round"}]:
            await h.sess.handle(msg)
            self.assertEqual(h.of("error")[-1]["code"], "invalid_phase")
        for err in h.of("error"):
            self.assertTrue(err["recoverable"])

    async def test_start_validation(self):
        h = self.harness(cast=TEST_CAST)
        await h.sess.handle({"type": "start", "player_name": "A", "language": "hi"})
        self.assertEqual(h.of("error")[-1]["code"], "bad_player_name")
        await h.sess.handle({"type": "start", "player_name": "x" * 21, "language": "hi"})
        self.assertEqual(h.of("error")[-1]["code"], "bad_player_name")
        await h.sess.handle({"type": "start", "player_name": "Ashu", "language": "fr"})
        self.assertEqual(h.of("error")[-1]["code"], "bad_language")
        await h.sess.handle({"type": "start", "player_name": "Ashu", "language": "hi",
                             "mode": "marathon"})
        self.assertEqual(h.of("error")[-1]["code"], "bad_mode")
        self.assertEqual(len(h.of("state")), 0)              # nothing started
        await h.sess.handle({"type": "start", "player_name": "Ashu", "language": "hi"})
        await self.wait_question(h, 1)
        self.assertEqual(len(h.of("error")), 4)

    async def test_answer_outside_listening_is_rejected(self):
        h = self.harness(cast=TEST_CAST)
        await h.start()
        await self.wait_question(h, 1)
        await h.sess.handle({"type": "answer", "text": "jaldi"})   # no buzz yet
        self.assertEqual(h.of("error")[-1]["code"], "invalid_phase")
        await h.sess.handle({"type": "buzz"})
        await h.sess.handle({"type": "answer", "text": "theek"})
        await self.wait_verdict(h, 1)
        await h.sess.handle({"type": "answer", "text": "dobara"})  # verdict phase now
        self.assertEqual(h.of("error")[-1]["code"], "invalid_phase")

    async def test_unknown_frame_and_ping(self):
        h = self.harness(cast=TEST_CAST)
        await h.start()
        await self.wait_question(h, 1)
        n = len(h.frames)
        await h.sess.handle({"type": "ping"})                # liveness: silently ignored
        await h.sess.handle({"type": "wat"})
        self.assertEqual(h.of("error")[-1]["code"], "unknown_frame")
        self.assertEqual(len(h.frames), n + 1)               # only the error frame
        await h.sess.handle("not a dict")
        self.assertEqual(h.of("error")[-1]["code"], "bad_frame")

    async def test_restart_mid_game(self):
        h = self.harness(cast=TEST_CAST)
        await h.start()
        await self.play_question(h, 1)
        await self.wait_verdict(h, 1)
        first_ids = [q["id"] for q in h.sess.selected]
        questions_before = len(h.of("question"))
        await h.sess.handle({"type": "start", "player_name": "Naya", "language": "hi"})
        await self.wait_until(lambda: len(h.of("question")) > questions_before,
                              msg="restarted first question")
        self.assertEqual(h.of("question")[-1]["id"], first_ids[0])   # same deterministic arc
        self.assertEqual(h.last_state()["phase"], "question")
        self.assertEqual(h.last_state()["score"], 0)                 # fresh scoreboard


# ------------------------------------------------ fix-language (Agent B)

def _has_devanagari(s: str) -> bool:
    return any("\u0900" <= ch <= "\u097F" for ch in s)


def _has_latin(s: str) -> bool:
    return any(("a" <= ch <= "z") or ("A" <= ch <= "Z") for ch in s)


class PaceVoice(FakeVoice):
    """FakeVoice + the optional set_pace hook the engine uses for pacing."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.pace_calls = []

    def set_pace(self, pace):
        self.pace_calls.append(float(pace))


class TestFixLanguagePacing(GameTest):
    """Question reads are conversational (1.0); bank hints reach the voice
    clamped into the calm band [0.85, 1.05]."""

    async def test_pace_clamped_and_question_reads_conversational(self):
        fast_cast = {n: dict(entry, pace=1.4) for n, entry in TEST_CAST.items()}
        voice = PaceVoice()
        h = self.harness(voice=voice, cast=fast_cast)
        await h.start()
        await self.wait_question(h, 1)
        await self.wait_until(lambda: len(voice.pace_calls) >= 4, msg="4 paced utterances")
        # greeting, rules, round intro at the clamped persona pace; then the
        # question read is always 1.0 no matter how hot the persona is
        self.assertEqual(voice.pace_calls, [1.05, 1.05, 1.05, 1.0])

    async def test_reaction_applies_calmed_bank_pace_hint(self):
        bank = {"correct_fast": [{"text": "वाह! बिजली की रफ़्तार!", "pace_hint": 1.15,
                                  "role": "celebrate"}]}
        voice = PaceVoice()
        h = self.harness(voice=voice, cast=TEST_CAST, bank=bank)
        await h.start()
        await self.play_question(h, 1, clock_bump=0.3)      # fast buzz -> correct_fast
        await self.wait_verdict(h, 1)
        await self.wait_until(lambda: h.sess._last_reaction_pace == 1.15,
                              msg="bank hint stashed")
        self.assertEqual(h.sess.reaction_pace(), 1.05)       # clamped for synthesis
        await self.wait_until(lambda: 1.05 in voice.pace_calls, msg="reaction paced")
        self.assertTrue(all(p <= 1.05 for p in voice.pace_calls))

    async def test_repeat_question_stays_conversational(self):
        voice = PaceVoice()
        h = self.harness(voice=voice, cast=TEST_CAST)
        await h.start()
        q1 = await self.wait_question(h, 1)
        await self.wait_until(lambda: any(f["caption"] == q1["text"]
                                          for f in h.of("audio_start")),
                              msg="question speech")
        await h.sess.handle({"type": "repeat_question"})
        await self.wait_until(lambda: voice.pace_calls.count(1.0) >= 2,
                              msg="repeat read paced 1.0")


class TestFixLanguageBankAndCast(GameTest):
    """The per-language cast rides inside the bank; bank variants cycle."""

    async def test_cast_rides_inside_bank_and_is_popped(self):
        cast = {n: dict(entry, speaker=f"spk_{n}") for n, entry in TEST_CAST.items()}
        bank = {"cast": cast, "greeting": "नमस्ते {name}!"}
        h = self.harness(bank=bank)
        await h.start()
        await self.wait_question(h, 1)
        self.assertEqual(h.sess.cast[2]["speaker"], "spk_2")
        self.assertNotIn("cast", h.sess.bank)
        self.assertEqual(h.voice.set_calls[0][0], "spk_1")

    async def test_reaction_bank_variants_cycle_with_hints(self):
        bank = {"correct_fast": [{"text": "पहला", "pace_hint": 1.05, "role": "celebrate"},
                                 {"text": "दूसरा", "pace_hint": 1.0, "role": "celebrate"}]}
        h = self.harness(cast=TEST_CAST, bank=bank)
        t1, p1 = h.sess._bank_entry("correct_fast")
        t2, p2 = h.sess._bank_entry("correct_fast")
        t3, p3 = h.sess._bank_entry("correct_fast")
        self.assertEqual((t1, t2, t3), ("पहला", "दूसरा", "पहला"))
        self.assertEqual((p1, p2, p3), (1.05, 1.0, 1.05))

    async def test_string_list_bank_keeps_first_text_semantics(self):
        h = self.harness(cast=TEST_CAST, bank={"correct_fast": ["एक", "दो"]})
        self.assertEqual(h.sess._bank_line("correct_fast"), "एक")
        self.assertEqual(h.sess._bank_line("correct_fast"), "एक")

    async def test_prefix_fallback_still_matches(self):
        h = self.harness(cast=TEST_CAST, bank={"correct_fast_2": "दो नंबर"})
        self.assertEqual(h.sess._bank_line("correct_fast"), "दो नंबर")


class TestFixLanguageSessionDefaults(GameTest):
    """en/mr sessions get their own script-matched host copy; hi unchanged."""

    async def test_en_session_host_copy_is_english(self):
        h = self.harness(language="en", cast=TEST_CAST)
        await h.start()
        await self.wait_question(h, 1)
        caps = [f["caption"] for f in h.of("audio_start")]
        self.assertTrue(caps)
        for cap in caps:                       # no Devanagari reaches en-IN personas
            self.assertFalse(_has_devanagari(cap), cap)
        self.assertIn("BhashaBuzzer", caps[0])
        q1 = h.of("question")[0]["text"]
        self.assertIn(q1, caps)                # question reads speak the question

    async def test_mr_session_host_copy_is_marathi(self):
        h = self.harness(language="mr", cast=TEST_CAST)
        await h.start()
        await self.wait_question(h, 1)
        caps = [f["caption"] for f in h.of("audio_start")]
        self.assertTrue(caps)
        self.assertTrue(_has_devanagari(caps[0]))
        self.assertIn("BhashaBuzzer", caps[0])

    async def test_hi_session_host_copy_unchanged(self):
        h = self.harness(cast=TEST_CAST)
        await h.start()
        await self.wait_question(h, 1)
        caps = [f["caption"] for f in h.of("audio_start")]
        self.assertEqual(caps[0], game.DEFAULT_LINES["greeting"].replace("{name}", "Ashu"))
        self.assertTrue(_has_devanagari(caps[1]))


if __name__ == "__main__":
    unittest.main()
