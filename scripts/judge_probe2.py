"""Judge probe round 2: find the FAST working judge config on Sarvam's chat platform. ~Rs 1-2."""
import json
import time
from pathlib import Path

import httpx

CHALL = Path(__file__).resolve().parents[2]
KEY = None
for line in (CHALL / ".env").read_text(encoding="utf-8").splitlines():
    if line.startswith("SARVAM_API_KEY="):
        KEY = line.split("", 1)[1].strip() if False else line.split("=", 1)[1].strip()

BASE = "https://api.sarvam.ai"
HDRS = {"api-subscription-key": KEY, "Content-Type": "application/json"}

SYS = (
    "You judge a Hinglish voice quiz. Input: question, expected answer, aliases, player's spoken answer (STT). "
    "Reply with ONLY a JSON object, no other text: "
    '{"verdict":"correct|wrong|partial|unclear","close_call":true/false,'
    '"template_id":"correct_fast|correct_confident|correct_slow|wrong_confident|wrong_funny|wrong_close|'
    'partial|unclear|timeout|no_answer|repeat_request|streak_3|streak_5|final_win|final_lose|game_over",'
    '"hype_line":"<60 chars Hindi/Hinglish native script quizmaster banter>"}. '
    "Transliteration/surname variants count as correct (Virat = विराट कोहली = Kohli)."
)
Q = ('Question: "भारत की राजधानी क्या है?" expected: "New Delhi" '
     'aliases: ["नई दिल्ली","Delhi","दिल्ली","newdelhi"] heard: "dilli" persona: "Ratan — dramatic quizmaster"')

def call(model, label, extra=None, max_tokens=300):
    payload = {
        "model": model, "temperature": 0.2, "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": SYS}, {"role": "user", "content": Q}],
    }
    if extra:
        payload.update(extra)
    t0 = time.perf_counter()
    try:
        r = httpx.post(f"{BASE}/v1/chat/completions", headers=HDRS, timeout=45, json=payload)
    except Exception as e:
        print(f"{label}: EXC {e}")
        return {"error": str(e)[:150]}
    ms = round((time.perf_counter() - t0) * 1000)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    ch = body.get("choices", [{}])[0]
    content = ch.get("message", {}).get("content")
    finish = ch.get("finish_reason")
    parsed = None
    if content:
        try:
            s = content[content.find("{"):content.rfind("}") + 1]
            parsed = json.loads(s)
        except Exception:
            parsed = "unparseable"
    print(f"{label}: {r.status_code} in {ms}ms finish={finish} -> {str(parsed)[:220] if parsed else str(content)[:120]}")
    return {"status": r.status_code, "ms": ms, "finish": finish, "parsed": parsed}

res = {}
print("=== models list ===")
try:
    r = httpx.get(f"{BASE}/v1/models", headers=HDRS, timeout=15)
    print(r.status_code, r.text[:600])
    res["models"] = r.text[:800]
except Exception as e:
    res["models"] = str(e)[:200]

res["p105b_plain"] = call("sarvam-105b", "105b plain (no response_format)", {"reasoning_effort": "low"})
res["p105b_plain_ms"] = call("sarvam-105b", "105b plain take2", {"reasoning_effort": "low", "max_tokens": 150})
res["p30b"] = call("sarvam-30b", "sarvam-30b plain")
res["gemma"] = call("gemma-4", "gemma-4")
res["deepseek"] = call("deepseek-v4.1-flash", "deepseek v4.1 flash")
res["glm"] = call("glm-5.3", "glm-5.3")

out = CHALL / "research" / "plans" / "judge-probe2.json"
out.write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
print("saved ->", out)
