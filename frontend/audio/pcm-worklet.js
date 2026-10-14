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
// FIX (feat/fix-audio — live bug: host voice "too fast, words inaudible/garbled"):
//   F1 'chunk' frames go to an internal QUEUE; every render quantum pumps queue→ring
//      as space frees. Audio is NEVER dropped on burst arrival. (The old drop-oldest
//      overflow teleported the read cursor: a cached utterance dumps the whole blob
//      in ~ms, so every chunk past the 3rd discarded unplayed audio — skipped words,
//      torn-buffer garbage, utterance finished early = "too fast".)
//   F2 the render loop FREEZES the play cursor (r+srcPos) when fewer than 2 source
//      frames remain — it used to advance srcPos/r past w, permanently fast-forwarding
//      past audio that arrived late. Starve→resume is now sample-exact and gapless.
//   F3 150 ms start prime: playback goes audible only once ≥150 ms of source is
//      buffered (or as soon as flush arrives) — absorbs WS/synthesis jitter with no
//      mid-word gaps, fires no false underrun while priming, and reports first-frame
//      at the first AUDIBLE sample. TTFB = first chunk (~25–100 ms) + ≤150 ms.
//   F4 'stop'/'begin' leave zero dirty state between utterances (queue, ring, cursor
//      and flags all reset); drained fires only when flush AND queue AND ring are
//      fully exhausted.
//
// All PCM logic is exported as pure functions (no worklet globals needed) so
// `node --test` exercises it headlessly; processor registration is guarded so
// this file also evaluates outside an AudioWorkletGlobalScope.

export const PLAYER_RING_FRAMES = 32768;       // src frames: 1.365 s @ 24 kHz, 64 KB (D7)
export const PLAYER_PRIME_MS = 150;             // start prefetch before going audible (F3)
export const PLAYER_MAX_QUEUE_FRAMES = 720000;  // 30 s @ 24 kHz — runaway-server cap (F1)
export const RECORDER_TARGET_RATE = 16000;  // answer WAV rate (contract §1)
export const RECORDER_PREROLL_MS = 200;    // mic pre-roll discard (D4)
export const RECORDER_MAX_MS = 8000;       // contract §2: 8 s hard auto-stop

export function clamp01(v) { return v < 0 ? 0 : v > 1 ? 1 : v; }
export function clampInt16(v) { return v < -32768 ? -32768 : v > 32767 ? 32767 : v; }

// ------------------------------------------------------------------ bb-player

/**
 * Fresh player state. ctxRate = AudioContext rate, srcRate = PCM rate (24000).
 * primeMs = start prefetch (F3): stay silent until this much source time is
 * buffered (or flush arrives). Pass 0 to disable priming (headless tests).
 */
export function createPlayerState(ctxRate, srcRate = 24000, ringFrames = PLAYER_RING_FRAMES, primeMs = PLAYER_PRIME_MS) {
  const safeCtx = ctxRate > 0 ? ctxRate : 48000;
  const safeSrc = srcRate > 0 ? srcRate : 24000;
  const safeRing = ringFrames > 0 ? ringFrames : PLAYER_RING_FRAMES;
  const prime = Math.min(Math.max(0, Math.round((primeMs / 1000) * safeSrc)), safeRing >> 1);
  return {
    ring: safeRing,
    buf: new Int16Array(safeRing),
    w: 0, r: 0,        // absolute write/read counters; ring index = counter % ring
    srcPos: 0,          // fractional source-position accumulator (carry across blocks)
    ratio: safeCtx / safeSrc,            // ctxRate/srcRate (e.g. ×2 @ 48 kHz) — informational
    step: safeSrc / safeCtx,            // src frames advanced PER OUTPUT frame (resample step)
    q: [],             // pending source chunks (F1) — burst absorption, never dropped
    qn: 0,             // frames currently queued (not yet in the ring)
    prime,             // src frames to buffer before going audible (F3)
    primed: false,      // this utterance has gone audible (F3)
    active: false,
    flushing: false,
    firstFrame: false,
    dry: false,
    utteranceId: null,
    gain: 1,
  };
}

/**
 * Move queued frames into the ring as space frees (F1). Handles ring-index
 * wraparound and partial chunk writes — the remainder stays queued.
 * Returns the number of frames moved into the ring.
 */
export function playerPump(st) {
  let moved = 0;
  while (st.qn > 0) {
    const free = st.ring - (st.w - st.r);
    if (free <= 0) break;
    const c = st.q[0];
    const n = Math.min(c.length, free);
    const start = st.w % st.ring;
    if (start + n <= st.ring) {
      st.buf.set(c.subarray(0, n), start);
    } else {
      const first = st.ring - start;
      st.buf.set(c.subarray(0, first), start);
      st.buf.set(c.subarray(first, n), 0);
    }
    st.w += n; moved += n; st.qn -= n;
    if (n >= c.length) st.q.shift(); else st.q[0] = c.subarray(n);
  }
  return moved;
}

/**
 * Append Int16 source frames (F1): enqueue, then pump whatever fits into the
 * ring. Never drops frames unless the pending queue exceeds the runaway cap
 * (30 s @ 24 kHz) — in that pathological case the NEWEST whole chunks are
 * dropped and the dropped frame count is returned (→ 'overflow' event).
 */
export function playerWriteChunk(st, i16) {
  if (!i16 || i16.length === 0) return 0;
  st.q.push(i16);
  st.qn += i16.length;
  let dropped = 0;
  if (st.qn > PLAYER_MAX_QUEUE_FRAMES) {
    while (st.qn > PLAYER_MAX_QUEUE_FRAMES && st.q.length > 1) {
      const c = st.q.pop();
      st.qn -= c.length;
      dropped += c.length;
    }
  }
  playerPump(st);
  return dropped;
}

/** Instant silence + zero dirty state between utterances (F4 — barge path). */
export function playerReset(st) {
  st.w = 0; st.r = 0; st.srcPos = 0;
  st.q.length = 0; st.qn = 0;
  st.active = false; st.flushing = false;
  st.primed = false; st.dry = false; st.firstFrame = false;
}

/**
 * Render one block (out.length frames) from the ring, linear-interp resampling
 * srcRate→ctxRate. Underruns fill silence and keep timing — the play cursor
 * FREEZES (F2) so late audio resumes sample-exactly; playback is never
 * restarted and no source frame is ever skipped.
 * Returns { first, drySpell, drained, rendered }:
 *   first    — first rendered frame of this utterance (→ post first-frame)
 *   drySpell — wet→dry transition while active & not flushing (→ post underrun,
 *              once per dry spell; never fires while priming, F3)
 *   drained  — flushing and queue+ring exhausted (→ post drained; complete)
 *   rendered — output frames actually sourced from PCM this block (the rest are
 *              gap silence) — lets tests stitch the gapless stream back together
 */
export function playerRenderBlock(st, out) {
  playerPump(st);
  const avail = st.w - st.r;          // whole src frames in the ring
  const pending = avail - st.srcPos;  // float src frames not yet rendered
  // F3: stay silent (no underrun noise, no first-frame) until primed or flushed
  if (st.active && !st.flushing && !st.primed && avail < st.prime) {
    out.fill(0);
    return { first: false, drySpell: false, drained: false, rendered: 0 };
  }
  st.primed = true;
  // Need both interpolation taps (floor(p), floor(p)+1) inside written data:
  // p + 1 < w means pending > 1. Below that: gap silence (F2 freeze) or drained.
  if (pending <= 1) {
    out.fill(0);
    let drySpell = false;
    if (st.active && !st.flushing && !st.dry) { st.dry = true; drySpell = true; }
    const drained = st.active && st.flushing && st.qn === 0;
    if (drained) st.active = false;
    return { first: false, drySpell, drained, rendered: 0 };
  }
  st.dry = false;
  const ring = st.ring, buf = st.buf, step = st.step, g = st.gain;
  let i = 0;
  for (; i < out.length; i++) {
    const p = st.r + st.srcPos;
    if (p + 1 >= st.w) break;              // F2: freeze cursor — resume sample-exact
    const i0 = Math.floor(p);
    const frac = p - i0;
    const s = buf[i0 % ring] * (1 - frac) + buf[(i0 + 1) % ring] * frac;
    const v = (s / 32768) * g;
    out[i] = v < -1 ? -1 : v > 1 ? 1 : v;
    st.srcPos += step;   // srcRate/ctxRate src frames per output frame (0.5 @ 48k←24k)
  }
  const consumed = Math.floor(st.srcPos);
  st.r += consumed;
  st.srcPos -= consumed;
  if (i < out.length) out.fill(0, i);      // starved tail of the block: gap silence
  const first = !st.firstFrame;
  st.firstFrame = true;
  let drySpell = false;
  if (i < out.length && st.active && !st.flushing && !st.dry) { st.dry = true; drySpell = true; }
  return { first, drySpell, drained: false, rendered: i };
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
            const st = this.st;
            playerReset(st);   // F4: instant silence, zero dirty state
            this.port.postMessage({ ev: 'stopped', utteranceId: st.utteranceId });
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
