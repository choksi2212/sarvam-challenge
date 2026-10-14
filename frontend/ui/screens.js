/* ui/screens.js — BhashaBuzzer stage router (A9 stage markup · fix-screens motion polish)
 *
 * CONTRACT §5 (binding):
 *   export function showScreen(id)        // one of §6 section ids; handles transitions
 *   export function setPhaseBadge(phase) // small phase indicator
 *
 * Owns ONLY section visibility (crossfade) + the phase badge. No game logic —
 * app.js (A10) maps server `state.phase` → screen + badge.
 *
 * Section ids (§6): screen-splash · screen-setup · screen-lobby · screen-question
 *                   screen-buzz · screen-verdict · screen-final
 * Accepts either the full id ("screen-question") or the bare screen name
 * ("question") — both resolve to the same section.
 *
 * Transition spec (design direction: minimal professional, 150-250ms micro-motion):
 *   1. CROSSFADE, NOT HAND-OFF — the incoming screen starts its fade+slide in
 *      immediately while the outgoing screen is still fading out. The outgoing
 *      section is pinned inline (position:absolute; inset:0; z-index:0) for the
 *      exit so both occupy the same stage box instead of stacking in flow:
 *      no layout jump, no blank gap, no flash of two screens.
 *   2. 160ms EXIT, CSS-SYNCED — the delay before [hidden] lands is read from the
 *      live computed animation-duration (fallback 160ms), so JS stays in
 *      lockstep with whatever screen-out duration styles.css ships.
 *   3. ZERO RE-SHOW FLASH — showScreen for the screen that is already current
 *      and fully active is a no-op; the entry animation never restarts (app.js
 *      re-shows screen-buzz for the listening AND judging phases).
 *   4. RACE-FREE EXITS — each pending exit carries a per-section token. Reviving
 *      a section cancels its timer and invalidates the token; a fired hide only
 *      lands if its token is still current, so mid-exit revival can never hide
 *      a screen that just came back.
 *   5. REDUCED MOTION — prefers-reduced-motion is consulted via matchMedia on
 *      every call: instant cut (previous hidden immediately, no slide/fade,
 *      inline animation:none), plus a media 'change' listener that re-normalises
 *      the current screen live when the OS preference flips.
 *
 * Mechanics (unchanged, CONTRACT §5/§6):
 *   - shown section gets .is-active   (display:flex + screen-in animation)
 *   - hidden section gets .is-exiting (screen-out animation), then [hidden]
 *   - initial screen = splash (auto-run at module load)
 */

const EXIT_FALLBACK_MS = 160; // subtle slide+fade; only used if CSS is unreadable

/** Canonical phase → badge label (CONTRACT §2 phases). */
const PHASE_LABELS = {
  lobby: 'LOBBY',
  round_intro: 'ROUND INTRO',
  question: 'QUESTION',
  listening: 'LISTENING',
  judging: 'JUDGING',
  verdict: 'VERDICT',
  next: 'NEXT',
  round_switch: 'ROUND SWITCH',
  final: 'FINAL'
};

let currentId = null;
const exits = new WeakMap(); // section → { timer, token } · one pending exit per section

/* ---------------- environment helpers (module stays importable headless) ----- */

/** True when the user asked the OS for reduced motion (checked per call). */
function reducedMotion() {
  try {
    return typeof window !== 'undefined'
      && typeof window.matchMedia === 'function'
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches === true;
  } catch {
    return false;
  }
}

/** Live computed screen-out duration in ms — keeps JS in sync with CSS. */
function exitDurationMs(section) {
  try {
    if (typeof window === 'undefined' || typeof window.getComputedStyle !== 'function') {
      return EXIT_FALLBACK_MS;
    }
    const seconds = parseFloat(window.getComputedStyle(section).animationDuration);
    return Number.isFinite(seconds) && seconds >= 0 ? Math.round(seconds * 1000) : EXIT_FALLBACK_MS;
  } catch {
    return EXIT_FALLBACK_MS;
  }
}

/** getElementById that tolerates a missing document (tests, early loads). */
function $id(id) {
  if (typeof document === 'undefined' || !document
      || typeof document.getElementById !== 'function') return null;
  return document.getElementById(id);
}

/** Resolve "question" | "screen-question" → the <section data-screen> element. */
function resolveScreen(id) {
  if (typeof id !== 'string' || !id) return null;
  const candidates = id.startsWith('screen-') ? [id] : [id, 'screen-' + id];
  for (const candidate of candidates) {
    const el = $id(candidate);
    if (el && el.tagName === 'SECTION' && el.dataset && el.dataset.screen) return el;
  }
  return null;
}

/* ----------------------- exit plumbing (race-free by token) ------------------ */

/** Drop the inline pin + animation override an exit (or reduced show) left. */
function clearInlineMotion(section) {
  section.style.position = '';
  section.style.inset = '';
  section.style.zIndex = '';
  section.style.animation = '';
}

/** Cancel any in-flight exit for `section` — timer, token and inline pin. */
function cancelExit(section) {
  const pending = exits.get(section);
  if (!pending) return;
  clearTimeout(pending.timer);
  exits.delete(section);
  clearInlineMotion(section);
}

/** Animate a section out (over the incoming screen), then apply [hidden]. */
function beginExit(section) {
  cancelExit(section); // defensive: never two exits for the same section
  section.classList.remove('is-active');
  clearInlineMotion(section); // a reduced-mode show may have set animation:none
  section.classList.add('is-exiting');
  // Pin over the stage so outgoing + incoming crossfade in place — in flow
  // layout the two sections would stack for the exit's duration.
  section.style.position = 'absolute';
  section.style.inset = '0';
  section.style.zIndex = '0';
  const token = {};
  const timer = setTimeout(() => {
    if (exits.get(section)?.token !== token) return; // revived mid-exit: skip
    exits.delete(section);
    clearInlineMotion(section);
    section.classList.remove('is-exiting');
    section.hidden = true;
  }, exitDurationMs(section));
  exits.set(section, { timer, token });
}

/* ------------------------------ CONTRACT §5 API ------------------------------ */

/**
 * Show one stage screen; all other screens are transitioned out.
 * @param {string} id section id ("screen-question") or bare name ("question")
 * @returns {string|null} id of the now-current screen (unchanged on bad input)
 */
export function showScreen(id) {
  const next = resolveScreen(id);
  if (!next) return currentId;

  const reviving = exits.has(next); // mid-exit come-back

  // Zero re-show flash: the current, fully-active screen is a no-op —
  // restarting the entry animation would flicker (listening → judging).
  if (currentId === next.id && !reviving) return currentId;

  const reduced = reducedMotion();

  // Hide the outgoing screen first, so the stage is never blank.
  if (currentId && currentId !== next.id) {
    const prev = $id(currentId);
    if (prev) {
      if (reduced) {
        cancelExit(prev); // drop any in-flight animated exit
        prev.classList.remove('is-active');
        prev.hidden = true; // instant cut — no fade, no timer
      } else {
        beginExit(prev); // crossfade out over the incoming screen
      }
    }
  }

  // Mid-exit revival: cancel that exit so this section can come back cleanly.
  if (reviving) cancelExit(next);

  next.classList.remove('is-exiting', 'is-active');
  next.hidden = false;
  if (reduced) {
    next.style.animation = 'none'; // instant cut — CSS entry animation stays off
  } else {
    next.style.animation = '';  // (re-)allow the CSS entry animation…
    void next.offsetWidth;      // …and force reflow so it restarts cleanly
  }
  next.classList.add('is-active');
  currentId = next.id;
  return currentId;
}

/**
 * Update the small top-bar phase indicator (#phase-badge).
 * @param {string} phase one of CONTRACT §2 phases (or any short string)
 * @returns {string} the label rendered
 */
export function setPhaseBadge(phase) {
  const key = String(phase == null ? '' : phase).trim();
  const badge = $id('phase-badge');
  if (!badge) return key;
  badge.dataset.phase = key || 'unknown';
  badge.textContent = PHASE_LABELS[key] || key.replaceAll('_', ' ').toUpperCase() || '—';
  return badge.textContent;
}

/** @returns {string|null} id of the currently shown screen. */
export function currentScreen() {
  return currentId;
}

/* Initial screen = splash (CONTRACT §5). Module scripts run after DOM parse,
 * but guard for early loads / non-DOM environments (tests) anyway. */
function init() {
  if (typeof document === 'undefined') return;
  // Keep the current screen's motion mode live when the OS preference flips.
  if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
    try {
      const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
      const sync = () => {
        const el = currentId ? $id(currentId) : null;
        if (el) el.style.animation = reducedMotion() ? 'none' : '';
      };
      if (typeof mq.addEventListener === 'function') mq.addEventListener('change', sync);
      else if (typeof mq.addListener === 'function') mq.addListener(sync); // old Safari
    } catch {
      /* matchMedia unavailable — preference is simply re-checked per call */
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => showScreen('screen-splash'), { once: true });
  } else {
    showScreen('screen-splash');
  }
}
init();
