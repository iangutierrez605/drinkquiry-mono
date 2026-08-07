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
  // §F2 (#19): logout is the choke point — SiteNav's logout, api.js's
  // dead-token drop and the pages' 401 handlers all route through here.
  // Purging host seats here (not in each caller) is what closes the
  // cross-account host-console leak.
  clearHostSeats();
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
// §F2 (Handoff #19): the pointer is SCOPED to the account that made it.
// The pre-#19 bare-string value was unscoped and never cleared on logout,
// so on a shared browser user B's /host visit resumed user A's LIVE host
// console (the cross-account leak, owner bug 2). Stored shape is now
// {code, userId}; loadHostGame returns the code only when the CALLER's
// userId matches. A legacy bare string (or an all-digit code that
// JSON-parses as a number, or a userId-less object) cannot prove
// ownership — removed on sight, so the first post-deploy load silently
// drops old pointers (expected once; resume still works from /profile).
export function saveHostGame(code, userId) {
  localStorage.setItem(
    "dq_host_game",
    JSON.stringify({ code: code.toUpperCase(), userId: userId ?? null }),
  );
}
export function loadHostGame(userId) {
  const raw = localStorage.getItem("dq_host_game");
  if (raw == null) return null;
  let parsed = null;
  try {
    parsed = JSON.parse(raw);
  } catch {
    parsed = null; // legacy bare-string code — not valid JSON
  }
  if (
    !parsed ||
    typeof parsed !== "object" ||
    typeof parsed.code !== "string" ||
    parsed.userId == null
  ) {
    localStorage.removeItem("dq_host_game");
    return null;
  }
  return userId != null && parsed.userId === userId ? parsed.code : null;
}
export function clearHostGame() {
  localStorage.removeItem("dq_host_game");
}

// §F2 (Handoff #19): logout must not leave the HOST's seat tokens behind —
// they authenticate the game surface by design, so a later login by a
// DIFFERENT account in the same browser could operate the first account's
// console. Purge every host-ROLE seat (+ its bare-token twin) and the
// active-game pointer. Player seats and unparseable legacy entries stay
// (C-2: someone logged out of a host account may still be playing as a
// player in another tab of this browser).
export function clearHostSeats() {
  const doomed = [];
  for (let i = 0; i < localStorage.length; i += 1) {
    const key = localStorage.key(i);
    if (!key || !key.startsWith("dq_seat_")) continue;
    try {
      const seat = JSON.parse(localStorage.getItem(key));
      if (seat && seat.role === "host") doomed.push(key);
    } catch {
      /* unparseable legacy entry — leave it (C-2) */
    }
  }
  for (const key of doomed) {
    localStorage.removeItem(key);
    localStorage.removeItem(`dq_token_${key.slice("dq_seat_".length)}`);
  }
  localStorage.removeItem("dq_host_game");
}
