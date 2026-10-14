// BhashaBuzzer — audio/wav-encoder.js (A8)
// Pure, dependency-free WAV encode + base64 + RIFF helpers (contract §5; plan §5.3, D5).
//
//   encodeWavToB64(float32Chunks, {sampleRate = 16000, channels = 1}) -> string
//     float32Chunks: iterable of Float32Array (sample frames in [-1, 1]).
//     Int16Array chunks are also accepted and appended bit-exactly (defensive).
// Zero state; safe as a browser ES module and under `node --test`.

export const RIFF_HEADER_BYTES = 44;

/** Read up to `len` ASCII chars from bytes at offset (plan §10.1 peekStr). */
export function peekStr(bytes, offset = 0, len = 4) {
  let s = '';
  const end = Math.min(offset + len, bytes.length);
  for (let i = offset; i < end; i++) s += String.fromCharCode(bytes[i]);
  return s;
}

/** D5 magic validation: 'RIFF'@0 AND 'WAVE'@8 — never a blind 44-byte skip. */
export function isRiffWav(bytes) {
  return !!bytes && bytes.length >= 12 &&
    peekStr(bytes, 0, 4) === 'RIFF' && peekStr(bytes, 8, 4) === 'WAVE';
}

/** Chunked base64 (0x8000 bytes/step) — no call-stack overflow on 256 KB payloads. */
export function toBase64(u8) {
  let s = '';
  const step = 0x8000;
  for (let i = 0; i < u8.length; i += step) {
    s += String.fromCharCode.apply(null, u8.subarray(i, Math.min(i + step, u8.length)));
  }
  return btoa(s);
}

/** base64 → fresh, 2-byte-aligned Uint8Array. */
export function b64ToBytes(b64) {
  const s = atob(b64);
  const u8 = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) u8[i] = s.charCodeAt(i);
  return u8;
}

function writeAscii(dv, offset, str) {
  for (let i = 0; i < str.length; i++) dv.setUint8(offset + i, str.charCodeAt(i));
}

/**
 * Encode PCM chunks into a 44-byte-header 16-bit little-endian WAV (plan §5.3).
 * Float chunks: clamp [-1,1], scale by 32768 (round), clip +32768→32767 — this
 * makes the Int16→float→Int16 round trip bit-exact. Int16 chunks: appended raw.
 * Returns a fresh Uint8Array (RIFF + fmt + data). No platform dependencies.
 */
export function encodeWav(chunks, { sampleRate = 16000, channels = 1 } = {}) {
  const list = chunks == null ? [] : (Array.isArray(chunks) ? chunks : Array.from(chunks));
  let frames = 0;
  for (const c of list) frames += c.length;
  const dataLen = frames * 2;
  const u8 = new Uint8Array(RIFF_HEADER_BYTES + dataLen);
  const dv = new DataView(u8.buffer);
  writeAscii(dv, 0, 'RIFF');
  dv.setUint32(4, 36 + dataLen, true);            // RIFF chunk size
  writeAscii(dv, 8, 'WAVE');
  writeAscii(dv, 12, 'fmt ');
  dv.setUint32(16, 16, true);                     // fmt chunk size
  dv.setUint16(20, 1, true);                      // audio format: PCM
  dv.setUint16(22, channels, true);               // channels (1 = mono)
  dv.setUint32(24, sampleRate, true);             // sample rate
  dv.setUint32(28, sampleRate * channels * 2, true); // byte rate
  dv.setUint16(32, channels * 2, true);            // block align
  dv.setUint16(34, 16, true);                     // bits per sample
  writeAscii(dv, 36, 'data');
  dv.setUint32(40, dataLen, true);                // data chunk size
  let off = RIFF_HEADER_BYTES;
  for (const c of list) {
    if (c instanceof Int16Array) {
      for (let i = 0; i < c.length; i++) { dv.setInt16(off, c[i], true); off += 2; }
    } else {
      for (let i = 0; i < c.length; i++) {
        let v = +c[i];
        if (v < -1) v = -1; else if (v > 1) v = 1;
        let x = Math.round(v * 32768);
        if (x > 32767) x = 32767;                 // +1.0 clips
        dv.setInt16(off, x, true); off += 2;
      }
    }
  }
  return u8;
}

/** Contract §5: encodeWavToB64(float32Chunks, {sampleRate = 16000, channels = 1}) -> string */
export function encodeWavToB64(float32Chunks, opts) {
  return toBase64(encodeWav(float32Chunks, opts));
}
