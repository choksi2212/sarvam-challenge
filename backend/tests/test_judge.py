"""Tests for backend/judge.py — stdlib unittest + httpx.MockTransport ONLY.

NO live Sarvam API calls anywhere: every LLM-path test injects a
MockTransport; every fallback test never touches the network. The API key
getter is patched in every judge_answer test, so nothing here can reach the
real endpoint even if the host machine has SARVAM_API_KEY set.

Run from the worktree root:
    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

import httpx

try:
    import judge
except ImportError:  # pragma: no cover — package-style import (backend.judge)
    from backend import judge


# ------------------------------------------------------------------- fixtures

# Binding enum, CONTRACT §3 personas block (A5) — judge must return from THIS list only.
CONTRACT_TEMPLATE_IDS = [
    "correct_fast", "correct_confident", "correct_slow", "wrong_confident",
    "wrong_funny", "wrong_close", "partial", "unclear", "timeout",
    "no_answer", "repeat_request", "streak_3", "streak_5", "final_win",
    "final_lose", "game_over",
]

# Bank excerpts (research/plans/questions-draft.json) — kept verbatim.
CRICKET_01 = {
    "id": "cricket-01", "category": "Cricket", "difficulty": 2, "language": "hi-IN",
    "question_text": "2011 के Cricket World Cup final में India की captain कौन थी?",
    "expected_answer": "MS Dhoni",
    "accepted_answer_aliases": ["Dhoni", "MS Dhoni", "महेंद्र सिंह धोनी", "धोनी",
                                 "Mahendra Singh Dhoni", "Mahi", "Captain Cool", "माही"],
}
CRICKET_12 = {
    "id": "cricket-12", "category": "Cricket", "difficulty": 5, "language": "hi-IN",
    "question_text": "1983 World Cup में Kapil Dev का famous 175 not out किस team के against आया था?",
    "expected_answer": "Zimbabwe",
    "accepted_answer_aliases": ["Zimbabwe", "ज़िम्बाब्वे", "जिम्बाब्वे", "झिम्बाब्वे",
                                 "zimbawe", "zim"],
}
GK_05 = {
    "id": "gk-05", "category": "GK/Bharat", "difficulty": 2, "language": "en-IN",
    "question_text": "On which date did the Constitution of India come into force?",
    "expected_answer": "26 January 1950",
    "accepted_answer_aliases": ["26 January 1950", "26 January", "26 जनवरी 1950",
                                 "26 जनवरी", "26th January 1950", "Republic Day",
                                 "republic day", "26/1/1950",
                                 "twenty six January nineteen fifty"],
}
GK_09 = {
    "id": "gk-09", "category": "GK/Bharat", "difficulty": 4, "language": "hi-IN",
    "question_text": "भारत की सबसे ऊँची peak Kangchenjunga कितने metres ऊँची है?",
    "expected_answer": "8,586 metres",
    "accepted_answer_aliases": ["8586", "8,586", "8586 metres", "8586 meters",
                                 "8586 m", "आठ हज़ार पाँच सौ छियासी",
                                 "eight thousand five hundred eighty six",
                                 "28,169 feet", "28169 feet"],
}
MATH_06 = {
    "id": "math-06", "category": "Ganit", "difficulty": 2, "language": "hi-IN",
    "question_text": "Aap के पास ₹10,000 थे. ₹3,500 का shopping करने के बाद कितने rupees बचे?",
    "expected_answer": "₹6,500",
    "accepted_answer_aliases": ["6500", "6,500", "₹6500", "₹6,500", "6500 rupees",
                                 "six thousand five hundred", "छह हज़ार पाँच सौ",
                                 "छह हजार पांच सौ", "6500 rupaye"],
}
BOLLY_12 = {
    "id": "bolly-12", "category": "Bollywood", "difficulty": 5, "language": "hi-IN",
    "question_text": "Oscar award जीतने वाले first Indian कौन थे?",
    "expected_answer": "Bhanu Athaiya",
    "accepted_answer_aliases": ["Bhanu Athaiya", "भानु अथैया", "भानू अथैया",
                                 "Bhanu Athiya", "bhanu athaiya"],
}
SCI_06 = {
    "id": "sci-06", "category": "Vigyan", "difficulty": 3, "language": "en-IN",
    "question_text": "Which gas makes up about 78 percent of Earth's atmosphere?",
    "expected_answer": "Nitrogen",
    "accepted_answer_aliases": ["Nitrogen", "nitrogen", "नाइट्रोजन", "नैट्रोजन", "N2"],
}
# Synthetic fixtures for script-bridge and near-miss coverage.
VIRAT = {
    "id": "test-virat", "language": "en-IN",
    "question_text": "Who holds the record for most centuries in international cricket?",
    "expected_answer": "Virat Kohli",
    "accepted_answer_aliases": ["Virat Kohli", "विराट कोहली", "Virat", "Kohli", "विराट"],
}
VIRAT_DEVANAGARI_ONLY = {
    "id": "test-virat-dev", "language": "hi-IN",
    "question_text": "अंतरराष्ट्रीय cricket में सबसे ज़्यादा centuries किसके हैं?",
    "expected_answer": "विराट कोहली",
    "accepted_answer_aliases": ["विराट कोहली", "विराट"],
}
CAPITAL = {
    "id": "test-capital", "language": "hi-IN",
    "question_text": "भारत की capital कौन सी है?",
    "expected_answer": "Delhi",
    "accepted_answer_aliases": ["Delhi", "दिल्ली", "dilli"],
}


def assert_valid_verdict(tc: unittest.TestCase, result: dict) -> None:
    tc.assertIsInstance(result, dict)
    tc.assertEqual(set(result), {"verdict", "close_call", "template_id", "hype_line"})
    tc.assertIn(result["verdict"], ("correct", "wrong", "partial", "unclear"))
    tc.assertIsInstance(result["close_call"], bool)
    tc.assertIn(result["template_id"], CONTRACT_TEMPLATE_IDS)
    tc.assertIsInstance(result["hype_line"], str)


# ------------------------------------------------------------------ normalize

class TestNormalizeAnswer(unittest.TestCase):
    def test_latin_lowercased_terminal_punct_stripped(self):
        self.assertEqual(judge.normalize_answer("Virat Kohli"), "virat kohli")
        self.assertEqual(judge.normalize_answer("Delhi?"), "delhi")
        self.assertEqual(judge.normalize_answer("delhi!!!"), "delhi")

    def test_devanagari_script_preserved(self):
        # Script equivalence rides the alias list + LLM — never fuzzy matching.
        self.assertEqual(judge.normalize_answer("विराट कोहली"), "विराट कोहली")
        self.assertEqual(judge.normalize_answer("दिल्ली।"), "दिल्ली")
        self.assertNotEqual(judge.normalize_answer("Virat Kohli"),
                            judge.normalize_answer("विराट कोहली"))

    def test_delhi_script_variants(self):
        self.assertEqual(judge.normalize_answer("Delhi"), "delhi")
        self.assertEqual(judge.normalize_answer("dilli"), "dilli")
        self.assertEqual(judge.normalize_answer("दिल्ली"), "दिल्ली")

    def test_comma_folding_indian_vs_western(self):
        self.assertEqual(judge.normalize_answer("5,00,000"), "500000")
        self.assertEqual(judge.normalize_answer("500,000"), "500000")
        self.assertEqual(judge.normalize_answer("8,586"), "8586")
        self.assertEqual(judge.normalize_answer("₹10,000"), "₹10000")
        self.assertEqual(judge.normalize_answer("3,00,000"),
                         judge.normalize_answer("300,000"))

    def test_devanagari_digits_to_ascii(self):
        self.assertEqual(judge.normalize_answer("३"), "3")
        self.assertEqual(judge.normalize_answer("५,००,०००"), "500000")
        self.assertEqual(judge.normalize_answer("२०११"), "2011")

    def test_word_numbers_to_digits(self):
        self.assertEqual(judge.normalize_answer("ek"), "1")
        self.assertEqual(judge.normalize_answer("aik"), "1")
        self.assertEqual(judge.normalize_answer("teen"), "3")
        self.assertEqual(judge.normalize_answer("तीन"), "3")
        self.assertEqual(judge.normalize_answer("das"), "10")
        self.assertEqual(judge.normalize_answer("दस"), "10")
        self.assertEqual(judge.normalize_answer("chhe"), "6")
        self.assertEqual(judge.normalize_answer("ek hazaar"), "1 hazaar")
        self.assertEqual(judge.normalize_answer("छह हज़ार पाँच सौ"), "6 हज़ार 5 सौ")
        # Bank word-form alias transforms symmetrically on both sides.
        self.assertEqual(judge.normalize_answer("one hundred forty four"),
                         judge.normalize_answer("one hundred forty four"))

    def test_fillers_and_honorifics_stripped(self):
        self.assertEqual(judge.normalize_answer("Dhoni ji"), "dhoni")
        self.assertEqual(judge.normalize_answer("धोनी जी"), "धोनी")
        self.assertEqual(judge.normalize_answer("Dhoni sir tha I think"), "dhoni")
        self.assertEqual(judge.normalize_answer("shayad Delhi..."), "delhi")
        self.assertEqual(judge.normalize_answer("maine Delhi bola"), "delhi bola")
        self.assertEqual(judge.normalize_answer("मुझे लगता है शायद दिल्ली।"), "दिल्ली")
        self.assertEqual(judge.normalize_answer("haan delhi na"), "delhi")
        self.assertEqual(judge.normalize_answer("Gandhi ji"), "gandhi")

    def test_hesitation_dots(self):
        self.assertEqual(judge.normalize_answer("दिल्ली........"), "दिल्ली")
        self.assertEqual(judge.normalize_answer("delhi . . ."), "delhi")
        self.assertEqual(judge.normalize_answer("umm ... dhoni"), "dhoni")

    def test_whitespace_collapsed(self):
        self.assertEqual(judge.normalize_answer("  धोनी   जी  "), "धोनी")

    def test_blank_and_filler_only(self):
        self.assertEqual(judge.normalize_answer(""), "")
        self.assertEqual(judge.normalize_answer("   "), "")
        self.assertEqual(judge.normalize_answer("जी"), "")
        self.assertEqual(judge.normalize_answer(" ji ... hmm "), "")
        self.assertEqual(judge.normalize_answer(None), "")

    def test_expected_answers_not_corrupted(self):
        # Guards against over-aggressive filler sets: "MS" is a name initial,
        # not an honorific; units and slashes survive.
        self.assertEqual(judge.normalize_answer("MS Dhoni"), "ms dhoni")
        self.assertEqual(judge.normalize_answer("26/1/1950"), "26/1/1950")
        self.assertEqual(judge.normalize_answer("8586 metres"), "8586 metres")
        self.assertEqual(judge.normalize_answer("₹6,500"), "₹6500")


# ------------------------------------------------------- deterministic verdict

class TestDeterministicVerdict(unittest.TestCase):
    def _check(self, question, heard, verdict, close_call, template_id):
        result = judge.deterministic_verdict(question, heard)
        assert_valid_verdict(self, result)
        self.assertEqual(result["verdict"], verdict)
        self.assertEqual(result["close_call"], close_call)
        self.assertEqual(result["template_id"], template_id)
        self.assertEqual(result["hype_line"], "")  # deterministic never hypes

    def test_correct_exact_and_aliases(self):
        self._check(CRICKET_01, "MS Dhoni", "correct", False, "correct_confident")
        self._check(CRICKET_01, "धोनी", "correct", False, "correct_confident")
        self._check(CRICKET_01, "महेंद्र सिंह धोनी", "correct", False, "correct_confident")
        self._check(CRICKET_01, "Mahi", "correct", False, "correct_confident")
        self._check(CRICKET_01, "Dhoni sir tha I think", "correct", False,
                    "correct_confident")  # bank few-shot #2
        self._check(CRICKET_12, "Zimbabwe", "correct", False, "correct_confident")
        self._check(CRICKET_12, "ज़िम्बाब्वे", "correct", False, "correct_confident")
        self._check(GK_09, "8,586", "correct", False, "correct_confident")
        self._check(GK_09, "8586 metres", "correct", False, "correct_confident")
        self._check(MATH_06, "6,500", "correct", False, "correct_confident")
        self._check(MATH_06, "₹6,500", "correct", False, "correct_confident")
        self._check(MATH_06, "छह हज़ार पाँच सौ", "correct", False, "correct_confident")
        self._check(GK_05, "26 January", "correct", False, "correct_confident")
        self._check(GK_05, "Republic Day", "correct", False, "correct_confident")
        self._check(GK_05, "26/1/1950", "correct", False, "correct_confident")

    def test_partial_band_ratio_gte_080(self):
        # difflib ratios: virat koli 0.952, mahendra singh 0.824, zimbabve 0.875
        self._check(VIRAT, "virat koli", "partial", True, "partial")
        self._check(CRICKET_01, "mahendra singh", "partial", True, "partial")
        self._check(CRICKET_12, "zimbabve", "partial", True, "partial")

    def test_wrong_close_band_ratio_gte_060(self):
        # ratios: "8486" vs 8586 → 0.750 (digit transposition), "new delhi"
        # vs "delhi" → 0.714
        self._check(GK_09, "8,486", "wrong", True, "wrong_close")
        self._check(CAPITAL, "new delhi", "wrong", True, "wrong_close")

    def test_wrong_plain(self):
        # ratios: sachin tendulkar 0.250, oxygen 0.571, ar rahman 0.273
        self._check(CRICKET_01, "sachin tendulkar", "wrong", False, "wrong_confident")
        self._check(SCI_06, "oxygen", "wrong", False, "wrong_confident")
        self._check(BOLLY_12, "AR Rahman", "wrong", False, "wrong_confident")

    def test_unclear_blank_or_filler_only(self):
        self._check(CRICKET_01, "", "unclear", False, "unclear")
        self._check(CRICKET_01, "   ", "unclear", False, "unclear")
        self._check(CRICKET_01, "hmm ji ...", "unclear", False, "unclear")

    def test_cross_script_bridge_is_alias_list_only(self):
        # Listed alias → exact-match correct.
        self._check(VIRAT, "virat kohli", "correct", False, "correct_confident")
        self._check(VIRAT, "विराट कोहली", "correct", False, "correct_confident")
        # Unlisted Latin spelling vs Devanagari-only candidates → no fuzzy
        # cross-script credit (question-bank §6.1.4): ratio ~0.09 → wrong.
        self._check(VIRAT_DEVANAGARI_ONLY, "virat kohli", "wrong", False,
                    "wrong_confident")

    def test_tolerates_malformed_questions(self):
        self._check({"id": "x", "expected_answer": "Delhi"}, "delhi",
                    "correct", False, "correct_confident")
        self._check({}, "delhi", "wrong", False, "wrong_confident")
        self._check(None, "", "unclear", False, "unclear")

    def test_template_enum_matches_contract(self):
        self.assertEqual(list(judge.TEMPLATE_IDS), CONTRACT_TEMPLATE_IDS)


# ------------------------------------------------------------- prompt quality

class TestJudgePrompt(unittest.TestCase):
    def test_system_prompt_carries_rubric(self):
        prompt = judge._SYSTEM_PROMPT
        for verdict in ("correct", "wrong", "partial", "unclear"):
            self.assertIn(verdict, prompt)
        for template_id in CONTRACT_TEMPLATE_IDS:  # full 16-id enum listed
            self.assertIn(template_id, prompt)
        self.assertIn("60 characters", prompt)          # hype_line cap
        self.assertIn("विराट कोहली", prompt)             # transliteration rule
        self.assertIn("Virat", prompt)
        self.assertIn("different entity", prompt)       # never reward wrong entity
        self.assertIn("close_call", prompt)
        self.assertIn("template_id", prompt)
        self.assertIn("hype_line", prompt)

    def test_four_fewshots_adapted_from_question_bank(self):
        self.assertEqual(len(judge._FEWSHOTS), 4)
        for user_msg, assistant_msg in judge._FEWSHOTS:
            user = json.loads(user_msg)
            self.assertEqual(
                set(user),
                {"question_id", "question", "expected_answer", "aliases",
                 "heard", "persona_label"})
            parsed = judge._parse_llm_verdict(assistant_msg)
            self.assertIsNotNone(parsed)  # few-shot assistant JSON is valid
            self.assertLessEqual(len(parsed["hype_line"]), 60)
            self.assertIn(parsed["template_id"], CONTRACT_TEMPLATE_IDS)
        # Adapted from question-bank.md §6.3: cricket-01, gk-05, bolly-12.
        ids = {json.loads(u)["question_id"] for u, _ in judge._FEWSHOTS}
        self.assertEqual(ids, {"cricket-01", "gk-05", "bolly-12"})
        heard = [json.loads(u)["heard"] for u, _ in judge._FEWSHOTS]
        self.assertIn("धोनी", heard)
        self.assertIn("Dhoni sir tha I think", heard)
        self.assertIn("AR Rahman", heard)

    def test_hard_timeout_is_2_5s(self):
        self.assertEqual(judge.JUDGE_TIMEOUT_S, 2.5)
        self.assertEqual(judge._TIMEOUT, httpx.Timeout(2.5))

    def test_llm_constants(self):
        self.assertEqual(judge.JUDGE_MODEL, "sarvam-105b-conversations")
        self.assertEqual(judge.JUDGE_URL, "https://api.sarvam.ai/v1/chat/completions")
        self.assertEqual(judge.JUDGE_REASONING_EFFORT, "low")
        self.assertEqual(judge.JUDGE_TEMPERATURE, 0.2)
        self.assertEqual(judge.JUDGE_MAX_TOKENS, 300)

    def test_api_key_env_priority(self):
        with mock.patch.dict(os.environ, {"SARVAM_API_KEY": "env-key-123"}):
            self.assertEqual(judge._get_api_key(), "env-key-123")


# ------------------------------------------------------------ LLM path (mocked)

LLM_OK = ('Great try! {"verdict":"correct","close_call":false,'
          '"template_id":"correct_confident","hype_line":"शाबाश! क्या जवाब!"}')


class TestJudgeAnswerLLM(unittest.IsolatedAsyncioTestCase):
    """httpx.MockTransport only — zero live Sarvam traffic."""

    def setUp(self):
        self.requests: list[httpx.Request] = []
        key_patcher = mock.patch.object(judge, "_get_api_key",
                                        return_value="test-key")
        key_patcher.start()
        self.addCleanup(key_patcher.stop)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _content_response(content, status=200):
        return httpx.Response(
            status,
            json={"id": "mock", "model": judge.JUDGE_MODEL,
                  "choices": [{"index": 0, "finish_reason": "stop",
                               "message": {"role": "assistant",
                                           "content": content}}]},
        )

    def _transport(self, content=None, status=200, handler=None):
        requests = self.requests

        def _handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if handler is not None:
                return handler(request)
            return self._content_response(content, status)

        return httpx.MockTransport(_handler)

    async def _judge(self, question=CRICKET_01, heard="धोनी",
                     persona="Meera (warm quizmaster)", **transport_kwargs):
        transport = self._transport(**transport_kwargs)
        with mock.patch.object(judge, "_TRANSPORT", transport):
            return await judge.judge_answer(question, heard, persona)

    # -- LLM 200 + request shape -------------------------------------------

    async def test_llm_200_parsed_with_contract_request(self):
        result = await self._judge(content=LLM_OK)
        self.assertEqual(result, {"verdict": "correct", "close_call": False,
                                   "template_id": "correct_confident",
                                   "hype_line": "शाबाश! क्या जवाब!"})
        assert_valid_verdict(self, result)
        self.assertEqual(len(self.requests), 1)

        request = self.requests[0]
        self.assertEqual(str(request.url), judge.JUDGE_URL)
        self.assertEqual(request.headers["api-subscription-key"], "test-key")

        body = json.loads(request.content.decode("utf-8"))
        self.assertEqual(body["model"], "sarvam-105b-conversations")
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertEqual(body["temperature"], 0.2)
        self.assertEqual(body["max_tokens"], 300)
        self.assertNotIn("response_format", body)  # amendment: JSON-in-prompt

        messages = body["messages"]
        self.assertEqual(len(messages), 10)  # system + 4×(user+assistant) + user
        self.assertEqual([m["role"] for m in messages],
                         ["system"] + ["user", "assistant"] * 4 + ["user"])
        self.assertEqual(messages[0]["content"], judge._SYSTEM_PROMPT)
        for i in range(1, 9, 2):  # few-shot pairs parse as valid verdict JSON
            self.assertIsNotNone(judge._parse_llm_verdict(messages[i + 1]["content"]))

        user = json.loads(messages[-1]["content"])
        self.assertEqual(user["question_id"], "cricket-01")
        self.assertEqual(user["question"], CRICKET_01["question_text"])
        self.assertEqual(user["expected_answer"], "MS Dhoni")
        self.assertIn("Dhoni", user["aliases"])
        self.assertEqual(user["heard"], "धोनी")
        self.assertEqual(user["persona_label"], "Meera (warm quizmaster)")

    async def test_llm_markdown_fence_parsed(self):
        fenced = ('```json\n{"verdict":"wrong","close_call":true,'
                  '"template_id":"wrong_close","hype_line":"अरे नहीं!"}\n```')
        result = await self._judge(question=BOLLY_12, heard="AR Rahman",
                                   content=fenced)
        self.assertEqual(result, {"verdict": "wrong", "close_call": True,
                                   "template_id": "wrong_close",
                                   "hype_line": "अरे नहीं!"})

    async def test_llm_template_pass_through_and_enum_membership(self):
        for verdict, template_id in (("wrong", "wrong_funny"),
                                     ("correct", "correct_fast"),
                                     ("partial", "partial"),
                                     ("unclear", "no_answer")):
            content = json.dumps({"verdict": verdict, "close_call": False,
                                  "template_id": template_id, "hype_line": "ok"},
                                 ensure_ascii=False)
            result = await self._judge(content=content)
            self.assertEqual(result["template_id"], template_id)
            self.assertIn(result["template_id"], CONTRACT_TEMPLATE_IDS)

    async def test_llm_hype_line_truncated_to_60(self):
        long_hype = "शाबाश! " + "म" * 100
        content = json.dumps({"verdict": "correct", "close_call": False,
                              "template_id": "correct_confident",
                              "hype_line": long_hype}, ensure_ascii=False)
        result = await self._judge(content=content)
        self.assertLessEqual(len(result["hype_line"]), 60)

    async def test_llm_never_raises_on_junk_inputs(self):
        result = await self._judge(question=None, heard="hello",
                                   persona=None, content=LLM_OK)
        assert_valid_verdict(self, result)

    # -- fallback ladder: timeout / HTTP error / parse fail / bad fields ----

    async def test_timeout_falls_back_to_deterministic(self):
        def raise_timeout(request):
            raise httpx.ReadTimeout("simulated: exceeded the hard 2.5s cap")
        # Correct-answer timeout → deterministic still says correct.
        result = await self._judge(handler=raise_timeout)
        self.assertEqual(result, judge.deterministic_verdict(CRICKET_01, "धोनी"))
        self.assertEqual(result, {"verdict": "correct", "close_call": False,
                                   "template_id": "correct_confident",
                                   "hype_line": ""})
        # Wrong-answer timeout → deterministic wrong, no close_call.
        result2 = await self._judge(question=BOLLY_12, heard="AR Rahman",
                                    handler=raise_timeout)
        self.assertEqual(result2, {"verdict": "wrong", "close_call": False,
                                    "template_id": "wrong_confident",
                                    "hype_line": ""})

    async def test_http_500_falls_back_to_deterministic(self):
        result = await self._judge(status=500)
        self.assertEqual(result, judge.deterministic_verdict(CRICKET_01, "धोनी"))

    async def test_content_null_falls_back(self):
        # The probed sarvam-105b failure shape: content is null.
        result = await self._judge(content=None)
        self.assertEqual(result, judge.deterministic_verdict(CRICKET_01, "धोनी"))

    async def test_malformed_json_falls_back(self):
        for content in ("The answer is obviously Dhoni, great show!",
                        '{"verdict": "correct"',           # truncated JSON
                        '[1, 2, 3]',                        # no object
                        '{"verdict":"correct"} extra {'):
            result = await self._judge(content=content)
            self.assertEqual(result,
                             judge.deterministic_verdict(CRICKET_01, "धोनी"),
                             msg=f"content={content!r}")

    async def test_missing_or_invalid_fields_fall_back(self):
        bad_payloads = [
            '{"verdict":"correct","close_call":false,"template_id":"correct_confident"}',  # no hype_line
            '{"verdict":"yes","close_call":false,"template_id":"wrong_confident","hype_line":"x"}',  # bad enum
            '{"verdict":"correct","close_call":"false","template_id":"correct_confident","hype_line":"x"}',  # string bool
            '{"verdict":"correct","close_call":false,"template_id":"bogus_template","hype_line":"x"}',  # bad template
            '{"verdict":"correct","close_call":false,"template_id":"correct_confident","hype_line":42}',  # non-str hype
            'null',
        ]
        for content in bad_payloads:
            result = await self._judge(content=content)
            self.assertEqual(result,
                             judge.deterministic_verdict(CRICKET_01, "धोनी"),
                             msg=f"content={content!r}")

    # -- no-network guards ---------------------------------------------------

    async def test_blank_heard_short_circuits_to_unclear(self):
        for heard in ("", "   ", "ji ji", "hmm..."):
            self.requests.clear()
            result = await self._judge(heard=heard, content=LLM_OK)
            self.assertEqual(result["verdict"], "unclear")
            self.assertEqual(result["template_id"], "unclear")
            self.assertEqual(self.requests, [])  # zero LLM calls, zero latency

    async def test_missing_api_key_falls_back_without_network(self):
        with mock.patch.object(judge, "_get_api_key", return_value=""):
            result = await self._judge(content=LLM_OK)
        self.assertEqual(result, judge.deterministic_verdict(CRICKET_01, "धोनी"))
        self.assertEqual(self.requests, [])

    async def test_every_returned_template_id_in_enum(self):
        """Sweep: LLM-shaped and deterministic results all stay in the enum."""
        results = []
        for content in (LLM_OK,
                        '{"verdict":"wrong","close_call":true,"template_id":"wrong_close","hype_line":"x"}',
                        '{"verdict":"wrong","close_call":false,"template_id":"wrong_funny","hype_line":"x"}',
                        '{"verdict":"partial","close_call":true,"template_id":"partial","hype_line":"x"}'):
            results.append(await self._judge(content=content))
        for question, heard in ((CRICKET_01, "धोनी"), (CRICKET_01, ""),
                                (GK_09, "8,486"), (SCI_06, "oxygen"),
                                (VIRAT, "virat koli"), (BOLLY_12, "AR Rahman"),
                                (None, None)):
            results.append(judge.deterministic_verdict(question, heard))
        for result in results:
            assert_valid_verdict(self, result)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
