// BhashaBuzzer — audio/mic-recorder.js (A8)
// Push-to-talk capture → 16 kHz mono WAV base64 (contract §5; plan §5).
// Device-rate → 16 kHz decimation + 200 ms pre-roll discard + 8 s hard cap all
// happen in the 'bb-recorder' worklet (pcm-worklet.js, D6). The main thread
// collects Int16 batches, drives the waveform tap, and encodes via
// wav-encoder on stop. The mic stream is released on stop/cancel (permission
// stays granted — re-request on next buzz is instant).

import { encodeWavToB64 } from './wav-encoder.js';
import { RECORDER_TARGET_RATE } from './pcm-worklet.js';

export class MicRecorder {
  /**
   * @param {AudioContext|null} audioContext pass the app's shared context
   *   (plan D2: ONE AudioContext per page). If omitted, a dedicated one is
   *   created on first start().
   */
  constructor(audioContext = null) {
    this.ctx = audioContext || null;
    this._rec = null;
    this._lastResult = null;
  }

  get active() { return this._rec !== null; }

  /**
   * Start capture (contract §5).
   * @param {{onWaveform?: (Uint8Array) => void, onAutoStop?: ({wavB64: string, ms: number}) => void}} opts
   *   onWaveform — per-rAF Uint8Array time-domain view (analyser tap) while
   *                recording; null-safe to omit.
   *   onAutoStop — fires ONCE when the 8 s hard cap is reached (additive
   *                option). The result is also cached: a subsequent stop()
   *                returns the same {wavB64, ms}, so app.js sends exactly one
   *                answer whether the user released or the cap fired.
   * @throws if getUserMedia/AudioWorklet unavailable or permission denied.
   */
  async start({ onWaveform = null, onAutoStop = null } = {}) {
    if (this._rec) throw new Error('MicRecorder: already recording');
    if (typeof navigator === 'undefined' || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error('MicRecorder: getUserMedia unavailable (needs https/localhost + a mic)');
    }
    if (!this.ctx) {
      const AC = (typeof window !== 'undefined') && (window.AudioContext || window.webkitAudioContext);
      if (!AC) throw new Error('MicRecorder: WebAudio unavailable');
      this.ctx = new AC();
    }
    const ctx = this.ctx;
    if (ctx.state === 'suspended') { try { await ctx.resume(); } catch (_) { /* gesture pending */ } }
    if (!ctx.audioWorklet || typeof ctx.audioWorklet.addModule !== 'function') {
      throw new Error('MicRecorder: AudioWorklet unsupported');
    }
    await ctx.audioWorklet.addModule(new URL('./pcm-worklet.js', import.meta.url).href);

    // plan §5.1 constraints — AEC/NS/AGC are the final leak/quality belts (D4)
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: 1,
      },
      video: false,
    });

    const src = ctx.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(ctx, 'bb-recorder', {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      outputChannelCount: [1],
    });
    const sink = ctx.createGain();
    sink.gain.value = 0; // zero-gain pull sink: worklet stays hot, zero loopback
    src.connect(node);
    node.connect(sink);
    sink.connect(ctx.destination);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    src.connect(analyser); // parallel waveform tap (plan §4.1)

    const rec = {
      stream, src, node, sink, analyser,
      onWaveform, onAutoStop,
      chunks: [],        // Float32Array per worklet batch (16 kHz) — contract shape
      samples: 0,
      wave: onWaveform ? new Uint8Array(analyser.fftSize) : null,
      raf: 0,
    };
    this._rec = rec;
    this._lastResult = null;

    node.port.onmessage = (e) => {
      const m = e.data || {};
      if (m.ev === 'samples') {
        const i16 = new Int16Array(m.i16);        // transferred ArrayBuffer
        const f = new Float32Array(i16.length);  // encodeWavToB64(float32Chunks)
        for (let i = 0; i < i16.length; i++) f[i] = i16[i] / 32768;
        rec.chunks.push(f);
        rec.samples += f.length;
      } else if (m.ev === 'max') {
        this._finish(true);                       // 8 s hard auto-stop
      }
    };
    node.port.postMessage({ cmd: 'start' });

    if (onWaveform) {
      const tick = () => {
        if (this._rec !== rec) return;
        try { analyser.getByteTimeDomainData(rec.wave); onWaveform(rec.wave); } catch (_) { /* rAF race */ }
        rec.raf = requestAnimationFrame(tick);
      };
      rec.raf = requestAnimationFrame(tick);
    }
  }

  /**
   * Release push-to-talk → {wavB64, ms} (16 kHz mono WAV). After an auto-stop,
   * returns the cached result. Never started / cancelled → {wavB64: null, ms: 0}
   * (app.js re-arms on short audio per plan §5.2 guards).
   */
  async stop() {
    if (this._rec) return this._finish(false);
    return this._lastResult || { wavB64: null, ms: 0 };
  }

  /** Abandon capture (WS drop etc.): release mic, discard audio, no result. */
  cancel() {
    const rec = this._rec;
    if (!rec) return;
    this._rec = null;
    this._lastResult = null;
    this._teardown(rec);
  }

  _finish(auto) {
    const rec = this._rec;
    if (!rec) return this._lastResult || { wavB64: null, ms: 0 };
    this._rec = null;
    this._teardown(rec);
    const wavB64 = encodeWavToB64(rec.chunks, { sampleRate: RECORDER_TARGET_RATE, channels: 1 });
    const result = {
      wavB64,
      ms: Math.round(rec.samples / (RECORDER_TARGET_RATE / 1000)),
      auto, // additive: true when the 8 s cap fired, not user release
    };
    this._lastResult = result;
    if (auto && typeof rec.onAutoStop === 'function') {
      try { rec.onAutoStop(result); } catch (err) { console.warn('[MicRecorder] onAutoStop threw', err); }
    }
    return result;
  }

  _teardown(rec) {
    if (rec.raf) cancelAnimationFrame(rec.raf);
    rec.raf = 0;
    try { if (rec.node && rec.node.port) rec.node.port.onmessage = null; } catch (_) { /* node gone */ }
    try { rec.stream.getTracks().forEach((t) => t.stop()); } catch (_) { /* already stopped */ }
    for (const n of [rec.src, rec.node, rec.sink, rec.analyser]) {
      try { n && n.disconnect(); } catch (_) { /* already disconnected */ }
    }
  }
}
