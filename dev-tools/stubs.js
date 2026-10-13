// dev-tools/stubs.js — BhashaBuzzer A10 dev-only fakes.
// SIBLING MODULES (ui/screens.js, audio/host-player.js, audio/mic-recorder.js) are owned
// by other agents and are ABSENT in the A10 worktree. frontend/app.js imports them
// dynamically and falls back to THESE stubs so this worktree runs standalone:
//   open http://localhost:8080/frontend/index.html?mock=1   (dev-tools/mock-server.js)
// Zero DOM, zero deps — importable from node --test too.

/* ---------- tiny pure base64 (no btoa dependency, browser+node identical) ---------- */

const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';

function bytesToB64(bytes) {
  let out = '';
  for (let i = 0; i < bytes.length; i += 3) {
    const b0 = bytes[i];
    const b1 = i + 1 < bytes.length ? bytes[i + 1] : 0;
    const b2 = i + 2 < bytes.length ? bytes[i + 2] : 0;
    out += B64[b0 >> 2];
    out += B64[((b0 & 3) << 4) | (b1 >> 4)];
    out += i + 1 < bytes.length ? B64[((b1 & 15) << 2) | (b2 >> 6)] : '=';
    out += i + 2 < bytes.length ? B64[b2 & 63] : '=';
  }
  return out;
}

/** Minimal valid 16 kHz mono 16-bit WAV of pure silence. */
export function makeSilentWavBytes(sampleRate = 16000, samples = 1600) {
  const dataLen = samples * 2;
  const u8 = new Uint8Array(44 + dataLen);
  const dv = new DataView(u8.buffer);
  const ascii = (off, s) => { for (let i = 0; i < s.length; i++) u8[off + i] = s.charCodeAt(i); };
  ascii(0, 'RIFF'); dv.setUint32(4, 36 + dataLen, true); ascii(8, 'WAVE');
  ascii(12, 'fmt '); dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
  dv.setUint32(24, sampleRate, true); dv.setUint32(28, sampleRate * 2, true);
  dv.setUint16(32, 2, true); dv.setUint16(34, 16, true);
  ascii(36, 'data'); dv.setUint32(40, dataLen, true);
  return u8; // samples stay zero = silence
}

/** Pre-built tiny silent WAV (0.1 s @16 kHz) — what the MicRecorder stub "records". */
export const TINY_WAV_B64 = bytesToB64(makeSilentWavBytes(16000, 1600));

/** Fake waveform levels (deterministic), fed to onWaveform by the stub recorder. */
export function fakeWaveform(step = 0) {
  const out = new Float32Array(16);
  for (let i = 0; i < out.length; i++) {
    out[i] = Math.abs(Math.sin(step * 0.7 + i * 0.45)) * (0.45 + 0.4 * Math.abs(Math.sin(step + i)));
  }
  return out;
}

/* ---------- HostPlayer stub (audio/host-player.js stand-in) ---------- */

export class HostPlayer {
  constructor() {
    this.ready = false;
    this.speaking = false;
    this._v = 1;
    this.calls = []; // test introspection: {api, args}
  }
  async init() { this.ready = true; this.calls.push({ api: 'init' }); }
  /** Fake first-sample callback fires ~10 ms in — app.js latency chips get real ticks. */
  startUtterance(utteranceId, { sampleRate = 24000, onFirstSample } = {}) {
    this.calls.push({ api: 'startUtterance', utteranceId, sampleRate });
    this.speaking = true;
    if (typeof onFirstSample === 'function') setTimeout(() => onFirstSample(), 10);
  }
  feedChunk(utteranceId, b64) { this.calls.push({ api: 'feedChunk', utteranceId, bytes: String(b64 ?? '').length }); }
  endUtterance(utteranceId) { this.calls.push({ api: 'endUtterance', utteranceId }); this.speaking = false; }
  stopNow() { this.calls.push({ api: 'stopNow' }); this.speaking = false; }
  get volume() { return this._v; }
  set volume(v) { this._v = Number(v) || 0; }
}

/* ---------- MicRecorder stub (audio/mic-recorder.js stand-in) ---------- */

export class MicRecorder {
  constructor() {
    this._recording = false;
    this._timer = null;
    this._t0 = 0;
    this._step = 0;
  }
  async start({ onWaveform } = {}) {
    this._recording = true;
    this._t0 = Date.now();
    this._step = 0;
    if (this._timer) clearInterval(this._timer);
    this._timer = setInterval(() => {
      this._step += 1;
      if (typeof onWaveform === 'function') onWaveform(fakeWaveform(this._step));
    }, 120);
    return this;
  }
  /** ~Silence "recording". ms is clamped ≥350 so the accidental-tap guard in app.js
   *  never trips on the stub — the mock demo flow always completes. */
  async stop() {
    this._clear();
    const held = this._t0 ? Date.now() - this._t0 : 0;
    const ms = Math.min(8000, Math.max(350, held || 1000));
    return { wavB64: TINY_WAV_B64, ms };
  }
  cancel() { this._clear(); }
  _clear() {
    if (this._timer) { clearInterval(this._timer); this._timer = null; }
    this._recording = false;
  }
}

/* ---------- screens stubs (ui/screens.js stand-ins) ---------- */

export function showScreen(id) {
  if (typeof document !== 'undefined' && document.body) {
    document.body.dataset.screen = String(id);
    const sections = document.querySelectorAll('section[id^="screen-"]');
    for (const s of sections) s.classList.toggle('active', s.id === id);
  }
  if (typeof console !== 'undefined' && console.log) console.log('[stub] showScreen →', id);
}

export function setPhaseBadge(phase) {
  if (typeof document !== 'undefined' && document.body) {
    document.body.dataset.phase = String(phase ?? '');
  }
  if (typeof console !== 'undefined' && console.log) console.log('[stub] phase →', phase);
}
