"""BhashaBuzzer — first LIVE end-to-end smoke (integration checkpoint).
Plays a scripted demo match against the running server with REAL Sarvam calls.
Measures TTFB per utterance and validates the full contract frame flow.
Spend: ~500-1500 TTS chars (~Rs 1-3). The cache layer persists everything it synthesizes.
"""
import asyncio
import base64
import json
import sys
import time
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parents[1]          # integration worktree root
CHALL = ROOT.parent.parent                           # sarvam-challenge/ (.env lives here)
KEY = None
for line in (CHALL / ".env").read_text(encoding="utf-8").splitlines():
    if line.startswith("SARVAM_API_KEY="):
        KEY = line.split("=", 1)[1].strip()

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
URL = f"ws://127.0.0.1:{PORT}/ws"

# Map question text -> expected answer from the bank (so we can answer CORRECTLY and validate scoring)
bank = json.loads((ROOT / "backend" / "data" / "questions.json").read_text(encoding="utf-8"))
if isinstance(bank, dict):
    bank = bank.get("questions", bank)
BY_TEXT = {q["question_text"]: q for q in bank}
BY_ID = {q["id"]: q for q in bank}


async def main():
    report = {"frames": [], "utterances": {}, "verdicts": []}
    cur_ut = None
    question_text = None
    got_verdict = asyncio.Event()

    async with websockets.connect(URL, max_size=2**24) as ws:
        print(f"[smoke] connected {URL}")
        t0 = time.perf_counter()
        await ws.send(json.dumps({"type": "start", "player_name": "Manas", "language": "hi"}, ensure_ascii=False))
        phase = None
        answered = False
        while time.perf_counter() - t0 < 120:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=30)
            except asyncio.TimeoutError:
                print("[smoke] 30s frame silence — ending")
                break
            m = json.loads(raw)
            t = m.get("type")
            report["frames"].append(t)

            if t == "state":
                phase = m.get("phase")
                print(f"[{time.perf_counter()-t0:6.1f}s] state: phase={phase} round={m.get('round')} "
                      f"score={m.get('score')} speaker={m.get('speaker')} persona={m.get('persona_label')}")
                if phase == "question" and answered:
                    break  # reached next question after a full verdict cycle — done
            elif t == "question":
                question_text = m.get("text")
                q = BY_TEXT.get(question_text)
                print(f"[{time.perf_counter()-t0:6.1f}s] QUESTION ({m.get('category')}, d{m.get('difficulty')}): {question_text}")
                print(f"         expected: {q['expected_answer'] if q else '?? unknown'}")
            elif t == "audio_start":
                cur_ut = m["utterance_id"]
                report["utterances"][cur_ut] = {"t_start": time.perf_counter(), "chunks": 0, "bytes": 0,
                                                "first_chunk_ms": None, "speaker": m.get("speaker"),
                                                "caption": (m.get("caption") or "")[:70]}
            elif t == "audio_chunk":
                u = report["utterances"].get(m.get("utterance_id") or cur_ut)
                if u is not None:
                    if u["first_chunk_ms"] is None:
                        u["first_chunk_ms"] = round((time.perf_counter() - u["t_start"]) * 1000)
                    u["chunks"] += 1
                    u["bytes"] += len(base64.b64decode(m.get("b64", "")))
                    if u["chunks"] == 1:
                        magic = base64.b64decode(m.get("b64", ""))[:4]
                        u["first_magic"] = magic.decode("latin1")
            elif t == "audio_end":
                u = report["utterances"].get(m.get("utterance_id"), {})
                u["total_ms"] = m.get("total_ms")
                u["ttfb_ms"] = m.get("ttfb_ms")
                u["chars"] = m.get("chars")
                u["cache_hit"] = m.get("cache_hit")
                u["source"] = m.get("source")
                u["e2e_ms"] = m.get("e2e_ms")
                if question_text is not None and not answered and m.get("utterance_id") == cur_ut:
                    # question speech done -> buzz + answer CORRECTLY (from bank)
                    q = BY_TEXT.get(question_text)
                    ans = q["expected_answer"] if q else "दिल्ली"
                    await ws.send(json.dumps({"type": "buzz"}))
                    await asyncio.sleep(0.25)
                    await ws.send(json.dumps({"type": "answer", "text": ans}, ensure_ascii=False))
                    answered = True
                    print(f"[{time.perf_counter()-t0:6.1f}s] BUZZ + answered (typed): {ans}")
            elif t == "verdict":
                print(f"[{time.perf_counter()-t0:6.1f}s] VERDICT: {m.get('correct')} close_call={m.get('close_call')} "
                      f"heard='{m.get('heard')}' +{m.get('score_delta')} -> score={m.get('score')} "
                      f"template={m.get('template_id')}")
                print(f"         hype: {m.get('hype_line')}")
                report["verdicts"].append(m)
                got_verdict.set()
            elif t == "error":
                print(f"[{time.perf_counter()-t0:6.1f}s] ERROR frame: {m}")
            else:
                print(f"[{time.perf_counter()-t0:6.1f}s] {t}: {str(m)[:110]}")

    print("\n================ SMOKE REPORT ================")
    for uid, u in report["utterances"].items():
        cap = u["caption"] + "…" if len(u["caption"]) > 60 else u["caption"]
        print(f"ut {uid[:14]}: {u.get('speaker')} | chunks={u['chunks']} bytes={u['bytes']} "
              f"ttfb={u.get('first_chunk_ms')}ms total={u.get('total_ms')}ms src={u.get('source')} "
              f"cache={u.get('cache_hit')} | {cap}")
    print(f"frame sequence: {report['frames'][:40]}")
    print(f"verdicts: {len(report['verdicts'])}")

    out = CHALL / "research" / "plans" / "e2e-smoke-report.json"
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    print("saved ->", out)


asyncio.run(main())
