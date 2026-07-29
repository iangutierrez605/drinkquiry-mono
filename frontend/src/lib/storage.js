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
export function saveAuth(auth) {
  localStorage.setItem("dq_auth", JSON.stringify(auth)); // {token, user}
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
