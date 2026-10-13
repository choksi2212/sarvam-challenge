// frontend/ui/game.js — BhashaBuzzer A10
// PURE display / persistence helpers. NO DOM, NO fetch, NO WebSocket, NO timers
// created at import time. Everything here is importable under `node --test`.
// app.js (A10) wires these to the §6 DOM skeleton; ui/screens.js (A9) owns markup/styles.

/* ---------- name (setup screen) ---------- */

/** Sanitize a player name: strip control chars, collapse whitespace, clamp to 2–20 chars.
 *  Pure; returns {ok, name, reason}. reason: 'ok' | 'too-short' | 'empty'. */
export function sanitizeName(raw, { min = 2, max = 20 } = {}) {
  let s = String(raw ?? '');
  // strip C0/C1 control characters (keep nothing below 0x20; 0x7f too)
  s = s.replace(/[\u0000-\u001f\u007f-\u009f]/g, '');
  // collapse whitespace runs to single spaces (all Unicode ws)
  s = s.replace(/\s+/g, ' ').trim();
  // hard clamp to max (CONTRACT §1: player_name 2–20 chars)
  s = [...s].slice(0, max).join('');
  if (s.length === 0) return { ok: false, name: s, reason: 'empty' };
  if (s.length < min) return { ok: false, name: s, reason: 'too-short' };
  return { ok: true, name: s, reason: 'ok' };
}

/* ---------- verdict stamp map (CONTRACT §1 verdict.correct) ---------- */

/** Verdict → stamp. cls goes on #verdict-stamp (with `verdict-` prefix + data-verdict attr
 *  so A9 can style by [data-verdict]); text is the big stamp word; tone is the palette role. */
export const VERDICT_STAMPS = {
  correct: { cls: 'verdict-correct', text: 'सही!', word: 'CORRECT', tone: 'green' },
  wrong:   { cls: 'verdict-wrong',   text: 'गलत!', word: 'WRONG',   tone: 'red' },
  partial: { cls: 'verdict-partial', text: 'लगभग!', word: 'ALMOST', tone: 'amber' },
  unclear: { cls: 'verdict-unclear', text: '?',     word: 'UNCLEAR', tone: 'dim' },
};

/** Map a server verdict word to its stamp. Unknown/missing → unclear (never throws). */
export function verdictStamp(verdict) {
  const key = String(verdict ?? '').toLowerCase().trim();
  return VERDICT_STAMPS[key] ?? VERDICT_STAMPS.unclear;
}

/** The verdict word as the Daily-5 ✓/✗ mark. */
export function dailyMark(verdict) {
  switch (String(verdict ?? '').toLowerCase()) {
    case 'correct': return '✓';
    case 'partial': return '~';
    case 'unclear': return '?';
    default: return '✗';
  }
}

/* ---------- score roll + score formatting ---------- */

/** Score display text. Scores are never negative (CONTRACT §2). Numbers >4 digits get
 *  comma-grouped (Indian grouping — en-IN); ≤4 digits stay plain. */
export function formatScore(n) {
  const v = Math.max(0, Math.round(Number(n) || 0));
  if (v < 10000) return String(v);
  try {
    return v.toLocaleString('en-IN');
  } catch {
    // manual Indian grouping fallback: rightmost 3, then groups of 2
    const s = String(v);
    const last3 = s.slice(-3);
    let rest = s.slice(0, -3);
    const parts = [];
    while (rest.length > 2) { parts.unshift(rest.slice(-2)); rest = rest.slice(0, -2); }
    if (rest) parts.unshift(rest);
    return [...parts, last3].join(',');
  }
}

/** Score-delta pop text: "+150" / "-30" / "+0". */
export function scoreDeltaText(delta) {
  const d = Math.round(Number(delta) || 0);
  if (d < 0) return `-${formatScore(-d)}`;
  return `+${formatScore(d)}`;
}

/** rAF count-up from `from` to `to` over `durationMs`, easing cubic-out, calling
 *  onTick(formattedText) each frame and exactly once more with the final value.
 *  `raf` is injectable for tests (defaults to requestAnimationFrame when present).
 *  Returns stop(). Safe when from===to (single immediate tick). */
export function rollScore(from, to, durationMs, onTick, raf) {
  const scheduler = raf ?? (typeof requestAnimationFrame === 'function' ? requestAnimationFrame : (cb) => setTimeout(() => cb(Date.now()), 16));
  const a = Math.max(0, Math.round(Number(from) || 0));
  const b = Math.max(0, Math.round(Number(to) || 0));
  const dur = Math.max(0, Number(durationMs) || 0);
  let start = null, stopped = false;
  const ease = (p) => 1 - Math.pow(1 - p, 3);
  if (typeof onTick !== 'function') return () => { stopped = true; };
  if (a === b || dur === 0) { onTick(formatScore(b)); return () => { stopped = true; }; }
  const frame = (ts) => {
    if (stopped) return;
    if (start === null) start = ts;
    const p = Math.min(1, (ts - start) / dur);
    const val = a + (b - a) * ease(p);
    onTick(formatScore(Math.round(val)));
    if (p < 1) scheduler(frame);
    else onTick(formatScore(b));
  };
  scheduler(frame);
  return () => { stopped = true; };
}

/* ---------- speed bonus (CONTRACT §2: +50/+30/+15 at ≤1/2/3 s) ---------- */

/** Client-side display hint of the speed bonus for a reaction time (ms).
 *  The SERVER score is authoritative; this only powers the "+50 ⚡" pop. */
export function speedBonusFor(reactionMs) {
  const ms = Number(reactionMs);
  if (!Number.isFinite(ms) || ms < 0) return 0;
  if (ms <= 1000) return 50;
  if (ms <= 2000) return 30;
  if (ms <= 3000) return 15;
  return 0;
}

export function speedBonusText(reactionMs) {
  const b = speedBonusFor(reactionMs);
  return b ? `⚡ +${b}` : '';
}

/* ---------- latency chips — the honest ticker (D11, never mix clocks) ----------
 * Three chips (§6: #lat-host, #lat-loop, #lat-server):
 *   HOST VOICE   = audio_start receipt → first sample played — CLIENT clocks only.
 *                  If the client missed the first-sample event, we fall back to the
 *                  server's ttfb_ms and TAG the chip source as 'server-ttfb' so the
 *                  label stays honest.
 *   YOU → HOST   = answer release → first sample of the host reaction — CLIENT clocks only.
 *   SERVER TTFB  = audio_end.ttfb_ms verbatim — SERVER clock, labeled as such.
 * e2e_ms rides along as a secondary label (server clock, never subtracted client-side). */

export const LATENCY_LABELS = { host: 'HOST VOICE', loop: 'YOU → HOST', server: 'SERVER TTFB' };
export const LATENCY_EL_IDS = { host: 'lat-host', loop: 'lat-loop', server: 'lat-server' };

/** 212 → "212 ms", 3240 → "3.2 s", missing → "—". */
export function formatMs(ms, { dash = '—' } = {}) {
  const v = Number(ms);
  if (!Number.isFinite(v) || v < 0) return dash;
  if (v < 1000) return `${Math.round(v)} ms`;
  return `${(v / 1000).toFixed(1)} s`;
}

/** One latency chip value object. kind: 'host'|'loop'|'server'. */
export function latencyChip(kind, valueMs, { source, e2eMs } = {}) {
  const label = LATENCY_LABELS[kind] ?? String(kind).toUpperCase();
  const src =
    source ??
    (kind === 'server' || kind === 'host' ? 'server-ttfb' : 'client');
  const text = `${label} ${formatMs(valueMs)}`;
  let title = '';
  if (kind === 'server') {
    title = `server-measured ttfb ${Math.round(Number(valueMs) || 0)} ms`;
    if (Number.isFinite(Number(e2eMs))) title += ` · server e2e ${Math.round(e2eMs)} ms`;
  } else if (src === 'server-ttfb') {
    title = 'server-measured ttfb (client first-sample missed)';
  } else {
    title = 'client-measured (performance.now)';
  }
  return { el: LATENCY_EL_IDS[kind] ?? `lat-${kind}`, kind, label, text, source: src, title };
}

/** All three chips at once (audio_end moment). Values may be null → dash. */
export function latencyTicker({ hostMs, hostSource = 'client', loopMs, serverTtfbMs, serverE2eMs } = {}) {
  return {
    host: latencyChip('host', hostMs, { source: hostSource }),
    loop: latencyChip('loop', loopMs, { source: 'client' }),
    server: latencyChip('server', serverTtfbMs, { e2eMs: serverE2eMs }),
  };
}

/* ---------- localStorage: Daily-5 + host COMBOS + day streak (D14) ---------- */

export const LS_KEYS = {
  daily5Prefix: 'bb.daily5.',   // bb.daily5.<YYYY-MM-DD> = JSON array of verdict words
  combos: 'bb.combos',          // JSON array of unique persona_labels seen
  dayStreak: 'bb.day-streak',   // {last:'YYYY-MM-DD', count:n}
};
export const TOTAL_COMBOS = 224;

function storage(store) {
  return store ?? (typeof localStorage !== 'undefined' ? localStorage : null);
}

function readJSON(store, key, fallback) {
  const s = storage(store);
  if (!s) return fallback;
  try {
    const raw = s.getItem(key);
    if (raw == null) return fallback;
    const v = JSON.parse(raw);
    return v ?? fallback;
  } catch {
    return fallback;
  }
}

function writeJSON(store, key, value) {
  const s = storage(store);
  if (!s) return false;
  try { s.setItem(key, JSON.stringify(value)); return true; } catch { return false; }
}

/** IST date key YYYY-MM-DD (the Daily-5 seed is IST-dated per game design). */
export function istDateKey(now = new Date()) {
  const d = now instanceof Date ? now : new Date(now);
  const ist = new Date(d.getTime() + 5.5 * 60 * 60 * 1000);
  const y = ist.getUTCFullYear();
  const m = String(ist.getUTCMonth() + 1).padStart(2, '0');
  const day = String(ist.getUTCDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

export function daily5Key(now = new Date()) {
  return `${LS_KEYS.daily5Prefix}${istDateKey(now)}`;
}

/** Today's verdict list (strings). Corrupt data → []. */
export function loadDaily5(store, now = new Date()) {
  const v = readJSON(storage(store), daily5Key(now), []);
  return Array.isArray(v) ? v.filter((x) => typeof x === 'string') : [];
}

/** Append a verdict to today's Daily-5 list. Returns the updated summary. */
export function recordDaily5(store, verdict, now = new Date()) {
  const key = daily5Key(now);
  const list = readJSON(storage(store), key, []);
  const safe = Array.isArray(list) ? list.filter((x) => typeof x === 'string') : [];
  safe.push(String(verdict ?? 'unclear').toLowerCase());
  writeJSON(storage(store), key, safe);
  return daily5Summary(store, now);
}

/** { list, marks, played, correct } for the final screen's Daily-5 row. */
export function daily5Summary(store, now = new Date()) {
  const list = loadDaily5(store, now);
  return {
    list,
    marks: list.map(dailyMark),
    played: list.length,
    correct: list.filter((v) => v === 'correct').length,
  };
}

/** Unique persona_labels seen → the "COMBOS n/224" meta-game. Set round-tripped
 *  through localStorage as a JSON array. */
export function loadCombos(store) {
  const v = readJSON(storage(store), LS_KEYS.combos, []);
  const arr = Array.isArray(v) ? v : [];
  return new Set(arr.filter((x) => typeof x === 'string' && x.length > 0));
}

export function addCombo(store, personaLabel) {
  const set = loadCombos(store);
  let isNew = false;
  const label = String(personaLabel ?? '').trim();
  if (label && !set.has(label)) {
    set.add(label);
    isNew = true;
    writeJSON(storage(store), LS_KEYS.combos, [...set].sort());
  }
  return { set, count: set.size, isNew };
}

/** "3/224" */
export function combosText(count) {
  const n = Math.max(0, Math.round(Number(count) || 0));
  return `${n}/${TOTAL_COMBOS}`;
}

/** Consecutive-IST-day play streak. Call once per game start. */
export function recordDayStreak(store, now = new Date()) {
  const prev = readJSON(storage(store), LS_KEYS.dayStreak, null) || {};
  const today = istDateKey(now);
  const yesterday = istDateKey(new Date((now instanceof Date ? now : new Date(now)).getTime() - 24 * 60 * 60 * 1000));
  let count = 1;
  if (prev && prev.last === today) {
    count = Math.max(1, Number(prev.count) || 1);
  } else if (prev && prev.last === yesterday) {
    count = (Math.max(0, Number(prev.count) || 0)) + 1;
  }
  const rec = { last: today, count };
  writeJSON(storage(store), LS_KEYS.dayStreak, rec);
  return rec;
}

/* ---------- persona lower-third (§7) ---------- */

/** 'ratan_hi_customer_expressive' → 'RATAN' (voice segment, uppercased). */
export function personaNameFromSpeaker(speaker) {
  const s = String(speaker ?? '').trim();
  if (!s) return 'HOST';
  return s.split('_')[0].toUpperCase();
}

/** 'ratan_hi_customer_expressive' → 'CUSTOMER EXPRESSIVE' (style segments after voice+lang). */
export function personaStyleFromSpeaker(speaker) {
  const s = String(speaker ?? '').trim();
  const parts = s.split('_').filter(Boolean);
  if (parts.length <= 2) return 'HOST';
  return parts.slice(2).join(' ').toUpperCase();
}

/** Render targets: #persona-name ← derived name; #persona-style ← server persona_label
 *  (authoritative) else derived style; #persona-id ← the LITERAL speaker id (judge candy). */
export function personaFields({ speaker, personaLabel } = {}) {
  return {
    name: personaNameFromSpeaker(speaker),
    style: String(personaLabel ?? '').trim() || personaStyleFromSpeaker(speaker),
    id: String(speaker ?? ''),
  };
}

/* ---------- category chip (question card, S5) ---------- */

export const CATEGORY_CHIPS = {
  cricket: 'क्रिकेट',
  'gk/bharat': 'GK · भारत',
  gk: 'GK',
  bharat: 'भारत',
  india: 'भारत',
  ganit: 'गणित',
  math: 'गणित',
  bollywood: 'Bollywood',
  vigyan: 'विज्ञान',
  science: 'विज्ञान',
  'pop-culture/tech': 'पॉप-कल्चर',
  'pop-culture': 'पॉप-कल्चर',
  pop: 'पॉप-कल्चर',
  tech: 'टेक',
};

/** Chip text for a category. Known categories get Devanagari/label text; unknown pass through. */
export function categoryChip(category) {
  const raw = String(category ?? '').trim();
  if (!raw) return '';
  const key = raw.toLowerCase();
  return CATEGORY_CHIPS[key] ?? raw;
}

/* ---------- phase → §6 screen map ---------- */

const PHASE_SCREENS = {
  lobby: 'screen-lobby',
  round_intro: 'screen-lobby',
  question: 'screen-question',
  listening: 'screen-buzz',
  judging: 'screen-buzz',
  verdict: 'screen-verdict',
  next: 'screen-verdict',
  round_switch: 'screen-lobby',
  final: 'screen-final',
};

/** Contract §2 phase → §6 section id (null if unknown). */
export function phaseScreen(phase) {
  return PHASE_SCREENS[String(phase ?? '').toLowerCase()] ?? null;
}

/* ---------- answer frame builder (CONTRACT §1: audio_b64 XOR text — never both) ---------- */

export function answerFrameFrom({ wavB64, text } = {}) {
  const wav = String(wavB64 ?? '').trim();
  const txt = String(text ?? '').trim();
  if (wav) return { type: 'answer', audio_b64: wav };
  if (txt) return { type: 'answer', text: txt };
  return null;
}

/* ---------- rec timer (#rec-timer, 8 s hard cap) ---------- */

/** Remaining-time text for the recording screen: "8s" … "0s". */
export function recTimerText(elapsedMs, capMs = 8000) {
  const cap = Math.max(0, Number(capMs) || 8000);
  const el = Math.max(0, Number(elapsedMs) || 0);
  return `${Math.max(0, Math.ceil((cap - el) / 1000))}s`;
}
