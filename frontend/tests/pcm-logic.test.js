// BhashaBuzzer — frontend/tests/pcm-logic.test.js (A8)
// node --test — pure PCM logic exported from the worklet module + RIFF strip:
// ring buffer, resample/decimate math, pre-roll discard, 8 s cap, per-id
// header strip. No AudioWorklet needed (registration is guarded in the module).

import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  PLAYER_RING_FRAMES,
  RECORDER_TARGET_RATE,
  createPlayerState,
  playerWriteChunk,
  playerRenderBlock,
  createRecorderState,
  recorderProcessBlock,
} from '../audio/pcm-worklet.js';
import { RiffStripper, pcmToInt16LE } from '../audio/host-player.js';
import { encodeWav, isRiffWav } from '../audio/wav-encoder.js';

// ---------------------------------------------------------------- constants

test('constants: ring 32768 (D7), 16 kHz target, 200 ms preroll, 8 s cap', () => {
  assert.equal(PLAYER_RING_FRAMES, 32768);
  assert.equal(RECORDER_TARGET_RATE, 16000);
  const st = createRecorderState(48000);
  assert.equal(st.ratio, 3);          // exactly ÷3 at 48 kHz (D2)
  assert.equal(st.preroll, 9600);     // 200 ms @ 48 kHz
  assert.equal(st.maxOut, 128000);    // 8 s @ 16 kHz
  const p = createPlayerState(48000, 24000);
  assert.equal(p.ratio, 2);           // exactly ×2 at 48 kHz (D2)
});

// ---------------------------------------------------------------- player ring

test('player ring: overflow drops oldest, keeps newest 32768 (D7)', () => {
  const st = createPlayerState(48000, 24000);
  const n = 40000;
  const ramp = new Int16Array(n);
  for (let i = 0; i < n; i++) ramp[i] = i % 30000;
  const dropped = playerWriteChunk(st, ramp);
  assert.equal(dropped, n - PLAYER_RING_FRAMES); // 7232
  assert.equal(st.w - st.r, PLAYER_RING_FRAMES);
  const out = new Float32Array(4);
  const f = playerRenderBlock(st, out);
  assert.equal(f.first, true);
  // read pointer advanced to 7232 → first rendered source frame is ramp[7232]
  assert.equal(out[0], 7232 / 32768);
  assert.equal(out[1], 7232.5 / 32768); // linear interp at frac 0.5
});

test('player render: exact ×2 resample (24 kHz → 48 kHz) matches interpolation', () => {
  const st = createPlayerState(48000, 24000);
  playerWriteChunk(st, new Int16Array([0, 1000, 2000, 3000]));
  const out = new Float32Array(8);
  playerRenderBlock(st, out);
  const expect = [0, 500, 1000, 1500, 2000, 2500, 3000, 3000]; // tail held at end
  for (let i = 0; i < 8; i++) assert.equal(out[i], expect[i] / 32768);
});

test('player render: non-integer ratio (44.1 kHz device) interpolates correctly', () => {
  const st = createPlayerState(44100, 24000);
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
    // out is Float32Array (AudioWorklet output) — allow float32 rounding
    assert.ok(Math.abs(out[i] - v / 32768) < 1e-7, `frame ${i}`);
  }
});

test('player: underrun fires once per dry spell; silence keeps timing (D7/§4.5)', () => {
  const st = createPlayerState(48000, 24000);
  st.active = true;
  const out = new Float32Array(128);
  assert.equal(playerRenderBlock(st, out).drySpell, true);  // first dry block
  assert.equal(playerRenderBlock(st, out).drySpell, false); // still dry — no repeat
  playerWriteChunk(st, new Int16Array([1000, 2000, 3000, 4000]));
  const f2 = playerRenderBlock(st, out);
  assert.equal(f2.first, true);       // first-frame posted once per utterance
  assert.equal(f2.drySpell, false);
  const dry = [];
  for (let i = 0; i < 10; i++) dry.push(playerRenderBlock(st, out)); // drain fully
  assert.equal(dry.filter((b) => b.drySpell).length, 1);              // NEW spell fires once
});

test('player: flush + empty ring → drained once, then inactive', () => {
  const st = createPlayerState(48000, 24000);
  st.active = true;
  st.flushing = true;
  const out = new Float32Array(128);
  assert.equal(playerRenderBlock(st, out).drained, true);
  assert.equal(st.active, false);
  const f2 = playerRenderBlock(st, out);
  assert.equal(f2.drained, false);    // no repeat
  assert.equal(f2.drySpell, false);   // inactive — not an underrun
});

test('player: gain scales output and clamps to [-1, 1]', () => {
  const st = createPlayerState(48000, 24000);
  st.active = true;
  st.gain = 2;
  playerWriteChunk(st, new Int16Array([32767, -32768]));
  const out = new Float32Array(4);
  playerRenderBlock(st, out);
  assert.equal(out[0], 1);
  assert.equal(out[2], -1);
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

test('modules load headlessly: worklet registration guard holds outside AudioWorklet', () => {
  // importing pcm-worklet.js at the top of this file already proves the guard;
  // sanity-check that the pure API is present and typed as functions.
  assert.equal(typeof createPlayerState, 'function');
  assert.equal(typeof recorderProcessBlock, 'function');
  assert.equal(typeof RiffStripper, 'function');
});
