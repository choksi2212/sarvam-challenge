// BhashaBuzzer — frontend/tests/wav-encoder.test.js (A8)
// node --test — pure WAV encode + base64 + RIFF helper logic (contract §5, plan §5.3/D5).

import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  encodeWav,
  encodeWavToB64,
  toBase64,
  b64ToBytes,
  peekStr,
  isRiffWav,
  RIFF_HEADER_BYTES,
} from '../audio/wav-encoder.js';

test('encodeWav: 44-byte RIFF header fields for 16 kHz mono', () => {
  const wav = encodeWav([new Float32Array([0, 0.5, -0.5, 1, -1])]);
  assert.equal(wav.length, RIFF_HEADER_BYTES + 10);
  const dv = new DataView(wav.buffer);
  assert.equal(peekStr(wav, 0, 4), 'RIFF');
  assert.equal(dv.getUint32(4, true), 36 + 10);
  assert.equal(peekStr(wav, 8, 4), 'WAVE');
  assert.equal(peekStr(wav, 12, 4), 'fmt ');
  assert.equal(dv.getUint32(16, true), 16);    // fmt chunk size
  assert.equal(dv.getUint16(20, true), 1);      // PCM
  assert.equal(dv.getUint16(22, true), 1);      // mono
  assert.equal(dv.getUint32(24, true), 16000);  // sample rate
  assert.equal(dv.getUint32(28, true), 32000);  // byte rate
  assert.equal(dv.getUint16(32, true), 2);     // block align
  assert.equal(dv.getUint16(34, true), 16);    // bits per sample
  assert.equal(peekStr(wav, 36, 4), 'data');
  assert.equal(dv.getUint32(40, true), 10);    // data length
});

test('encodeWav: float samples → Int16LE, symmetric scale, +1 clips to 32767', () => {
  const wav = encodeWav([new Float32Array([0, 0.5, -0.5, 1, -1])]);
  const dv = new DataView(wav.buffer);
  assert.equal(dv.getInt16(44, true), 0);
  assert.equal(dv.getInt16(46, true), 16384);   // round(0.5 × 32768)
  assert.equal(dv.getInt16(48, true), -16384);  // symmetric
  assert.equal(dv.getInt16(50, true), 32767);   // +1.0 clips
  assert.equal(dv.getInt16(52, true), -32768);  // −1.0 full scale
});

test('encodeWav: Int16Array chunks pass through bit-exactly', () => {
  const wav = encodeWav([new Int16Array([1, -2, 32767, -32768])]);
  const dv = new DataView(wav.buffer);
  assert.equal(dv.getUint32(40, true), 8);
  assert.equal(dv.getInt16(44, true), 1);
  assert.equal(dv.getInt16(46, true), -2);
  assert.equal(dv.getInt16(48, true), 32767);
  assert.equal(dv.getInt16(50, true), -32768);
});

test('encodeWav: Int16 → float32 (i/32768) → encode is bit-exact', () => {
  const i16 = new Int16Array([0, 1, -1, 12345, -12345, 32767, -32768]);
  const f32 = new Float32Array(i16.length);
  for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
  assert.deepEqual(encodeWav([f32]), encodeWav([i16]));
});

test('encodeWavToB64: empty capture → 44-byte header-only WAV', () => {
  const bytes = b64ToBytes(encodeWavToB64([]));
  assert.equal(bytes.length, 44);
  assert.equal(peekStr(bytes, 0, 4), 'RIFF');
  assert.equal(new DataView(bytes.buffer).getUint32(40, true), 0);
});

test('encodeWavToB64: accepts non-array iterables (generators)', () => {
  function* gen() { yield new Float32Array([0.25, -0.25]); }
  const bytes = b64ToBytes(encodeWavToB64(gen()));
  assert.equal(bytes.length, 48);
  const dv = new DataView(bytes.buffer);
  assert.equal(dv.getInt16(44, true), 8192);   // round(0.25 × 32768)
  assert.equal(dv.getInt16(46, true), -8192);
});

test('encodeWav: multi-chunk concat preserves order', () => {
  const a = new Float32Array([0.5, -0.5]);
  const b = new Int16Array([7, -7]);
  const wav = encodeWav([a, b]);
  const dv = new DataView(wav.buffer);
  assert.equal(dv.getUint32(40, true), 8);
  assert.equal(dv.getInt16(44, true), 16384);
  assert.equal(dv.getInt16(46, true), -16384);
  assert.equal(dv.getInt16(48, true), 7);
  assert.equal(dv.getInt16(50, true), -7);
});

test('encodeWav honors sampleRate and channels options', () => {
  const wav = encodeWav([new Float32Array([0, 0])], { sampleRate: 8000, channels: 2 });
  const dv = new DataView(wav.buffer);
  assert.equal(dv.getUint16(22, true), 2);         // channels
  assert.equal(dv.getUint32(24, true), 8000);       // sample rate
  assert.equal(dv.getUint32(28, true), 8000 * 4);   // byte rate = rate×ch×2
  assert.equal(dv.getUint16(32, true), 4);          // block align
  assert.equal(dv.getUint32(40, true), 4);
  assert.equal(wav.length, 48);
});

test('8 s answer budget: 128000 samples → 256,044-byte WAV (plan §5.3)', () => {
  const wav = encodeWav([new Float32Array(128000)]);
  assert.equal(wav.length, 256044);
  assert.equal(new DataView(wav.buffer).getUint32(40, true), 256000);
});

test('toBase64/b64ToBytes: round-trip all 256 byte values', () => {
  const u8 = new Uint8Array(256);
  for (let i = 0; i < 256; i++) u8[i] = i;
  assert.deepEqual(b64ToBytes(toBase64(u8)), u8);
});

test('toBase64: chunked path survives 300 KB without stack overflow', () => {
  const n = 300000;
  const u8 = new Uint8Array(n);
  for (let i = 0; i < n; i++) u8[i] = (i * 7) & 0xff;
  const b64 = toBase64(u8);
  assert.equal(b64.length, 4 * Math.ceil(n / 3));
  assert.deepEqual(b64ToBytes(b64), u8);
});

test('b64ToBytes: fresh aligned Uint8Array of exact length', () => {
  const u8 = b64ToBytes(toBase64(new Uint8Array([1, 2, 3])));
  assert.ok(u8 instanceof Uint8Array);
  assert.equal(u8.buffer.byteLength, 3);
  assert.deepEqual(Array.from(u8), [1, 2, 3]);
});

test('isRiffWav: magic validation (D5) — RIFF@0 AND WAVE@8', () => {
  const wav = encodeWav([new Float32Array([0])]);
  assert.equal(isRiffWav(wav), true);
  assert.equal(isRiffWav(wav.subarray(4)), false);     // magic moved/absent
  assert.equal(isRiffWav(new Uint8Array(8)), false);   // too short to tell
  assert.equal(isRiffWav(null), false);
  const notMagic = new Uint8Array(64).fill(0x41);
  assert.equal(isRiffWav(notMagic), false);
});
