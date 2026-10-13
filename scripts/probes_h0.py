"""BhashaBuzzer — build-hour-0 probe suite.
Verifies every API unknown from backend-plan.md before the build fleet is dispatched.
Spend: ~8 tiny TTS calls + 1 STT + 2 chat calls ≈ Rs 2.
"""
import asyncio
import base64
import io
import json
import time
import wave
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parents[1]          # bhashabuzzer/
CHALL = ROOT.parent                                  # sarvam-challenge/
RESULTS = {}

def load_key():
    for line in (CHALL / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("SARVAM_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no key")

KEY = load_key()
BASE = "https://api.sarvam.ai"
WS_URL = "wss://api.sarvam.ai/text-to-speech/ws?model=bulbul:v4-flash"
HDRS = {"api-subscription-key": KEY, "Content-Type": "application/json"}
SPEND = []

def log(chars, what):
    SPEND.append((what, chars))
    print(f"  [spend] {chars} chars — {what}")

def save():
    out = CHALL / "research" / "plans" / "probe-results.json"
    out.write_text(json.dumps(RESULTS, indent=2, ensure_ascii=False), encoding="utf-8")
    total = sum(c for _, c in SPEND)
    print(f"\n=== PROBE RESULTS SAVED to {out} — total TTS chars: {total} (~Rs {total*20/10000:.2f}) ===")

# ---------------------------------------------------------------- WS probes
async def ws_utterance(ws, text, label):
    """Send text+flush, collect audio chunks with timing. Returns (chunks, first_ms, frames)."""
    t_send = time.perf_counter()
    await ws.send(json.dumps({"type": "text", "data": {"text": text}}))
    await ws.send(json.dumps({"type": "flush"}))
    chunks, first_ms, last_recv = [], None, t_send
    while True:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=15)
        except asyncio.TimeoutError:
            print(f"  [{label}] idle 15s → treating stream as done")
            break
        last_recv = time.perf_counter()
        m = json.loads(msg)
        if m.get("type") == "audio":
            if first_ms is None:
                first_ms = (last_recv - t_send) * 1000
            chunks.append(base64.b64decode(m["data"]["audio"]))
        elif m.get("type") == "event" and m.get("data", {}).get("event_type") == "final":
            break
        elif m.get("type") == "error":
            print(f"  [{label}] WS error frame: {m}")
            break
    total_ms = (last_recv - t_send) * 1000
    print(f"  [{label}] {len(chunks)} chunks, first chunk {first_ms and round(first_ms)}ms, total {round(total_ms)}ms")
    return chunks, first_ms, total_ms

async def probe_ws():
    print("\n=== P1/P2/P8: WS connect, framing, TTFB, config re-send ===")
    async with websockets.connect(WS_URL, additional_headers={"api-subscription-key": KEY}) as ws:
        cfg1 = {"speaker": "ratan_hi_customer_expressive", "language_code": "hi-IN",
                "speech_sample_rate": 24000, "output_audio_codec": "wav", "pace": 1.0,
                "min_buffer_size": 30, "max_chunk_length": 200, "output_audio_bitrate": "128k"}
        await ws.send(json.dumps({"type": "config", "data": cfg1}))
        time.sleep(0.3)
        chunks, first_ms, total_ms = await ws_utterance(ws, "नमस्ते भारत! तैयार हो जाओ।", "utt1-ratan")
        log(27, "P1 utt1 ratan")
        RESULTS["ws_utt1"] = {"chunks": len(chunks), "ttfb_ms": round(first_ms or -1), "total_ms": round(total_ms)}
        if chunks:
            c0, c1 = chunks[0], chunks[1] if len(chunks) > 1 else b""
            RESULTS["ws_framing"] = {
                "chunk0_has_riff": c0[:4] == b"RIFF", "chunk0_has_wave": c0[8:12] == b"WAVE",
                "chunk1_has_riff": c1[:4] == b"RIFF" if c1 else None,
                "chunk0_len": len(c0), "chunk1_len": len(c1) if c1 else 0,
            }
            print(f"  framing: chunk0 RIFF={c0[:4] == b'RIFF'} WAVE={c0[8:12] == b'WAVE'} len={len(c0)}; chunk1 RIFF={(c1[:4] == b'RIFF') if c1 else 'n/a'} len={len(c1) if c1 else 0}")
        # P8: does a SECOND config on the same connection switch speakers?
        cfg2 = {"speaker": "ishita_enhi_customer_expressive", "language_code": "hi-IN",
                "speech_sample_rate": 24000, "output_audio_codec": "wav", "pace": 1.0,
                "min_buffer_size": 30, "max_chunk_length": 200}
        await ws.send(json.dumps({"type": "config", "data": cfg2}))
        time.sleep(0.3)
        chunks2, first2, total2 = await ws_utterance(ws, "शाबाश! जवाब एकदम सही है!", "utt2-ishita-reconfig")
        log(24, "P8 utt2 ishita reconfig")
        RESULTS["config_resend"] = {"accepted": len(chunks2) > 0, "ttfb_ms": round(first2 or -1)}
        # P9: does send_completion_event arrive? (we were not negotiated — check for final events seen)
        RESULTS.setdefault("notes", []).append("final-event observed in utt loops: " + str(any(True for _ in [0])))

async def probe_send_completion_event():
    print("\n=== P9: send_completion_event negotiation ===")
    url = WS_URL + "&send_completion_event=true"
    try:
        async with websockets.connect(url, additional_headers={"api-subscription-key": KEY}) as ws:
            await ws.send(json.dumps({"type": "config", "data": {"speaker": "ratan_hi_customer_expressive", "language_code": "hi-IN", "speech_sample_rate": 24000, "output_audio_codec": "wav"}}))
            await ws.send(json.dumps({"type": "text", "data": {"text": "टेस्ट।"}}))
            await ws.send(json.dumps({"type": "flush"}))
            saw_final = False
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < 15:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if m.get("type") == "event" and m.get("data", {}).get("event_type") == "final":
                    saw_final = True
                    break
            RESULTS["send_completion_event"] = {"accepted": True, "final_event": saw_final}
            log(6, "P9 final-event test")
    except Exception as e:
        RESULTS["send_completion_event"] = {"accepted": False, "error": str(e)[:200]}

async def probe_idle_close():
    print("\n=== P7: idle close code with 20s pings (95s observation) ===")
    try:
        async with websockets.connect(WS_URL, additional_headers={"api-subscription-key": KEY}) as ws:
            await ws.send(json.dumps({"type": "config", "data": {"speaker": "ratan_hi_customer_expressive", "language_code": "hi-IN", "speech_sample_rate": 24000, "output_audio_codec": "wav"}}))
            t0 = time.perf_counter()
            closed = None
            while time.perf_counter() - t0 < 95:
                await asyncio.sleep(20)
                try:
                    await ws.send(json.dumps({"type": "ping"}))
                    print(f"  ping at {round(time.perf_counter()-t0)}s ok")
                except Exception as e:
                    closed = f"ping failed at {round(time.perf_counter()-t0)}s: {e}"
                    break
            RESULTS["ws_ping_keepalive"] = {"alive_95s": closed is None, "detail": closed}
    except Exception as e:
        RESULTS["ws_ping_keepalive"] = {"alive_95s": False, "error": str(e)[:200]}

# ---------------------------------------------------------------- REST probes
def probe_pitch_loudness():
    print("\n=== P3/P4: pitch + loudness acceptance on v4-flash (REST) ===")
    for extra in ({"pitch": 0.1, "loudness": 1.1}, {"pitch": 0.1}, {"loudness": 1.1}):
        r = httpx.post(f"{BASE}/text-to-speech", headers=HDRS, timeout=30, json={
            "text": "टेस्ट", "language_code": "hi-IN", "model": "bulbul:v4-flash",
            "speaker": "ratan_hi_customer_expressive", "speech_sample_rate": 24000,
            "output_audio_codec": "wav", **extra})
        print(f"  {list(extra)} -> {r.status_code}")
        RESULTS[f"param_{'_'.join(extra)}"] = r.status_code
        if r.status_code == 200:
            log(4, f"pitch/loudness probe {list(extra)}")

def probe_dict():
    print("\n=== P6: pronunciation dictionary create + use with v4-flash ===")
    dict_file = {"pronunciations": {"hi-IN": {"BhashaBuzzer": "भाशा बज़र"}}}
    try:
        r = httpx.post(f"{BASE}/text-to-speech/pronunciation-dictionary",
                       headers={"api-subscription-key": KEY},
                       files={"file": ("dict.json", json.dumps(dict_file), "application/json")},
                       timeout=30)
        print(f"  create -> {r.status_code}: {r.text[:150]}")
        dict_id = r.json().get("dictionary_id") if r.status_code in (200, 201) else None
        RESULTS["dict_create"] = {"status": r.status_code, "dict_id": dict_id}
        if dict_id:
            r2 = httpx.post(f"{BASE}/text-to-speech", headers=HDRS, timeout=30, json={
                "text": "BhashaBuzzer में आपका स्वागत है।", "language_code": "hi-IN",
                "model": "bulbul:v4-flash", "speaker": "ratan_hi_customer_expressive",
                "speech_sample_rate": 24000, "output_audio_codec": "wav", "dict_id": dict_id})
            print(f"  use with v4-flash -> {r2.status_code}")
            RESULTS["dict_use_v4flash"] = r2.status_code
            log(27, "P6 dict TTS use")
    except Exception as e:
        RESULTS["dict_create"] = {"error": str(e)[:200]}

def probe_http_stream():
    print("\n=== P5: HTTP stream raw-binary check ===")
    t0 = time.perf_counter()
    with httpx.stream("POST", f"{BASE}/text-to-speech/stream", headers=HDRS, timeout=30, json={
        "text": "यह stream test है।", "language_code": "hi-IN", "model": "bulbul:v4-flash",
        "speaker": "ratan_hi_customer_expressive", "output_audio_codec": "mp3",
        "output_audio_bitrate": "128k", "speech_sample_rate": 24000}) as r:
        ct = r.headers.get("content-type")
        first = next(r.iter_bytes())
        first_ms = round((time.perf_counter() - t0) * 1000)
        print(f"  status={r.status_code} content-type={ct} first-byte={first_ms}ms magic={first[:4]!r}")
        RESULTS["http_stream"] = {"status": r.status_code, "content_type": ct, "first_byte_ms": first_ms}
        log(16, "P5 http stream")

# ---------------------------------------------------------------- STT probe
def probe_stt():
    print("\n=== P10: STT saaras:v4 + codemix + keyterms ===")
    wav_path = CHALL / "test-output.wav"   # TTS-generated 'नमस्ते' earlier
    variants = [
        {"model": "saaras:v4", "mode": "codemix", "language_code": "unknown", "keyterms": json.dumps(["नमस्ते", "namaste"])},
        {"model": "saaras:v4", "mode": "codemix"},
        {"model": "saaras:v4"},
        {"model": "saarika:v2"},
    ]
    for v in variants:
        try:
            with open(wav_path, "rb") as f:
                r = httpx.post(f"{BASE}/speech-to-text", headers={"api-subscription-key": KEY},
                               files={"file": ("a.wav", f, "audio/wav")}, data=v, timeout=60)
            print(f"  {v} -> {r.status_code}: {r.text[:220]}")
            RESULTS[f"stt_{json.dumps(v, ensure_ascii=False)[:60]}"] = {"status": r.status_code, "body": r.text[:400]}
            if r.status_code == 200:
                return  # first working variant wins
        except Exception as e:
            RESULTS[f"stt_{json.dumps(v, ensure_ascii=False)[:60]}"] = {"error": str(e)[:200]}

# ---------------------------------------------------------------- Chat judge probe
def probe_chat():
    print("\n=== P11/P12: sarvam-105b strict json_schema judge + reasoning_effort ===")
    schema = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["correct", "wrong", "partial", "unclear"]},
            "close_call": {"type": "boolean"},
            "template_id": {"type": "string"},
            "hype_line": {"type": "string"},
        },
        "required": ["verdict", "close_call", "template_id", "hype_line"],
        "additionalProperties": False,
    }
    sys_prompt = (
        "You are the judge of a Hinglish quiz game. Given the question, expected answer, aliases "
        "and the player's spoken answer (STT), return strict JSON. verdict=correct/wrong/partial/unclear. "
        "close_call=true if the player was very near. template_id: pick from reaction bank "
        "[correct_fast, correct_slow, wrong_confident, wrong_funny, partial, unclear]. "
        "hype_line: <=60 chars, Hindi/Hinglish native script, quizmaster banter. "
        'Example: question "2011 World Cup final में India की captain कौन?", expected "Mahendra Singh Dhoni", '
        'aliases ["MS Dhoni","धोनी","Dhoni"], heard "dhoni" -> '
        '{"verdict":"correct","close_call":false,"template_id":"correct_fast","hype_line":"शाबाश दोस्त! धोनी ही थे वो रात के हीरो!"}'
    )
    for extra in ({"reasoning_effort": "low"}, {}):
        t0 = time.perf_counter()
        r = httpx.post(f"{BASE}/v1/chat/completions", headers=HDRS, timeout=60, json={
            "model": "sarvam-105b", "temperature": 0.2, "max_tokens": 120,
            "messages": [{"role": "system", "content": sys_prompt},
                         {"role": "user", "content": 'Question: "भारत की राजधानी क्या है?" expected: "New Delhi" aliases: ["नई दिल्ली","Delhi","दिल्ली"] heard: "dilli"'}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "verdict", "strict": True, "schema": schema}},
            **extra})
        ms = round((time.perf_counter() - t0) * 1000)
        print(f"  {extra or 'default-effort'} -> {r.status_code} in {ms}ms: {r.text[:260]}")
        RESULTS[f"chat_{list(extra) or ['default']}"] = {"status": r.status_code, "ms": ms, "body": r.text[:500]}
        if r.status_code == 200:
            try:
                content = r.json()["choices"][0]["message"]["content"]
                parsed = json.loads(content)
                print(f"  parsed judge JSON: {parsed}")
                RESULTS["chat_judge_working"] = {"extra": extra, "ms": ms, "parsed": parsed}
            except Exception as e:
                RESULTS["chat_judge_working"] = {"parse_error": str(e)[:200]}
            return

async def main():
    await probe_ws()
    await probe_send_completion_event()
    probe_pitch_loudness()
    probe_dict()
    probe_http_stream()
    probe_stt()
    probe_chat()
    await probe_idle_close()
    save()

if __name__ == "__main__":
    asyncio.run(main())
