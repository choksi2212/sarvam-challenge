# BhashaBuzzer — Binding Contract (v2, FROZEN)
Frozen: Tue Oct 13, 2026 ~11:10 PM IST · Owner: orchestrator (changes = amendment commits only)
Every agent implements against THIS document. Where a plan doc disagrees, this wins.
Constants marked **[PROBED]** were verified live against api.sarvam.ai on 2026-10-13 (see research/plans/probe-results.json in the partner workspace).

## 1. Game WebSocket protocol (browser ⇄ server)

`ws://<host>/ws` · JSON text frames · one connection per game session.

### Client → Server
| frame | fields | semantics |
|---|---|---|
| `start` | `player_name` (2–20 chars), `language` (`"hi"\|"enhi"\|"en"\|"mr"`) | init session; server picks persona cast; host greeting + rules; then `round_intro` |
| `buzz` | — | player grabs mic; server cancels in-flight host audio for current utterance (barge), phase→`listening` |
| `answer` | `audio_b64` (base64 WAV 16 kHz mono) **OR** `text` (typed shy-mode) | push-to-talk release; never both |
| `barge` | — | client already stopped local playback; server drops pending chunks of current utterance_id |
| `repeat_question` | — | re-speak CURRENT question from cache (no re-synthesis, new utterance_id) |
| `skip_question` | — | next question; streak resets; zero penalty |
| `next_round` | — | after final scoreboard (client ack) |
| `ping` | — | every 25 s app-level liveness |

### Server → Client
| frame | fields | semantics |
|---|---|---|
| `state` | `phase`, `round`, `question_index`, `score`, `streak`, `speaker`, `persona_label`, `mode` | full state refresh; `speaker` is the literal Sarvam speaker ID (judge candy); phases in §2 |
| `question` | `id`, `text`, `category`, `difficulty`, `language` | question card + host starts speaking it |
| `audio_start` | `utterance_id`, `speaker`, `sample_rate`, `codec`(`"wav"`), `caption` | client latency t0; `caption` = exact host-speech text for the CC bar |
| `audio_chunk` | `utterance_id`, `b64` | **FIRST chunk = 44-byte RIFF header only [PROBED]**; subsequent = raw PCM continuation (Int16LE, mono, 24 kHz). Client strips header once after magic validation. Server guarantees this shape for BOTH live-WS and cached-blob paths (cached WAV → header chunk + PCM chunks). |
| `audio_end` | `utterance_id`, `ttfb_ms`, `total_ms`, `chars`, `cache_hit`, `source`(`"ws"\|"stream"\|"rest"\|"cache"`), `stt_ms`, `judge_ms`, `e2e_ms` | server-side decomposition of the just-ended utterance |
| `verdict` | `utterance_id`, `correct`(`"correct"\|"wrong"\|"partial"\|"unclear"`), `close_call`, `heard`, `template_id`, `hype_line`, `fact_line`, `score_delta`, `score`, `streak` | host reaction audio starts immediately after (same or new utterance_id) |
| `error` | `phase`, `code`, `recoverable`, `message` | recoverable=true → UI shows toast, session continues |

## 2. Phases & transitions
`lobby → round_intro → question → listening → judging → verdict → next → (question | round_switch | final)`
- `start`→lobby (greeting+rules, cached) → round_intro (per-round persona lower-third + intro VO)
- question: `question` frame + host audio; buzz window 10 s; barge allowed during host speech
- listening: answer window 5 s (client VAD 1.2 s cut / 8 s hard cap)
- judging: STT → judge (≤1.5 s target); empty/garbage STT → verdict `unclear` → host asks to repeat (1 grace repeat, no penalty)
- verdict: reaction (style-matched template + optional LLM hype_line) + fact_line; beat 1.2 s → next
- `skip_question` → next question, streak reset, no penalty · `repeat_question` → cached replay
- Demo Match: R1×2 (100 pts) → R2×2 (150) → R3×2 (200) → sudden-death final (300). Daily 5: 5 Qs, date-seeded.
- Scoring: base×round + speed bonus (+50/+30/+15 at ≤1/2/3 s) + streak (×1.5/×2/×2.5 at 2/3/4+); partial = ½ base, streak-neutral; never negative.

## 3. Binding backend signatures (Python 3.14, stdlib + fastapi/httpx/websockets only)

```python
# backend/config.py  (A7 owns)
SARVAM_API_KEY: str          # env var SARVAM_API_KEY, else ../.env line; NEVER hardcoded, never printed
BASE_URL="https://api.sarvam.ai"; TTS_MODEL="bulbul:v4-flash"; STT_MODEL="saaras:v4"; JUDGE_MODEL="sarvam-105b"
TTS_WS_URL="wss://api.sarvam.ai/text-to-speech/ws?model=bulbul:v4-flash&send_completion_event=true"
FRONTEND_DIR, DATA_DIR, CACHE_DIR, LOGS_DIR; MAX_SESSIONS=8

# backend/cache.py  (A2 owns)
GOVERNOR_LIMITS = {"warn": 30000, "cached_only": 40000, "hard": 45000}   # chars
def tts_key(text, speaker, language_code, pace, sample_rate, codec, dict_id=None, pitch=None, loudness=None) -> str
def cache_get(key) -> bytes | None            # wav blob
def cache_put(key, wav_bytes: bytes, meta: dict) -> None   # atomic tmp->rename; LRU byte-cap
def judge_key(question_id: str, normalized_answer: str) -> str
def judge_get(key) -> dict | None; def judge_put(key, verdict: dict) -> None
def log_spend(chars: int, what: str) -> None  # JSONL: {ts, chars, what, cumulative}
def chars_used() -> int

# backend/sarvam_tts.py  (A1 owns)
class VoiceChannel:
    async def connect() -> None                                  # open WS w/ auth header
    async def set_speaker(speaker: str, language_code: str, pace: float = 1.0) -> None
        # [PROBED] mid-connection {type:"config"} re-send WORKS — switch speakers without reconnect
    async def speak(text, utterance_id: str, on_first_chunk=None) -> AsyncIterator[bytes]
        # yields chunks in CONTRACT §1 audio_chunk shape: first = 44-byte RIFF header, rest raw PCM
        # [PROBED] Sarvam WS already sends exactly this shape — pass through, verify defensively
        # {type:"text"} + {type:"flush"}; terminates on {type:"event", data:{event_type:"final"}} [PROBED]
    async def ping() -> None; async def close() -> None
    # internal: 20 s ping keepalive task [PROBED: pings keep socket alive]; reconnect w/ backoff on abnormal close (1003 → 5 s cooldown; 1006 → retry; 4xxx auth/quota → fatal signal)
async def speak_via_stream(text, speaker, language_code, pace=1.0, sample_rate=24000) -> bytes   # POST /text-to-speech/stream, codec wav (set explicitly)
async def speak_via_rest(text, speaker, language_code, pace=1.0, sample_rate=24000) -> bytes    # POST /text-to-speech, decode audios[0]
def wav_blob_to_chunks(wav: bytes, chunk_pcm_bytes=32768) -> tuple[bytes, list[bytes]]          # cached blob → (header_chunk, [pcm_chunks]) matching live shape

# backend/sarvam_stt.py  (A3 owns)
class STTUnavailable(Exception)
async def transcribe_answer(wav_bytes: bytes, keyterms: list[str] | None = None) -> dict
    # POST /speech-to-text multipart: model="saaras:v4", mode="codemix", language_code="unknown",
    # keyterms=json.dumps(keyterms) [ALL PROBED 200]; returns {"text": str, "language_code": str, "raw": dict}
    # timeout 12 s; empty/garbage → STTUnavailable

# backend/judge.py  (A4 owns)
async def judge_answer(question: dict, heard: str, persona_label: str) -> dict
    # {"verdict": "correct|wrong|partial|unclear", "close_call": bool, "template_id": str, "hype_line": str}
    # sarvam-105b /v1/chat/completions, response_format json_schema STRICT (see §4 judge params), temp 0.2
    # ladder: strict schema → json_object → tolerant parse → string_match_fallback(question, heard)
def normalize_answer(text: str) -> str   # strip fillers/honorifics; fold Indian↔western commas; Devanagari numerals→ASCII; word-numbers→digits (aik→1 etc.); lowercase latin
def string_match_fallback(question: dict, heard: str) -> dict

# backend/personas.py  (A5 owns)
TEMPLATE_IDS = ["correct_fast","correct_confident","correct_slow","wrong_confident","wrong_funny","wrong_close",
                "partial","unclear","timeout","no_answer","repeat_request","streak_3","streak_5",
                "final_win","final_lose","game_over"]   # binding enum — judge returns ids from THIS list only
ROUNDS: dict[lang, dict[int, dict]]   # {lang: {round: {"speaker": str, "label": str, "style_tag": str}}}
FINAL: dict[lang, dict]              # sudden-death host per language
def persona_for_round(round_num: int, language: str) -> dict
def persona_label(speaker: str) -> str

# backend/game.py  (A6 owns)
class GameSession:
    def __init__(self, send: Callable[[dict], Awaitable[None]], voice, judge_fn, bank: dict) -> None
        # voice + judge_fn INJECTED (duck-typed): voice.speak(text, utterance_id, on_first_chunk) yields bytes;
        # voice.set_speaker(...); judge_fn(question, heard, persona_label) -> dict. Tests use FakeVoice/FakeJudge.
    async def handle(msg: dict) -> None   # one C→S frame from §1; drives §2 phases + emits S→C frames via send
    # modes: "demo" (7Q fixed arc) | "daily5" (5Q, date-seeded); deterministic per-session question order

# backend/main.py  (A7 owns)  — FastAPI app
# GET /  → frontend/ static · GET /healthz → {"ok", "chars_used", "sessions"} · POST /api/warmup → prewarm
# WS /ws → per-connection: GameSession(send) + VoiceChannel; TRANSPORT LADDER lives here:
#   cache.get(tts_key(...)) → [hit: wav_blob_to_chunks] → [miss: voice.speak (WS)] → on WS failure:
#   speak_via_stream → speak_via_rest → error frame with text-only. log_spend on every synthesis.
# Latency stamps: t_buzz/t_audio_in/t_stt_done/t_judge_done/t_first_chunk/t_tts_end → audio_end fields + logs/latency.jsonl
# Guards: session semaphore (≤8), WS-open stagger ≥300 ms, 429/503 backoff 0.5→4 s ≤2 retries, judge ≤24/min
```

## 4. Sarvam API constants **[PROBED 2026-10-13]**
- **WS TTFB measured: 23 ms first chunk (warm), 180 ms after in-place speaker reconfig** — well under the 250 ms claim.
- **WAV chunk framing: first chunk = 44-byte RIFF header ONLY; rest = raw PCM continuation** — matches §1 exactly.
- **Config re-send mid-connection: WORKS** — `set_speaker` = new `{type:"config"}` message, no reconnect.
- `send_completion_event=true` on the WS URL: accepted; `{type:"event",data:{event_type:"final"}}` observed.
- `pitch`/`loudness` accepted on v4-flash REST (200) — use sparingly; identity comes from persona + pace.
- Pronunciation dict: created `p_8eea3524` (hi-IN), accepted with v4-flash. Pass `dict_id` in WS config when used.
- STT: `saaras:v4` + `mode="codemix"` + `language_code="unknown"` + `keyterms` JSON-string → 200, transcript + detected language_code + probability. 22.05 kHz WAV accepted; client sends 16 kHz mono.
- HTTP stream: 200 `audio/mpeg` default codec — **always set `output_audio_codec:"wav"` explicitly**.
- Chat judge: `sarvam-105b` **is a reasoning model** — `max_tokens=120` burned out mid-reasoning (`finish_reason:"length"`, `content:null`) at 905 ms. **Judge params: `max_tokens=2000`, `reasoning_effort="low"`, `response_format=json_schema strict`** (re-verified at integration; if `content` still null → raise `max_tokens` to 4000 or drop `reasoning_effort`).
- Everywhere: `model` explicit, `enable_preprocessing:true`, numbers >4 digits comma-grouped, `wav` @24 kHz, mono.
- Speaker IDs: lowercase, from `data/personas.json` ONLY (all verified against the live 224-ID roster).

## 5. Binding JS module APIs (frontend, no framework, no build step)
```js
// ws-client.js (A10)
export class GameWS { constructor(url, {onOpen, onClose, onError, onFrame}); send(obj); close(); get state(); }
   // JSON text frames; auto-reconnect w/ exponential backoff + jitter; onOpen re-sends last start if reconnected

// audio/host-player.js (A8)
export class HostPlayer {
  async init()                 // AudioContext at DEVICE rate; addModule('audio/pcm-worklet.js'); call on user gesture
  startUtterance(utteranceId, {sampleRate = 24000, onFirstSample})   // t0; per-id strip-state reset
  feedChunk(utteranceId, b64)  // main thread: validate+strip RIFF on FIRST chunk per id; enqueue Int16 → worklet
  endUtterance(utteranceId)
  stopNow()                    // barge: clear ring buffer, silence instantly
  get volume(); set volume(v)  // 0..1
}

// audio/mic-recorder.js (A8)
export class MicRecorder {
  async start({onWaveform})    // getUserMedia {audio:true, echoCancellation:true, noiseSuppression:true, autoGainControl:true}
                               // 200 ms pre-roll discarded; 8 s hard auto-stop
  async stop()                 // → {wavB64, ms}   (16 kHz mono WAV)
  cancel()
}

// audio/wav-encoder.js (A8)
export function encodeWavToB64(float32Chunks, {sampleRate = 16000, channels = 1}) -> string

// ui/screens.js (A9)
export function showScreen(id)           // one of §6 section ids; handles transitions
export function setPhaseBadge(phase)     // small phase indicator
```

## 6. Binding DOM skeleton (ids BOTH A9 and A10 target — A9 owns the markup/styles, A10 owns behavior)
```
#app
  header#topbar: #latency-ticker (#lat-host, #lat-loop, #lat-server) · #score-chip (#score) · #combo-chip (#combo-count)
  main#stage
    section#screen-splash      > #btn-enter
    section#screen-setup       > #input-name · #lang-chips · #btn-start
    section#screen-lobby        > #lobby-msg
    section#screen-question     > #q-card (#q-text, #q-category, #q-difficulty) · #btn-repeat · #btn-skip
    section#screen-buzz         > #buzzer · #waveform · #rec-timer
    section#screen-verdict      > #verdict-stamp · #heard-text · #host-line
    section#screen-final        > #final-score · #btn-again
    #persona-lower-third        > #persona-name · #persona-style · #persona-id
    #cc-bar                     (global caption bar)
    #type-fallback              > #type-input · #type-submit   (shy mode; visible when mic denied or toggled)
    #toast
```

## 7. Cache & budget rules (sacred)
- Cache-first on EVERY synthesis; identical (text, speaker, params) NEVER re-billed. Judge cache on (question_id, normalized heard).
- Every synthesis → `log_spend(chars, what)`; governor: warn at 30k, cached-only at 40k, hard-stop 45k chars (server falls back to cache/text-only gracefully).
- Reaction templates pre-rendered for the 4 demo personas at warmup; live LLM hype_line only as garnish.

## 8. Build rules (all agents)
- **NO live Sarvam API calls during build.** Mocks/fakes/in-process WS stubs only. Never read any `.env`. Integration + warmup run only by the orchestrator.
- Own ONLY your assigned files. Never `git merge`/`checkout` other branches. `git add` only your paths.
- Commit every ~30 min of work: `git -C <worktree> add <your paths> && git -C <worktree> commit -m "feat(<area>): …" && git -C <worktree> push -u origin feat/<branch>` (retry push once on transient failure).
- Tests: stdlib `unittest` (`backend/tests/test_<module>.py`), runnable via `python -m unittest discover -s backend/tests`; pure-JS modules get `node --test` files under `frontend/tests/`. All tests must pass in your worktree before your final push.
- Style: production-pragmatic. Type hints on public functions. No scope creep: BUILD_PLAN §R15 CUT LIST is final (no 2-player, no te/kn/pa/bn UI, no LinkedIn automation).
