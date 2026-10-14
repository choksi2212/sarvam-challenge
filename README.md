# BhashaBuzzer — बोलो और जीतो 🎙️⚡

**A real-time Hinglish voice quiz-show in your browser with an AI host — built 100% on Sarvam's sovereign stack for the [Sarvam Bulbul Build Challenge](https://x.com/SarvamAI/status/2109980905318326594).**

Hold the buzzer. Speak your answer. The host talks back **before you sit down**.

- 🗣️ **Host**: **Bulbul V4 Flash** TTS over WebSocket — sub-200 ms first audio, measured on our live smoke runs (75–197 ms TTFB)
- 👂 **Judge**: **saaras:v4** STT (codemix) → **sarvam-105b-conversations** LLM judge (~450 ms verdicts) with a deterministic stdlib fallback that never blocks the game
- 🎭 **224 voice-style combos as gameplay**: every round switches the host persona — dramatic quizmaster `ratan_hi_customer_expressive` → teasing Hinglish `ishita_enhi_customer_expressive` → stadium hype `ratan_hi_sports` → sudden-death suspense `mukul_hi_suspense`
- 🇮🇳 **Hinglish-first**: questions in native-script code-mixed Hindi/English, pronunciation dictionaries, comma-grouped numbers read correctly
- ♿ **Shy mode**: typed-answer fallback keeps the game fully playable without a mic
- 📊 **Honest latency ticker**: client-measured + server-decomposed (TTS TTFB / STT / judge / e2e) — never mixing clocks

## Quickstart

```bash
pip install -r requirements.txt
cp .env.example .env            # add your SARVAM_API_KEY
uvicorn backend.main:app --reload
# open http://127.0.0.1:8000 — tap ENTER STAGE, pick a name + language, buzz in
```

## How it works

```
browser (vanilla JS, AudioWorklet PCM player + mic recorder)
   ⇅ game WS (JSON frames)
FastAPI relay — transport ladder: sha256 cache → Bulbul WS → HTTP-stream → REST → text
   ├── question bank: 72 verified Hinglish questions (6 categories)
   ├── judge: sarvam-105b-conversations → deterministic alias/difflib fallback
   ├── personas: 4-language cast × 16 reaction templates (all roster-verified IDs)
   └── latency JSONL + spend governor (free-tier credits are sacred)
```

- `CONTRACT.md` — the binding interface contract all modules were built against
- 245 tests green (`python -m unittest discover -s backend/tests` + `node --test frontend/tests/`)
- Deploy: Dockerfile (WS-holding uvicorn) + `render.yaml` included

## Verification highlights
- Live end-to-end match: 5 utterances streamed at 75–197 ms TTFB, judge verdict `correct +150` with Hinglish banter, sub-3-second buzz→verdict loop
- The **224-combo counter** ticks up on-screen as hosts switch — the voice catalog *is* the game

**बोलो और जीतो।** Built with ❤️ for India's languages on [Sarvam AI](https://sarvam.ai).
