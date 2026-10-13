// frontend/ws-client.js — BhashaBuzzer A10 (CONTRACT §5)
// export class GameWS { constructor(url, {onOpen, onClose, onError, onFrame}); send(obj); close(); get state(); }
//   JSON text frames · auto-reconnect w/ exponential backoff + jitter (250 ms → 8 s cap)
//   · onOpen re-sends the last `start` frame (queued starts are flushed the same way).

export class GameWS {
  /** Backoff delay for attempt n: 250·2^n ms, ±25% jitter, hard-capped at 8 s.
   *  rand injectable for tests (called once, should return 0..1; 0.5 = no jitter). */
  static backoffDelay(attempt, rand = Math.random) {
    const n = Math.max(0, Math.floor(Number(attempt) || 0));
    const base = Math.min(GameWS.BACKOFF_BASE_MS * 2 ** n, GameWS.BACKOFF_CAP_MS);
    const r = Math.min(1, Math.max(0, Number(rand?.()) || 0));
    return Math.min(base * (1 + 0.25 * (r * 2 - 1)), GameWS.BACKOFF_CAP_MS);
  }

  static BACKOFF_BASE_MS = 250;
  static BACKOFF_CAP_MS = 8000;

  constructor(url, { onOpen, onClose, onError, onFrame, wsFactory } = {}) {
    this.url = String(url);
    this.onOpen = onOpen ?? null;
    this.onClose = onClose ?? null;
    this.onError = onError ?? null;
    this.onFrame = onFrame ?? null;
    this._wsFactory = wsFactory ?? null; // tests / alt transports; browser: global WebSocket
    this._ws = null;
    this._state = 'idle';
    this._attempt = 0;
    this._timer = null;
    this._lastStart = null; // last start frame — re-sent on reopen (CONTRACT §5)
    this._closedByUser = false;
    this.connect();
  }

  /** Current transport state: 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed'. */
  get state() {
    return this._state;
  }

  /** The last start frame seen (for app-side restarts / debugging). */
  get lastStart() {
    return this._lastStart;
  }

  /** Send a JSON text frame. `start` frames are remembered and re-sent on reopen; a
   *  start queued while connecting/reconnecting is accepted (returns true) and flushed
   *  by onOpen. Other frames sent while not open are dropped (returns false) —
   *  buzz/answer are time-critical; queuing them across a reconnect would send stale
   *  input. Returns whether the frame was sent or queued. */
  send(obj) {
    if (!obj || typeof obj !== 'object') return false;
    if (obj.type === 'start') {
      this._lastStart = { ...obj };
      if (this._state === 'open' && this._ws) {
        try { this._ws.send(JSON.stringify(obj)); return true; } catch { return false; }
      }
      return this._state === 'connecting' || this._state === 'reconnecting';
    }
    if (this._state !== 'open' || !this._ws) return false;
    try {
      this._ws.send(JSON.stringify(obj));
      return true;
    } catch {
      return false;
    }
  }

  /** Intentional close: no reconnect, transport state → 'closed'. */
  close() {
    this._closedByUser = true;
    if (this._timer) { clearTimeout(this._timer); this._timer = null; }
    const ws = this._ws;
    this._ws = null;
    this._state = 'closed';
    if (ws) {
      try { ws.close(); } catch { /* already dead */ }
    }
  }

  connect() {
    if (this._closedByUser) return;
    if (this._ws) return; // already have a socket in flight
    this._state = this._attempt === 0 ? 'connecting' : 'reconnecting';
    let ws;
    try {
      const Ctor = this._wsFactory ?? WebSocket;
      ws = new Ctor(this.url);
    } catch (err) {
      this._fail(err);
      return;
    }
    this._ws = ws;

    ws.onopen = () => {
      this._state = 'open';
      this._attempt = 0;
      // CONTRACT §5: re-send last start on (re)open. Also flushes a start queued
      // while the socket was still connecting.
      if (this._lastStart) {
        try { ws.send(JSON.stringify(this._lastStart)); } catch { /* closed again already */ }
      }
      if (this.onOpen) this.onOpen();
    };

    ws.onmessage = (ev) => {
      let msg = null;
      try { msg = JSON.parse(typeof ev.data === 'string' ? ev.data : ''); } catch { return; }
      if (this.onFrame) this.onFrame(msg);
    };

    ws.onerror = (ev) => {
      if (this.onError) this.onError(ev);
    };

    ws.onclose = (ev) => {
      this._ws = null;
      if (this._closedByUser) {
        this._state = 'closed';
      } else {
        this._scheduleReconnect();
      }
      if (this.onClose) this.onClose(ev);
    };
  }

  _scheduleReconnect() {
    if (this._closedByUser) return;
    this._state = 'reconnecting';
    const delay = GameWS.backoffDelay(this._attempt);
    this._attempt += 1;
    if (this._timer) clearTimeout(this._timer);
    this._timer = setTimeout(() => {
      this._timer = null;
      this.connect();
    }, delay);
  }

  _fail(err) {
    if (this.onError) this.onError(err);
    this._scheduleReconnect();
  }
}

export default GameWS;
