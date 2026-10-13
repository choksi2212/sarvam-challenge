/* ui/screens.js — BhashaBuzzer stage router (A9: stage markup)
 *
 * CONTRACT §5 (binding):
 *   export function showScreen(id)        // one of §6 section ids; handles transitions
 *   export function setPhaseBadge(phase)  // small phase indicator
 *
 * Owns ONLY section visibility (fade/slide) + the phase badge. No game logic —
 * app.js (A10) maps server `state.phase` → screen + badge.
 *
 * Section ids (§6): screen-splash · screen-setup · screen-lobby · screen-question
 *                   screen-buzz · screen-verdict · screen-final
 * Accepts either the full id ("screen-question") or the bare screen name
 * ("question") — both resolve to the same section.
 *
 * Mechanics:
 *   - shown section gets .is-active  (display:flex + screen-in animation: fade + slide)
 *   - hidden section gets .is-exiting (screen-out animation) and the [hidden]
 *     attribute once the 220 ms exit animation is done
 *   - initial screen = splash (auto-run at module load)
 */

const EXIT_MS = 220;

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
const exitTimers = new WeakMap();

/** Resolve "question" | "screen-question" → the <section data-screen> element. */
function resolveScreen(id) {
  if (typeof id !== 'string' || !id) return null;
  const candidates = id.startsWith('screen-') ? [id] : [id, 'screen-' + id];
  for (const candidate of candidates) {
    const el = document.getElementById(candidate);
    if (el && el.tagName === 'SECTION' && el.dataset && el.dataset.screen) return el;
  }
  return null;
}

/** Animate a section out, then apply [hidden] when the animation is done. */
function beginExit(section) {
  const pending = exitTimers.get(section);
  if (pending) clearTimeout(pending);
  section.classList.remove('is-active');
  section.classList.add('is-exiting');
  const timer = setTimeout(() => {
    section.classList.remove('is-exiting');
    section.hidden = true;
    exitTimers.delete(section);
  }, EXIT_MS);
  exitTimers.set(section, timer);
}

/**
 * Show one stage screen; all other screens are transitioned out.
 * @param {string} id section id ("screen-question") or bare name ("question")
 * @returns {string|null} id of the now-current screen (unchanged on bad input)
 */
export function showScreen(id) {
  const next = resolveScreen(id);
  if (!next) return currentId;

  if (currentId && currentId !== next.id) {
    const prev = document.getElementById(currentId);
    if (prev) beginExit(prev);
  }

  // If this section is mid-exit, cancel that so it can come back cleanly.
  const pending = exitTimers.get(next);
  if (pending) {
    clearTimeout(pending);
    exitTimers.delete(next);
  }

  next.classList.remove('is-exiting', 'is-active');
  next.hidden = false;
  void next.offsetWidth; // force reflow so the entry animation restarts
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
  const badge = document.getElementById('phase-badge');
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
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => showScreen('screen-splash'), { once: true });
  } else {
    showScreen('screen-splash');
  }
}
init();
