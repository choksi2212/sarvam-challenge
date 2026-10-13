"""A2 tests — cache layer + warmup dry-run (stdlib unittest; CONTRACT §8).

Covers: key stability/sensitivity, atomic cache_put, LRU eviction order,
judge-cache round-trip, spend ledger + chars_used cumulative, governor
thresholds, and warmup --dry-run with the lazy sibling imports mocked
(backend.personas / backend.sarvam_tts / backend.data / backend.config are
absent from the A2 worktree — CONTRACT §8 — so fakes are injected into
sys.modules exactly like the lazy runtime imports expect).

Run: python -m unittest discover -s backend/tests   (from the repo root)
"""

from __future__ import annotations

import ast
import asyncio
import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend import cache
from backend.scripts import warmup  # namespace subpackage (no __init__.py needed)

TTS_ARGS = {
    "text": "नमस्ते भारत! यह है BhashaBuzzer।",
    "speaker": "ratan_hi_customer_expressive",
    "language_code": "hi-IN",
    "pace": 1.0,
    "sample_rate": 24000,
    "codec": "wav",
}

DEMO_PERSONAS = ("ratan_hi_customer_expressive", "ishita_enhi_customer_expressive",
                 "ratan_hi_sports", "mukul_hi_suspense")


def fake_wav(marker: str = "") -> bytes:
    """Deterministic tiny stand-in for a Sarvam wav blob (always 100 bytes)."""
    payload = b"RIFF" + marker.encode("utf-8")
    return payload + b"\x00" * (100 - len(payload))


class CacheTestCase(unittest.TestCase):
    """Isolated cache/logs dirs per test via cache.configure()."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cache_dir = self.tmp / "cache"
        self.logs_dir = self.tmp / "logs"
        cache.configure(cache_dir=self.cache_dir, logs_dir=self.logs_dir)

    def tearDown(self) -> None:
        cache.configure()  # reset all overrides to env/defaults
        self._tmp.cleanup()


class TestTtsKey(CacheTestCase):

    def test_stable_and_sensitive(self) -> None:
        k1 = cache.tts_key(**TTS_ARGS)
        k2 = cache.tts_key(**TTS_ARGS)
        self.assertEqual(k1, k2)
        self.assertEqual(len(k1), 64)
        self.assertRegex(k1, r"^[0-9a-f]{64}$")
        # every field change must move the key
        for field, other in {
            "text": "नमस्ते भारत", "speaker": "ratan_hi_sports",
            "language_code": "en-IN", "pace": 1.05, "sample_rate": 8000,
            "codec": "mp3", "dict_id": "p_8eea3524", "pitch": 0.5, "loudness": 1.5,
        }.items():
            changed = dict(TTS_ARGS)
            changed[field] = other
            self.assertNotEqual(k1, cache.tts_key(**changed),
                                f"key must change when {field} changes")

    def test_numeric_normalization_and_optionals(self) -> None:
        base = dict(TTS_ARGS)
        # int vs float of the same value → same key (caller drift is not a cache miss)
        self.assertEqual(cache.tts_key(**{**base, "pace": 1}),
                         cache.tts_key(**{**base, "pace": 1.0}))
        self.assertEqual(cache.tts_key(**{**base, "sample_rate": 24000.0}),
                         cache.tts_key(**{**base, "sample_rate": 24000}))
        # None vs a real value must differ (sentinel, not "0")
        self.assertNotEqual(cache.tts_key(**base), cache.tts_key(**{**base, "pitch": 0.0}))
        self.assertNotEqual(cache.tts_key(**base), cache.tts_key(**{**base, "loudness": 0.0}))
        self.assertNotEqual(cache.tts_key(**base), cache.tts_key(**{**base, "dict_id": ""}))
        # delimiter injection cannot alias two different tuples
        self.assertNotEqual(
            cache.tts_key("a|b", "c", "hi-IN", 1.0, 24000, "wav"),
            cache.tts_key("a", "b|c", "hi-IN", 1.0, 24000, "wav"))

    def test_rejects_non_str_text(self) -> None:
        with self.assertRaises(TypeError):
            cache.tts_key(text=b"bytes", speaker="s", language_code="hi-IN",
                          pace=1.0, sample_rate=24000, codec="wav")  # type: ignore[arg-type]


class TestJudgeKey(CacheTestCase):

    def test_stable_and_sensitive(self) -> None:
        k1 = cache.judge_key("cricket-01", "ms dhoni")
        self.assertEqual(k1, cache.judge_key("cricket-01", "ms dhoni"))
        self.assertNotEqual(k1, cache.judge_key("cricket-02", "ms dhoni"))
        self.assertNotEqual(k1, cache.judge_key("cricket-01", "ms dh0ni"))
        self.assertRegex(k1, r"^[0-9a-f]{64}$")


class TestTtsBlobCache(CacheTestCase):

    def test_put_get_roundtrip_with_sidecar(self) -> None:
        key = cache.tts_key(**TTS_ARGS)
        self.assertIsNone(cache.cache_get(key))  # miss on empty cache
        blob = fake_wav("intro")
        meta = {"model": "bulbul:v4-flash", "speaker": TTS_ARGS["speaker"], "chars": 27}
        cache.cache_put(key, blob, meta)
        self.assertEqual(cache.cache_get(key), blob)
        sidecar = json.loads((self.cache_dir / "tts" / key[:2] / f"{key}.meta.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(sidecar["model"], "bulbul:v4-flash")
        self.assertEqual(sidecar["chars"], 27)
        self.assertEqual(sidecar["key"], key)
        self.assertEqual(sidecar["size"], len(blob))
        self.assertIn("created_ms", sidecar)

    def test_put_uses_tmp_then_rename(self) -> None:
        replaces: list[tuple[str, str]] = []
        real_replace = os.replace

        def spy(src, dst):
            replaces.append((str(src), str(dst)))
            return real_replace(src, dst)

        key = cache.tts_key(**TTS_ARGS)
        with mock.patch.object(cache.os, "replace", spy):
            cache.cache_put(key, fake_wav("x"), {"model": "m"})
        pairs = {(s, d) for s, d in replaces}
        self.assertTrue(any(s.endswith(f"{key}.wav.tmp") and d.endswith(f"{key}.wav")
                            for s, d in pairs))
        self.assertTrue(any(s.endswith(f"{key}.meta.json.tmp") and d.endswith(f"{key}.meta.json")
                            for s, d in pairs))
        # nothing left behind as .tmp after a successful put
        self.assertEqual(list(self.cache_dir.rglob("*.tmp")), [])

    def test_put_is_atomic_when_rename_fails(self) -> None:
        key = cache.tts_key(**TTS_ARGS)
        old_blob = fake_wav("old-clip")
        cache.cache_put(key, old_blob, {"model": "m", "chars": 1})
        with mock.patch.object(cache.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                cache.cache_put(key, fake_wav("new-clip"), {"model": "m", "chars": 2})
        # reader still sees the previous, fully-written clip — never a partial one
        self.assertEqual(cache.cache_get(key), old_blob)
        # failed put leaves no tmp litter
        self.assertEqual(list(self.cache_dir.rglob("*.tmp")), [])

    def test_lru_eviction_order(self) -> None:
        cache.configure(cache_dir=self.cache_dir, logs_dir=self.logs_dir,
                        max_cache_bytes=320)
        keys = {name: cache.tts_key(text=f"clip {name}", speaker="s", language_code="hi-IN",
                                     pace=1.0, sample_rate=24000, codec="wav")
                for name in "ABCD"}
        for name in "ABC":
            cache.cache_put(keys[name], fake_wav(name), {"model": "m"})
        self.assertEqual(cache.cache_get(keys["A"]), fake_wav("A"))  # A → most-recent
        cache.cache_put(keys["D"], fake_wav("D"), {"model": "m"})      # 400 B > 320 B cap
        # strict LRU: B (oldest untouched) evicted; A (touched), C, D survive
        self.assertIsNone(cache.cache_get(keys["B"]))
        self.assertFalse((self.cache_dir / "tts" / keys["B"][:2] / f"{keys['B']}.wav").exists())
        self.assertFalse((self.cache_dir / "tts" / keys["B"][:2] / f"{keys['B']}.meta.json").exists())
        self.assertEqual(cache.cache_get(keys["A"]), fake_wav("A"))
        self.assertEqual(cache.cache_get(keys["C"]), fake_wav("C"))
        self.assertEqual(cache.cache_get(keys["D"]), fake_wav("D"))
        index = json.loads((self.cache_dir / "tts" / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(set(index["entries"]), {keys[n] for n in "ACD"})

    def test_index_rebuilt_from_disk_when_missing(self) -> None:
        key = cache.tts_key(**TTS_ARGS)
        cache.cache_put(key, fake_wav("intro"), {"model": "m"})
        (self.cache_dir / "tts" / "index.json").unlink()  # simulate crash/other process
        # reconfigure clears the in-memory index fast path → forces a disk rebuild
        cache.configure(cache_dir=self.cache_dir, logs_dir=self.logs_dir)
        self.assertEqual(cache.cache_get(key), fake_wav("intro"))
        self.assertTrue((self.cache_dir / "tts" / "index.json").exists())

    def test_env_byte_cap_is_respected(self) -> None:
        with mock.patch.dict(os.environ, {"BHASHA_CACHE_MAX_BYTES": "320"}):
            cache.configure(cache_dir=self.cache_dir, logs_dir=self.logs_dir)
            self.assertEqual(cache.max_cache_bytes(), 320)
        self.assertEqual(cache.max_cache_bytes(), cache.DEFAULT_MAX_CACHE_BYTES)


class TestJudgeCache(CacheTestCase):

    def test_roundtrip_and_miss(self) -> None:
        key = cache.judge_key("cricket-01", "ms dhoni")
        self.assertIsNone(cache.judge_get(key))
        verdict = {"verdict": "correct", "close_call": False, "template_id": "correct_fast",
                   "hype_line": "King कोहली!", "score_delta": 100}
        cache.judge_put(key, verdict)
        self.assertEqual(cache.judge_get(key), verdict)
        self.assertIsNone(cache.judge_get(cache.judge_key("cricket-01", "ms dh0ni")))
        # same verdict, second question → both live side by side
        k2 = cache.judge_key("gk-05", "26 january 1950")
        cache.judge_put(k2, {"verdict": "wrong", "template_id": "wrong_funny"})
        self.assertEqual(cache.judge_get(k2)["verdict"], "wrong")
        self.assertEqual(cache.judge_get(key), verdict)

    def test_survives_corrupt_store(self) -> None:
        key = cache.judge_key("cricket-01", "ms dhoni")
        cache.judge_put(key, {"verdict": "correct"})
        store = self.cache_dir / "judge" / "judge_cache.json"
        store.write_text("{not json", encoding="utf-8")
        self.assertIsNone(cache.judge_get(key))       # torn file → miss, not crash
        cache.judge_put(key, {"verdict": "partial"})  # put re-initialises the store
        self.assertEqual(cache.judge_get(key), {"verdict": "partial"})


class TestSpendLedger(CacheTestCase):

    def test_ledger_lines_and_cumulative(self) -> None:
        self.assertEqual(cache.chars_used(), 0)
        cache.log_spend(120, "warmup:host_intro")
        cache.log_spend(50, "live:question:cricket-01")
        cache.log_spend(30, "live:reaction:correct_fast")
        self.assertEqual(cache.chars_used(), 200)
        lines = [json.loads(line) for line in
                 (self.logs_dir / "spend.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(lines), 3)
        self.assertEqual([r["chars"] for r in lines], [120, 50, 30])
        self.assertEqual([r["cumulative"] for r in lines], [120, 170, 200])
        self.assertEqual([r["what"] for r in lines][0], "warmup:host_intro")
        for record in lines:
            self.assertIn("ts", record)
            self.assertTrue(record["ts"])

    def test_chars_used_empty_and_tolerant(self) -> None:
        fresh = self.tmp / "fresh_logs"
        cache.configure(cache_dir=self.cache_dir, logs_dir=fresh)
        self.assertEqual(cache.chars_used(), 0)  # no ledger at all
        fresh.mkdir(parents=True, exist_ok=True)
        (fresh / "spend.jsonl").write_text(
            '{"ts": "t", "chars": 10, "what": "x", "cumulative": 10}\n'
            "this line is torn garbage\n"
            '{"ts": "t", "chars": 5, "what": "y", "cumulative": 15}\n',
            encoding="utf-8")
        self.assertEqual(cache.chars_used(), 15)  # skips the torn line

    def test_log_spend_validates_chars(self) -> None:
        with self.assertRaises(TypeError):
            cache.log_spend(True, "bool is not an int count")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            cache.log_spend("120", "str is not an int count")  # type: ignore[arg-type]


class TestGovernor(CacheTestCase):

    def test_thresholds_are_binding(self) -> None:
        self.assertEqual(cache.GOVERNOR_LIMITS,
                         {"warn": 30_000, "cached_only": 40_000, "hard": 45_000})

    def test_level_boundaries(self) -> None:
        for chars, expected in [
            (0, "ok"), (29_999, "ok"),
            (30_000, "warn"), (30_001, "warn"), (39_999, "warn"),
            (40_000, "cached_only"), (44_999, "cached_only"),
            (45_000, "hard"), (60_000, "hard"),
        ]:
            self.assertEqual(cache.governor_level(chars), expected, f"chars={chars}")

    def test_level_reads_the_ledger(self) -> None:
        self.assertEqual(cache.governor_level(), "ok")
        cache.log_spend(30_000, "test:warn line")
        self.assertEqual(cache.governor_level(), "warn")
        cache.log_spend(10_000, "test:cached_only line")
        self.assertEqual(cache.governor_level(), "cached_only")
        cache.log_spend(5_000, "test:hard line")
        self.assertEqual(cache.governor_level(), "hard")


def install_fake_siblings(tmp: Path) -> types.ModuleType:
    """Inject fake backend.personas / backend.config / backend.sarvam_tts into
    sys.modules, mirroring the contract shapes A5/A7/A1 will ship. Returns the
    fake sarvam_tts module (its ``warmup_calls`` records every synthesis)."""
    manifest = warmup.load_mock_manifest()
    fake_personas = types.ModuleType("backend.personas")
    fake_personas.TEMPLATE_IDS = [t["id"] for t in manifest["reaction_templates"]]
    fake_personas.ROUNDS = {"hi": {
        1: {"speaker": DEMO_PERSONAS[0], "label": "Quizmaster", "style_tag": "expressive"},
        2: {"speaker": DEMO_PERSONAS[1], "label": "Teaser", "style_tag": "expressive"},
        3: {"speaker": DEMO_PERSONAS[2], "label": "Stadium", "style_tag": "sports"},
    }}
    fake_personas.FINAL = {"hi": {"speaker": DEMO_PERSONAS[3], "label": "The Boss",
                                  "style_tag": "suspense"}}
    sys.modules["backend.personas"] = fake_personas

    data_dir = tmp / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    bank = []
    for q in manifest["demo_questions"]:
        if q["id"] == "gk-11":     # deliberately absent → manifest fallback path
            continue
        fact = next(f["text"] for f in manifest["fun_facts"]
                    if f["question_id"] == q["id"])
        bank.append({"id": q["id"], "category": "demo", "difficulty": 2,
                     "question_text": f"BANK::{q['text']}",
                     "fun_fact_line": f"BANK-FACT::{fact}",
                     "language": q["language"]})
    (data_dir / "questions.json").write_text(
        json.dumps({"questions": bank}, ensure_ascii=False), encoding="utf-8")
    fake_config = types.ModuleType("backend.config")
    fake_config.DATA_DIR = str(data_dir)
    sys.modules["backend.config"] = fake_config

    fake_tts = types.ModuleType("backend.sarvam_tts")
    calls: list[dict] = []

    async def speak_via_rest(text, speaker, language_code="hi-IN",
                             pace=1.0, sample_rate=24000):
        calls.append({"text": text, "speaker": speaker,
                      "language_code": language_code, "pace": pace})
        # speaker first so per-item cache assertions can find it in the blob
        return b"RIFF" + f"{speaker}|{pace}|".encode("utf-8") + text.encode("utf-8")[:64] + b"\x00"

    fake_tts.speak_via_rest = speak_via_rest
    fake_tts.warmup_calls = calls
    sys.modules["backend.sarvam_tts"] = fake_tts
    return fake_tts


def remove_fake_siblings() -> None:
    for name in ("backend.personas", "backend.config", "backend.sarvam_tts"):
        sys.modules.pop(name, None)


class TestWarmupDryRun(CacheTestCase):

    def _run_main(self, argv: list[str]) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = warmup.main(argv)
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_dry_run_output(self) -> None:
        before_modules = set(sys.modules)
        out = self._run_main(["--dry-run"])
        items = warmup.build_render_list_from_mock()

        self.assertIn("DRY RUN", out)
        self.assertIn(str(warmup.MOCK_DATA_PATH), out)
        # exact render list: one speaker= pair per item, every text printed
        self.assertEqual(out.count("speaker="), len(items))
        for item in items:
            self.assertIn(item.text, out)
            self.assertIn(f"{item.kind} {item.id}", out)
        # demo-path coverage: intro+rules+3 round intros+final+16×4+7Q+7 facts
        kinds = [i.kind for i in items]
        self.assertEqual(kinds.count("host_intro"), 1)
        self.assertEqual(kinds.count("rules"), 1)
        self.assertEqual(kinds.count("round_intro"), 3)
        self.assertEqual(kinds.count("final_intro"), 1)
        self.assertEqual(kinds.count("reaction"), 64)   # 16 templates × 4 personas
        self.assertEqual(kinds.count("question"), 7)
        self.assertEqual(kinds.count("fun_fact"), 7)
        for persona in DEMO_PERSONAS:
            self.assertIn(persona, out)
        # totals + rupee estimate consistent with the manifest
        manifest = warmup.load_mock_manifest()
        rate = manifest["pricing"]["rs_per_10k_chars"]
        total_chars = sum(i.chars for i in items)
        self.assertIn(f"TOTAL items={len(items)} chars={total_chars}", out)
        expected_rs = round(total_chars * rate / 10_000, 2)
        self.assertIn(f"ESTIMATED Rs {expected_rs:.2f}", out)
        summary = json.loads([l for l in out.splitlines() if l.startswith("SUMMARY ")][0][8:])
        self.assertEqual(summary, {"dry_run": True, "items": len(items),
                                   "chars": total_chars, "estimated_rs": expected_rs})
        # dry-run touched no siblings and made no API-capable import
        new_modules = set(sys.modules) - before_modules
        self.assertFalse(new_modules & {"backend.personas", "backend.sarvam_tts",
                                        "backend.config", "backend.data"})

    def test_default_mode_is_dry_run(self) -> None:
        out = self._run_main([])
        self.assertIn("DRY RUN", out)
        self.assertIn("--live", out)  # explains how to actually render

    def test_dry_run_ignores_mocked_lazy_imports(self) -> None:
        clean = self._run_main(["--dry-run"])
        install_fake_siblings(self.tmp)
        try:
            with_fakes = self._run_main(["--dry-run"])
        finally:
            remove_fake_siblings()
        # output is derived from the committed manifest, never from siblings
        self.assertEqual(clean, with_fakes)


class TestWarmupLiveWithFakes(CacheTestCase):
    """Live path exercised ONLY through fakes — zero Sarvam API calls (§8)."""

    def setUp(self) -> None:
        super().setUp()
        self.fake_tts = install_fake_siblings(self.tmp)

    def tearDown(self) -> None:
        remove_fake_siblings()
        super().tearDown()

    def test_build_render_list_uses_lazy_siblings(self) -> None:
        manifest = warmup.load_mock_manifest()
        items = warmup.build_render_list()
        # same demo-path shape as the mock manifest
        self.assertEqual(len(items), 84)
        kinds = [i.kind for i in items]
        self.assertEqual((kinds.count("host_intro"), kinds.count("rules"),
                          kinds.count("round_intro"), kinds.count("final_intro"),
                          kinds.count("reaction"), kinds.count("question"),
                          kinds.count("fun_fact")), (1, 1, 3, 1, 64, 7, 7))
        # template enum comes from the fake personas module (contract order kept)
        reaction_ids = [i.id for i in items if i.kind == "reaction"]
        expected = [f"reaction:{tid}:{spk}" for tid in
                    [t["id"] for t in manifest["reaction_templates"]] for spk in DEMO_PERSONAS]
        self.assertEqual(reaction_ids, expected)
        # speakers come from personas.ROUNDS / FINAL, not from the manifest
        by_id = {i.id: i for i in items}
        self.assertEqual(by_id["round_intro_r1"].speaker, DEMO_PERSONAS[0])
        self.assertEqual(by_id["round_intro_r2"].speaker, DEMO_PERSONAS[1])
        self.assertEqual(by_id["round_intro_r3"].speaker, DEMO_PERSONAS[2])
        self.assertEqual(by_id["final_intro"].speaker, DEMO_PERSONAS[3])
        # question + fact texts come from backend/data/questions.json (the live bank)
        for q in manifest["demo_questions"]:
            item = by_id[f"question:{q['id']}"]
            fact = by_id[f"fun_fact:{q['id']}"]
            if q["id"] == "gk-11":  # not in the fake bank → manifest fallback
                self.assertEqual(item.text, q["text"])
                self.assertTrue(fact.text)
            else:
                self.assertEqual(item.text, f"BANK::{q['text']}")
                self.assertTrue(fact.text.startswith("BANK-FACT::"))

    def test_render_all_caches_ledger_and_is_idempotent(self) -> None:
        items = warmup.build_render_list()
        summary = asyncio.run(warmup.render_all(items))
        self.assertEqual((summary["rendered"], summary["skipped"], summary["failed"]),
                         (84, 0, 0))
        self.assertEqual(summary["chars"], sum(i.chars for i in items))
        # every clip cached under its exact tts_key, blob intact
        for item in items:
            key = cache.tts_key(text=item.text, speaker=item.speaker,
                                language_code=item.language_code, pace=item.pace,
                                sample_rate=item.sample_rate, codec=item.codec)
            blob = cache.cache_get(key)
            self.assertIsNotNone(blob, f"missing cache entry for {item.kind}:{item.id}")
            self.assertTrue(blob.startswith(b"RIFF"))
            self.assertIn(item.speaker.encode("utf-8"), blob)
        # one ledger line per synthesis, cumulative matches
        lines = (self.logs_dir / "spend.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 84)
        records = [json.loads(l) for l in lines]
        self.assertEqual(records[-1]["cumulative"], cache.chars_used())
        self.assertEqual(cache.chars_used(), sum(i.chars for i in items))
        self.assertEqual(len(self.fake_tts.warmup_calls), 84)
        # second run: everything already cached → pure skip, zero new spend
        before = cache.chars_used()
        calls_before = len(self.fake_tts.warmup_calls)
        summary2 = asyncio.run(warmup.render_all(items))
        self.assertEqual((summary2["rendered"], summary2["skipped"], summary2["failed"]),
                         (0, 84, 0))
        self.assertEqual(cache.chars_used(), before)
        self.assertEqual(len(self.fake_tts.warmup_calls), calls_before)
        # meta sidecar carries warmup provenance
        key = cache.tts_key(text=items[0].text, speaker=items[0].speaker,
                            language_code=items[0].language_code, pace=items[0].pace,
                            sample_rate=items[0].sample_rate, codec=items[0].codec)
        meta = json.loads((self.cache_dir / "tts" / key[:2] / f"{key}.meta.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(meta["kind"], items[0].kind)
        self.assertEqual(meta["model"], "bulbul:v4-flash")
        self.assertEqual(meta["source"], "rest")

    def test_render_all_isolates_per_item_failures(self) -> None:
        items = warmup.build_render_list()
        failed_item = next(i for i in items if i.id == "question:cricket-01")

        async def boom(text, speaker, language_code="hi-IN", pace=1.0, sample_rate=24000):
            if text == failed_item.text:
                raise RuntimeError("synthetic failure")
            return b"RIFF" + b"\x00" * 10

        summary = asyncio.run(warmup.render_all(items, synth=boom))
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["rendered"], 83)
        self.assertIn("question:cricket-01", summary["failures"][0])
        self.assertEqual(cache.chars_used(),
                         sum(i.chars for i in items) - failed_item.chars)

    def test_live_cli_end_to_end(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = warmup.main(["--live"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("live render of 84 demo-path items", out)
        self.assertIn('"failed": 0', out)
        self.assertEqual(cache.chars_used() > 0, True)


class TestNoEagerSiblingImports(unittest.TestCase):
    """CONTRACT §8: siblings must be imported lazily, never at module import."""

    def _top_level_imports(self, path: Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in tree.body:  # module level only — function bodies are fine
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module or "")
        return names

    def test_warmup_has_no_module_level_sibling_imports(self) -> None:
        names = self._top_level_imports(REPO_ROOT / "backend" / "scripts" / "warmup.py")
        for name in names:
            self.assertFalse(name.startswith("backend."),
                             f"eager sibling import: {name}")

    def test_cache_never_imports_siblings(self) -> None:
        tree = ast.parse((REPO_ROOT / "backend" / "cache.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name.startswith("backend."))
            elif isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or "").startswith("backend."))


if __name__ == "__main__":
    unittest.main()
