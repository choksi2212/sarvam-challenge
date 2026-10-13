"""BhashaBuzzer demo-path warmup — A2 (CONTRACT §3/§7; backend-plan §5.3, D12).

Pre-renders EVERY demo-path clip into the TTS cache so demo-day synthesis is
all cache hits (CONTRACT §7: identical (text, speaker, params) never re-billed):

* host intro + rules VO (R1 quizmaster)
* 3 round intros (R1/R2/R3 persona each) + sudden-death final intro
* all 16 reaction templates (personas.TEMPLATE_IDS) × the 4 demo personas:
  ratan_hi_customer_expressive, ishita_enhi_customer_expressive,
  ratan_hi_sports, mukul_hi_suspense
* the demo questions (7-question demo match) + their fun-fact lines

Sibling modules (backend.sarvam_tts, backend.personas, backend.data,
backend.config) are NOT present in the A2 worktree (CONTRACT §8: each agent owns
only their files), so every sibling is imported LAZILY inside a function — this
module therefore imports standalone and is unit-testable with fakes injected
into ``sys.modules``.

CLI (CONTRACT §8: NEVER run live during build; the orchestrator runs --live
once, post-integration):

* ``python -m backend.scripts.warmup --dry-run``  (default) — prints the exact
  render list (text + speaker pairs), total char count and estimated rupees,
  sourced ONLY from the committed mock manifest
  (backend/scripts/warmup_demo_data.json). Zero Sarvam API calls, zero sibling
  imports.
* ``--live`` — synthesize-and-cache each missing clip via
  ``backend.sarvam_tts.speak_via_rest``, log every synthesis in the spend
  ledger, idempotent (already-cached keys are skipped).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
REPO_ROOT = BACKEND_DIR.parent
MOCK_DATA_PATH = SCRIPT_DIR / "warmup_demo_data.json"

# Mirror of contract constants (A1/A7 own the live ones; read-only here).
TTS_MODEL = "bulbul:v4-flash"
DEMO_LANG_KEY = "hi"          # personas.ROUNDS / FINAL language key for the demo cast
DEMO_LANGUAGE = "hi-IN"       # TTS language_code for the hi/Hinglish demo path
DEFAULT_SAMPLE_RATE = 24000
DEFAULT_CODEC = "wav"


@dataclass(frozen=True)
class RenderItem:
    """One planned synthesis: text + the exact voice parameters that key it."""

    id: str
    kind: str
    text: str
    speaker: str
    language_code: str = DEMO_LANGUAGE
    pace: float = 1.0
    sample_rate: int = DEFAULT_SAMPLE_RATE
    codec: str = DEFAULT_CODEC
    dict_id: str | None = None
    pitch: float | None = None
    loudness: float | None = None

    @property
    def chars(self) -> int:
        return len(self.text)


# ---------------------------------------------------------------------------
# Lazy sibling imports (siblings are ABSENT in the A2 worktree — CONTRACT §8)
# ---------------------------------------------------------------------------

def _ensure_repo_on_path() -> None:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))


def _import_backend(name: str) -> Any:
    """Import a sibling module lazily (fakes in sys.modules win — test hook)."""
    _ensure_repo_on_path()
    return importlib.import_module(name)


def _cache_module() -> Any:
    return _import_backend("backend.cache")


def _personas_module() -> Any:
    return _import_backend("backend.personas")


def _tts_module() -> Any:
    return _import_backend("backend.sarvam_tts")


def _config_module() -> Any:
    return _import_backend("backend.config")


def _data_dir() -> Path:
    """DATA_DIR from A7's config if present, else <repo>/data (contract layout)."""
    try:
        cfg = _config_module()
    except ImportError:
        return REPO_ROOT / "data"
    return Path(getattr(cfg, "DATA_DIR", REPO_ROOT / "data"))


def _load_questions() -> list[dict]:
    """The live question bank (backend/data/questions.json) — LAZY sibling data."""
    path = _data_dir() / "questions.json"
    with path.open("r", encoding="utf-8") as fh:
        bank = json.load(fh)
    if isinstance(bank, dict):  # tolerate {"questions": [...]} wrappers
        bank = bank.get("questions", [])
    if not isinstance(bank, list):
        raise ValueError(f"{path}: expected a list of question objects")
    return bank


def _load_reactions() -> dict[str, str]:
    """template_id -> reaction line from backend/data/reactions.json (if shipped)."""
    path = _data_dir() / "reactions.json"
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    lines: dict[str, str] = {}
    if isinstance(data, dict):
        for tid, value in data.items():
            if isinstance(value, str):
                lines[tid] = value
            elif isinstance(value, dict):
                text = value.get("text") or value.get("line") or value.get("hi-IN") or value.get("hi")
                if isinstance(text, str):
                    lines[tid] = text
    return lines


# ---------------------------------------------------------------------------
# Mock manifest (committed demo-path data — the --dry-run source of truth)
# ---------------------------------------------------------------------------

def load_mock_manifest() -> dict:
    """The committed mock demo-path manifest under backend/scripts/."""
    with MOCK_DATA_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def build_render_list_from_mock() -> list[RenderItem]:
    """Exact demo-path render list from the committed manifest (no siblings)."""
    manifest = load_mock_manifest()
    persona_cfg: dict[str, dict] = manifest["personas"]

    def make(item_id: str, kind: str, text: str, speaker: str,
             pace: float | None = None, language_code: str | None = None) -> RenderItem:
        cfg = persona_cfg.get(speaker, {})
        return RenderItem(
            id=item_id, kind=kind, text=text, speaker=speaker,
            language_code=language_code or cfg.get("language_code", DEMO_LANGUAGE),
            pace=pace if pace is not None else cfg.get("pace", 1.0),
        )

    items: list[RenderItem] = []
    intro = manifest["host_intro"]
    items.append(make(intro["id"], "host_intro", intro["text"], intro["speaker"]))
    rules = manifest["rules"]
    items.append(make(rules["id"], "rules", rules["text"], rules["speaker"]))
    for entry in manifest["round_intros"]:
        items.append(make(entry["id"], "round_intro", entry["text"], entry["speaker"],
                          pace=entry.get("pace")))
    final = manifest.get("final_intro")
    if final:
        items.append(make(final["id"], "final_intro", final["text"], final["speaker"],
                          pace=final.get("pace")))
    for tpl in manifest["reaction_templates"]:          # 16 templates (contract order)
        for speaker in manifest["template_personas"]:   # × the 4 demo personas
            items.append(make(f"reaction:{tpl['id']}:{speaker}", "reaction",
                              tpl["text"], speaker, pace=tpl.get("pace")))
    for q in manifest["demo_questions"]:
        items.append(make(f"question:{q['id']}", "question", q["text"], q["speaker"],
                          language_code=q.get("language", DEMO_LANGUAGE)))
    for fact in manifest["fun_facts"]:
        items.append(make(fact["id"], "fun_fact", fact["text"], fact["speaker"]))
    return items


# ---------------------------------------------------------------------------
# Live render list (LAZY sibling imports; mock manifest is the fallback)
# ---------------------------------------------------------------------------

def _question_text(q: dict | None) -> str | None:
    if not isinstance(q, dict):
        return None
    text = q.get("question_text") or q.get("text")
    return text if isinstance(text, str) else None


def _fact_text(q: dict | None) -> str | None:
    if not isinstance(q, dict):
        return None
    text = q.get("fun_fact_line") or q.get("fact_line") or q.get("fun_fact")
    return text if isinstance(text, str) else None


def build_render_list(log: Callable[[str], None] = print) -> list[RenderItem]:
    """LIVE render list. Siblings are imported lazily INSIDE this function.

    Layering (live wins, committed manifest is the fallback): template ids and
    the demo cast come from backend.personas (TEMPLATE_IDS / ROUNDS / FINAL);
    question + reaction texts come from backend/data JSONs; anything a sibling
    does not ship yet falls back to the committed mock manifest, so the whole
    demo path is always renderable post-integration.
    """
    personas = _personas_module()               # lazy: backend.personas
    manifest = load_mock_manifest()
    reactions = _load_reactions()              # lazy: backend/data/reactions.json
    bank = {q.get("id"): q for q in _load_questions()}  # lazy: backend/data/questions.json

    # ---- demo cast: R1..R3 + sudden-death final speakers (contract §3 personas)
    rounds = getattr(personas, "ROUNDS", None) or {}
    final = getattr(personas, "FINAL", None) or {}
    hi_rounds = rounds.get(DEMO_LANG_KEY, {}) if isinstance(rounds, dict) else {}
    round_speaker: dict[int, str | None] = {
        rn: (hi_rounds.get(rn) or {}).get("speaker") for rn in (1, 2, 3)
    }
    final_speaker = (final.get(DEMO_LANG_KEY) or {}).get("speaker") if isinstance(final, dict) else None
    cast: list[str] = []
    for speaker in (*round_speaker.values(), final_speaker):
        if speaker and speaker not in cast:
            cast.append(speaker)
    if not cast:
        cast = list(manifest["template_personas"])
    round_speaker[4] = final_speaker  # round 4 == sudden-death final

    def speaker_for_round(round_num: int, fallback: str) -> str:
        return round_speaker.get(round_num) or fallback

    # ---- reaction templates: ids from personas (contract enum), texts from data
    template_ids = list(getattr(personas, "TEMPLATE_IDS", None)
                        or [t["id"] for t in manifest["reaction_templates"]])
    mock_tpl = {t["id"]: t for t in manifest["reaction_templates"]}

    # ---- questions/facts: live bank rows for the manifest's demo ids
    mock_questions = {q["id"]: q for q in manifest["demo_questions"]}
    mock_facts = {f["question_id"]: f for f in manifest["fun_facts"]}

    items: list[RenderItem] = []
    missing: list[str] = []

    def add(item_id: str, kind: str, text: str | None, speaker: str,
            round_num: int | None = None, pace: float | None = None,
            language_code: str | None = None) -> None:
        if not text:
            missing.append(item_id)
            return
        cfg = manifest["personas"].get(speaker, {})
        items.append(RenderItem(
            id=item_id, kind=kind, text=text, speaker=speaker,
            language_code=language_code or cfg.get("language_code", DEMO_LANGUAGE),
            pace=pace if pace is not None else cfg.get("pace", 1.0),
        ))

    intro = manifest["host_intro"]
    add(intro["id"], "host_intro", intro["text"], speaker_for_round(1, intro["speaker"]))
    rules = manifest["rules"]
    add(rules["id"], "rules", rules["text"], speaker_for_round(1, rules["speaker"]))

    for entry in manifest["round_intros"]:
        add(entry["id"], "round_intro", entry["text"],
            speaker_for_round(int(entry.get("round", 1)), entry["speaker"]),
            pace=entry.get("pace"))
    final_intro = manifest.get("final_intro")
    if final_intro:
        add(final_intro["id"], "final_intro", final_intro["text"],
            speaker_for_round(4, final_intro["speaker"]), pace=final_intro.get("pace"))

    for tid in template_ids:
        tpl = mock_tpl.get(tid, {})
        text = reactions.get(tid) or tpl.get("text")
        for speaker in cast:
            add(f"reaction:{tid}:{speaker}", "reaction", text, speaker,
                pace=tpl.get("pace"))

    for qid in mock_questions:
        entry = mock_questions[qid]
        row = bank.get(qid)
        round_num = int(entry.get("round", 1))
        add(f"question:{qid}", "question", _question_text(row) or entry["text"],
            speaker_for_round(round_num, entry["speaker"]),
            language_code=(row or {}).get("language", entry.get("language", DEMO_LANGUAGE)))
        fact_entry = mock_facts.get(qid, {})
        add(f"fun_fact:{qid}", "fun_fact", _fact_text(row) or fact_entry.get("text"),
            speaker_for_round(round_num, fact_entry.get("speaker", entry["speaker"])))

    if missing:
        log(f"warmup: WARNING — no text for {len(missing)} items: {missing}")
    return items


# ---------------------------------------------------------------------------
# Rendering (LIVE path — orchestrator only; never called during build)
# ---------------------------------------------------------------------------

async def render_all(items: list[RenderItem], *,
                     synth: Callable[..., Awaitable[bytes]] | None = None,
                     cache: Any = None,
                     log: Callable[[str], None] = print) -> dict:
    """Cache-first, idempotent synth loop: miss → speak_via_rest → put → log_spend.

    ``synth`` / ``cache`` default to the LAZY sibling imports
    (backend.sarvam_tts.speak_via_rest / backend.cache); tests inject fakes.
    """
    cache = cache or _cache_module()
    if synth is None:
        synth = getattr(_tts_module(), "speak_via_rest")  # lazy: backend.sarvam_tts

    rendered: list[RenderItem] = []
    skipped: list[RenderItem] = []
    failures: list[str] = []
    for item in items:
        key = cache.tts_key(text=item.text, speaker=item.speaker,
                            language_code=item.language_code, pace=item.pace,
                            sample_rate=item.sample_rate, codec=item.codec,
                            dict_id=item.dict_id, pitch=item.pitch,
                            loudness=item.loudness)
        if cache.cache_get(key) is not None:
            skipped.append(item)
            continue
        try:
            wav = await synth(text=item.text, speaker=item.speaker,
                              language_code=item.language_code, pace=item.pace,
                              sample_rate=item.sample_rate)
        except Exception as exc:  # one bad clip must not abort the warmup
            failures.append(f"{item.id} ({type(exc).__name__}: {exc})")
            log(f"warmup: FAIL {item.id} — {exc}")
            continue
        cache.cache_put(key, wav, {"model": TTS_MODEL, "speaker": item.speaker,
                                  "chars": item.chars, "kind": item.kind,
                                  "source": "rest", "text": item.text})
        cache.log_spend(item.chars, f"warmup:{item.kind}:{item.id}")
        rendered.append(item)
    return {
        "rendered": len(rendered),
        "skipped": len(skipped),
        "failed": len(failures),
        "chars": sum(i.chars for i in rendered),
        "rendered_ids": [i.id for i in rendered],
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _estimated_rs(chars: int, rs_per_10k: float) -> float:
    return round(chars * rs_per_10k / 10_000, 2)


def print_dry_run(items: list[RenderItem], manifest: dict | None = None,
                  out: Any = None) -> None:
    """Exact render list + char total + rupee estimate — no API calls, no siblings."""
    if out is None:
        out = sys.stdout
    manifest = manifest or load_mock_manifest()
    rate = float(manifest.get("pricing", {}).get("rs_per_10k_chars", 20.0))
    total_chars = sum(i.chars for i in items)
    n = len(items)
    print("BhashaBuzzer warmup — DRY RUN (no Sarvam API calls, no sibling imports)", file=out)
    print(f"manifest: {MOCK_DATA_PATH}", file=out)
    for idx, item in enumerate(items, 1):
        print(f"[{idx}/{n}] {item.kind} {item.id} | speaker={item.speaker} "
              f"lang={item.language_code} pace={item.pace} | chars={item.chars}", file=out)
        print(f"    {item.text}", file=out)
    print(f"TOTAL items={n} chars={total_chars}", file=out)
    print(f"ESTIMATED Rs {_estimated_rs(total_chars, rate):.2f} "
          f"(at Rs {rate} per 10K chars, model {TTS_MODEL})", file=out)
    print("SUMMARY " + json.dumps({"dry_run": True, "items": n, "chars": total_chars,
                                   "estimated_rs": _estimated_rs(total_chars, rate)},
                                  ensure_ascii=False), file=out)


async def _run_live() -> int:
    items = build_render_list()
    print(f"warmup: live render of {len(items)} demo-path items "
          f"(cache-first; existing keys skipped)")
    summary = await render_all(items)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    cache = _cache_module()
    print(f"warmup: chars_used={cache.chars_used()} governor={cache.governor_level()}")
    return 0 if summary["failed"] == 0 else 1


def _force_utf8_stdout() -> None:
    """Hindi text must survive Windows pipes (cp1252 default would crash)."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="warmup",
        description="Pre-render the BhashaBuzzer demo path into the TTS cache.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the exact render list + chars + estimated Rs "
                             "from the committed mock manifest; no API calls")
    parser.add_argument("--live", action="store_true",
                        help="actually synthesize into the cache — ORCHESTRATOR "
                             "ONLY (CONTRACT §8: never run live during build)")
    args = parser.parse_args(argv)
    _force_utf8_stdout()

    if not args.live or args.dry_run:  # default is a dry run; --dry-run wins over --live
        print_dry_run(build_render_list_from_mock())
        if not args.dry_run:
            print("note: default mode is a dry run; pass --live to render "
                  "(orchestrator only)")
        return 0
    return asyncio.run(_run_live())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
