# BhashaBuzzer — बोलो और जीतो 🎙️⚡

A real-time Hinglish voice quiz-show in your browser with an AI host — built 100% on Sarvam's sovereign stack (**Bulbul V4 Flash** TTS · **saaras:v4** STT · **sarvam-105b** judge) for the **Sarvam Bulbul Build Challenge**.

Hold the buzzer, speak your answer, and the host talks back before you sit down.

🟡 **Under active development** — ships Oct 14, 2026.

## Development workflow
- `main` stays production-clean: docs + infra only. All product code lands via `feat/*` branches, merged only after full verification.
- The binding interface contract between all modules: [CONTRACT.md](CONTRACT.md) (frozen).
- Live API verification results: `scripts/probes_h0.py` → `research/plans/probe-results.json` (partner workspace).

## Quickstart (placeholder — lands with the first feature merge)
```bash
pip install -r requirements.txt
uvicorn backend.main:app --reload
```
