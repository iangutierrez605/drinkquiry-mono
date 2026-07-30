// Reload-safety contract from the handoff doc:
// after create/join, store the participant token keyed by game code so a
// reload reclaims the same seat. We store a small "seat" object per code
// (token + participant id + name + role) plus the host's auth token and a
// pointer to the host's most recent game so /host can offer "resume".

const seatKey = (code) => `dq_seat_${code.toUpperCase()}`;

export function saveSeat(code, seat) {
  localStorage.setItem(seatKey(code), JSON.stringify(seat));
  // Back-compat with the doc's suggested key so a bare token is also findable.
  localStorage.setItem(`dq_token_${code.toUpperCase()}`, seat.token);
}

export function loadSeat(code) {
  try {
    const raw = localStorage.getItem(seatKey(code));
    if (raw) return JSON.parse(raw);
    const bare = localStorage.getItem(`dq_token_${code.toUpperCase()}`);
    return bare ? { token: bare } : null;
  } catch {
    return null;
  }
}

export function clearSeat(code) {
  localStorage.removeItem(seatKey(code));
  localStorage.removeItem(`dq_token_${code.toUpperCase()}`);
}

// ---- Host auth token (Knox) ----
// §F (Handoff #9): saving/clearing auth dispatches a window event so the
// SiteNav (mounted once at the App level) updates immediately when a page's
// inline AuthScreen logs in or a logout button fires — no polling, no prop
// drilling across routes.
const AUTH_EVENT = "dq-auth-changed";

function announceAuthChange() {
  try {
    window.dispatchEvent(new Event(AUTH_EVENT));
  } catch {
    /* non-browser env */
  }
}

export function onAuthChange(handler) {
  window.addEventListener(AUTH_EVENT, handler);
  return () => window.removeEventListener(AUTH_EVENT, handler);
}

export function saveAuth(auth) {
  localStorage.setItem("dq_auth", JSON.stringify(auth)); // {token, user}
  announceAuthChange();
}
export function loadAuth() {
  try {
    return JSON.parse(localStorage.getItem("dq_auth"));
  } catch {
    return null;
  }
}
export function clearAuth() {
  localStorage.removeItem("dq_auth");
  announceAuthChange();
}

// ---- Age gate acknowledgement (§H, Handoff #9) ----
// Versioned key: bump to _v2 if the copy ever changes materially and every
// browser should be re-prompted. Stores an ISO timestamp (when they agreed),
// but any truthy value counts as acknowledged.
const AGE_ACK_KEY = "dq_age_ack_v1";

export function loadAgeAck() {
  try {
    return localStorage.getItem(AGE_ACK_KEY);
  } catch {
    return null;
  }
}
export function saveAgeAck() {
  try {
    localStorage.setItem(AGE_ACK_KEY, new Date().toISOString());
  } catch {
    /* storage unavailable — the gate will just show again */
  }
}

// ---- Host's active game (for resume after reload) ----
export function saveHostGame(code) {
  localStorage.setItem("dq_host_game", code.toUpperCase());
}
export function loadHostGame() {
  return localStorage.getItem("dq_host_game");
}
export function clearHostGame() {
  localStorage.removeItem("dq_host_game");
}
