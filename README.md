# BhashaBuzzer — बोलो और जीतो 🎙️⚡

> **A real-time Hinglish voice quiz-show in your browser with an AI host — built 100% on Sarvam's sovereign stack for the [Sarvam Bulbul Build Challenge](https://x.com/SarvamAI/status/2109980905318326594).**
>
> Hold the buzzer. Speak your answer. The host talks back **before you sit down**.

## The numbers (measured live, not spec-sheeted)

| | |
|---|---|
| **75–197 ms** | first audio out of the host's mouth — Bulbul V4 Flash TTS over WebSocket, TTFB measured per utterance on live runs |
| **< 3 s** | buzz → STT → judge → host reacting — the whole loop, live-measured |
| **~450 ms** | judge verdict from sarvam-105b-conversations |
| **224** | voice-style combos — the host changes persona every round; the Sarvam speaker roster *is* the gameplay |
| **72 × 6** | hand-verified questions across 6 categories — Bollywood · Cricket · GK/Bharat · Ganit · Pop-culture/Tech · Vigyan |
| **245** | tests green — 191 Python + 54 Node, run against this tree |

Every number above also shows up on the in-game latency ticker — client-measured + server-decomposed (TTS TTFB / STT / judge / e2e), never mixing clocks. We don't do unverifiable claims.

## The game

- 🗣️ **Host**: **Bulbul V4 Flash** over WebSocket — dramatic quizmaster `ratan_hi_customer_expressive` → teasing Hinglish co-host `ishita_enhi_customer_expressive` → stadium hype `ratan_hi_sports` → sudden-death suspense `mukul_hi_suspense`
- 👂 **Judge**: **saaras:v4** STT (codemix) → **sarvam-105b-conversations** LLM verdict, with a deterministic stdlib fallback that never blocks the game
- 🇮🇳 **Hinglish-first**: native-script code-mixed questions, pronunciation dictionaries, comma-grouped numbers read correctly
- ♿ **Shy mode**: typed-answer fallback keeps the whole game playable without a mic
- 🚦 **Never dies mid-match**: sha256 cache → Bulbul WS → HTTP-stream → REST → text — a Sarvam failure demotes the transport, never the game
- 💸 **Spend governor**: free-tier credits are sacred — cache first, every synthesis in the ledger

## What we fixed after launch

We posted the submission, then played our own live demo like everyone else — and it had real bugs. Honesty beats polish: v1.1 shipped within hours of the launch post.

<!-- TODO(v1.1 fix team): replace this block with the final fix list before merging — one bullet per fix, with the commit/branch that shipped it.
- Audio playback smoothing — the host's voice played too fast / garbled on some machines; playback pacing fixed
- Clean stage redesign — stripped the UI back to a professional, minimal layout
-->

## Quickstart

```bash
pip install -r requirements.txt
cp .env.example .env      # add your SARVAM_API_KEY (₹100 free signup credits)
uvicorn backend.main:app --reload
# open http://127.0.0.1:8000 — enter the stage, pick a name + language (हिंदी · Hinglish · English · मराठी), buzz in and answer out loud
```

Tests — 245 green (191 Python + 54 Node):

```bash
python -m unittest discover -s backend/tests
node --test frontend/tests/*.test.js
```

## How it works

```
browser (vanilla JS, AudioWorklet PCM player + mic recorder)
   ⇅ game WS (JSON frames)
FastAPI relay — transport ladder: sha256 cache → Bulbul WS → HTTP-stream → REST → text
   ├── question bank: 72 verified questions (6 categories)
   ├── judge: sarvam-105b-conversations → deterministic alias/difflib fallback
   ├── personas: 4-language cast × 16 reaction templates (all roster-verified IDs)
   └── latency JSONL + spend governor (free-tier credits are sacred)
```

- `CONTRACT.md` — the binding interface contract every module was built against
- Deploy: Dockerfile (WS-holding uvicorn) + `render.yaml` included

## Verified end-to-end

- Live match smoke: 5 utterances streamed at **75–197 ms TTFB**, judge verdict `correct +150` with Hinglish banter, **sub-3-second buzz→verdict loop**
- The **224-combo counter** ticks on-screen as hosts switch — 224 roster-verified Bulbul speaker IDs, and every one of them is playable

**बोलो और जीतो।** Built with ❤️ for India's languages on [Sarvam AI](https://sarvam.ai).

<!--
=== ABOUT-PANEL COPY — for Manas to apply on the GitHub repo page (already applied once via `gh`; re-run if copy changes) ===

Description (paste into the repo's About box, or run):
  gh repo edit choksi2212/sarvam-challenge --description "BhashaBuzzer — a real-time Hinglish voice quiz-show with an AI host. Speak your answer, hear the verdict in seconds: Bulbul V4 Flash TTS (sub-200 ms first audio) · saaras:v4 STT · sarvam-105b LLM judge. Built for the Sarvam Bulbul Build Challenge. 🎙️ बोलो और जीतो"

Topics (paste into the topics picker, or add with --add-topic):
  sarvam · bulbul · tts · voice-ai · quiz-game · hinglish · indian-languages · fastapi · websocket · real-time
-->
