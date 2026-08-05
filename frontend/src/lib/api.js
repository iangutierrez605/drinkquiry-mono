// REST + WebSocket base configuration and thin API client.
// The backend contract is fixed (see the handoff doc); this file is the only
// place that knows URLs and auth header formats.

import { clearAuth } from "./storage";

// API base resolution (Handoff #5) — no hardcoded hosts anywhere:
// 1. VITE_API_BASE, if set, always wins (build-time env; "" means same-origin).
// 2. On the Vite dev server (port 5173/3000), assume Django on port 8000 of
//    the SAME host the page was loaded from — so localhost, a LAN IP on your
//    phone, or a tunnel all work with zero configuration.
// 3. Anything else (the built app behind a reverse proxy) → same-origin
//    relative URLs; the proxy routes /api, /ws, and /media to Django.
function defaultApiBase() {
  if (typeof window === "undefined") return "";
  const { protocol, hostname, port } = window.location;
  if (port === "5173" || port === "3000") return `${protocol}//${hostname}:8000`;
  return ""; // same-origin
}

export const API_BASE = (import.meta.env.VITE_API_BASE ?? defaultApiBase()).replace(/\/$/, "");

// §H (Handoff #12): the ONE place the support address lives (C1). Rendered
// as mailto: links in the site footer, the auth screen, the buzzer's
// no-such-game branch and the 404 page. NOTE (owner-run): Resend is
// send-only — the mailbox itself must exist as a forward/alias at the
// domain's mail hosting, or mail to it bounces.
export const SUPPORT_EMAIL = "support@drinkquiry.com";
export const WS_BASE = API_BASE
  ? API_BASE.replace(/^http/, "ws")
  : typeof window === "undefined"
    ? ""
    : `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}`;

/** Media file fields come back as absolute or root-relative URLs. */
export function mediaUrl(url) {
  if (!url) return null;
  if (/^https?:\/\//.test(url)) return url;
  return API_BASE + (url.startsWith("/") ? url : "/" + url);
}

export class ApiError extends Error {
  constructor(status, data) {
    super(typeof data === "string" ? data : JSON.stringify(data));
    this.status = status;
    this.data = data;
  }
}

/** Flatten DRF error payloads ({field: [msgs]}, {detail: "..."}) to one string. */
export function errorText(err) {
  if (!(err instanceof ApiError)) return err?.message || "Something went wrong.";
  const d = err.data;
  if (!d) return `Request failed (${err.status}).`;
  if (typeof d === "string") return d;
  if (d.detail) return d.detail;
  return Object.entries(d)
    .map(([field, msgs]) => {
      const text = Array.isArray(msgs) ? msgs.join(" ") : String(msgs);
      return field === "non_field_errors" ? text : `${field}: ${text}`;
    })
    .join("\n");
}

/**
 * Detect the backend's structured quota 403s:
 *   {detail, code: "quota_*", used, limit}
 * Returns that payload, or null for any other error. Any other 403 keeps
 * flowing through the existing errorText()/upsell paths.
 */
export function quotaError(err) {
  if (!(err instanceof ApiError) || err.status !== 403) return null;
  const d = err.data;
  if (d && typeof d.code === "string" && d.code.startsWith("quota_")) return d;
  return null;
}

async function request(path, { method = "GET", body, authToken, formData } = {}) {
  const headers = {};
  if (authToken) headers["Authorization"] = `Token ${authToken}`;
  let payload;
  if (formData) {
    payload = formData; // browser sets multipart boundary
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const res = await fetch(`${API_BASE}${path}`, { method, headers, body: payload });
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!res.ok) {
    // Knox tokens expire (default 10h). A 401 on an authed call means the
    // stored host login is dead — clear it so the next visit shows the login
    // form instead of erroring forever. clearAuth (not a raw removeItem) so
    // the §F SiteNav hears about it and flips to "Log in" immediately.
    if (res.status === 401 && authToken) {
      try {
        clearAuth();
      } catch {
        /* storage unavailable */
      }
    }
    throw new ApiError(res.status, data);
  }
  return data;
}

/** Build a query string from a params object; empty/null values are dropped.
 *  URLSearchParams does the encoding — emoji, quotes and & in a search term
 *  arrive server-side intact (§G, Handoff #15). */
function qs(params = {}) {
  const s = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== "" && v != null),
  ).toString();
  return s ? `?${s}` : "";
}

/** Follow DRF pagination `next` links until exhausted; returns one flat array.
 *
 * ⚠️ C21 (Handoff #15): this helper is a SCALE LANDMINE — every call site is
 * a future outage (at 2,000 categories it means 40 sequential requests and a
 * 2,000-row render; at 500 pending items it detonated #14c). It survives for
 * exactly TWO owner-bounded lists — `tournaments` and `gameHistory`, both
 * capped by one host's own activity — and nothing else. New list consumers
 * use the paged `*Page` methods below (server `?search=` + page envelope);
 * wiring a NEW call site through here needs a written justification of why
 * the list is and stays bounded. */
async function allPages(path, authToken) {
  const items = [];
  let url = `${API_BASE}${path}`;
  while (url) {
    const headers = authToken ? { Authorization: `Token ${authToken}` } : {};
    const res = await fetch(url, { headers });
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new ApiError(res.status, data);
    if (Array.isArray(data)) return data; // pagination disabled server-side
    items.push(...(data?.results ?? []));
    url = data?.next || null;
  }
  return items;
}

export const api = {
  // ---- Auth (Knox) ----
  register: (fields) => request("/api/auth/register/", { method: "POST", body: fields }),
  // The backend's LoginView validates a JSON body {email, password}
  // (it wraps Knox's LoginView) and returns {expiry, token, user}.
  login: (email, password) =>
    request("/api/auth/login/", { method: "POST", body: { email, password } }),
  logout: (token) => request("/api/auth/logout/", { method: "POST", authToken: token }),
  profile: (token) => request("/api/auth/profile/", { authToken: token }),
  // §H (Handoff #11): venue branding rides the profile PATCH — multipart
  // when a logo file is attached, JSON otherwise (brand_name edits, the
  // brand_logo_clear flag). Free-plan writes get a plain 403 server-side.
  updateProfile: (token, changes) =>
    changes instanceof FormData
      ? request("/api/auth/profile/", { method: "PATCH", authToken: token, formData: changes })
      : request("/api/auth/profile/", { method: "PATCH", authToken: token, body: changes }),

  // ---- Password flows (§K, Handoff #9) ----
  // forgot ALWAYS 200s with the same body (no user enumeration server-side).
  passwordForgot: (email) =>
    request("/api/auth/password/forgot/", { method: "POST", body: { email } }),
  passwordReset: (uid, token, newPassword) =>
    request("/api/auth/password/reset/", {
      method: "POST",
      body: { uid, token, new_password: newPassword },
    }),
  passwordChange: (token, currentPassword, newPassword) =>
    request("/api/auth/password/change/", {
      method: "POST",
      authToken: token,
      body: { current_password: currentPassword, new_password: newPassword },
    }),

  // ---- Content (server-paged since Handoff #15 — C21: never allPages) ----
  // All *Page methods return the DRF page envelope {results, count, next,
  // previous} AS-IS; `search` rides the server's `?search=` (icontains,
  // capped at 100 chars server-side), `page` is 1-based.
  // §G (Handoff #12): the anonymous shop window — no token, deliberately
  // unthrottled server-side. Rows: {id, name, description, photo,
  // question_count} (pinned).
  publicCategoriesPage: ({ search, page } = {}) =>
    request(`/api/categories/public/${qs({ search, page })}`),
  // Authed visible categories. `mine: 1` = the caller's OWN rows only (any
  // visibility/status) — the /create "My content" pager; `count` is then the
  // true owned total (feeds the usage meters).
  categoriesPage: (token, { search, page, mine } = {}) =>
    request(`/api/categories/${qs({ search, page, mine })}`, { authToken: token }),
  // Visible questions; `category` filters by id, `mine: 1` as above.
  questionsPage: (token, { category, search, page, mine } = {}) =>
    request(`/api/questions/${qs({ category, search, page, mine })}`, { authToken: token }),
  createCategory: (token, formData) =>
    request("/api/categories/", { method: "POST", authToken: token, formData }),
  createQuestion: (token, formData) =>
    request("/api/questions/", { method: "POST", authToken: token, formData }),
  // PATCH with formData when a file changes, JSON otherwise (owner-only).
  updateCategory: (token, id, changes) =>
    changes instanceof FormData
      ? request(`/api/categories/${id}/`, { method: "PATCH", authToken: token, formData: changes })
      : request(`/api/categories/${id}/`, { method: "PATCH", authToken: token, body: changes }),
  deleteCategory: (token, id) =>
    request(`/api/categories/${id}/`, { method: "DELETE", authToken: token }),
  updateQuestion: (token, id, changes) =>
    changes instanceof FormData
      ? request(`/api/questions/${id}/`, { method: "PATCH", authToken: token, formData: changes })
      : request(`/api/questions/${id}/`, { method: "PATCH", authToken: token, body: changes }),
  deleteQuestion: (token, id) =>
    request(`/api/questions/${id}/`, { method: "DELETE", authToken: token }),

  // ---- Bulk question upload (CSV; Handoff #4 §G) ----
  // formData: file (required), dry_run, skip_duplicates, official (staff only).
  bulkQuestions: (token, formData) =>
    request("/api/questions/bulk/", { method: "POST", authToken: token, formData }),

  // ---- Moderation (staff only; Handoff #4 §F, extended by #8 §K) ----
  moderationCounts: (token) => request("/api/moderation/counts/", { authToken: token }),
  // #15: ONE page of a review queue — {results, count, next, previous}.
  // kind: "categories" | "questions"; status defaults to pending server-side.
  // The queues page at the server's sizes (questions 25, categories 50);
  // never run this through allPages (that fetch-all is what #14c punished).
  moderationPage: (kind, token, { status, page } = {}) =>
    request(`/api/moderation/${kind}/${qs({ status, page })}`, { authToken: token }),
  moderationApprove: (token, kind, id) =>
    request(`/api/moderation/${kind}/${id}/approve/`, { method: "POST", authToken: token }),
  moderationReject: (token, kind, id, note) =>
    request(`/api/moderation/${kind}/${id}/reject/`, { method: "POST", authToken: token, body: { note } }),
  // §K1 via §F2 (#15): the near-duplicate review aid for a WHOLE page of
  // cards in ONE request — {"<id>": [match, ...]}, ids capped at 50 per call
  // (chunk bigger lists). Replaces the per-card fetches (and their #14c
  // gate) entirely; the single GET …/<id>/similar/ endpoint still exists
  // server-side but no longer has a client method — nothing should per-card
  // it again (C20).
  moderationSimilarBatch: (token, ids) =>
    request("/api/moderation/questions/similar/batch/", { method: "POST", authToken: token, body: { ids } }),

  // ---- §I (Handoff #9): the staff question library ----
  // REAL pagination — returns {results, count, next, previous} as-is; never
  // run this through allPages (the library is the "thousands of questions"
  // surface — and since #15 the pending-queue tabs page the same way via
  // moderationPage above). `params` is a plain object; empty values dropped.
  moderationLibrary: (token, params = {}) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== "" && v != null),
    ).toString();
    return request(`/api/moderation/questions/?${qs}`, { authToken: token });
  },
  // §I3: versioned edit — the response is the NEW question row (201).
  moderationRevise: (token, id, changes) =>
    request(`/api/moderation/questions/${id}/revise/`, { method: "POST", authToken: token, body: changes }),
  // §I2: staff soft delete (any owner's question); 409 if already deleted.
  moderationDelete: (token, id) =>
    request(`/api/moderation/questions/${id}/delete/`, { method: "POST", authToken: token }),

  // ---- §J (Handoff #9): staff user management ----
  // Paginated like the library: {results, count, next, previous} as-is.
  adminUsers: (token, params = {}) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== "" && v != null),
    ).toString();
    return request(`/api/moderation/users/?${qs}`, { authToken: token });
  },
  // PATCH accepts plan / plan_expires_at / limit_overrides ONLY (anything
  // else is a server-side 400 — is_staff editing stays in Django admin).
  adminUserPatch: (token, id, changes) =>
    request(`/api/moderation/users/${id}/`, { method: "PATCH", authToken: token, body: changes }),

  // §K2: the Flagged tab (open host reports, grouped per question) + resolve.
  moderationFlags: (token) => request("/api/moderation/flags/", { authToken: token }),
  moderationFlagResolve: (token, questionId, action, note) =>
    request(`/api/moderation/flags/${questionId}/resolve/`, {
      method: "POST",
      authToken: token,
      body: note ? { action, note } : { action },
    }),
  // §K2: any authenticated host flags a public question (409 if they already
  // have one open on it; the question stays playable either way).
  reportQuestion: (token, questionId, reason) =>
    request(`/api/questions/${questionId}/report/`, {
      method: "POST",
      authToken: token,
      body: reason ? { reason } : {},
    }),

  // ---- §G (Handoff #10): themes ----
  // Host-facing discovery list (active themes, per-user visible categories
  // with per-user usable counts). A BARE ARRAY — staff-curated scale, no
  // page envelope (do NOT wrap in a page helper); #15 adds server-side
  // `?search=` for the strip's search box.
  themes: (token, search) => request(`/api/themes/${qs({ search })}`, { authToken: token }),
  // Staff CRUD (IsAdminUser server-side; the tab's is_staff gate is
  // cosmetic). Unpaginated list — plain array.
  moderationThemes: (token) => request("/api/moderation/themes/", { authToken: token }),
  createTheme: (token, body) =>
    request("/api/moderation/themes/", { method: "POST", authToken: token, body }),
  updateTheme: (token, id, changes) =>
    request(`/api/moderation/themes/${id}/`, { method: "PATCH", authToken: token, body: changes }),
  // Soft delete; 409 = someone else already deleted it.
  deleteTheme: (token, id) =>
    request(`/api/moderation/themes/${id}/`, { method: "DELETE", authToken: token }),

  // ---- §I (Handoff #13): tournaments (all host-private, Knox) ----
  tournaments: (token) => allPages("/api/tournaments/", token),
  createTournament: (token, { name, location }) =>
    request("/api/tournaments/", { method: "POST", authToken: token, body: { name, location } }),
  tournament: (token, id) => request(`/api/tournaments/${id}/`, { authToken: token }),
  finishTournament: (token, id) =>
    request(`/api/tournaments/${id}/finish/`, { method: "POST", authToken: token }),
  deleteTournament: (token, id) =>
    request(`/api/tournaments/${id}/`, { method: "DELETE", authToken: token }),
  advanceRound: (token, id, roundNumber, perGame) =>
    request(`/api/tournaments/${id}/rounds/${roundNumber}/advance/`, {
      method: "POST",
      authToken: token,
      body: { per_game: perGame },
    }),

  // ---- Games ----
  // §H (#13): buzz_sound (1–4) is the host's per-game sound choice; the
  // server defaults to 1 when omitted, so older callers keep working.
  // §F4 ROOT CAUSE (#17): this destructure is a WHITELIST — it rebuilt the
  // body from four named fields and silently dropped `tournament` +
  // `round_number`, which the create screen had been dutifully sending
  // since #13. Result: the 🏆 banner showed, the server 201'd a PLAIN
  // game, and no bracket ever populated — the owner's original bug, and
  // invisible to every automated check because the suite and the smoke
  // exercise the API with their own (correct) bodies. The pairing spread
  // mirrors the call site and the server's both-or-neither rule. If you
  // add a create field, add it HERE too — this seam has no test harness.
  createGame: (token, { mode, categories, questions_per_category, buzz_sound, tournament, round_number }) =>
    request("/api/games/", {
      method: "POST",
      authToken: token,
      body: {
        mode,
        categories,
        questions_per_category,
        ...(buzz_sound ? { buzz_sound } : {}),
        ...(tournament ? { tournament, round_number } : {}),
      },
    }),
  gameSnapshot: (code) => request(`/api/games/${code.toUpperCase()}/`),
  // Host-private answer for the open cell (Handoff #6 §F1). Knox-authed and
  // host-checked server-side; works pre-reveal — that's its whole point. The
  // answer never rides a snapshot or WS payload before reveal (rule 5).
  gameAnswer: (token, code) => request(`/api/games/${code.toUpperCase()}/answer/`, { authToken: token }),
  // Game history + post-game report (Handoff #6 §G2), host's own games only.
  // Since #8 §I the history rows include unfinished games too (status field).
  gameHistory: (token) => allPages("/api/games/history/", token),
  gameReport: (token, code) => request(`/api/games/${code.toUpperCase()}/report/`, { authToken: token }),
  // §I (Handoff #8): re-issue the host's own seat token so a game can be
  // resumed from a new browser/device. Knox-authed, host-only.
  gameHostSeat: (token, code) => request(`/api/games/${code.toUpperCase()}/host-seat/`, { authToken: token }),
  // §J3 (Handoff #8): host-private lobby preview (questions AND answers —
  // never any part of a snapshot) + per-cell replace (lobby only, 409 after).
  gameBoard: (token, code) => request(`/api/games/${code.toUpperCase()}/board/`, { authToken: token }),
  replaceCell: (token, code, cellId) =>
    request(`/api/games/${code.toUpperCase()}/cells/${cellId}/replace/`, { method: "POST", authToken: token }),
  // §F (Handoff #16): lobby-only whole-COLUMN category swap. Body is a bare
  // integer category id (theme-unaware like creation, §G #10); the response
  // is ONE column in the board-detail shape, patched in place by the lobby
  // preview. 409s: game started / category already on the board / deleted /
  // too thin (creation's shortage message).
  replaceColumnCategory: (token, code, columnId, categoryId) =>
    request(`/api/games/${code.toUpperCase()}/columns/${columnId}/replace/`, {
      method: "POST",
      authToken: token,
      body: { category_id: categoryId },
    }),
  joinGame: (code, name, participantToken) =>
    request(`/api/games/${code.toUpperCase()}/join/`, {
      method: "POST",
      body: participantToken ? { name, participant_token: participantToken } : { name },
    }),
};