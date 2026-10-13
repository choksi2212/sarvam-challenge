// frontend/app.js — BhashaBuzzer A10: wires EVERYTHING per CONTRACT §1 + §6.
// Vanilla ES module. No framework, no build step. This file owns behavior; markup/styles
// (index.html, styles.css, ui/*) are A9's, the audio engine (audio/*) is A8's.
//
// SIBLING FILES ARE ABSENT in the A10 worktree: ui/screens.js, audio/host-player.js and
// audio/mic-recorder.js are imported DYNAMICALLY with a try/catch fallback to
// dev-tools/stubs.js — this keeps the worktree runnable AND zero-merge-conflict.
// ws-client.js + ui/game.js are A10's own (static imports).
//
// Dev:  npm i && node mock-server.js  (in dev-tools/), then open
//       http://localhost:8080/frontend/index.html?mock=1
//       (?mock=1 points GameWS at ws://localhost:8080/ws).

import { GameWS } from './ws-client.js';
import {
  sanitizeName, verdictStamp, formatScore, scoreDeltaText, rollScore,
  speedBonusText, latencyChip,
  recordDaily5, daily5Summary, loadCombos, addCombo, combosText, recordDayStreak,
  personaFields, categoryChip, phaseScreen, answerFrameFrom, recTimerText,
} from './ui/game.js';

/* ---------- tiny DOM helpers (every §6 id may be absent pre-merge — never crash) ---------- */

const $ = (id) => document.getElementById(id);
const setTxt = (el, s) => { if (el) el.textContent = String(s ?? ''); };
const setCls = (el, on, off) => { if (el) { if (on) el.classList.add(on); if (off) el.classList.remove(off); } };
const onEv = (el, ev, fn, opts) => { if (el) el.addEventListener(ev, fn, opts); };
const now = () => performance.now();

/* ---------- module loading with dev-tools/stubs.js fallback ---------- */

const stubsP = import('../dev-tools/stubs.js').catch(() => null);

async function withFallback(primary, pick, inlineFactory) {
  try {
    const mod = await import(primary);
    const picked = pick(mod);
    if (picked) return picked;
    throw new Error(`missing export in ${primary}`);
  } catch {
    const stubs = await stubsP;
    const fromStubs = stubs ? pick(stubs) : null;
    if (fromStubs) return fromStubs;
    console.warn(`[buzzer] ${primary} unavailable — using inline fallback`);
    return inlineFactory();
  }
}

const showScreenMod = withFallback('./ui/screens.js', (m) => (m?.showScreen ? m : null), () => ({
  showScreen: (id) => { document.body.dataset.screen = id; },
  setPhaseBadge: (p) => { document.body.dataset.phase = p ?? ''; },
}));

const HostPlayerCtor = withFallback('./audio/host-player.js', (m) => m?.HostPlayer ?? null, () => class {
  async init() {}
  startUtterance(_id, { onFirstSample } = {}) { if (onFirstSample) setTimeout(onFirstSample, 10); }
  feedChunk() {} endUtterance() {} stopNow() {}
  get volume() { return 1; } set volume(_v) {}
});

const MicRecorderCtor = withFallback('./audio/mic-recorder.js', (m) => m?.MicRecorder ?? null, () => class {
  async start() {}
  async stop() { return { wavB64: '', ms: 0 }; }
  cancel() {}
});

/* ---------- app state ---------- */

const params = new URLSearchParams(location.search);
const MOCK = params.get('mock') !== null && params.get('mock') !== '0';
const WS_URL = MOCK
  ? 'ws://localhost:8080/ws'
  : `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`;

const MAX_HOLD_MS = 8000;        // §2: 8 s hard cap on the answer window
const BUZZ_DEBOUNCE_MS = 800;    // §3: spam-proofing
const PING_MS = 25000;           // §1: app-level liveness

let screens = null;             // resolved { showScreen, setPhaseBadge }
let HostPlayer = null;
let MicRecorder = null;
let ws = null;                  // GameWS
let host = null;                // HostPlayer instance
let mic = null;                 // MicRecorder instance
let pingTimer = null;

let phase = 'splash';           // last server phase (§2)
let currentScreen = 'screen-splash';
let lastQuestion = null;        // last `question` frame
let lastStart = null;           // { player_name, language } for restarts
let lastPersona = null;         // persona_label seen (COMBOS update on change)
let lastSpeaker = null;
let currentUtterance = null;    // utterance_id being played (stale-chunk guard, D5/8.9)
const deadUtterances = new Set();
let hostSpeaking = false;
let micDenied = false;          // → typed-fallback mode (D12)
let recording = false;
let buzzDownAt = 0;             // client clock, for the accidental-tap guard
let lastBuzzAt = 0;              // debounce
let recTimerInterval = null;
let autoStopTimer = null;
let toastTimer = null;
let confettiTimer = null;
let hadConnectedOnce = false;

// latency timestamps — ALL client-side ones use performance.now() (D11: never mix clocks)
let tAudioStart = 0;            // audio_start receipt
let tAnswerSent = 0;            // answer release (buzz-release→first-sample numerator)
let awaitingReaction = false;   // next first-sample closes the YOU → HOST loop
let lastLoopMs = null;
let hostVoiceUtt = null;        // utterance_id whose first sample the client actually heard
let displayedScore = 0;         // for the rAF roll

/* ---------- screens wrapper ---------- */

function showScreen(id) {
  if (!id) return;
  currentScreen = id;
  screens?.showScreen(id);
}

function setPhaseBadge(p) { screens?.setPhaseBadge(p); }

/* ---------- toast (#toast) ---------- */

function toast(msg, ms = 3200) {
  const el = $('toast');
  if (!el) { console.log('[toast]', msg); return; }
  setTxt(el, msg);
  el.classList.add('show');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), ms);
}

/* ---------- latency chips (#lat-host · #lat-loop · #lat-server) — honest labels ---------- */

function setChip(kind, ms, extra = {}) {
  const chip = latencyChip(kind, ms, extra);
  const el = $(chip.el);
  if (!el) return;
  setTxt(el, chip.text);
  if (chip.title) el.title = chip.title;
  el.dataset.source = chip.source;
  setCls(el, 'pulse', null);
  requestAnimationFrame(() => setCls(el, null, 'pulse')); // one beat for A9's pulse anim
}

/* ---------- persona lower-third + COMBOS (#persona-* · #combo-count) ---------- */

function updatePersona(m) {
  const f = personaFields({ speaker: m?.speaker, personaLabel: m?.persona_label });
  setTxt($('persona-name'), f.name);
  setTxt($('persona-style'), f.style);
  setTxt($('persona-id'), f.id); // LITERAL Sarvam speaker id — judge candy (§7)
  if (m?.persona_label && m.persona_label !== lastPersona) {
    lastPersona = m.persona_label;
    const res = addCombo(localStorage, m.persona_label);
    setTxt($('combo-count'), combosText(res.count));
    if (res.isNew) setCls($('combo-chip'), 'pop');
  }
  if (m?.speaker && m.speaker !== lastSpeaker) {
    if (lastSpeaker !== null) { // "NEW HOST" flash on switch (A9 styles it)
      setCls(document.body, 'new-host');
      setTimeout(() => setCls(document.body, null, 'new-host'), 1200);
    }
    lastSpeaker = m.speaker;
  }
}

/* ---------- score roll (#score) ---------- */

function rollScoreTo(score) {
  const target = Math.max(0, Math.round(Number(score) || 0));
  const from = displayedScore;
  displayedScore = target;
  const el = $('score');
  if (!el) return;
  rollScore(from, target, 600, (txt) => setTxt(el, txt));
}

/* ---------- CC bar (#cc-bar) ---------- */

function setCaption(text) {
  const el = $('cc-bar');
  if (!el) return;
  setTxt(el, text ?? '');
  setCls(el, text ? 'cc-on' : null, text ? null : 'cc-on');
}

/* ---------- WebSocket plumbing ---------- */

function connectWS() {
  if (ws) return;
  ws = new GameWS(WS_URL, {
    onOpen: () => {
      document.body.dataset.ws = 'live';
      if (hadConnectedOnce) toast('वापस आ गए! Back in the game.');
      hadConnectedOnce = true;
      startPing();
    },
    onClose: () => {
      // GameWS auto-reconnects w/ backoff+jitter and re-sends the last start on reopen.
      document.body.dataset.ws = 'reconnecting';
      stopPing();
      if (recording) { // 8.4: drop mid-listening capture, never send a stale answer
        try { mic?.cancel(); } catch { /* noop */ }
        recording = false;
        clearRecTimers();
      }
      if (hostSpeaking) {
        hostSpeaking = false;
        try { host?.stopNow(); } catch { /* noop */ }
      }
      toast('कनेक्शन टूटा — फिर से जुड़ रहे हैं…');
    },
    onError: () => { /* close handler owns the UI state */ },
    onFrame: handleFrame,
  });
}

function startPing() {
  stopPing();
  pingTimer = setInterval(() => { if (ws?.state === 'open') ws.send({ type: 'ping' }); }, PING_MS);
}
function stopPing() { if (pingTimer) { clearInterval(pingTimer); pingTimer = null; } }

/* ---------- frame router (CONTRACT §1 S→C) ---------- */

function handleFrame(m) {
  if (!m || typeof m.type !== 'string') return;
  switch (m.type) {
    case 'state': onState(m); break;
    case 'question': onQuestion(m); break;
    case 'audio_start': onAudioStart(m); break;
    case 'audio_chunk': onAudioChunk(m); break;
    case 'audio_end': onAudioEnd(m); break;
    case 'verdict': onVerdict(m); break;
    case 'error': onErrorFrame(m); break;
    default: break; // unknown/forward-compat frames ignored
  }
}

function onState(m) {
  phase = String(m.phase ?? phase);
  setPhaseBadge(phase);
  const screen = phaseScreen(phase);
  if (screen) showScreen(screen);
  rollScoreTo(m.score);
  updatePersona(m);
  if (m.round != null && $('lobby-msg') && (phase === 'round_intro' || phase === 'round_switch')) {
    setTxt($('lobby-msg'), `ROUND ${m.round}`);
  }
  if (phase === 'question') armButtons();
  if (phase === 'final') {
    setTxt($('final-score'), formatScore(m.score));
    const d5 = daily5Summary(localStorage);
    setTxt($('final-daily5'), d5.marks.join(' '));
  }
}

function onQuestion(m) {
  lastQuestion = m;
  setTxt($('q-text'), m.text ?? '');
  const catEl = $('q-category');
  if (catEl) {
    setTxt(catEl, categoryChip(m.category));
    catEl.dataset.category = m.category ?? '';
  }
  const difEl = $('q-difficulty');
  if (difEl) {
    setTxt(difEl, String(m.difficulty ?? ''));
    difEl.dataset.difficulty = String(m.difficulty ?? '');
  }
  if (phaseScreen('question') && currentScreen !== 'screen-question') showScreen('screen-question');
  armButtons();
}

function onAudioStart(m) {
  currentUtterance = m.utterance_id ?? null;
  deadUtterances.delete(currentUtterance);
  hostSpeaking = true;
  tAudioStart = now();
  const id = currentUtterance;
  try {
    host?.startUtterance(id, {
      sampleRate: m.sample_rate ?? 24000,
      onFirstSample: () => {
        const t = now();
        if (awaitingReaction) { // YOU → HOST: answer release → first reaction sample (client clock)
          lastLoopMs = t - tAnswerSent;
          awaitingReaction = false;
          setChip('loop', lastLoopMs);
          const bonus = speedBonusText(lastLoopMs);
          if (bonus) toast(`तेज़! ${bonus}`);
        }
        setChip('host', t - tAudioStart, { source: 'client' }); // HOST VOICE (client-measured)
        hostVoiceUtt = id;
      },
    });
  } catch (err) { console.warn('[buzzer] startUtterance failed', err); }
  const caption = m.caption ?? (phase === 'question' ? lastQuestion?.text : '') ?? '';
  setCaption(caption);
}

function onAudioChunk(m) {
  const id = m.utterance_id;
  if (id !== currentUtterance || deadUtterances.has(id)) return; // 8.9 stale-chunk race
  try { host?.feedChunk(id, m.b64 ?? ''); } catch (err) { console.warn('[buzzer] feedChunk failed', err); }
}

function onAudioEnd(m) {
  const id = m.utterance_id;
  if (id === currentUtterance && !deadUtterances.has(id)) {
    hostSpeaking = false;
    try { host?.endUtterance(id); } catch { /* noop */ }
  }
  // Latency chips: client loop from OUR timestamps; server ttfb_ms + e2e_ms labeled as server.
  setChip('server', Number.isFinite(Number(m.ttfb_ms)) ? m.ttfb_ms : null,
    { e2eMs: Number.isFinite(Number(m.e2e_ms)) ? m.e2e_ms : null });
  if (lastLoopMs != null) setChip('loop', lastLoopMs); // re-assert client-measured loop
  // HOST VOICE chip: keep the client-measured first-sample value. If this utterance's
  // first-sample callback never fired (worklet fallback etc.), fall back to the
  // SERVER's ttfb_ms — explicitly tagged 'server-ttfb' so the label stays honest.
  if (m.utterance_id !== hostVoiceUtt && Number.isFinite(Number(m.ttfb_ms))) {
    setChip('host', m.ttfb_ms, { source: 'server-ttfb' });
  }
}

function onVerdict(m) {
  const stamp = verdictStamp(m.correct);
  const el = $('verdict-stamp');
  if (el) {
    el.dataset.verdict = String(m.correct ?? 'unclear').toLowerCase();
    el.className = `stamp ${stamp.cls}`;
    setTxt(el, stamp.text);
    setCls(el, 'stamp-pop', null);
  }
  setTxt($('heard-text'), `YOU SAID: ${m.heard ?? '…'}`);
  setTxt($('host-line'), m.hype_line || m.fact_line || '');
  setCaption(m.hype_line || m.fact_line || m.host_line || '');

  // score roll + delta pop
  const chip = $('score-chip');
  if (chip && Number.isFinite(Number(m.score_delta))) {
    setTxt($('score-delta'), scoreDeltaText(m.score_delta));
    setCls(chip, 'pop');
    setTimeout(() => setCls(chip, null, 'pop'), 900);
  }
  rollScoreTo(m.score);

  if (m.correct === 'correct') {
    if (navigator.vibrate) navigator.vibrate(60); // §3 haptics
    setCls(document.body, 'confetti'); // A9 ships the canvas/CSS burst
    if (confettiTimer) clearTimeout(confettiTimer);
    confettiTimer = setTimeout(() => setCls(document.body, null, 'confetti'), 1700);
  }

  recordDaily5(localStorage, m.correct); // D14 persistence (today's ✓/✗)
  showScreen('screen-verdict');
  setPhaseBadge('verdict');
  phase = 'verdict';
}

function onErrorFrame(m) {
  const rec = m.recoverable !== false;
  toast(`अरे! ${m.message ?? 'कुछ गड़बड़ हुई'} — ${rec ? 'दोबारा कोशिश कर रहे हैं…' : 'कृपया reconnect करें'}`);
  if (!rec) document.body.dataset.ws = 'lost';
}

/* ---------- buzzer: press / release / typed fallback (D3, D4, D12) ---------- */

function clearRecTimers() {
  if (recTimerInterval) { clearInterval(recTimerInterval); recTimerInterval = null; }
  if (autoStopTimer) { clearTimeout(autoStopTimer); autoStopTimer = null; }
}

function armButtons() {
  for (const id of ['btn-repeat', 'btn-skip']) {
    const el = $(id);
    if (el) { el.disabled = false; el.removeAttribute('aria-disabled'); }
  }
}

function disableBtn(id, ms = 1200) {
  const el = $(id);
  if (!el) return;
  el.disabled = true;
  el.setAttribute('aria-disabled', 'true');
  setTimeout(() => { el.disabled = false; el.removeAttribute('aria-disabled'); }, ms);
}

async function buzzPress() {
  if (phase !== 'question' || recording) return;
  const t = now();
  if (t - lastBuzzAt < BUZZ_DEBOUNCE_MS) return; // 8.8 spam guard
  lastBuzzAt = t;

  // D4 barge: hard-stop local playback FIRST, then tell the server twice (buzz + barge).
  if (hostSpeaking) {
    hostSpeaking = false;
    try { host?.stopNow(); } catch { /* noop */ }
    if (currentUtterance) deadUtterances.add(currentUtterance);
    currentUtterance = null;
    ws?.send({ type: 'barge' });
  }
  ws?.send({ type: 'buzz' });
  setPhaseBadge('listening');
  phase = 'listening';
  showScreen('screen-buzz');

  buzzDownAt = t;
  clearRecTimers();
  const timerEl = $('rec-timer');
  if (timerEl) {
    setTxt(timerEl, recTimerText(0));
    recTimerInterval = setInterval(() => setTxt(timerEl, recTimerText(now() - buzzDownAt)), 250);
  }
  autoStopTimer = setTimeout(() => { if (recording) buzzRelease(); }, MAX_HOLD_MS); // 8 s hard cap

  if (micDenied) { // typed mode: skip the mic, focus the input
    revealTypeFallback();
    $('type-input')?.focus();
    return;
  }
  recording = true;
  try {
    await mic?.start({
      onWaveform: (levels) => renderWaveform($('waveform'), levels), // live gold bars
    });
  } catch (err) {
    recording = false;
    clearRecTimers();
    micDenied = true; // D12: mic denied → typed-answer mode, game stays playable
    revealTypeFallback();
    $('type-input')?.focus();
    toast('Mic नहीं मिला — no problem! Type करके जवाब दो।');
  }
}

async function buzzRelease() {
  if (!recording) return;
  recording = false;
  clearRecTimers();
  let res = null;
  try { res = await mic?.stop(); } catch (err) { console.warn('[buzzer] mic.stop failed', err); }
  if (!res?.wavB64) return;
  if (res.ms < 250) { // accidental tap (§5.2 guards): no answer sent, re-arm
    toast('Buzzer पकड़े रखो — hold and speak!');
    phase = 'question';
    showScreen('screen-question');
    return;
  }
  sendAnswer({ wavB64: res.wavB64 });
}

function sendAnswer({ wavB64, text } = {}) {
  const frame = answerFrameFrom({ wavB64, text });
  if (!frame) { toast('जवाब खाली है — कुछ बोलो या type करो!'); return; }
  tAnswerSent = now(); // client clock ONLY (D11)
  awaitingReaction = true;
  ws?.send(frame);
  phase = 'judging';
  setPhaseBadge('judging');
  showScreen('screen-buzz');
  $('type-fallback')?.classList.remove('show');
}

function revealTypeFallback() {
  const el = $('type-fallback');
  if (!el) return;
  el.classList.remove('hidden');
  el.classList.add('show');
  el.setAttribute('aria-hidden', 'false');
}

/** Live waveform → #waveform. A9 may ship a canvas or a bar-div strip; handle both. */
function renderWaveform(el, levels) {
  if (!el) return;
  const arr = levels instanceof Float32Array ? levels : Array.from(levels ?? []);
  if (!arr.length) return;
  if (el instanceof HTMLCanvasElement) {
    const ctx = el.getContext('2d');
    if (!ctx) return;
    const w = el.width || el.clientWidth || 300;
    const h = el.height || el.clientHeight || 120;
    ctx.clearRect(0, 0, w, h);
    const bw = w / arr.length;
    ctx.fillStyle = getComputedStyle(el).getPropertyValue('--gold').trim() || '#F4B41A';
    arr.forEach((v, i) => {
      const bh = Math.max(2, Math.min(1, Math.abs(v)) * h);
      ctx.fillRect(i * bw + bw * 0.15, (h - bh) / 2, Math.max(1, bw * 0.7), bh);
    });
    return;
  }
  const bars = el.querySelectorAll('.bar, .wbar');
  if (bars.length) {
    bars.forEach((bar, i) => {
      const v = arr[i % arr.length] ?? 0;
      bar.style.transform = `scaleY(${Math.max(0.08, Math.min(1, Math.abs(v) || 0.08))})`;
    });
  } else {
    el.style.setProperty('--wave-level', String(Math.abs(arr[Math.floor(arr.length / 2)] ?? 0)));
  }
}

/* ---------- setup: name + language (S2) ---------- */

const LANG_BY_TEXT = {
  hinglish: 'enhi', 'hinglish (हिं+en)': 'enhi',
  हिंदी: 'hi', hindi: 'hi',
  english: 'en',
  मराठी: 'mr', marathi: 'mr',
  தமிழ்: 'ta', tamil: 'ta',
};
const LANGS = new Set(['hi', 'enhi', 'en', 'mr', 'ta']);

function selectedLang() {
  const chips = $('lang-chips');
  const sel = chips?.querySelector('[aria-pressed="true"], .selected, .active, [data-selected="true"]');
  const val = sel?.dataset?.lang || LANG_BY_TEXT[String(sel?.textContent ?? '').trim().toLowerCase()];
  return LANGS.has(val) ? val : 'enhi';
}

function sendStart() {
  const res = sanitizeName($('input-name')?.value ?? '');
  if (!res.ok) {
    toast(res.reason === 'empty' ? 'नाम type करो!' : 'नाम थोड़ा बड़ा करो (2–20 chars)');
    return false;
  }
  const language = selectedLang();
  lastStart = { player_name: res.name, language };
  const ok = ws?.send({ type: 'start', ...lastStart }) ?? false;
  if (ok) { showScreen('screen-lobby'); recordDayStreak(localStorage); }
  else toast('Host से जुड़ रहे हैं… दोबारा START दबाओ');
  return ok;
}

/* ---------- boot + wiring ---------- */

function wireLangChips() {
  const chips = $('lang-chips');
  if (!chips) return;
  chips.addEventListener('click', (e) => {
    const chip = e.target.closest('[data-lang], [role="button"], button');
    if (!chip || !chips.contains(chip)) return;
    const all = chips.querySelectorAll('[data-lang], [role="button"], button');
    for (const c of all) c.setAttribute('aria-pressed', 'false');
    for (const c of chips.querySelectorAll('.selected, .active')) c.classList.remove('selected', 'active');
    const lang = chip.dataset?.lang || LANG_BY_TEXT[String(chip.textContent ?? '').trim().toLowerCase()];
    if (lang) chip.dataset.lang = lang;
    chip.setAttribute('aria-pressed', 'true');
    chip.classList.add('selected');
  });
}

async function enterStage() {
  if (ws) return; // already unlocked
  try { await host?.init(); } catch (err) { console.warn('[buzzer] audio unlock failed', err); } // D8 gesture unlock
  connectWS();
  showScreen('screen-setup');
  $('input-name')?.focus();
}

function wireInputs() {
  onEv($('btn-enter'), 'click', enterStage);

  wireLangChips();
  onEv($('btn-start'), 'click', sendStart);
  onEv($('input-name'), 'keydown', (e) => { if (e.key === 'Enter' && !e.repeat) { e.preventDefault(); sendStart(); } });

  // Push-to-talk: pointer events (unified touch+mouse), capture + preventDefault (§3).
  const buzzer = $('buzzer');
  if (buzzer) {
    buzzer.addEventListener('pointerdown', (e) => { e.preventDefault(); try { buzzer.setPointerCapture(e.pointerId); } catch { /* noop */ } buzzPress(); });
    buzzer.addEventListener('pointerup', (e) => { e.preventDefault(); buzzRelease(); });
    buzzer.addEventListener('pointercancel', () => buzzRelease());
    buzzer.addEventListener('lostpointercapture', () => buzzRelease());
  }

  // Space/Enter mirror press/release (zero-focus primary key; e.repeat kills auto-repeat).
  document.addEventListener('keydown', (e) => {
    if (e.repeat) return;
    const tag = (e.target?.tagName ?? '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || e.target?.isContentEditable) return;
    if (e.key !== ' ' && e.key !== 'Spacebar' && e.key !== 'Enter') return;
    if (currentScreen === 'screen-splash') { e.preventDefault(); enterStage(); return; }
    if (e.key === ' ' || e.key === 'Spacebar') { e.preventDefault(); buzzPress(); }
    else if (e.key === 'Enter' && phase === 'question') { e.preventDefault(); buzzPress(); }
  });
  document.addEventListener('keyup', (e) => {
    if (e.key === ' ' || e.key === 'Spacebar' || e.key === 'Enter') buzzRelease();
  });

  // Typed fallback (D12): visible on mic-deny; submit sends {type:'answer', text}.
  onEv($('type-submit'), 'click', () => sendAnswer({ text: $('type-input')?.value ?? '' }));
  onEv($('type-input'), 'keydown', (e) => {
    if (e.key === 'Enter' && !e.repeat) { e.preventDefault(); sendAnswer({ text: e.target.value ?? '' }); }
  });

  onEv($('btn-skip'), 'click', () => { ws?.send({ type: 'skip_question' }); disableBtn('btn-skip'); });
  onEv($('btn-repeat'), 'click', () => { ws?.send({ type: 'repeat_question' }); disableBtn('btn-repeat'); });
  onEv($('btn-again'), 'click', () => {
    if (!lastStart) { showScreen('screen-setup'); return; }
    ws?.send({ type: 'start', ...lastStart }); // GameWS re-sends it on reopen too
    phase = 'lobby';
    showScreen('screen-lobby');
  });

  // 8.7: tab hidden mid-recording → finalize immediately (half an answer beats a lost one)
  document.addEventListener('visibilitychange', () => {
    if (document.hidden && recording) buzzRelease();
  });
}

/* ---------- init ---------- */

async function init() {
  screens = await showScreenMod;
  HostPlayer = await HostPlayerCtor;
  MicRecorder = await MicRecorderCtor;
  host = new HostPlayer();
  mic = new MicRecorder();

  // Live COMBOS chip from day one (D14) — read-only load, never seeds a fake label.
  setTxt($('combo-count'), combosText(loadCombos(localStorage).size));
  setTxt($('lat-host'), 'HOST VOICE —');
  setTxt($('lat-loop'), 'YOU → HOST —');
  setTxt($('lat-server'), 'SERVER TTFB —');
  document.body.dataset.ws = 'off';

  wireInputs();
  showScreen('screen-splash');
  console.log(`[buzzer] A10 ready · ws=${WS_URL}${MOCK ? ' (mock)' : ''} · audio=${HostPlayer?.name || 'stub'}`);
}

init().catch((err) => console.error('[buzzer] boot failed', err));
