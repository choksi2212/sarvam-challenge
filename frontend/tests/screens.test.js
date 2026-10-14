// frontend/tests/screens.test.js — BhashaBuzzer fix-screens (CONTRACT §8: node --test from repo root)
// Covers ui/screens.js pure logic against a tiny DOM shim: bare-name id resolution,
// initial splash (both readyState paths), crossfade exit mechanics (is-exiting →
// [hidden] after the CSS-synced duration, with the 160ms fallback), the zero
// re-show-flash no-op (listening → judging both map to screen-buzz), race-free
// mid-exit revival (cancels the pending hide), prefers-reduced-motion instant
// cut + live preference flips, and setPhaseBadge label mapping.

import { test } from 'node:test';
import assert from 'node:assert/strict';

/* ----------------------------- tiny DOM shim -------------------------------- */

let loadCounter = 0; // each loadScreens() gets a fresh module instance (query cache-bust)

/** Real CSSStyleDeclaration returns '' for unset properties — mirror that. */
function makeStyle() {
  return new Proxy({}, {
    get: (t, k) => (typeof k === 'string' ? (k in t ? t[k] : '') : undefined),
    set: (t, k, v) => { t[k] = v; return true; }
  });
}

function makeClassList() {
  const set = new Set();
  return {
    add: (...cs) => cs.forEach((c) => set.add(c)),
    remove: (...cs) => cs.forEach((c) => set.delete(c)),
    contains: (c) => set.has(c)
  };
}

function makeSection(name) {
  const id = name.startsWith('screen-') ? name : 'screen-' + name;
  return {
    tagName: 'SECTION',
    id,
    hidden: false,
    dataset: { screen: name.replace(/^screen-/, '') },
    style: makeStyle(),
    classList: makeClassList(),
    reflows: 0,
    get offsetWidth() { this.reflows += 1; return 0; }
  };
}

/**
 * Install global document/window shims, then import a FRESH instance of
 * ui/screens.js (query string busts the ESM cache so currentId starts clean).
 */
async function loadScreens(env = {}) {
  const {
    screens = ['splash', 'setup', 'lobby', 'question', 'buzz', 'verdict', 'final'],
    readyState = 'complete',
    reduced = false,
    animOut = '0.16s',
    withBadge = true,
    withDocument = true,
    withWindow = true,
    withComputedStyle = true,
    extraElements = []
  } = env;

  const domListeners = [];
  const mqListeners = [];
  const byId = new Map();
  for (const name of screens) {
    const s = makeSection(name);
    s.hidden = s.id !== 'screen-splash'; // mirrors index.html markup: only splash starts visible
    byId.set(s.id, s);
  }
  if (withBadge) byId.set('phase-badge', { tagName: 'DIV', id: 'phase-badge', dataset: {}, textContent: '', style: {} });
  for (const el of extraElements) byId.set(el.id, el);

  const state = { reduced };

  globalThis.document = withDocument ? {
    readyState,
    getElementById: (id) => byId.get(id) ?? null,
    addEventListener: (type, cb) => domListeners.push({ type, cb })
  } : undefined;

  globalThis.window = withWindow ? {
    matchMedia: () => ({
      get matches() { return state.reduced; },
      addEventListener: (_t, cb) => mqListeners.push(cb),
      addListener: (cb) => mqListeners.push(cb)
    }),
    ...(withComputedStyle
      ? { getComputedStyle: () => ({ animationDuration: state.reduced ? '0s' : animOut }) }
      : {})
  } : undefined;

  const mod = await import(`../ui/screens.js?fresh=${++loadCounter}`);
  return { mod, byId, domListeners, mqListeners, state };
}

/* ------------------------------ id resolution ------------------------------- */

test('resolves bare name "question" and full id "screen-question" to the same section', async () => {
  const { mod, byId } = await loadScreens();
  assert.equal(mod.showScreen('question'), 'screen-question');
  assert.equal(mod.currentScreen(), 'screen-question');
  assert.ok(byId.get('screen-question').classList.contains('is-active'));
  assert.equal(byId.get('screen-question').hidden, false);

  assert.equal(mod.showScreen('screen-buzz'), 'screen-buzz');
  assert.ok(byId.get('screen-buzz').classList.contains('is-active'));
});

test('initial screen is splash (readyState complete → shown at module load)', async () => {
  const { mod, byId } = await loadScreens();
  assert.equal(mod.currentScreen(), 'screen-splash');
  assert.ok(byId.get('screen-splash').classList.contains('is-active'));
  for (const s of byId.values()) {
    if (s.id !== 'screen-splash' && s.tagName === 'SECTION') {
      assert.equal(s.classList.contains('is-active'), false, `${s.id} must not be active`);
    }
  }
});

test('readyState "loading" defers splash until DOMContentLoaded fires', async () => {
  const { mod, byId, domListeners } = await loadScreens({ readyState: 'loading' });
  assert.equal(mod.currentScreen(), null);
  const done = domListeners.find((l) => l.type === 'DOMContentLoaded');
  assert.ok(done, 'init must register a DOMContentLoaded listener');
  done.cb();
  assert.equal(mod.currentScreen(), 'screen-splash');
  assert.ok(byId.get('screen-splash').classList.contains('is-active'));
});

test('bad input leaves the current screen unchanged and never throws', async () => {
  const { mod } = await loadScreens();
  for (const bad of ['nope', '', null, undefined, 42, {}, 'screen-ghost']) {
    assert.equal(mod.showScreen(bad), 'screen-splash', `showScreen(${String(bad)}) should no-op`);
  }
});

test('an element matching the id but not a data-screen section is rejected', async () => {
  const div = { tagName: 'DIV', id: 'screen-typo', hidden: false, dataset: { screen: 'typo' }, style: {} };
  const { mod } = await loadScreens({ extraElements: [div] });
  assert.equal(mod.showScreen('typo'), 'screen-splash'); // untouched — not a <section>
});

test('module is importable and callable headless (no document, no window)', async () => {
  const { mod } = await loadScreens({ withDocument: false, withWindow: false });
  assert.equal(mod.currentScreen(), null);
  assert.equal(mod.showScreen('lobby'), null); // no throw, no screen
  assert.equal(mod.setPhaseBadge('verdict'), 'verdict'); // badge absent → key returned
});

/* --------------------------- crossfade + exit timers ------------------------- */

test('screen change crossfades: outgoing pinned + .is-exiting, incoming .is-active', async (t) => {
  const { mod, byId } = await loadScreens({ animOut: '0.25s' });
  t.mock.timers.enable({ apis: ['setTimeout'] });

  const splash = byId.get('screen-splash');
  const lobby = byId.get('screen-lobby');
  assert.equal(mod.showScreen('lobby'), 'screen-lobby');

  // Immediately: both visible, outgoing pinned over the stage (no flow stacking).
  assert.ok(splash.classList.contains('is-exiting'), 'splash exits with .is-exiting');
  assert.equal(splash.classList.contains('is-active'), false);
  assert.equal(splash.hidden, false, 'outgoing stays visible during the fade');
  assert.equal(splash.style.position, 'absolute', 'outgoing pinned over the stage');
  assert.equal(splash.style.inset, '0');
  assert.ok(lobby.classList.contains('is-active'));
  assert.equal(lobby.hidden, false);
  assert.ok(lobby.reflows >= 1, 'entry animation restarts via forced reflow');

  // [hidden] lands exactly when CSS says the exit finished (0.25s), not before.
  t.mock.timers.tick(249);
  assert.equal(splash.hidden, false, 'still fading at 249ms of a 250ms exit');
  t.mock.timers.tick(1);
  assert.equal(splash.hidden, true, 'hidden after the CSS-synced exit duration');
  assert.equal(splash.classList.contains('is-exiting'), false);
  assert.equal(splash.style.position, '', 'inline pin cleaned up');
  assert.ok(lobby.classList.contains('is-active'));
});

test('falls back to the 160ms spec when computed styles are unavailable', async (t) => {
  const { mod, byId } = await loadScreens({ withComputedStyle: false });
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const splash = byId.get('screen-splash');
  mod.showScreen('lobby');
  t.mock.timers.tick(159);
  assert.equal(splash.hidden, false, 'fallback exit is 160ms — not done at 159ms');
  t.mock.timers.tick(1);
  assert.equal(splash.hidden, true, 'hidden at the 160ms fallback');
});

test('re-showing the current, fully-active screen is a no-op (zero re-show flash)', async () => {
  const { mod, byId } = await loadScreens();
  const buzz = byId.get('screen-buzz');
  assert.equal(mod.showScreen('buzz'), 'screen-buzz');
  const reflowsAfterFirstShow = buzz.reflows;
  assert.ok(reflowsAfterFirstShow >= 1);

  // app.js does exactly this: listening → judging both call showScreen('screen-buzz').
  assert.equal(mod.showScreen('screen-buzz'), 'screen-buzz');
  assert.equal(buzz.reflows, reflowsAfterFirstShow, 'entry animation must NOT restart');
  assert.ok(buzz.classList.contains('is-active'));
  assert.equal(buzz.classList.contains('is-exiting'), false);
  assert.equal(mod.currentScreen(), 'screen-buzz');
});

test('mid-exit revival cancels the pending hide — the revived screen stays visible', async (t) => {
  const { mod, byId } = await loadScreens({ animOut: '0.3s' });
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const splash = byId.get('screen-splash');
  const setup = byId.get('screen-setup');

  mod.showScreen('setup');            // splash begins its exit
  assert.ok(splash.classList.contains('is-exiting'));
  mod.showScreen('splash');           // …revived before the exit finished

  assert.ok(splash.classList.contains('is-active'), 'revived with .is-active');
  assert.equal(splash.classList.contains('is-exiting'), false, 'exit class dropped');
  assert.equal(splash.hidden, false);
  assert.equal(splash.style.position, '', 'exit pin removed on revival');
  assert.equal(mod.currentScreen(), 'screen-splash');

  // Far past the original exit duration: revival must have cancelled the hide.
  t.mock.timers.tick(1000);
  assert.equal(splash.hidden, false, 'revived screen must never be hidden by the stale exit');
  assert.equal(splash.classList.contains('is-active'), true);
  assert.equal(setup.hidden, true, 'the screen it swapped with does hide cleanly');
  assert.equal(setup.classList.contains('is-exiting'), false);
});

test('rapid A→B→A churn ends with exactly one visible screen', async (t) => {
  const { mod, byId } = await loadScreens();
  t.mock.timers.enable({ apis: ['setTimeout'] });
  mod.showScreen('question');
  mod.showScreen('buzz');
  mod.showScreen('question');
  t.mock.timers.tick(1000);
  const visible = [...byId.values()].filter((s) => s.tagName === 'SECTION' && !s.hidden);
  assert.equal(visible.length, 1, 'exactly one screen visible after churn');
  assert.equal(visible[0].id, 'screen-question');
  assert.equal(mod.currentScreen(), 'screen-question');
});

/* --------------------------- reduced motion support -------------------------- */

test('prefers-reduced-motion: instant cut, no slide/fade, no exit timer', async (t) => {
  const { mod, byId } = await loadScreens({ reduced: true });
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const splash = byId.get('screen-splash');
  const lobby = byId.get('screen-lobby');

  assert.equal(mod.showScreen('lobby'), 'screen-lobby');
  assert.equal(splash.hidden, true, 'outgoing hidden immediately — no lingering fade');
  assert.equal(splash.classList.contains('is-exiting'), false);
  assert.equal(splash.style.position, '', 'no pin needed for an instant cut');
  assert.ok(lobby.classList.contains('is-active'));
  assert.equal(lobby.style.animation, 'none', 'CSS entry animation suppressed');
  assert.equal(lobby.reflows, 0, 'no reflow dance when nothing animates');

  t.mock.timers.tick(1000); // nothing was scheduled; state must be stable
  assert.ok(lobby.classList.contains('is-active'));
  assert.equal(lobby.hidden, false);
  assert.equal(splash.hidden, true);
});

test('a live prefers-reduced-motion flip re-normalises the current screen', async () => {
  const { mod, byId, mqListeners, state } = await loadScreens({ reduced: false });
  const question = byId.get('screen-question');
  mod.showScreen('question');
  assert.equal(question.style.animation, '');

  state.reduced = true;
  mqListeners.forEach((cb) => cb()); // OS preference flips to reduce
  assert.equal(question.style.animation, 'none', 'current screen de-animated live');

  state.reduced = false;
  mqListeners.forEach((cb) => cb()); // …and back
  assert.equal(question.style.animation, '');

  // New screens while reduced get instant cuts; toggling off restores motion.
  state.reduced = true;
  mod.showScreen('buzz');
  assert.equal(byId.get('screen-buzz').style.animation, 'none');
  assert.equal(byId.get('screen-question').hidden, true);
  state.reduced = false;
  mod.showScreen('question');
  assert.equal(byId.get('screen-question').style.animation, '', 'motion restored after flip');
});

/* ------------------------------- phase badge -------------------------------- */

test('setPhaseBadge maps CONTRACT §2 phases and passes unknowns through', async () => {
  const { mod, byId } = await loadScreens();
  const badge = byId.get('phase-badge');

  assert.equal(mod.setPhaseBadge('round_intro'), 'ROUND INTRO');
  assert.equal(badge.dataset.phase, 'round_intro');

  assert.equal(mod.setPhaseBadge('verdict'), 'VERDICT');

  assert.equal(mod.setPhaseBadge('weird_phase'), 'WEIRD PHASE'); // unknown → humanised
  assert.equal(badge.dataset.phase, 'weird_phase');

  assert.equal(mod.setPhaseBadge(null), '—'); // empty → dash
  assert.equal(badge.dataset.phase, 'unknown');
});
