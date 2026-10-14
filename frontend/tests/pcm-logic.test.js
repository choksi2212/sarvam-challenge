// BhashaBuzzer — frontend/tests/pcm-logic.test.js (A8)
// node --test — pure PCM logic exported from the worklet module + RIFF strip:
// ring buffer + chunk queue, cross-chunk gapless resampling, underrun
// behavior, start priming, resample/decimate math, pre-roll discard, 8 s cap,
// per-id header strip, Int16LE endianness. No AudioWorklet needed (processor
// registration is guarded in the module).

import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  PLAYER_RING_FRAMES,
  PLAYER_PRIME_MS,
  PLAYER_MAX_QUEUE_FRAMES,
  RECORDER_TARGET_RATE,
  createPlayerState,
  playerWriteChunk,
  playerPump,
  playerReset,
  playerRenderBlock,
  createRecorderState,
  recorderProcessBlock,
} from '../audio/pcm-worklet.js';
import { RiffStripper, pcmToInt16LE } from '../audio/host-player.js';
import { encodeWav, isRiffWav } from '../audio/wav-encoder.js';

const SRC_RATE = 24000;
const CTX_RATE = 48000;

// ---------------------------------------------------------------- helpers

/** Sine tone as Int16 PCM at SRC_RATE. */
function sineTone(freq, seconds, rate = SRC_RATE, amp = 12000) {
  const n = Math.round(seconds * rate);
  const t = new Int16Array(n);
  for (let i = 0; i < n; i++) t[i] = Math.round(Math.sin(2 * Math.PI * freq * i / rate) * amp);
  return t;
}

/** Reference render: whole source available up front, then drain to completion. */
function oneShot(src, blockLen = 128) {
  const st = createPlayerState(CTX_RATE, SRC_RATE, PLAYER_RING_FRAMES, 0);
  st.active = true;
  playerWriteChunk(st, src);
  st.flushing = true;
  return playOut(st, blockLen).rendered;
}

/** Render n blocks, collecting actually-rendered samples + event counts. */
function renderN(st, n, blockLen = 128) {
  const rendered = [];
  const ev = { first: 0, underrun: 0, drained: 0 };
  const out = new Float32Array(blockLen);
  for (let b = 0; b < n; b++) {
    const f = playerRenderBlock(st, out);
    if (f.first) ev.first++;
    if (f.drySpell) ev.underrun++;
    if (f.drained) ev.drained++;
    for (let i = 0; i < f.rendered; i++) rendered.push(out[i]);
  }
  return { rendered, ev };
}

/** Render until drained (state must be flushing); bounded loop. */
function playOut(st, blockLen = 128, maxBlocks = 400000) {
  const rendered = [];
  const ev = { first: 0, underrun: 0, drained: 0 };
  const out = new Float32Array(blockLen);
  for (let b = 0; b < maxBlocks; b++) {
    const f = playerRenderBlock(st, out);
    if (f.first) ev.first++;
    if (f.drySpell) ev.underrun++;
    for (let i = 0; i < f.rendered; i++) rendered.push(out[i]);
    if (f.drained) { ev.drained++; break; }
  }
  return { rendered, ev };
}

/** Two rendered sample streams must be identical (same math, same positions). */
function assertSameStream(a, b, msg = 'streams differ') {
  assert.equal(a.length, b.length, `${msg}: length ${a.length} vs ${b.length}`);
  for (let i = 0; i < a.length; i++) {
    assert.ok(Math.abs(a[i] - b[i]) < 1e-9, `${msg} at ${i}: ${a[i]} vs ${b[i]}`);
  }
}

// ---------------------------------------------------------------- constants

test('constants: ring 32768 (D7), prime 150 ms, 16 kHz target, 200 ms preroll, 8 s cap', () => {
  assert.equal(PLAYER_RING_FRAMES, 32768);
  assert.equal(PLAYER_PRIME_MS, 150);
  assert.equal(PLAYER_MAX_QUEUE_FRAMES, 720000);   // 30 s @ 24 kHz runaway cap
  assert.equal(RECORDER_TARGET_RATE, 16000);
  const st = createRecorderState(48000);
  assert.equal(st.ratio, 3);          // exactly ÷3 at 48 kHz (D2)
  assert.equal(st.preroll, 9600);     // 200 ms @ 48 kHz
  assert.equal(st.maxOut, 128000);    // 8 s @ 16 kHz
  const p = createPlayerState(48000, 24000);
  assert.equal(p.ratio, 2);           // exactly ×2 at 48 kHz (D2)
  assert.equal(p.prime, 3600);        // 150 ms @ 24 kHz start prefetch (F3)
});

// ---------------------------------------------------------------- player core

test('player render: exact ×2 resample (24 kHz → 48 kHz) matches interpolation', () => {
  const st = createPlayerState(48000, 24000, PLAYER_RING_FRAMES, 0);
  playerWriteChunk(st, new Int16Array([0, 1000, 2000, 3000]));
  const out = new Float32Array(8);
  const f = playerRenderBlock(st, out);
  assert.equal(f.rendered, 6);  // needs 2 taps: stops one src frame short of the end
  const expect = [0, 500, 1000, 1500, 2000, 2500, 0, 0]; // beyond last tap: gap silence
  for (let i = 0; i < 8; i++) assert.equal(out[i], expect[i] / 32768);
});

test('player render: non-integer ratio (44.1 kHz device) interpolates correctly', () => {
  const st = createPlayerState(44100, 24000, PLAYER_RING_FRAMES, 0);
  const step = 24000 / 44100; // src frames per output frame
  assert.ok(Math.abs(st.step - step) < 1e-12);
  const src = [0, 1000, 2000, 3000, 4000, 5000, 6000];
  playerWriteChunk(st, new Int16Array(src));
  const out = new Float32Array(4);
  playerRenderBlock(st, out);
  for (let i = 0; i < 4; i++) {
    const p = i * step;
    const i0 = Math.floor(p);
    const frac = p - i0;
    const v = i0 >= src.length - 1 ? src[src.length - 1] : src[i0] * (1 - frac) + src[i0 + 1] * frac;
    assert.ok(Math.abs(out[i] - v / 32768) < 1e-7, `frame ${i}`);
  }
});

test('player: gain scales output and clamps to [-1, 1]', () => {
  const st = createPlayerState(48000, 24000, PLAYER_RING_FRAMES, 0);
  st.active = true;
  st.gain = 2;
  playerWriteChunk(st, new Int16Array([32767, -32768]));
  const out = new Float32Array(4);
  playerRenderBlock(st, out);
  assert.equal(out[0], 1);                      // 32767 × 2 clamps to +1
  assert.equal(out[1], -1 / 32768);             // midpoint −0.5 src × 2 (tiny, no clamp)
  assert.equal(out[2], 0);                      // cursor froze after the only 2 src frames (F2)
  assert.equal(out[3], 0);
});

test('player: flush + empty ring → drained once, then inactive', () => {
  const st = createPlayerState(48000, 24000, PLAYER_RING_FRAMES, 0);
  st.active = true;
  st.flushing = true;
  const out = new Float32Array(128);
  assert.equal(playerRenderBlock(st, out).drained, true);
  assert.equal(st.active, false);
  const f2 = playerRenderBlock(st, out);
  assert.equal(f2.drained, false);    // no repeat
  assert.equal(f2.drySpell, false);   // inactive — not an underrun
});

// ------------------------------------------------- regression: live-bug fixes

test('RC1 regression: cache-hit burst dump — nothing dropped, nothing skipped', () => {
  const tone = sineTone(440, 10);            // 240000 frames ≫ 32768-frame ring
  const st = createPlayerState(CTX_RATE, SRC_RATE, PLAYER_RING_FRAMES, 0);
  st.active = true;
  let dropped = 0;
  for (let k = 0; k < tone.length; k += 16384) {   // server chunk size: 16384 frames
    dropped += playerWriteChunk(st, tone.subarray(k, Math.min(k + 16384, tone.length)));
  }
  assert.equal(dropped, 0);                          // old code dropped ~200k frames here
  assert.equal(st.qn + (st.w - st.r), tone.length);  // frame conservation
  st.flushing = true;
  const got = playOut(st);
  assert.equal(got.rendered.length, Math.ceil((tone.length - 1) / 0.5)); // full 10 s played
  assertSameStream(got.rendered, oneShot(tone), 'burst dump vs one-shot');
  assert.equal(got.ev.underrun, 0);   // queue + pump → never starves
  assert.equal(got.ev.drained, 1);
});

test('RC2/F2 regression: tone spanning 3 chunks decodes gapless (no discontinuity)', () => {
  const tone = sineTone(440, 0.3);   // 7200 src frames
  const st = createPlayerState(CTX_RATE, SRC_RATE, PLAYER_RING_FRAMES, 0);
  st.active = true;
  const third = Math.floor(tone.length / 3);
  playerWriteChunk(st, tone.subarray(0, third));
  const a = renderN(st, 200);       // 12800 src frames wanted > 2400 → starves, freezes
  playerWriteChunk(st, tone.subarray(third, 2 * third));
  const b = renderN(st, 200);       // starves again at the next boundary
  playerWriteChunk(st, tone.subarray(2 * third));
  st.flushing = true;
  const c = playOut(st);
  const seq = [...a.rendered, ...b.rendered, ...c.rendered];
  // sample-exact equality with the one-shot render = srcPos continuity held
  assertSameStream(seq, oneShot(tone), 'chunked-starved vs one-shot');
  assert.equal(seq.length, Math.ceil((tone.length - 1) / 0.5));
  assert.equal(a.ev.underrun + b.ev.underrun + c.ev.underrun, 2); // one event per dry spell
  let maxJump = 0;   // a skip/click would exceed the 440 Hz slope (~0.029/step)
  for (let i = 1; i < seq.length; i++) maxJump = Math.max(maxJump, Math.abs(seq[i] - seq[i - 1]));
  assert.ok(maxJump < 0.03, `discontinuity: max jump ${maxJump}`);
});

test('RC2/F2 regression: underrun fires once per dry spell; resume is sample-exact', () => {
  const st = createPlayerState(CTX_RATE, SRC_RATE, PLAYER_RING_FRAMES, 0);
  st.active = true;
  const A = new Int16Array(3000);
  const B = new Int16Array(3000);
  for (let i = 0; i < 3000; i++) { A[i] = i + 100; B[i] = 3100 + i; } // distinct ramps
  playerWriteChunk(st, A);
  const drain = renderN(st, 50);      // 3200 src frames wanted > 3000 → dry spell
  assert.equal(drain.ev.underrun, 1); // exactly once per spell
  assert.equal(drain.rendered.length, Math.ceil((A.length - 1) / 0.5));
  playerWriteChunk(st, B);            // late audio: must resume, not skip
  st.flushing = true;
  const rest = playOut(st);
  assert.equal(rest.ev.underrun, 0);
  const both = new Int16Array(6000);
  both.set(A, 0); both.set(B, A.length);
  assertSameStream([...drain.rendered, ...rest.rendered], oneShot(both), 'resume vs one-shot');
});

test('RC3/F3 regression: 150 ms prime — silent wait, no false underrun, audible at prime', () => {
  const st = createPlayerState(CTX_RATE, SRC_RATE);   // default prime = 3600 frames
  st.active = true;
  const out = new Float32Array(128);
  for (let i = 0; i < 20; i++) {      // no data at all — old code fired a false underrun here
    const f = playerRenderBlock(st, out);
    assert.equal(f.drySpell, false);
    assert.equal(f.first, false);
    assert.equal(f.rendered, 0);
  }
  playerWriteChunk(st, new Int16Array(2048).fill(1000));  // 85 ms — below prime
  let f = playerRenderBlock(st, out);
  assert.equal(f.first, false);       // still priming
  assert.equal(f.drySpell, false);
  playerWriteChunk(st, new Int16Array(2048).fill(1000));  // 4096 ≥ 3600 → audible
  f = playerRenderBlock(st, out);
  assert.equal(f.first, true);        // first-frame = first AUDIBLE sample
  assert.ok(f.rendered > 0);
  assert.equal(st.prime, 3600);       // 150 ms @ 24 kHz
});

test('F3: flush during priming plays a short utterance out fully', () => {
  const st = createPlayerState(CTX_RATE, SRC_RATE);   // prime 3600 > 480-frame clip
  st.active = true;
  playerWriteChunk(st, new Int16Array(480).fill(1000));   // 20 ms
  st.flushing = true;                 // audio_end beats the prime
  const got = playOut(st);
  assert.equal(got.ev.drained, 1);
  assert.equal(got.rendered.length, Math.ceil((480 - 1) / 0.5)); // 958 — everything played
  for (const v of got.rendered) assert.ok(v > 0);  // constant tone — no silence gaps
});

test('F1: partial pump + ring wraparound keeps exact stream order', () => {
  const src = sineTone(440, 0.05, SRC_RATE, 9000);       // 1200 frames
  const st = createPlayerState(CTX_RATE, SRC_RATE, 1024, 0);  // tiny ring forces wrap
  st.active = true;
  playerWriteChunk(st, src.subarray(0, 800));
  const pre = renderN(st, 2);           // consume 128 src frames
  playerWriteChunk(st, src.subarray(800));  // 400 more; only 352 fit → 48 queued
  assert.equal(st.qn, 48);
  st.flushing = true;
  const got = playOut(st);
  assertSameStream([...pre.rendered, ...got.rendered], oneShot(src), 'wraparound vs one-shot');
});

test('F1/F4: flush with data still queued plays the queue out fully', () => {
  const src = sineTone(300, 0.2, SRC_RATE, 10000);       // 4800 frames
  const st = createPlayerState(CTX_RATE, SRC_RATE, 1024, 0);  // ring holds 1024
  st.active = true;
  assert.equal(playerWriteChunk(st, src), 0);            // 3776 queued behind a full ring
  assert.equal(st.qn, src.length - 1024);
  st.flushing = true;                                       // audio_end arrives early
  const got = playOut(st);
  assert.equal(got.ev.underrun, 0);
  assert.equal(got.ev.drained, 1);
  assertSameStream(got.rendered, oneShot(src), 'queued playout vs one-shot');
});

test('F4: playerReset (barge) — instant silence and clean restart from frame 0', () => {
  const st = createPlayerState(CTX_RATE, SRC_RATE, PLAYER_RING_FRAMES, 0);
  st.active = true;
  const src = new Int16Array(1200);
  for (let i = 0; i < src.length; i++) src[i] = i + 100;
  playerWriteChunk(st, src);
  renderN(st, 5);                       // partway through
  playerReset(st);
  assert.equal(st.qn, 0);
  assert.equal(st.w - st.r, 0);
  assert.equal(st.srcPos, 0);
  assert.equal(st.active, false);
  const out = new Float32Array(128);
  const f = playerRenderBlock(st, out);  // silence, no events, no stale audio
  assert.equal(f.rendered, 0);
  assert.equal(f.drySpell, false);
  assert.equal(f.drained, false);
  st.active = true;                      // next utterance restarts cleanly
  playerWriteChunk(st, src);
  const f2 = playerRenderBlock(st, out);
  assert.equal(f2.first, true);
  assert.equal(f2.rendered, 128);
  assert.equal(out[0], 100 / 32768);    // begins at source frame 0
});

test('F1: runaway cap drops newest whole chunks only past 30 s pending', () => {
  const st = createPlayerState(CTX_RATE, SRC_RATE, PLAYER_RING_FRAMES, 0);
  st.active = true;
  const chunk = new Int16Array(16384);
  let dropped = 0;
  const nChunks = Math.ceil((PLAYER_MAX_QUEUE_FRAMES + PLAYER_RING_FRAMES) / 16384) + 1;
  for (let k = 0; k < nChunks; k++) dropped += playerWriteChunk(st, chunk);
  assert.ok(dropped > 0);
  assert.equal(dropped % 16384, 0);          // whole chunks only
  assert.ok(st.qn <= PLAYER_MAX_QUEUE_FRAMES);
  assert.ok(st.qn + (st.w - st.r) >= PLAYER_MAX_QUEUE_FRAMES); // kept the older audio
});

// ---------------------------------------------------------------- recorder

test('recorder: exact ÷3 decimation (48 kHz → 16 kHz), pre-roll straddled', () => {
  const st = createRecorderState(48000);
  const preroll = 9600;
  const input = new Float32Array(preroll + 4800).fill(9); // junk everywhere first
  for (let k = 0; k < 4800; k++) input[preroll + k] = k / 4800; // 0→1 ramp after
  const r = recorderProcessBlock(st, input);
  assert.equal(r.i16.length, 1600);
  assert.equal(r.i16[0], 0);
  assert.equal(r.i16[5], 102); // round(15/4800 × 32767) = round(102.397)
  for (let j = 1600 - 1; j >= 0; j -= 399) {
    const v = (3 * j) / 4800;
    assert.equal(r.i16[j], Math.round(v * 32767));
  }
});

test('recorder: 200 ms pre-roll discarded across block boundaries (D4)', () => {
  const st = createRecorderState(48000);
  assert.equal(recorderProcessBlock(st, new Float32Array(9600).fill(0.9)).i16, null);
  const sig = new Float32Array(4800);
  for (let k = 0; k < 4800; k++) sig[k] = k / 4800;
  const r = recorderProcessBlock(st, sig);
  assert.equal(r.i16.length, 1600);
  assert.equal(r.i16[0], 0); // first post-preroll sample lands at frac 0
});

test('recorder: 8 s hard cap — exactly 128000 samples, max fires once', () => {
  const st = createRecorderState(48000);
  const block = new Float32Array(480).fill(0.5); // 10 ms @ 48 kHz
  let total = 0;
  let maxCount = 0;
  let guard = 0;
  while (maxCount === 0 && guard++ < 2000) {
    const r = recorderProcessBlock(st, block);
    if (r.i16) total += r.i16.length;
    if (r.maxed) maxCount++;
  }
  assert.equal(total, 128000);
  assert.equal(maxCount, 1);
  const after = recorderProcessBlock(st, block);
  assert.equal(after.i16, null);       // capped — nothing more
  assert.equal(after.maxed, false);    // max not repeated
});

test('recorder: cap truncates mid-block on a single big input', () => {
  const st = createRecorderState(48000);
  recorderProcessBlock(st, new Float32Array(9600).fill(0)); // pre-roll
  const r = recorderProcessBlock(st, new Float32Array(400000).fill(0.25));
  assert.equal(r.i16.length, 128000);
  assert.equal(r.maxed, true);
  assert.equal(r.i16[0], 8192); // round(0.25 × 32767)
});

// ---------------------------------------------------------------- RIFF strip (D5)

test('RiffStripper: contract shape — 44-byte header first chunk, raw PCM after', () => {
  const s = new RiffStripper();
  const header = encodeWav([]); // valid 44-byte RIFF/WAVE header, zero data
  assert.equal(isRiffWav(header), true);
  const r1 = s.feed(header);
  assert.equal(r1.length, 0); // header-only chunk → nothing to enqueue
  const pcm = new Uint8Array([1, 2, 3, 4, 5, 6]);
  assert.deepEqual(s.feed(pcm), pcm); // second chunk untouched
});

test('RiffStripper: header split across tiny chunks stashes until 44 bytes', () => {
  const s = new RiffStripper();
  const header = encodeWav([]);
  assert.equal(s.feed(header.subarray(0, 10)), null); // partial magic — stash
  assert.equal(s.feed(header.subarray(10, 30)), null);
  const rest = new Uint8Array(14 + 4);
  rest.set(header.subarray(30), 0);
  rest.set(new Uint8Array([9, 8, 7, 6]), 14);
  assert.deepEqual(s.feed(rest), new Uint8Array([9, 8, 7, 6]));
});

test('RiffStripper: no RIFF magic → raw PCM passthrough from byte 0', () => {
  const s = new RiffStripper();
  const raw = new Uint8Array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]);
  assert.deepEqual(s.feed(raw), raw);
  const more = new Uint8Array([15, 16]);
  assert.deepEqual(s.feed(more), more);
});

test('RiffStripper: full WAV in one chunk (cached-blob drift) strips to PCM', () => {
  const s = new RiffStripper();
  const wav = encodeWav([new Float32Array([0.5, -0.5])]); // 44 + 4 bytes
  const pcm = s.feed(wav);
  assert.equal(pcm.length, 4);
  assert.deepEqual(Array.from(pcm), Array.from(wav.subarray(44)));
});

// ---------------------------------------------------------------- Int16LE assembly

test('pcmToInt16LE: little-endian byte pairs → Int16 samples (contract §1)', () => {
  const i16 = pcmToInt16LE(new Uint8Array([0x00, 0x40, 0x00, 0xc0, 0xff, 0x7f, 0x00, 0x80]));
  assert.deepEqual(Array.from(i16), [16384, -16384, 32767, -32768]);
  assert.equal(pcmToInt16LE(new Uint8Array([1, 2, 3])).length, 1); // odd trailing byte dropped
});

test('endianness regression: LE not BE; sign wrap; encodeWav round-trip bit-exact', () => {
  assert.equal(pcmToInt16LE(new Uint8Array([0x34, 0x12]))[0], 0x1234);  // low byte first
  assert.notEqual(pcmToInt16LE(new Uint8Array([0x34, 0x12]))[0], 0x3412);
  assert.equal(pcmToInt16LE(new Uint8Array([0x00, 0x80]))[0], -32768);  // 0x8000 sign bit
  assert.equal(pcmToInt16LE(new Uint8Array([0xff, 0xff]))[0], -1);       // 0xFFFF wraps
  assert.equal(pcmToInt16LE(new Uint8Array([0xfe, 0x7f]))[0], 32766);    // +max odd value
  // full round trip through encodeWav (explicit LE via DataView):
  const src = new Int16Array([0, 1, -1, 12345, -12345, 32767, -32768, 0x1234, -2]);
  const wav = encodeWav([src]);
  const back = pcmToInt16LE(wav.subarray(44));
  assert.deepEqual(Array.from(back), Array.from(src));
});

test('modules load headlessly: worklet registration guard holds outside AudioWorklet', () => {
  assert.equal(typeof createPlayerState, 'function');
  assert.equal(typeof playerRenderBlock, 'function');
  assert.equal(typeof playerPump, 'function');
  assert.equal(typeof recorderProcessBlock, 'function');
  assert.equal(typeof RiffStripper, 'function');
});
