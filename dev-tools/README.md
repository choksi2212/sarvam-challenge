# BhashaBuzzer dev-tools (A10) — mock backend

Dev-only. **NOT deployed** — this directory exists so the frontend can run before the
real FastAPI backend (A6/A7) and the sibling frontend modules (A8 `audio/*`, A9 `ui/*`)
are merged. Everything here is zero-dep except the `ws` npm package (dev-only).

## What's inside

| file | purpose |
|---|---|
| `mock-server.js` | Node WS server on `ws://localhost:8080/ws` streaming a canned FULL match (CONTRACT §1/§2): `start` → lobby → round_intro → question → (buzz → listening → judging ~1 s → verdict) → next … 7 questions (R1×2 · R2×2 · R3×2 · sudden-death final), alternating correct/wrong verdicts. Audio frames are SILENCE in the probed §1 shape: first `audio_chunk` = a valid 44-byte RIFF header ONLY, then 24 kHz Int16LE mono PCM chunks (~2 s per utterance). Also serves the worktree root over HTTP so `/frontend/index.html` and `/dev-tools/stubs.js` are both reachable. |
| `stubs.js` | Minimal fakes for the sibling modules that are ABSENT in this worktree: `HostPlayer`, `MicRecorder`, `showScreen`, `setPhaseBadge` (+ a tiny valid silent WAV). `frontend/app.js` imports the real `ui/screens.js` / `audio/host-player.js` / `audio/mic-recorder.js` first and falls back to these via dynamic import — so this worktree runs standalone AND merges with zero conflicts. |
| `package.json` | `ws` dependency only. |

## Run the mock match

```bash
cd dev-tools
npm i          # once; installs the ws package (dev-only dependency)
node mock-server.js
```

Then open:

```
http://localhost:8080/frontend/index.html?mock=1
```

The `?mock=1` query param makes `frontend/app.js` point `GameWS` at
`ws://localhost:8080/ws` (without it, the app derives `ws(s)://<location.host>/ws` —
the shape the real FastAPI backend will serve).

Notes:

- `PORT=9090 node mock-server.js` changes the port (then edit the mock URL in the
  browser address bar accordingly — the `?mock=1` URL is hardcoded to 8080 in app.js
  per the contract's dev flow).
- In THIS worktree `frontend/index.html` is A9's file and doesn't exist yet — the
  static route still serves `/frontend/app.js`, `/dev-tools/stubs.js`, etc. Once A9's
  branch merges, the full page boots against the mock with zero changes.
- The mock emits real contract frames including `audio_start.caption`,
  `audio_end.ttfb_ms/e2e_ms`, and `state.speaker`/`persona_label` — the latency chips,
  CC bar, persona lower-third and COMBOS counter all tick against it.

## Tests

`frontend/tests/app-logic.test.js` runs under `node --test` from the repo root. The
mock-server integration test auto-skips if `npm i` hasn't been run (no `ws` package):
the unit tests (pure helpers, GameWS with a fake socket, stubs sanity) always run.
