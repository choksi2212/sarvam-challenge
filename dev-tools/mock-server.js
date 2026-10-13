// dev-tools/mock-server.js — BhashaBuzzer A10 dev backend (dev-only; NOT deployed).
// Node + `ws` package (npm i in dev-tools/). Two jobs:
//   1. Static server for the worktree root (so /frontend/index.html and
//      /dev-tools/stubs.js are both reachable — app.js's stub-fallback import works).
//   2. ws://localhost:8080/ws — a canned FULL match per CONTRACT §1/§2:
//      start → state lobby → round_intro → question → (buzz → listening → judging 1s
//      → verdict) → next … 7 questions (R1×2 · R2×2 · R3×2 · sudden-death final),
//      alternating correct/wrong verdicts, audio frames as SILENCE:
//      first audio_chunk = a valid 44-byte RIFF header ONLY, then 24 kHz Int16LE
//      mono PCM chunks, ~2 s per utterance (§1 audio framing, PROBED shape).
//
// Run:  cd dev-tools && npm i && node mock-server.js
// Then: open http://localhost:8080/frontend/index.html?mock=1

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import wsPkg from 'ws';

const { WebSocketServer } = wsPkg;

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WORKTREE_ROOT = path.resolve(HERE, '..');

/* ---------- canned match data (Hinglish demo arc) ---------- */

const ROUNDS = [
  { round: 1, base: 100, speaker: 'ratan_hi_customer_expressive', label: 'EXPRESSIVE QUIZMASTER' },
  { round: 2, base: 150, speaker: 'ishita_enhi_customer_expressive', label: 'TEASING FRIEND' },
  { round: 3, base: 200, speaker: 'ratan_hi_sports', label: 'SPORTS HYPE' },
  { round: 4, base: 300, speaker: 'mukul_hi_suspense', label: 'SUSPENSE FINALE', final: true },
];

const QUESTIONS = [
  { id: 'cricket-01', text: 'भारत के पहले क्रिकेट विश्व कप जीतने वाले कप्तान कौन थे?', category: 'Cricket', difficulty: 1, answer: 'कपिल देव' },
  { id: 'gk-01', text: 'भारत की राजधानी क्या है?', category: 'GK/Bharat', difficulty: 1, answer: 'नई दिल्ली' },
  { id: 'math-01', text: 'बारह गुणा बारह कितने होते हैं?', category: 'Ganit', difficulty: 1, answer: '144' },
  { id: 'bolly-01', text: 'फ़िल्म "शोले" में गब्बर सिंह का किरदार किसने निभाया?', category: 'Bollywood', difficulty: 2, answer: 'अमजद खान' },
  { id: 'sci-01', text: 'पानी का रासायनिक सूत्र क्या है?', category: 'Vigyan', difficulty: 1, answer: 'H2O' },
  { id: 'pop-01', text: 'YouTube की शुरुआत किस साल हुई थी?', category: 'Pop-culture/Tech', difficulty: 2, answer: '2005' },
  { id: 'gk-09', text: 'ताजमहल भारत के किस शहर में है?', category: 'GK/Bharat', difficulty: 1, answer: 'आगरा' },
];

const HYPE = {
  correct: [
    'वाह! एकदम सही जवाब — शाबाश!',
    'क्या बात! सही — full marks!',
    'बहत ख़ूब! सही जवाब!',
  ],
  wrong: [
    'अरे नहीं! सही जवाब था — {answer}।',
    'अफ़सोस! ये ग़लत है। सही जवाब — {answer}।',
  ],
};

const FACTS = [
  'Fun fact: भारत ने 1983 में विश्व कप जीता था।',
  'Fun fact: नई दिल्ली 1911 से भारत की राजधानी है।',
  'Fun fact: 12 × 12 = 144, एक ग्रॉस भी कहते हैं।',
  'Fun fact: शोले 1975 में रिलीज़ हुई थी।',
  'Fun fact: H2O — दो हाइड्रोजन, एक ऑक्सीजन।',
  'Fun fact: YouTube 2005 में शुरू हुआ था।',
  'Fun fact: ताजमहल 1653 में बनकर तैयार हुआ।',
];

/* ---------- helpers ---------- */

const sleep = (ms) => new Promise((r) => { setTimeout(r, Math.max(0, ms)); });

/** 44-byte canonical WAV header for Int16LE mono PCM @24 kHz (CONTRACT §1 first chunk). */
function wavHeaderBytes(dataLen, sampleRate = 24000) {
  const u8 = new Uint8Array(44);
  const dv = new DataView(u8.buffer);
  const ascii = (off, s) => { for (let i = 0; i < s.length; i++) u8[off + i] = s.charCodeAt(i); };
  ascii(0, 'RIFF'); dv.setUint32(4, 36 + dataLen, true); ascii(8, 'WAVE');
  ascii(12, 'fmt '); dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
  dv.setUint32(24, sampleRate, true); dv.setUint32(28, sampleRate * 2, true);
  dv.setUint16(32, 2, true); dv.setUint16(34, 16, true);
  ascii(36, 'data'); dv.setUint32(40, dataLen, true);
  return Buffer.from(u8);
}

const b64 = (buf) => Buffer.from(buf).toString('base64');

/* ---------- session ---------- */

function newSession(sock, opts) {
  return {
    sock,
    opts,
    started: false,
    epoch: 0,            // bumped by buzz/skip/stop → in-flight speak() loops abort
    qIdx: 0,
    score: 0,
    streak: 0,
    answerCount: 0,       // alternates correct/wrong
    phase: 'lobby',
    name: 'प्रतियोगी',
    language: 'enhi',
    uttSeq: 0,
    tBuzz: 0,
    tAnswer: 0,
    answerTimeout: null,
    busy: false,
  };
}

const roundFor = (qIdx) => ROUNDS[Math.min(ROUNDS.length - 1, Math.floor(Math.min(qIdx, 6) / 2))];
const isFinalQ = (qIdx) => qIdx >= QUESTIONS.length - 1;

function personaOf(s) {
  const r = roundFor(s.qIdx);
  return { speaker: r.speaker, label: r.label, round: r.round, base: r.base };
}

function send(s, obj) {
  if (s.sock.readyState === 1) {
    try { s.sock.send(JSON.stringify(obj)); } catch { /* dropped */ }
  }
}

function stateFrame(s, extra = {}) {
  const p = personaOf(s);
  return {
    type: 'state',
    phase: s.phase,
    round: p.round,
    question_index: s.qIdx,
    score: s.score,
    streak: s.streak,
    speaker: p.speaker,
    persona_label: p.label,
    mode: 'demo',
    ...extra,
  };
}

/** Stream one utterance of SILENCE in the PROBED §1 shape:
 *  audio_start → header-only chunk → PCM chunks → audio_end.
 *  Aborts (silently) if the session epoch moves on (buzz/skip/stop). */
async function speak(s, { caption, text = '', utteranceMs, e2eFields = null }) {
  const epoch = s.epoch;
  const id = `utt-${++s.uttSeq}`;
  const ms = utteranceMs ?? s.opts.utteranceMs;
  const sampleRate = 24000;
  const totalSamples = Math.round((sampleRate * ms) / 1000);
  const chunkSamples = 12000; // 0.5 s of audio per chunk
  const chunkInterval = (chunkSamples / sampleRate) * 1000;
  const p = personaOf(s);
  send(s, { type: 'audio_start', utterance_id: id, speaker: p.speaker, sample_rate: sampleRate, codec: 'wav', caption: caption ?? '' });
  // FIRST chunk = 44-byte RIFF header ONLY (contract §1)
  send(s, { type: 'audio_chunk', utterance_id: id, b64: b64(wavHeaderBytes(totalSamples * 2, sampleRate)) });
  let sent = 0;
  while (sent < totalSamples) {
    if (s.epoch !== epoch || s.sock.readyState !== 1) return id;
    const n = Math.min(chunkSamples, totalSamples - sent);
    send(s, { type: 'audio_chunk', utterance_id: id, b64: b64(Buffer.alloc(n * 2)) }); // silence
    sent += n;
    await sleep(chunkInterval);
  }
  if (s.epoch !== epoch || s.sock.readyState !== 1) return id;
  send(s, {
    type: 'audio_end', utterance_id: id,
    ttfb_ms: 148, total_ms: ms, chars: text.length, cache_hit: false, source: 'ws',
    stt_ms: e2eFields ? e2eFields.stt_ms : 0,
    judge_ms: e2eFields ? e2eFields.judge_ms : 0,
    e2e_ms: e2eFields ? e2eFields.e2e_ms : ms,
  });
  return id;
}

async function speakQuestion(s) {
  const q = QUESTIONS[s.qIdx];
  await speak(s, { caption: q.text, text: q.text });
}

async function introSequence(s) {
  const epoch = s.epoch;
  s.busy = true;
  s.phase = 'lobby';
  send(s, stateFrame(s));
  await speak(s, { caption: `नमस्ते ${s.name}! BhashaBuzzer में आपका स्वागत है — बोलो और जीतो!`, text: `नमस्ते ${s.name}!` });
  if (s.epoch !== epoch || s.sock.readyState !== 1) { s.busy = false; return; }
  s.phase = 'round_intro';
  const p = personaOf(s);
  send(s, stateFrame(s));
  await speak(s, { caption: `ROUND ${p.round} — host ${p.speaker} (${p.label})`, text: `round ${p.round}` });
  s.busy = false;
  if (s.epoch !== epoch || s.sock.readyState !== 1) return;
  await askCurrent(s, { roundSwitch: true });
}

/** Show + speak the current question. */
async function askCurrent(s, { roundSwitch = false } = {}) {
  if (s.sock.readyState !== 1) return;
  const p = personaOf(s);
  if (roundSwitch) {
    s.phase = 'round_switch';
    send(s, stateFrame(s));
  }
  s.phase = 'question';
  send(s, stateFrame(s));
  const q = QUESTIONS[s.qIdx];
  send(s, { type: 'question', id: q.id, text: q.text, category: q.category, difficulty: q.difficulty, language: s.language });
  await speakQuestion(s);
}

async function deliverVerdict(s) {
  const epoch = s.epoch;
  const q = QUESTIONS[s.qIdx];
  const p = personaOf(s);
  const correct = s.answerCount % 2 === 0 ? 'correct' : 'wrong'; // alternating per answer
  s.answerCount += 1;
  const reactionMs = s.tAnswer ? s.tAnswer - s.tBuzz : 99999; // player speed = buzz→answer arrival
  const speedBonus = correct === 'correct' && reactionMs <= 1000 ? 50
    : correct === 'correct' && reactionMs <= 2000 ? 30
    : correct === 'correct' && reactionMs <= 3000 ? 15 : 0;
  const delta = correct === 'correct' ? p.base + speedBonus : 0;
  if (correct === 'correct') { s.streak += 1; } else { s.streak = 0; }
  s.score += delta;
  const hype = (correct === 'correct' ? HYPE.correct : HYPE.wrong)[s.answerCount % 2]
    .replace('{answer}', q.answer);
  send(s, {
    type: 'verdict',
    utterance_id: `react-${s.uttSeq}`,
    correct,
    close_call: correct === 'wrong' && s.answerCount % 4 === 3,
    heard: s.lastHeard ?? (correct === 'correct' ? q.answer : '…'),
    template_id: correct === 'correct' ? (speedBonus ? 'correct_fast' : 'correct_confident') : (s.answerCount % 4 === 3 ? 'wrong_close' : 'wrong_funny'),
    hype_line: hype,
    fact_line: FACTS[s.qIdx % FACTS.length],
    score_delta: delta,
    score: s.score,
    streak: s.streak,
  });
  s.phase = 'verdict';
  send(s, stateFrame(s));
  await speak(s, { caption: hype, text: hype, e2eFields: { stt_ms: 620, judge_ms: 900, e2e_ms: 2400 } });
  if (s.epoch !== epoch || s.sock.readyState !== 1) return;

  if (isFinalQ(s.qIdx)) {
    s.phase = 'final';
    send(s, stateFrame(s));
    return;
  }
  s.phase = 'next';
  send(s, stateFrame(s));
  await sleep(s.opts.beatMs);
  s.qIdx += 1;
  await askCurrent(s, { roundSwitch: roundFor(s.qIdx).round !== roundFor(s.qIdx - 1).round });
}

/* ---------- frame handling (C→S per §1) ---------- */

async function handle(s, msg) {
  if (!msg || typeof msg.type !== 'string') return;
  switch (msg.type) {
    case 'start': {
      s.name = String(msg.player_name ?? 'प्रतियोगी').slice(0, 20) || 'प्रतियोगी';
      s.language = ['hi', 'enhi', 'en', 'mr'].includes(msg.language) ? msg.language : 'enhi';
      if (s.started) {
        // reconnect re-`start`: re-join the CURRENT spot instead of resetting the match
        s.epoch += 1;
        send(s, stateFrame(s));
        if (s.phase === 'question') {
          const q = QUESTIONS[s.qIdx];
          send(s, { type: 'question', id: q.id, text: q.text, category: q.category, difficulty: q.difficulty, language: s.language });
          await speakQuestion(s);
        }
        return;
      }
      s.started = true;
      s.epoch = 0;
      await introSequence(s);
      return;
    }
    case 'buzz': {
      if (s.phase !== 'question' || s.busy) return;
      s.epoch += 1;          // cancels in-flight host audio (barge, §1)
      s.tBuzz = Date.now();
      s.phase = 'listening';
      send(s, stateFrame(s));
      clearTimeout(s.answerTimeout);
      s.answerTimeout = setTimeout(() => { // answer window safety net (§2: 5 s + grace)
        if (s.phase === 'listening') { s.phase = 'judging'; send(s, stateFrame(s)); deliverVerdict(s); }
      }, 9000);
      return;
    }
    case 'answer': {
      if (s.phase !== 'listening') return;
      clearTimeout(s.answerTimeout);
      s.tAnswer = Date.now();
      s.lastHeard = typeof msg.text === 'string' && msg.text.trim() ? msg.text.trim().slice(0, 120) : null;
      s.phase = 'judging';
      send(s, stateFrame(s));
      await sleep(s.opts.judgingMs); // judging beat (~1 s)
      await deliverVerdict(s);
      return;
    }
    case 'barge': return; // epoch already bumped on buzz; nothing pending to drop
    case 'repeat_question': {
      if (!s.started || s.busy || s.phase !== 'question') return;
      s.epoch += 1; // new utterance_id ⇒ fresh RIFF strip state client-side
      await speakQuestion(s);
      return;
    }
    case 'skip_question': {
      if (!s.started || s.busy) return;
      s.epoch += 1;
      s.streak = 0;          // §2: streak resets, zero penalty
      clearTimeout(s.answerTimeout);
      if (isFinalQ(s.qIdx)) { s.phase = 'final'; send(s, stateFrame(s)); return; }
      s.phase = 'next';
      send(s, stateFrame(s));
      await sleep(s.opts.beatMs);
      s.qIdx += 1;
      await askCurrent(s, { roundSwitch: roundFor(s.qIdx).round !== roundFor(s.qIdx - 1).round });
      return;
    }
    case 'next_round': return; // demo match auto-advances
    case 'ping': return;      // liveness only
    default: return;
  }
}

/* ---------- static file server (worktree root) ---------- */

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.wav': 'audio/wav',
  '.md': 'text/plain; charset=utf-8',
};

function createStatic(root) {
  return (req, res) => {
    try {
      const urlPath = decodeURIComponent((req.url ?? '/').split('?')[0]);
      let rel = urlPath === '/' ? '/frontend/index.html' : urlPath;
      const filePath = path.normalize(path.join(root, rel));
      if (!filePath.startsWith(path.normalize(root))) { res.writeHead(403).end('forbidden'); return; }
      fs.readFile(filePath, (err, data) => {
        if (err) { res.writeHead(404, { 'content-type': 'text/plain' }); res.end('404 — run from the worktree root, or open /frontend/index.html'); return; }
        res.writeHead(200, { 'content-type': MIME[path.extname(filePath).toLowerCase()] ?? 'application/octet-stream' });
        res.end(data);
      });
    } catch {
      res.writeHead(500).end('err');
    }
  };
}

/* ---------- server factory (exported for node --test) ---------- */

export function createMockServer({ port = 8080, staticRoot = WORKTREE_ROOT, utteranceMs = 2000, judgingMs = 1000, beatMs = 800 } = {}) {
  const opts = { utteranceMs, judgingMs, beatMs };
  const server = http.createServer(createStatic(staticRoot));
  const wss = new WebSocketServer({ server, path: '/ws' });

  const sessions = new Set();

  wss.on('connection', (sock) => {
    const s = newSession(sock, opts);
    sessions.add(s);
    sock.on('message', (data) => {
      let msg;
      try { msg = JSON.parse(String(data)); } catch { return; }
      Promise.resolve(handle(s, msg)).catch((err) => console.error('[mock] handler error:', err));
    });
    sock.on('close', () => { s.epoch += 1; clearTimeout(s.answerTimeout); sessions.delete(s); });
    sock.on('error', () => {});
  });

  const start = () => new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, () => resolve(server.address().port));
  });
  const stop = () => new Promise((resolve) => {
    for (const s of sessions) { clearTimeout(s.answerTimeout); s.epoch += 1; }
    for (const c of wss.clients) { try { c.terminate(); } catch { /* noop */ } }
    wss.close(() => server.close(() => resolve()));
  });

  return { server, wss, start, stop, opts };
}

/* ---------- main ---------- */

const invoked = process.argv[1] ? pathToFileURL(path.resolve(process.argv[1])).href : '';
if (invoked === import.meta.url) {
  const port = Number(process.env.PORT ?? 8080);
  const mock = createMockServer({ port });
  mock.start().then((actual) => {
    console.log(`BhashaBuzzer mock backend`);
    console.log(`  static : http://localhost:${actual}/  (worktree root)`);
    console.log(`  game   : ws://localhost:${actual}/ws`);
    console.log(`  open   : http://localhost:${actual}/frontend/index.html?mock=1`);
  }).catch((err) => { console.error('mock-server failed to start:', err.message); process.exit(1); });
}
