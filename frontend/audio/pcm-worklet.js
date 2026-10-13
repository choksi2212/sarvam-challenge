// BhashaBuzzer — audio/pcm-worklet.js (A8)
// ONE worklet module, TWO processors (plan D6):
//   'bb-player'   — ring-buffer PCM player, linear-interp resample srcRate→device
//                   rate (D2: one AudioContext at DEVICE rate; D7: 32768-frame ring)
//   'bb-recorder' — mic decimator deviceRate→16 kHz with 200 ms pre-roll discard
//                   and 8 s hard cap (plan §5.2)
//
// Main→worklet protocol (plan §4.2):  {cmd:'begin'|'chunk'|'flush'|'stop'|'gain'|'start'}
// Worklet→main events:                first-frame | underrun | drained | stopped |
//                                     overflow | samples | max
//
// All PCM logic is exported as pure functions (no worklet globals needed) so
// `node --test` exercises it headlessly; processor registration is guarded so
// this file also evaluates outside an AudioWorkletGlobalScope.

export const PLAYER_RING_FRAMES = 32768;    // src frames: 1.365 s @ 24 kHz, 64 KB (D7)
export const RECORDER_TARGET_RATE = 16000;  // answer WAV rate (contract §1)
export const RECORDER_PREROLL_MS = 200;    // mic pre-roll discard (D4)
export const RECORDER_MAX_MS = 8000;       // contract §2: 8 s hard auto-stop

export function clamp01(v) { return v < 0 ? 0 : v > 1 ? 1 : v; }
export function clampInt16(v) { return v < -32768 ? -32768 : v > 32767 ? 32767 : v; }

// ------------------------------------------------------------------ bb-player

/** Fresh player state. ctxRate = AudioContext rate, srcRate = PCM rate (24000). */
export function createPlayerState(ctxRate, srcRate = 24000, ringFrames = PLAYER_RING_FRAMES) {
  const safeCtx = ctxRate > 0 ? ctxRate : 48000;
  const safeSrc = srcRate > 0 ? srcRate : 24000;
  return {
    ring: ringFrames,
    buf: new Int16Array(ringFrames),
    w: 0, r: 0,        // absolute write/read counters; ring index = counter % ring
    srcPos: 0,          // fractional source-position accumulator
    ratio: safeCtx / safeSrc,            // ctxRate/srcRate (e.g. ×2 @ 48 kHz) — informational
    step: safeSrc / safeCtx,            // src frames advanced PER OUTPUT frame (the resample step)
    active: false,
    flushing: false,
    firstFrame: false,
    dry: false,
    utteranceId: null,
    gain: 1,
  };
}

/** Append Int16 source frames to the ring. Overflow drops oldest; returns dropped count. */
export function playerWriteChunk(st, i16) {
  if (!i16 || i16.length === 0) return 0;
  const n = i16.length;
  const free = st.ring - (st.w - st.r);
  let dropped = 0;
  if (n > free) { dropped = n - free; st.r += dropped; }
  const start = st.w % st.ring;
  const first = Math.min(n, st.ring - start);
  st.buf.set(i16.subarray(0, first), start);
  if (first < n) st.buf.set(i16.subarray(first), 0);
  st.w += n;
  return dropped;
}

/**
 * Render one block (out.length frames) from the ring, linear-interp resampling
 * srcRate→ctxRate. Underrun fills silence and keeps timing — never restarts (D7).
 * Returns { first, drySpell, drained }:
 *   first    — first rendered frame of this utterance (→ post first-frame)
 *   drySpell — wet→dry transition while active & not flushing (→ post underrun,
 *              once per dry spell)
 *   drained  — flushing and ring empty (→ post drained; utterance complete)
 */
export function playerRenderBlock(st, out) {
  const avail = st.w - st.r;
  if (avail <= 0) {
    out.fill(0);
    let drySpell = false;
    if (st.active && !st.flushing && !st.dry) { st.dry = true; drySpell = true; }
    const drained = st.active && st.flushing;
    if (drained) st.active = false;
    return { first: false, drySpell, drained };
  }
  st.dry = false;
  const ring = st.ring, buf = st.buf, step = st.step, g = st.gain;
  const last = st.w - 1;
  for (let i = 0; i < out.length; i++) {
    const p = st.r + st.srcPos;
    const i0 = Math.floor(p);
    const frac = p - i0;
    let s;
    if (i0 >= last) s = buf[last % ring];   // tail hold: never read stale ring data
    else s = buf[i0 % ring] * (1 - frac) + buf[(i0 + 1) % ring] * frac;
    const v = (s / 32768) * g;
    out[i] = v < -1 ? -1 : v > 1 ? 1 : v;
    st.srcPos += step;   // srcRate/ctxRate src frames per output frame (0.5 @ 48k←24k)
  }
  const consumed = Math.floor(st.srcPos);
  st.r += consumed;
  st.srcPos -= consumed;
  const first = !st.firstFrame;
  st.firstFrame = true;
  return { first, drySpell: false, drained: false };
}

// ---------------------------------------------------------------- bb-recorder

/** Fresh recorder state: decimate ctxRate→targetRate(16 kHz), preroll + cap. */
export function createRecorderState(ctxRate, targetRate = RECORDER_TARGET_RATE) {
  return {
    ratio: ctxRate / targetRate,
    preroll: Math.floor((RECORDER_PREROLL_MS / 1000) * ctxRate), // input frames
    discarded: 0,
    acc: 0,
    emitted: 0,
    maxOut: Math.round((RECORDER_MAX_MS / 1000) * targetRate),  // 128000 @ 16 kHz
    maxed: false,
  };
}

/**
 * One render-quantum step: discard pre-roll, linear-interp decimate to 16 kHz.
 * input: Float32Array block at the AudioContext rate.
 * Returns { i16: Int16Array|null, maxed }: i16 is null while still discarding
 * pre-roll or after the cap; output truncates exactly at maxOut and `maxed`
 * fires exactly once (→ main thread 8 s hard auto-stop).
 */
export function recorderProcessBlock(st, input) {
  if (!input || input.length === 0) return { i16: null, maxed: false };
  let in0 = input;
  if (st.discarded < st.preroll) {                    // 200 ms pre-roll discard (D4)
    const need = st.preroll - st.discarded;
    if (in0.length <= need) { st.discarded += in0.length; return { i16: null, maxed: false }; }
    in0 = in0.subarray(need);
    st.discarded = st.preroll;
  }
  if (st.emitted >= st.maxOut) return { i16: null, maxed: false };
  const room = st.maxOut - st.emitted;
  const out = [];
  while (st.acc + 1 < in0.length && out.length < room) {
    const i0 = Math.floor(st.acc);
    const frac = st.acc - i0;
    const v = in0[i0] * (1 - frac) + in0[i0 + 1] * frac;
    out.push(clampInt16(Math.round(v * 32767)));
    st.acc += st.ratio;
  }
  st.acc -= in0.length;
  st.emitted += out.length;
  let maxed = false;
  if (st.emitted >= st.maxOut && !st.maxed) { st.maxed = true; maxed = true; }
  return { i16: new Int16Array(out), maxed };
}

// ----------------------------------------------- AudioWorklet processor registration
// Guarded so this module also loads under `node --test` (no worklet globals there).
if (typeof registerProcessor === 'function' &&
    typeof AudioWorkletProcessor === 'function' &&
    typeof sampleRate === 'number') {

  class PlayerProcessor extends AudioWorkletProcessor {
    constructor() {
      super();
      this.gain = 1;
      this.st = createPlayerState(sampleRate, 24000);
      this.port.onmessage = (e) => {
        const m = e.data || {};
        switch (m.cmd) {
          case 'begin': {
            this.st = createPlayerState(sampleRate, m.srcRate);
            this.st.gain = this.gain;
            this.st.active = true;
            this.st.utteranceId = m.utteranceId;
            break;
          }
          case 'chunk': {
            // stale in-flight chunk after barge: drop (plan §4.4 / §8.9)
            if (m.utteranceId !== this.st.utteranceId || !this.st.active) return;
            const dropped = playerWriteChunk(this.st, new Int16Array(m.i16));
            if (dropped > 0) {
              this.port.postMessage({ ev: 'overflow', dropped, utteranceId: this.st.utteranceId });
            }
            break;
          }
          case 'flush':
            this.st.flushing = true;
            break;
          case 'stop': {
            this.st.active = false;
            this.st.flushing = false;
            this.st.w = this.st.r = 0;
            this.st.srcPos = 0;
            this.port.postMessage({ ev: 'stopped', utteranceId: this.st.utteranceId });
            break;
          }
          case 'gain':
            this.gain = clamp01(m.value);
            this.st.gain = this.gain;
            break;
        }
      };
    }

    process(_inputs, outputs) {
      const out = outputs[0] && outputs[0][0];
      if (!out) return true;
      const f = playerRenderBlock(this.st, out);
      if (f.first) {
        this.port.postMessage({ ev: 'first-frame', utteranceId: this.st.utteranceId, at: currentTime });
      }
      if (f.drySpell) this.port.postMessage({ ev: 'underrun', utteranceId: this.st.utteranceId });
      if (f.drained) this.port.postMessage({ ev: 'drained', utteranceId: this.st.utteranceId });
      return true; // stay hot: zero warm-up latency on the play path (plan §4.5)
    }
  }

  class RecorderProcessor extends AudioWorkletProcessor {
    constructor() {
      super();
      this.st = createRecorderState(sampleRate);
      this.port.onmessage = (e) => {
        const m = e.data || {};
        if (m.cmd === 'start') this.st = createRecorderState(sampleRate);
      };
    }

    process(inputs, outputs) {
      const out = outputs[0] && outputs[0][0];
      if (out) out.fill(0); // sink only — muted downstream by a zero-gain node
      const in0 = inputs[0] && inputs[0][0];
      if (in0) {
        const r = recorderProcessBlock(this.st, in0);
        if (r.i16 && r.i16.length > 0) {
          this.port.postMessage({ ev: 'samples', i16: r.i16.buffer }, [r.i16.buffer]);
        }
        if (r.maxed) this.port.postMessage({ ev: 'max' });
      }
      return true;
    }
  }

  registerProcessor('bb-player', PlayerProcessor);
  registerProcessor('bb-recorder', RecorderProcessor);
}
