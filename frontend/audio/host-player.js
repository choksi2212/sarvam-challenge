// BhashaBuzzer — audio/host-player.js (A8)
// Host-voice PCM player over the 'bb-player' AudioWorklet (contract §5; plan §4).
// ONE AudioContext at the DEVICE rate (D2). 24 kHz Int16 PCM arrives as base64
// chunks (contract §1: FIRST chunk = 44-byte RIFF header only, rest = raw PCM
// continuation), is RIFF-stripped ONCE per utterance_id on the main thread with
// magic validation (D5), then transferred into the worklet ring buffer for
// gapless playback. The worklet stays hot: first chunk plays with zero warm-up.

import { b64ToBytes, isRiffWav, RIFF_HEADER_BYTES } from './wav-encoder.js';

function _concat(a, b) {
  const u8 = new Uint8Array(a.length + b.length);
  u8.set(a, 0);
  u8.set(b, a.length);
  return u8;
}

/** Int16LE PCM bytes → fresh, aligned Int16Array (contract §1: mono Int16LE). */
export function pcmToInt16LE(bytes) {
  const n = bytes.length >> 1;
  const i16 = new Int16Array(n);
  for (let i = 0; i < n; i++) i16[i] = bytes[2 * i] | (bytes[2 * i + 1] << 8);
  return i16;
}

/**
 * Per-utterance RIFF strip state (D5). feed(bytes) returns the PCM payload to
 * enqueue (possibly zero-length), or null while a partial header is stashed.
 * No RIFF/WAVE magic on the first bytes ⇒ input is treated as raw PCM.
 * Exported for headless tests (frontend/tests/pcm-logic.test.js).
 */
export class RiffStripper {
  constructor() {
    this.done = false;
    this.pending = null;
  }

  feed(bytes) {
    if (this.done) return bytes;
    const b = this.pending ? _concat(this.pending, bytes) : bytes;
    this.pending = null;
    if (!isRiffWav(b)) {
      if (b.length >= 12) { this.done = true; return b; } // raw PCM — no magic
      this.pending = b; return null;                      // not enough bytes to tell yet
    }
    if (b.length < RIFF_HEADER_BYTES) { this.pending = b; return null; } // stash until 44
    this.done = true;
    return b.subarray(RIFF_HEADER_BYTES);                 // strip ONCE, first chunk only
  }
}

export class HostPlayer {
  constructor() {
    this.ctx = null;
    this.node = null;
    this._volume = 1;
    this._ready = null;
    this._currentId = null;
    this._stripper = null;
    this._firstCb = null;
    this._firstSeen = false;
    this._t0Perf = 0;
    this._t0Ctx = 0;
    /**
     * Optional event sink for app.js (A10): onEvent = ({ev, utteranceId, ...}) => {}.
     * Events: first-frame {at, latencyMs}, underrun, drained, stopped, overflow {dropped}.
     */
    this.onEvent = null;
  }

  /** AudioContext at DEVICE rate + addModule(pcm-worklet.js). Call on user gesture (D8). */
  async init() {
    if (this._ready) return this._ready;
    this._ready = (async () => {
      const AC = (typeof window !== 'undefined') && (window.AudioContext || window.webkitAudioContext);
      if (!AC) throw new Error('HostPlayer: WebAudio AudioContext unavailable');
      const ctx = new AC(); // device-default rate (D2)
      if (ctx.state === 'suspended') { try { await ctx.resume(); } catch (_) { /* gesture pending */ } }
      if (!ctx.audioWorklet || typeof ctx.audioWorklet.addModule !== 'function') {
        throw new Error('HostPlayer: AudioWorklet unsupported');
      }
      await ctx.audioWorklet.addModule(new URL('./pcm-worklet.js', import.meta.url).href);
      const node = new AudioWorkletNode(ctx, 'bb-player', {
        numberOfInputs: 0,
        numberOfOutputs: 1,
        outputChannelCount: [1],
      });
      node.port.onmessage = (e) => this._onWorkletEvent(e.data || {});
      node.connect(ctx.destination);
      this.ctx = ctx;
      this.node = node;
      node.port.postMessage({ cmd: 'gain', value: this._volume });
    })().catch((err) => { this._ready = null; throw err; });
    return this._ready;
  }

  get volume() { return this._volume; }
  set volume(v) {
    let x = +v;
    if (!Number.isFinite(x)) x = 1;
    this._volume = x < 0 ? 0 : x > 1 ? 1 : x;
    if (this.node) this.node.port.postMessage({ cmd: 'gain', value: this._volume });
  }

  /**
   * t0 for a new utterance: resets per-id strip state (contract §5).
   * onFirstSample(utteranceId, atPerfMs) fires when the first sample renders;
   * atPerfMs is on the performance.now() clock — app.js computes
   * chip-B latency as atPerfMs − (audio_start receipt timestamp) (plan §6).
   */
  startUtterance(utteranceId, { sampleRate = 24000, onFirstSample = null } = {}) {
    if (!this.node) return;
    this._currentId = utteranceId;
    this._stripper = new RiffStripper();
    this._firstCb = typeof onFirstSample === 'function' ? onFirstSample : null;
    this._firstSeen = false;
    this._t0Perf = performance.now();
    this._t0Ctx = this.ctx ? this.ctx.currentTime : 0;
    this.node.port.postMessage({ cmd: 'begin', utteranceId, srcRate: sampleRate });
  }

  /** Main thread: validate+strip RIFF on FIRST chunk per id; enqueue Int16 → worklet. */
  feedChunk(utteranceId, b64) {
    if (!this.node || utteranceId !== this._currentId) return; // stale (barge race §8.9)
    let bytes;
    try { bytes = b64ToBytes(b64); } catch (err) { console.warn('[HostPlayer] bad b64 chunk', err); return; }
    const pcm = this._stripper ? this._stripper.feed(bytes) : bytes;
    if (!pcm || pcm.length < 2) return; // header-only chunk or stashed partial header
    const i16 = pcmToInt16LE(pcm);
    this.node.port.postMessage({ cmd: 'chunk', utteranceId, i16: i16.buffer }, [i16.buffer]);
  }

  /** Natural end (audio_end): play out ring remainder, then `drained`. */
  endUtterance(utteranceId) {
    if (this.node && utteranceId === this._currentId) this.node.port.postMessage({ cmd: 'flush' });
  }

  /** Barge: clear ring buffer, silence instantly (D4). Late chunks drop as stale. */
  stopNow() {
    if (this.node) this.node.port.postMessage({ cmd: 'stop' });
    this._currentId = null;
    this._stripper = null;
    this._firstCb = null;
  }

  _onWorkletEvent(m) {
    switch (m.ev) {
      case 'first-frame': {
        if (this._firstSeen) break; // once per utterance
        this._firstSeen = true;
        // map AudioContext time → performance.now() clock (worklet has no perf clock)
        const atPerf = this._t0Perf + (m.at - this._t0Ctx) * 1000;
        if (this._firstCb) { try { this._firstCb(m.utteranceId, atPerf); } catch (err) { console.warn(err); } }
        this._emit({ ev: 'first-frame', utteranceId: m.utteranceId, at: atPerf, latencyMs: atPerf - this._t0Perf });
        break;
      }
      case 'underrun':
      case 'drained':
      case 'stopped':
      case 'overflow':
        this._emit(m);
        break;
      default:
        break;
    }
  }

  _emit(m) {
    if (typeof this.onEvent === 'function') {
      try { this.onEvent(m); } catch (err) { console.warn('[HostPlayer] onEvent threw', err); }
    } else if (m.ev === 'underrun' || m.ev === 'overflow') {
      console.debug('[HostPlayer]', m.ev, m.utteranceId != null ? `(${m.utteranceId})` : '', m.dropped != null ? `dropped=${m.dropped}` : '');
    }
  }
}
