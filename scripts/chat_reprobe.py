"""Follow-up probe: fix the judge call shape (max_tokens was burned by reasoning). ~Rs 0.5."""
import json
import time
from pathlib import Path

import httpx

CHALL = Path(__file__).resolve().parents[2]
KEY = None
for line in (CHALL / ".env").read_text(encoding="utf-8").splitlines():
    if line.startswith("SARVAM_API_KEY="):
        KEY = line.split("=", 1)[1].strip()

BASE = "https://api.sarvam.ai"
HDRS = {"api-subscription-key": KEY, "Content-Type": "application/json"}
schema = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["correct", "wrong", "partial", "unclear"]},
        "close_call": {"type": "boolean"},
        "template_id": {"type": "string", "enum": ["correct_fast", "correct_confident", "correct_slow",
                                                    "wrong_confident", "wrong_funny", "wrong_close",
                                                    "partial", "unclear", "timeout", "no_answer",
                                                    "repeat_request", "streak_3", "streak_5",
                                                    "final_win", "final_lose", "game_over"]},
        "hype_line": {"type": "string"},
    },
    "required": ["verdict", "close_call", "template_id", "hype_line"],
    "additionalProperties": False,
}
SYS = (
    "You judge a Hinglish voice quiz. Given question, expected answer, aliases, and the player's "
    "spoken answer (STT text), return strict JSON per the schema. verdict: correct/wrong/partial/unclear. "
    "close_call: true if near-miss (transliteration/surname variants count as correct: Virat = विराट कोहली). "
    "template_id: pick the best reaction id. hype_line: <=60 chars, Hindi/Hinglish NATIVE SCRIPT, "
    "quizmaster banter matching the persona."
)

def call(user_msg, label, extra):
    t0 = time.perf_counter()
    r = httpx.post(f"{BASE}/v1/chat/completions", headers=HDRS, timeout=90, json={
        "model": "sarvam-105b", "temperature": 0.2, "max_tokens": 2000,
        "messages": [{"role": "system", "content": SYS}, {"role": "user", "content": user_msg}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "verdict", "strict": True, "schema": schema}},
        **extra})
    ms = round((time.perf_counter() - t0) * 1000)
    body = r.json() if r.status_code == 200 else {}
    content = body.get("choices", [{}])[0].get("message", {}).get("content")
    finish = body.get("choices", [{}])[0].get("finish_reason")
    print(f"{label}: {r.status_code} in {ms}ms finish={finish} content={str(content)[:160]}")
    if content:
        try:
            print("  parsed:", json.dumps(json.loads(content), ensure_ascii=False))
        except Exception as e:
            print("  parse error:", e)
    return {"status": r.status_code, "ms": ms, "finish": finish, "content": content}

q = ('Question: "भारत की राजधानी क्या है?" expected: "New Delhi" '
     'aliases: ["नई दिल्ली","Delhi","दिल्ली","newdelhi"] heard: "dilli" persona: "Ratan — dramatic quizmaster"')

res = {}
res["low_2000"] = call(q, "reasoning_effort=low max_tokens=2000", {"reasoning_effort": "low"})
if not res["low_2000"]["content"]:
    res["low_4000"] = call(q, "reasoning_effort=low max_tokens=4000", {"reasoning_effort": "low", "max_tokens": 4000})
    res["none_2000"] = call(q, "reasoning_effort=none max_tokens=2000", {"reasoning_effort": "none"})
    res["default_2000"] = call(q, "default effort max_tokens=2000", {})
# a second judge case to sanity-check quality + variance
if any(v["content"] for v in res.values()):
    q2 = ('Question: "2011 के World Cup final में India की captain कौन थी?" expected: "Mahendra Singh Dhoni" '
          'aliases: ["MS Dhoni","धोनी","Dhoni","महेंद्र सिंह धोनी"] heard: "sachin tendulkar" persona: "Ratan — dramatic quizmaster"')
    res["wrong_case"] = call(q2, "wrong-answer case", {"reasoning_effort": "low"})

out = CHALL / "research" / "plans" / "chat-probe-results.json"
out.write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
print("saved ->", out)
