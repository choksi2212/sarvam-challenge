"""A5 tests — persona cast + reaction bank (stdlib unittest, no network).

Covers: binding TEMPLATE_IDS enum; exact cast per language (task/CONTRACT §3 +
BUILD_PLAN R2); every speaker id verified against the live 224-ID roster file
when reachable (skips with a note elsewhere); all 16 template ids per language;
JSON loads clean; persona_for_round correctness; non-empty labels; non-empty
native-script reaction texts with danda + Latin-script code-mixing.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import personas  # noqa: E402

LANGUAGES = ("hi", "enhi", "en", "mr")

# Exact cast per BUILD_PLAN R2 / task brief (all ids verified against
# research/speaker-ids-v4flash.txt, the live 224-ID roster).
EXPECTED_ROUNDS = {
    "hi": {1: "ratan_hi_customer_expressive", 2: "ishita_enhi_customer_expressive", 3: "ratan_hi_sports"},
    "enhi": {1: "ratan_hi_customer_expressive", 2: "ishita_enhi_customer_expressive", 3: "ratan_hi_sports"},
    "en": {1: "simran_en_customer", 2: "ishita_enhi_customer_expressive", 3: "ashwin_en_sports_energetic"},
    "mr": {1: "soham_mr_narration", 2: "ritu_mr_narration", 3: "mukul_mr_stories"},
}
EXPECTED_FINAL = {
    "hi": "mukul_hi_suspense",
    "enhi": "mukul_hi_suspense",
    "en": "varun_en_suspense",
    "mr": "mukul_mr_stories",
}
EXPECTED_UNIQUE_SPEAKERS = {
    "ratan_hi_customer_expressive", "ishita_enhi_customer_expressive",
    "ratan_hi_sports", "mukul_hi_suspense", "simran_en_customer",
    "ashwin_en_sports_energetic", "varun_en_suspense", "soham_mr_narration",
    "ritu_mr_narration", "mukul_mr_stories",
}
CONTRACT_TEMPLATE_IDS = [
    "correct_fast", "correct_confident", "correct_slow", "wrong_confident",
    "wrong_funny", "wrong_close", "partial", "unclear", "timeout",
    "no_answer", "repeat_request", "streak_3", "streak_5", "final_win",
    "final_lose", "game_over",
]
VALID_ROLES = {"celebrate", "tease", "explain", "repeat", "timeout", "hype", "finale", "outro"}
DEVANAGARI = re.compile(r"[\u0900-\u097F]")
LATIN = re.compile(r"[A-Za-z]")


def _roster_candidates() -> list[Path]:
    root = Path(__file__).resolve().parents[2]  # worktree root
    candidates: list[Path] = []
    env_path = os.environ.get("BHASHABUZZER_ROSTER")
    if env_path:
        candidates.append(Path(env_path))
    candidates += [
        root.parent.parent / "research" / "speaker-ids-v4flash.txt",
        root.parent / "research" / "speaker-ids-v4flash.txt",
        root / "research" / "speaker-ids-v4flash.txt",
        root / "speaker-ids-v4flash.txt",
    ]
    return candidates


def _find_roster() -> Path | None:
    for candidate in _roster_candidates():
        if candidate.is_file():
            return candidate
    return None


def _cast_entries() -> list[tuple[str, dict]]:
    entries = []
    for lang, rounds in personas.ROUNDS.items():
        for num in sorted(rounds):
            entries.append((f"rounds[{lang}][{num}]", rounds[num]))
    for lang, entry in personas.FINAL.items():
        entries.append((f"final[{lang}]", entry))
    return entries


class CastContractTests(unittest.TestCase):
    """CONTRACT §3 binding surface: enum, cast shape, lookups."""

    def test_template_ids_binding_enum(self) -> None:
        self.assertEqual(personas.TEMPLATE_IDS, CONTRACT_TEMPLATE_IDS)
        self.assertEqual(len(personas.TEMPLATE_IDS), 16)
        self.assertEqual(len(set(personas.TEMPLATE_IDS)), 16)

    def test_rounds_shape_and_cast(self) -> None:
        self.assertEqual(sorted(personas.ROUNDS), sorted(LANGUAGES))
        for lang in LANGUAGES:
            self.assertEqual(sorted(personas.ROUNDS[lang]), [1, 2, 3], lang)
            for num in (1, 2, 3):
                entry = personas.ROUNDS[lang][num]
                self.assertEqual(entry["speaker"], EXPECTED_ROUNDS[lang][num])
                for key in ("speaker", "label", "style_tag"):
                    self.assertIsInstance(entry[key], str)
                    self.assertTrue(entry[key].strip(), f"{lang} R{num} {key}")
                self.assertIsInstance(entry.get("pace"), (int, float))
                self.assertTrue(entry.get("language_code"), f"{lang} R{num} language_code")

    def test_final_cast_and_mr_suspense_styling(self) -> None:
        self.assertEqual(sorted(personas.FINAL), sorted(LANGUAGES))
        for lang in LANGUAGES:
            entry = personas.FINAL[lang]
            self.assertEqual(entry["speaker"], EXPECTED_FINAL[lang])
            self.assertEqual(entry["style_tag"], "suspense")
        # mr final reuses mukul_mr_stories with suspense styling: slower pace.
        self.assertEqual(personas.FINAL["mr"]["speaker"], personas.ROUNDS["mr"][3]["speaker"])
        self.assertLess(personas.FINAL["mr"]["pace"], personas.ROUNDS["mr"][3]["pace"])

    def test_unique_cast_speakers(self) -> None:
        speakers = {entry["speaker"] for _, entry in _cast_entries()}
        self.assertEqual(speakers, EXPECTED_UNIQUE_SPEAKERS)

    def test_speaker_ids_lowercase_roster_format(self) -> None:
        for where, entry in _cast_entries():
            speaker = entry["speaker"]
            self.assertEqual(speaker, speaker.lower(), where)
            self.assertRegex(speaker, r"^[a-z][a-z0-9_]*$", where)

    def test_persona_for_round_per_language(self) -> None:
        for lang in LANGUAGES:
            for num in (1, 2, 3):
                persona = personas.persona_for_round(num, lang)
                self.assertIsInstance(persona, dict)
                self.assertEqual(persona["speaker"], EXPECTED_ROUNDS[lang][num])
            final = personas.persona_for_round(4, lang)
            self.assertEqual(final["speaker"], EXPECTED_FINAL[lang])
            self.assertEqual(personas.persona_for_round(personas.FINAL_ROUND, lang)["speaker"], EXPECTED_FINAL[lang])

    def test_persona_for_round_returns_copy(self) -> None:
        persona = personas.persona_for_round(1, "hi")
        persona["label"] = "mutated"
        self.assertNotEqual(personas.persona_for_round(1, "hi")["label"], "mutated")

    def test_persona_for_round_errors(self) -> None:
        with self.assertRaises(KeyError):
            personas.persona_for_round(1, "ta")  # roster-light: not in cast
        with self.assertRaises(KeyError):
            personas.persona_for_round(0, "hi")
        with self.assertRaises(KeyError):
            personas.persona_for_round(-1, "hi")

    def test_labels_non_empty_and_lookup(self) -> None:
        for _, entry in _cast_entries():
            self.assertTrue(entry["label"].strip())
            self.assertIn("—", entry["label"])  # "Name — the Role" pattern
        for lang in LANGUAGES:
            for num in (1, 2, 3):
                speaker = personas.persona_for_round(num, lang)["speaker"]
                label = personas.persona_label(speaker)
                self.assertIsInstance(label, str)
                self.assertTrue(label.strip())
        self.assertTrue(personas.persona_label("mukul_hi_suspense").startswith("Mukul"))
        self.assertEqual(personas.persona_label("no_such_speaker_xyz"), "no_such_speaker_xyz")  # graceful fallback

    def test_personas_json_loads_clean(self) -> None:
        data = json.loads(personas.PERSONAS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(sorted(data["rounds"]), sorted(LANGUAGES))
        self.assertEqual(sorted(data["final"]), sorted(LANGUAGES))
        self.assertTrue(personas.PERSONAS_PATH.is_file())


class RosterVerificationTests(unittest.TestCase):
    """Every speaker id must appear in the live 224-ID roster file."""

    def test_every_speaker_in_roster(self) -> None:
        roster = _find_roster()
        if roster is None:
            self.skipTest(
                "speaker-ids-v4flash.txt not found; searched: "
                + ", ".join(str(p) for p in _roster_candidates())
            )
        roster_ids = set(roster.read_text(encoding="utf-8").split())
        self.assertGreaterEqual(len(roster_ids), 200, f"suspicious roster at {roster}")
        for where, entry in _cast_entries():
            self.assertIn(entry["speaker"], roster_ids, f"{where}: {entry['speaker']} not in {roster}")


class ReactionBankTests(unittest.TestCase):
    """reactions.json: 4 languages x 16 template ids, native script, code-mix."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.bank = personas.load_reactions()

    def test_languages_present(self) -> None:
        self.assertEqual(sorted(self.bank), sorted(LANGUAGES))

    def test_reactions_json_loads_clean(self) -> None:
        with open(personas.REACTIONS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(len(data), 4)

    def test_all_16_templates_per_language(self) -> None:
        for lang in LANGUAGES:
            self.assertEqual(set(self.bank[lang]), set(personas.TEMPLATE_IDS), lang)
            for template_id in personas.TEMPLATE_IDS:
                lines = self.bank[lang][template_id]
                self.assertIsInstance(lines, list)
                self.assertGreaterEqual(len(lines), 1, f"{lang}/{template_id}")

    def test_entry_shape(self) -> None:
        for lang in LANGUAGES:
            total = 0
            for template_id, lines in self.bank[lang].items():
                for line in lines:
                    self.assertIsInstance(line["text"], str)
                    self.assertTrue(line["text"].strip(), f"{lang}/{template_id}")
                    self.assertIsInstance(line["pace_hint"], (int, float))
                    self.assertNotIsInstance(line["pace_hint"], bool)
                    self.assertGreaterEqual(line["pace_hint"], 0.5)
                    self.assertLessEqual(line["pace_hint"], 2.0)
                    self.assertIsInstance(line["role"], str)
                    self.assertIn(line["role"], VALID_ROLES, f"{lang}/{template_id}")
                    total += 1
            self.assertGreaterEqual(total, 28, f"{lang}: bank should carry >= 28 lines")

    def test_native_script_and_code_mix(self) -> None:
        for lang in ("hi", "enhi", "mr"):
            texts = [line["text"] for lines in self.bank[lang].values() for line in lines]
            for text in texts:  # every Indic-language line is in Devanagari
                self.assertRegex(text, DEVANAGARI, f"{lang}: {text}")
            self.assertTrue(any("।" in t for t in texts), f"{lang}: danda punctuation expected")
            self.assertGreaterEqual(
                sum(1 for t in texts if LATIN.search(t)), 5,
                f"{lang}: Latin-script English loanwords expected (code-mixing rule)",
            )
        for template_id, lines in self.bank["en"].items():
            for line in lines:
                self.assertRegex(line["text"], LATIN)  # en bank is English (Latin script)
                self.assertIsNone(DEVANAGARI.search(line["text"]), f"en/{template_id}")

    def test_enhi_shares_hi_bank(self) -> None:
        self.assertEqual(self.bank["hi"], self.bank["enhi"])

    def test_pace_hint_examples(self) -> None:
        self.assertEqual(self.bank["hi"]["correct_fast"][0]["pace_hint"], 1.15)
        self.assertEqual(self.bank["hi"]["wrong_funny"][0]["pace_hint"], 0.95)
        self.assertEqual(self.bank["hi"]["repeat_request"][0]["pace_hint"], 0.95)
        self.assertEqual(self.bank["hi"]["streak_3"][0]["pace_hint"], 1.15)

    def test_d5_authoritative_lines_verbatim(self) -> None:
        hi = self.bank["hi"]
        verbatim = {
            "correct_fast": "वाह! बिजली की रफ़्तार! इसे कहते हैं सही जवाब, सही समय!",
            "wrong_confident": "इतना confidence, और जवाब ग़लत? हाय राम! दिल दुखा दिया!",
            "repeat_request": "ज़रा साफ़-साफ़ बोलिए, एक बार फिर से!",
            "streak_5": "पाँच का स्ट्रीक! Host को अब डर लगने लगा है!",
            "final_win": "Sudden death में भी धमाका! आज आप इस मैच के चैंपियन हैं। बधाई हो!",
            "game_over": "यही हुआ आज का BhashaBuzzer! Score याद रखना — कल फिर आना। बोलो और जीतो!",
        }
        for template_id, text in verbatim.items():
            self.assertIn(text, [line["text"] for line in hi[template_id]], template_id)

    def test_reaction_line_accessor(self) -> None:
        line = personas.reaction_line("hi", "correct_fast")
        self.assertEqual(set(line), {"text", "pace_hint", "role"})
        self.assertTrue(line["text"].strip())
        for lang in LANGUAGES:
            for template_id in personas.TEMPLATE_IDS:
                self.assertTrue(personas.reaction_line(lang, template_id)["text"].strip())
        with self.assertRaises(KeyError):
            personas.reaction_line("ta", "correct_fast")
        with self.assertRaises(KeyError):
            personas.reaction_line("hi", "not_a_template")
        # variant cycles: wrong_funny has 3 variants -> 3 wraps to 0
        self.assertEqual(
            personas.reaction_line("hi", "wrong_funny", 3),
            personas.reaction_line("hi", "wrong_funny", 0),
        )


if __name__ == "__main__":
    unittest.main()
