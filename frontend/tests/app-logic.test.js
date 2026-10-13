// frontend/tests/app-logic.test.js — BhashaBuzzer A10 (CONTRACT §8: node --test from repo root)
// Covers: game.js pure helper math + maps, verdict stamps, latency formatting (honest
// labels, never mixing clocks), localStorage serialization round-trips with a mocked
// global, GameWS reconnect/backoff/start-replay with a fake socket, stubs sanity, and a
// live full-match run against dev-tools/mock-server.js (auto-skipped when the dev-only
// `ws` package isn't installed).

import { test } from 'node:test';
import assert from 'node:assert/strict';

import * as G from '../ui/game.js';
import { GameWS } from '../ws-client.js';

const stubsP = import('../../dev-tools/stubs.js').catch(() => null);
let createMockServer = null;
try {
  ({ createMockServer } = await import('../../dev-tools/mock-server.js'));
} catch {
  createMockServer = null; // `npm i` not run in dev-tools/ — integration test will skip
}

const sleep = (ms) => new Promise((r) => { setTimeout(r, Math.max(0, ms)); });

async function waitFor(fn, timeoutMs = 4000, stepMs = 15) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    const v = fn();
    if (v) return v;
    await sleep(stepMs);
  }
  return null;
}

class MemStore {
  constructor(seed = {}) { this.m = new Map(Object.entries(seed)); }
  getItem(k) { return this.m.has(k) ? this.m.get(k) : null; }
  setItem(k, v) { this.m.set(k, String(v)); }
  removeItem(k) { this.m.delete(k); }
}

/* ================= game.js — name sanitization ================= */

test('sanitizeName: 2–20 chars, control chars stripped, whitespace collapsed', () => {
  assert.deepEqual(G.sanitizeName('  अmit   kumar  '), { ok: true, name: 'अmit kumar', reason: 'ok' });
  assert.equal(G.sanitizeName('a').ok, false);
  assert.equal(G.sanitizeName('a').reason, 'too-short');
  assert.equal(G.sanitizeName('   ').ok, false);
  assert.equal(G.sanitizeName('ab\u0000cd\u007f').name, 'abcd');
  assert.equal(G.sanitizeName('x'.repeat(40)).name.length, 20);
  assert.equal(G.sanitizeName('x'.repeat(40)).ok, true);
  assert.deepEqual(G.sanitizeName('राहुल'), { ok: true, name: 'राहुल', reason: 'ok' });
});

/* ================= game.js — verdict map ================= */

test('verdictStamp: correct→सही green, wrong→गलत red, partial→almost amber, unclear→?', () => {
  const c = G.verdictStamp('correct');
  assert.equal(c.text, 'सही!'); assert.equal(c.tone, 'green'); assert.equal(c.cls, 'verdict-correct');
  const w = G.verdictStamp('wrong');
  assert.equal(w.text, 'गलत!'); assert.equal(w.tone, 'red');
  const p = G.verdictStamp('partial');
  assert.equal(p.text, 'लगभग!'); assert.equal(p.word, 'ALMOST'); assert.equal(p.tone, 'amber');
  const u = G.verdictStamp('unclear');
  assert.equal(u.text, '?'); assert.equal(u.cls, 'verdict-unclear');
  // unknown/missing falls back to unclear, never throws
  assert.equal(G.verdictStamp('nonsense').cls, 'verdict-unclear');
  assert.equal(G.verdictStamp(undefined).cls, 'verdict-unclear');
  assert.equal(G.verdictStamp('CORRECT').cls, 'verdict-correct'); // case-insensitive
  // daily marks
  assert.equal(G.dailyMark('correct'), '✓');
  assert.equal(G.dailyMark('partial'), '~');
  assert.equal(G.dailyMark('unclear'), '?');
  assert.equal(G.dailyMark('wrong'), '✗');
  assert.equal(G.dailyMark('junk'), '✗');
});

/* ================= game.js — score formatting + roll ================= */

test('formatScore: plain ≤4 digits, comma-grouped above, never negative', () => {
  assert.equal(G.formatScore(0), '0');
  assert.equal(G.formatScore(9999), '9999');
  assert.equal(G.formatScore(10000), '10,000');
  assert.equal(G.formatScore(123456), '1,23,456'); // Indian grouping
  assert.equal(G.formatScore(-50), '0');           // CONTRACT §2: never negative
  assert.equal(G.formatScore('x'), '0');
  assert.equal(G.scoreDeltaText(150), '+150');
  assert.equal(G.scoreDeltaText(-30), '-30');
  assert.equal(G.scoreDeltaText(0), '+0');
});

test('rollScore: eased count-up, monotonic, exact landing, injectable raf', () => {
  const frames = [];
  const raf = (cb) => frames.push(cb);
  const ticks = [];
  G.rollScore(0, 100, 600, (v) => ticks.push(v), raf);
  let t = 0;
  const pump = (dt) => { t += dt; for (const cb of frames.splice(0)) cb(t); };
  pump(0);   // p=0 → '0'
  pump(300); // p=0.5, cubic-out eased → 88
  pump(299); // p≈0.998
  pump(1);   // p=1 → final '100'
  pump(50);  // nothing more scheduled
  assert.equal(ticks[0], '0');
  assert.equal(ticks.at(-1), '100');
  assert.ok(ticks.includes('88'), `expected eased mid value 88 in ${JSON.stringify(ticks)}`);
  const nums = ticks.map((s) => Number(s.replace(/,/g, '')));
  for (let i = 1; i < nums.length; i++) assert.ok(nums[i] >= nums[i - 1], 'monotonic');
  assert.equal(frames.length, 0, 'roll finishes exactly at target');
  // degenerate: from===to → single immediate tick
  const once = [];
  G.rollScore(7, 7, 600, (v) => once.push(v), raf);
  assert.deepEqual(once, ['7']);
  // stop() cancels
  const stopped = [];
  const stop = G.rollScore(0, 10, 600, (v) => stopped.push(v), raf);
  stop(); pump(700);
  assert.deepEqual(stopped, []);
});

/* ================= game.js — speed bonus ================= */

test('speedBonusFor/Text: +50/+30/+15 at ≤1/2/3 s, else 0', () => {
  assert.equal(G.speedBonusFor(0), 50);
  assert.equal(G.speedBonusFor(999), 50);
  assert.equal(G.speedBonusFor(1000), 50);
  assert.equal(G.speedBonusFor(1001), 30);
  assert.equal(G.speedBonusFor(2000), 30);
  assert.equal(G.speedBonusFor(2001), 15);
  assert.equal(G.speedBonusFor(3000), 15);
  assert.equal(G.speedBonusFor(3001), 0);
  assert.equal(G.speedBonusFor(99999), 0);
  assert.equal(G.speedBonusFor(NaN), 0);
  assert.equal(G.speedBonusText(900), '⚡ +50');
  assert.equal(G.speedBonusText(2500), '⚡ +15');
  assert.equal(G.speedBonusText(4000), '');
});

/* ================= game.js — latency chips (honest labels, no clock mixing) ================= */

test('formatMs: ms under 1 s, seconds above, dash for missing', () => {
  assert.equal(G.formatMs(212), '212 ms');
  assert.equal(G.formatMs(148), '148 ms');
  assert.equal(G.formatMs(3240), '3.2 s');
  assert.equal(G.formatMs(null), '—');
  assert.equal(G.formatMs(undefined), '—');
  assert.equal(G.formatMs('x'), '—');
  assert.equal(G.formatMs(-5), '—');
});

test('latencyChip: HOST VOICE (client) · YOU → HOST (client) · SERVER TTFB (server clock, e2e secondary)', () => {
  const host = G.latencyChip('host', 212, { source: 'client' });
  assert.equal(host.label, 'HOST VOICE');
  assert.equal(host.text, 'HOST VOICE 212 ms');
  assert.equal(host.source, 'client');
  assert.equal(host.el, 'lat-host');
  assert.equal(host.title, 'client-measured (performance.now)');

  const loop = G.latencyChip('loop', 3210);
  assert.equal(loop.label, 'YOU → HOST');
  assert.equal(loop.text, 'YOU → HOST 3.2 s');
  assert.equal(loop.source, 'client'); // loop is ALWAYS client-measured

  const server = G.latencyChip('server', 148, { e2eMs: 2400 });
  assert.equal(server.label, 'SERVER TTFB');
  assert.equal(server.text, 'SERVER TTFB 148 ms');
  assert.equal(server.source, 'server-ttfb');
  assert.equal(server.title, 'server-measured ttfb 148 ms · server e2e 2400 ms');

  // host chip falling back to the server's ttfb_ms must be TAGGED as such
  const hostFallback = G.latencyChip('host', 148, { source: 'server-ttfb' });
  assert.equal(hostFallback.source, 'server-ttfb');
  assert.match(hostFallback.title, /server-measured ttfb/);

  const ticker = G.latencyTicker({ hostMs: 212, loopMs: 3210, serverTtfbMs: 148, serverE2eMs: 2400 });
  assert.deepEqual([ticker.host.el, ticker.loop.el, ticker.server.el], ['lat-host', 'lat-loop', 'lat-server']);
  assert.deepEqual([ticker.host.source, ticker.loop.source, ticker.server.source], ['client', 'client', 'server-ttfb']);
  // missing values render as dash, never NaN
  assert.equal(G.latencyTicker({}).server.text, 'SERVER TTFB —');
});

/* ================= game.js — localStorage round-trips (mocked global) ================= */

test('istDateKey: IST (UTC+5:30) day boundary', () => {
  assert.equal(G.istDateKey(new Date('2026-10-13T18:00:00Z')), '2026-10-13'); // 23:30 IST
  assert.equal(G.istDateKey(new Date('2026-10-13T18:29:00Z')), '2026-10-13'); // 23:59 IST
  assert.equal(G.istDateKey(new Date('2026-10-13T18:31:00Z')), '2026-10-14'); // 00:01 IST next day
  assert.equal(G.istDateKey(new Date('2026-10-13T00:30:00Z')), '2026-10-13');
});

test('daily5: append/load round-trip through a mocked localStorage', () => {
  const store = new MemStore();
  const now = new Date('2026-10-13T12:00:00Z');
  assert.deepEqual(G.loadDaily5(store, now), []);
  let sum = G.recordDaily5(store, 'correct', now);
  sum = G.recordDaily5(store, 'wrong', now);
  sum = G.recordDaily5(store, 'partial', now);
  const again = G.loadDaily5(store, now); // fresh read = serialization round-trip
  assert.deepEqual(again, ['correct', 'wrong', 'partial']);
  assert.equal(sum.played, 3);
  assert.equal(sum.correct, 1);
  assert.deepEqual(sum.marks, ['✓', '✗', '~']);
  assert.ok(store.getItem(G.daily5Key(now)).includes('["correct","wrong","partial"]'));
  // different day → different key, independent list
  assert.deepEqual(G.loadDaily5(store, new Date('2026-10-14T12:00:00Z')), []);
  // corrupt storage tolerated
  const corrupt = new MemStore({ [G.daily5Key(now)]: '{not json' });
  assert.deepEqual(G.loadDaily5(corrupt, now), []);
});

test('combos: Set of persona_labels → n/224, persisted + deduped', () => {
  const store = new MemStore();
  let r = G.addCombo(store, 'EXPRESSIVE QUIZMASTER');
  assert.equal(r.isNew, true); assert.equal(r.count, 1);
  r = G.addCombo(store, 'EXPRESSIVE QUIZMASTER'); // dedupe
  assert.equal(r.isNew, false); assert.equal(r.count, 1);
  r = G.addCombo(store, 'TEASING FRIEND');
  assert.equal(r.count, 2);
  r = G.addCombo(store, ''); // junk ignored
  assert.equal(r.count, 2);
  // round-trip: fresh Set from the same store
  const reloaded = G.loadCombos(store);
  assert.ok(reloaded instanceof Set);
  assert.equal(reloaded.size, 2);
  assert.ok(reloaded.has('EXPRESSIVE QUIZMASTER') && reloaded.has('TEASING FRIEND'));
  assert.deepEqual([...reloaded], ['EXPRESSIVE QUIZMASTER', 'TEASING FRIEND']); // sorted persist
  assert.equal(G.combosText(reloaded.size), '2/224');
  assert.equal(G.combosText(224), '224/224');
  assert.equal(G.combosText(0), '0/224');
  // corrupt combos storage tolerated
  const corrupt = new MemStore({ [G.LS_KEYS.combos]: 'null' });
  assert.equal(G.loadCombos(corrupt).size, 0);
});

test('recordDayStreak: consecutive IST days count', () => {
  const store = new MemStore();
  const d1 = new Date('2026-10-12T10:00:00Z');
  const d2 = new Date('2026-10-13T10:00:00Z');
  const d3 = new Date('2026-10-14T10:00:00Z');
  const d5 = new Date('2026-10-16T10:00:00Z');
  assert.equal(G.recordDayStreak(store, d1).count, 1);
  assert.equal(G.recordDayStreak(store, d1).count, 1); // same day → unchanged
  assert.equal(G.recordDayStreak(store, d2).count, 2);
  assert.equal(G.recordDayStreak(store, d3).count, 3);
  assert.equal(G.recordDayStreak(store, d5).count, 1); // gap → reset
});

/* ================= game.js — persona / category / phase / misc ================= */

test('personaFields: literal speaker id kept, name+style derived, label wins', () => {
  const f = G.personaFields({ speaker: 'ratan_hi_customer_expressive', personaLabel: 'EXPRESSIVE QUIZMASTER' });
  assert.equal(f.name, 'RATAN');
  assert.equal(f.style, 'EXPRESSIVE QUIZMASTER');
  assert.equal(f.id, 'ratan_hi_customer_expressive'); // LITERAL id → #persona-id
  const derived = G.personaFields({ speaker: 'ishita_enhi_customer_expressive' });
  assert.equal(derived.name, 'ISHITA');
  assert.equal(derived.style, 'CUSTOMER EXPRESSIVE');
  assert.equal(G.personaNameFromSpeaker(''), 'HOST');
});

test('categoryChip: known categories mapped, unknown pass through', () => {
  assert.equal(G.categoryChip('Cricket'), 'क्रिकेट');
  assert.equal(G.categoryChip('cricket'), 'क्रिकेट');
  assert.equal(G.categoryChip('GK/Bharat'), 'GK · भारत');
  assert.equal(G.categoryChip('Ganit'), 'गणित');
  assert.equal(G.categoryChip('Bollywood'), 'Bollywood');
  assert.equal(G.categoryChip('Vigyan'), 'विज्ञान');
  assert.equal(G.categoryChip('Pop-culture/Tech'), 'पॉप-कल्चर');
  assert.equal(G.categoryChip('History'), 'History'); // unknown → verbatim
  assert.equal(G.categoryChip(''), '');
});

test('phaseScreen: every §2 phase maps to a §6 section', () => {
  assert.equal(G.phaseScreen('lobby'), 'screen-lobby');
  assert.equal(G.phaseScreen('round_intro'), 'screen-lobby');
  assert.equal(G.phaseScreen('question'), 'screen-question');
  assert.equal(G.phaseScreen('listening'), 'screen-buzz');
  assert.equal(G.phaseScreen('judging'), 'screen-buzz');
  assert.equal(G.phaseScreen('verdict'), 'screen-verdict');
  assert.equal(G.phaseScreen('next'), 'screen-verdict');
  assert.equal(G.phaseScreen('round_switch'), 'screen-lobby');
  assert.equal(G.phaseScreen('final'), 'screen-final');
  assert.equal(G.phaseScreen('what'), null);
  assert.equal(G.phaseScreen(undefined), null);
});

test('answerFrameFrom: audio XOR text, never both, null when empty', () => {
  assert.deepEqual(G.answerFrameFrom({ wavB64: 'QUJD' }), { type: 'answer', audio_b64: 'QUJD' });
  assert.deepEqual(G.answerFrameFrom({ text: 'कपिल देव' }), { type: 'answer', text: 'कपिल देव' });
  const both = G.answerFrameFrom({ wavB64: 'QUJD', text: 'hello' });
  assert.ok(!('text' in both) && both.audio_b64 === 'QUJD');
  assert.equal(G.answerFrameFrom({ text: '   ' }), null);
  assert.equal(G.answerFrameFrom({}), null);
  assert.equal(G.answerFrameFrom(), null);
});

test('recTimerText: countdown to the 8 s hard cap', () => {
  assert.equal(G.recTimerText(0), '8s');
  assert.equal(G.recTimerText(3500), '5s');
  assert.equal(G.recTimerText(7999), '1s');
  assert.equal(G.recTimerText(8000), '0s');
  assert.equal(G.recTimerText(9000), '0s');
});

/* ================= ws-client: backoff math ================= */

test('GameWS.backoffDelay: 250·2^n ±25% jitter, hard cap 8 s', () => {
  const B = GameWS.BACKOFF_BASE_MS, C = GameWS.BACKOFF_CAP_MS;
  assert.equal(B, 250); assert.equal(C, 8000);
  assert.equal(GameWS.backoffDelay(0, () => 0.5), 250); // no jitter
  assert.equal(GameWS.backoffDelay(0, () => 0), 187.5); // -25%
  assert.equal(GameWS.backoffDelay(0, () => 1), 312.5); // +25%
  assert.equal(GameWS.backoffDelay(1, () => 0.5), 500);
  assert.equal(GameWS.backoffDelay(2, () => 0.5), 1000);
  assert.equal(GameWS.backoffDelay(3, () => 0.5), 2000);
  assert.equal(GameWS.backoffDelay(4, () => 0.5), 4000);
  assert.equal(GameWS.backoffDelay(5, () => 0.5), 8000); // 8000·1 = cap
  assert.equal(GameWS.backoffDelay(5, () => 1), 8000);  // 10000 → capped at 8 s
  assert.equal(GameWS.backoffDelay(9, () => 0), 6000);  // 8000·0.75, deep attempts stay in range
  for (const r of [0, 0.25, 0.5, 0.75, 1]) {
    assert.ok(GameWS.backoffDelay(4, () => r) >= 3000 && GameWS.backoffDelay(4, () => r) <= 5000);
  }
  assert.equal(GameWS.backoffDelay(-3, () => 0.5), 250); // negative attempt clamps
});

/* ================= ws-client: transport with a fake socket ================= */

class FakeWebSocket {
  static instances = [];
  constructor(url) {
    this.url = url; this.readyState = 0; this.sent = [];
    this.onopen = null; this.onclose = null; this.onerror = null; this.onmessage = null;
    FakeWebSocket.instances.push(this);
  }
  send(data) { if (this.readyState !== 1) throw new Error('not open'); this.sent.push(String(data)); }
  close(code = 1000) { if (this.readyState === 3) return; this.readyState = 3; this.closedBy = code; this.onclose?.({ code }); }
  open() { this.readyState = 1; this.onopen?.(); }
  emitError() { this.onerror?.({}); }
  message(obj) { this.onmessage?.({ data: JSON.stringify(obj) }); }
  raw(str) { this.onmessage?.({ data: str }); }
  static last() { return FakeWebSocket.instances.at(-1); }
}

test('GameWS: open → send start → server drop → backoff reconnect → start re-sent', async () => {
  FakeWebSocket.instances.length = 0;
  const frames = [];
  let opens = 0, closes = 0;
  const ws = new GameWS('ws://fake/ws', {
    wsFactory: (u) => new FakeWebSocket(u),
    onFrame: (m) => frames.push(m),
    onOpen: () => { opens += 1; },
    onClose: () => { closes += 1; },
  });
  assert.equal(ws.state, 'connecting');
  const sock1 = FakeWebSocket.last();
  sock1.open();
  assert.equal(ws.state, 'open');
  assert.equal(opens, 1);

  // start frame goes out on the wire as a JSON text frame
  assert.equal(ws.send({ type: 'start', player_name: 'अमित', language: 'enhi' }), true);
  assert.equal(sock1.sent.length, 1);
  assert.deepEqual(JSON.parse(sock1.sent[0]), { type: 'start', player_name: 'अमित', language: 'enhi' });

  // inbound frames parse and route
  sock1.message({ type: 'state', phase: 'lobby', score: 0 });
  assert.deepEqual(frames[0], { type: 'state', phase: 'lobby', score: 0 });
  sock1.raw('not json'); // non-JSON frame ignored silently
  assert.equal(frames.length, 1);

  // server drops the socket (1006) → reconnect with backoff, then re-send last start
  sock1.close(1006);
  assert.equal(ws.state, 'reconnecting');
  assert.equal(closes, 1);
  const sock2 = await waitFor(() => (FakeWebSocket.last() !== sock1 ? FakeWebSocket.last() : null), 1500);
  assert.ok(sock2, 'reconnect attempt scheduled within backoff window');
  sock2.open();
  assert.equal(ws.state, 'open');
  assert.equal(opens, 2);
  assert.equal(sock2.sent.length, 1);
  assert.deepEqual(JSON.parse(sock2.sent[0]), { type: 'start', player_name: 'अमित', language: 'enhi' });
  ws.close();
  assert.equal(ws.state, 'closed');
});

test('GameWS: start queued while connecting is flushed on open; other frames dropped', async () => {
  FakeWebSocket.instances.length = 0;
  const ws = new GameWS('ws://fake/ws', { wsFactory: (u) => new FakeWebSocket(u) });
  const sock = FakeWebSocket.last();
  // socket still CONNECTING
  assert.equal(ws.send({ type: 'start', player_name: 'AB', language: 'hi' }), true); // accepted + queued
  assert.equal(ws.send({ type: 'buzz' }), false); // time-critical frames are dropped, not queued
  assert.equal(sock.sent.length, 0);
  sock.open();
  assert.equal(sock.sent.length, 1); // queued start flushed on open
  assert.deepEqual(JSON.parse(sock.sent[0]), { type: 'start', player_name: 'AB', language: 'hi' });
  // open now: non-start frames flow
  assert.equal(ws.send({ type: 'answer', text: 'hi' }), true);
  assert.deepEqual(JSON.parse(sock.sent[1]), { type: 'answer', text: 'hi' });
  ws.close();
});

test('GameWS: close() is final — no reconnect attempts afterwards', async () => {
  FakeWebSocket.instances.length = 0;
  const ws = new GameWS('ws://fake/ws', { wsFactory: (u) => new FakeWebSocket(u) });
  const before = FakeWebSocket.instances.length;
  ws.close();
  assert.equal(ws.state, 'closed');
  await sleep(700); // beyond the 250–313 ms first backoff slot
  assert.equal(FakeWebSocket.instances.length, before);
  ws.send({ type: 'start', player_name: 'AB', language: 'en' }); // start stashed but never flushed
  await sleep(100);
  assert.equal(FakeWebSocket.instances.length, before);
});

/* ================= stubs sanity ================= */

test('stubs: tiny silent WAV is a valid 16 kHz mono RIFF file', async () => {
  const stubs = await stubsP;
  assert.ok(stubs, 'dev-tools/stubs.js importable under node');
  const bytes = Buffer.from(stubs.TINY_WAV_B64, 'base64');
  assert.equal(bytes.slice(0, 4).toString(), 'RIFF');
  assert.equal(bytes.slice(8, 12).toString(), 'WAVE');
  assert.equal(bytes.readUInt32LE(4), bytes.length - 8);   // RIFF size
  assert.equal(bytes.readUInt32LE(24), 16000);             // sample rate
  assert.equal(bytes.readUInt16LE(22), 1);                 // mono
  assert.equal(bytes.readUInt16LE(34), 16);                // bits
  assert.equal(bytes.readUInt32LE(40), bytes.length - 44); // data size
  // payload really is silence
  let max = 0;
  for (let i = 44; i < bytes.length; i += 2) max = Math.max(max, Math.abs(bytes.readInt16LE(i)));
  assert.equal(max, 0);
});

test('stubs: HostPlayer fake first-sample + MicRecorder start/stop shape', async () => {
  const stubs = await stubsP;
  const hp = new stubs.HostPlayer();
  await hp.init();
  assert.equal(hp.ready, true);
  let fired = false;
  hp.startUtterance('u1', { sampleRate: 24000, onFirstSample: () => { fired = true; } });
  await sleep(60);
  assert.equal(fired, true); // fake first-sample callback fires
  hp.feedChunk('u1', 'QUJD'); hp.endUtterance('u1'); hp.stopNow();
  assert.equal(hp.speaking, false);
  hp.volume = 0.5; assert.equal(hp.volume, 0.5);

  const mr = new stubs.MicRecorder();
  await mr.start({});
  const res = await mr.stop();
  assert.ok(res.wavB64.length > 0);
  assert.equal(Buffer.from(res.wavB64, 'base64').slice(0, 4).toString(), 'RIFF');
  assert.ok(res.ms >= 350 && res.ms <= 8000); // stub clamps so the tap-guard never trips
  // cancel path doesn't throw
  await mr.start({}); mr.cancel();
  // showScreen/setPhaseBadge are safe without a DOM
  stubs.showScreen('screen-lobby');
  stubs.setPhaseBadge('question');
});

/* ================= live mock-server full match (skips without `ws`) ================= */

const EXPECTED_Q_IDS = ['cricket-01', 'gk-01', 'math-01', 'bolly-01', 'sci-01', 'pop-01', 'gk-09'];
const EXPECTED_DELTAS = [150, 0, 200, 0, 250, 0, 350]; // base + speed bonus (+50) on correct, R1→final

test('mock-server: full 7-question match over a real socket via GameWS', { timeout: 30000 }, async () => {
  if (!createMockServer) { assert.ok('skip: dev-tools `npm i` not run (ws package missing)'); return; }
  const mock = createMockServer({ port: 0, utteranceMs: 100, judgingMs: 60, beatMs: 30 });
  const port = await mock.start();

  try {
    const rec = { frames: [], phases: [], questions: [], verdicts: [], audioStarts: [], riffsChecked: 0 };
    let lastAudioStart = null;
    let headerSeen = false;
    const countPhase = (p) => rec.phases.filter((x) => x === p).length;

    const ws = new GameWS(`ws://localhost:${port}/ws`, {
      onFrame: (m) => {
        rec.frames.push(m);
        if (m.type === 'state') rec.phases.push(m.phase);
        if (m.type === 'question') rec.questions.push(m);
        if (m.type === 'verdict') rec.verdicts.push(m);
        if (m.type === 'audio_start') { lastAudioStart = m; rec.audioStarts.push(m); }
        if (m.type === 'audio_chunk' && lastAudioStart && m.utterance_id === lastAudioStart.utterance_id) {
          const bytes = Buffer.from(m.b64, 'base64');
          if (!headerSeen) { // FIRST chunk after an audio_start = 44-byte RIFF header ONLY (§1)
            assert.equal(bytes.length, 44, 'first chunk is header-only');
            assert.equal(bytes.slice(0, 4).toString(), 'RIFF');
            assert.equal(bytes.slice(8, 12).toString(), 'WAVE');
            headerSeen = true;
            rec.riffsChecked += 1;
          } else {
            assert.notEqual(bytes.slice(0, 4).toString(), 'RIFF'); // subsequent = raw PCM
            headerSeen = false;
          }
        }
      },
    });
    // GameWS queues the start while the socket connects, then flushes it on open.
    assert.equal(ws.send({ type: 'start', player_name: 'Test खिलाड़ी', language: 'enhi' }), true);

    await waitFor(() => countPhase('question') >= 1, 5000);
    assert.ok(countPhase('lobby') >= 1 && countPhase('round_intro') >= 1);

    // repeat_question: same question re-spoken with a FRESH utterance_id
    const q1 = rec.questions[0];
    const startCountBefore = rec.audioStarts.length;
    ws.send({ type: 'repeat_question' });
    await waitFor(() => rec.audioStarts.length > startCountBefore, 2000);
    const repeated = rec.audioStarts.at(-1);
    assert.equal(repeated.caption, q1.text); // caption carries the host speech (D13)
    assert.equal(repeated.sample_rate, 24000);
    assert.equal(repeated.codec, 'wav');

    // play all 7 questions: buzz → answer → alternating verdicts
    let prevScore = 0;
    for (let i = 0; i < 7; i++) {
      await waitFor(() => rec.questions.length >= i + 1, 5000);
      const qi = rec.questions[i];
      assert.ok(qi, `question ${i + 1} arrived`);
      assert.equal(qi.id, EXPECTED_Q_IDS[i]);
      ws.send({ type: 'buzz' });
      await waitFor(() => countPhase('listening') >= i + 1, 2000);
      ws.send({ type: 'answer', text: 'मेरा जवाब' });
      await waitFor(() => rec.verdicts.length >= i + 1, 5000);
      const v = rec.verdicts[i];
      assert.equal(v.correct, i % 2 === 0 ? 'correct' : 'wrong'); // alternating
      assert.equal(v.score_delta, EXPECTED_DELTAS[i]);
      assert.equal(v.score, prevScore + EXPECTED_DELTAS[i]);
      assert.equal(v.streak, i % 2 === 0 ? 1 : 0);
      assert.equal(v.heard, 'मेरा जवाब'); // typed answers echo as `heard`
      assert.ok(v.hype_line && v.template_id && typeof v.close_call === 'boolean');
      prevScore = v.score;
      await waitFor(() => countPhase(i === 6 ? 'final' : 'question') >= (i === 6 ? 1 : i + 2), 5000);
    }

    const finalState = rec.frames.filter((f) => f.type === 'state' && f.phase === 'final').at(-1);
    assert.equal(finalState.score, 950); // 150+200+250+350
    assert.equal(finalState.mode, 'demo');

    // personas rotate across rounds and the ids are the literal Sarvam speakers
    const speakers = [...new Set(rec.frames.filter((f) => f.type === 'state').map((f) => f.speaker))];
    assert.ok(speakers.includes('ratan_hi_customer_expressive'));
    assert.ok(speakers.includes('ishita_enhi_customer_expressive'));
    assert.ok(speakers.includes('ratan_hi_sports'));
    assert.ok(speakers.includes('mukul_hi_suspense'));
    // audio decomposition arrives on audio_end (server clock)
    const end = rec.frames.find((f) => f.type === 'audio_end');
    assert.ok(Number.isFinite(end.ttfb_ms) && Number.isFinite(end.e2e_ms));
    assert.ok(rec.riffsChecked >= 8, 'every utterance began with a 44-byte RIFF header chunk');

    // static server serves the worktree root (stub-fallback import path)
    const stubRes = await fetch(`http://localhost:${port}/dev-tools/stubs.js`);
    assert.equal(stubRes.status, 200);
    assert.match(stubRes.headers.get('content-type'), /text\/javascript/);

    ws.close();
  } finally {
    await mock.stop();
  }
});

test('mock-server: skip_question advances; reconnect re-`start` rejoins mid-question', { timeout: 20000 }, async () => {
  if (!createMockServer) { assert.ok('skip: dev-tools `npm i` not run (ws package missing)'); return; }
  const mock = createMockServer({ port: 0, utteranceMs: 80, judgingMs: 50, beatMs: 25 });
  const port = await mock.start();
  try {
    const rec = { phases: [], questions: [], states: [] };
    let openFlag = false;
    const sock = new WebSocket(`ws://localhost:${port}/ws`);
    sock.addEventListener('open', () => { openFlag = true; });
    sock.addEventListener('message', (ev) => {
      const m = JSON.parse(ev.data);
      if (m.type === 'state') { rec.phases.push(m.phase); rec.states.push(m); }
      if (m.type === 'question') rec.questions.push(m);
    });
    assert.ok(await waitFor(() => openFlag, 2000), 'socket opened');
    sock.send(JSON.stringify({ type: 'start', player_name: 'Rejoin', language: 'hi' }));
    assert.ok(await waitFor(() => rec.questions.length >= 1, 5000), 'first question asked');

    // skip: next question, streak reset, no penalty
    sock.send(JSON.stringify({ type: 'skip_question' }));
    assert.ok(await waitFor(() => rec.questions.length >= 2, 5000), 'skip advanced to Q2');
    assert.notEqual(rec.questions[1].id, rec.questions[0].id);
    assert.ok(rec.states.every((s) => typeof s.streak === 'number' && s.score >= 0));

    // drop the connection mid-question, then re-`start` on a NEW socket (GameWS behavior)
    sock.close();
    const rec2 = { questions: [], phases: [] };
    let openFlag2 = false;
    const sock2 = new WebSocket(`ws://localhost:${port}/ws`);
    sock2.addEventListener('open', () => { openFlag2 = true; });
    sock2.addEventListener('message', (ev) => {
      const m = JSON.parse(ev.data);
      if (m.type === 'question') rec2.questions.push(m);
      if (m.type === 'state') rec2.phases.push(m.phase);
    });
    assert.ok(await waitFor(() => openFlag2, 2000), 'second socket opened');
    sock2.send(JSON.stringify({ type: 'start', player_name: 'Rejoin', language: 'hi' }));
    assert.ok(await waitFor(() => rec2.questions.length >= 1, 5000), 'rejoin re-sent the question');
    assert.equal(rec2.questions[0].id, rec.questions[1].id); // same spot, not a reset to Q1
    assert.ok(rec2.phases.includes('question'));
    sock2.close();
  } finally {
    await mock.stop();
  }
});
