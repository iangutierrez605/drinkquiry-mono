// REST + WebSocket base configuration and thin API client.
// The backend contract is fixed (see the handoff doc); this file is the only
// place that knows URLs and auth header formats.

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
    // form instead of erroring forever.
    if (res.status === 401 && authToken) {
      try {
        localStorage.removeItem("dq_auth");
      } catch {
        /* storage unavailable */
      }
    }
    throw new ApiError(res.status, data);
  }
  return data;
}

/** Follow DRF pagination `next` links until exhausted; returns one flat array. */
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

  // ---- Content ----
  categories: (token) => allPages("/api/categories/", token),
  questions: (categoryId, token) =>
    allPages(categoryId ? `/api/questions/?category=${categoryId}` : "/api/questions/", token),
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

  // ---- Moderation (staff only; Handoff #4 §F) ----
  moderationCounts: (token) => request("/api/moderation/counts/", { authToken: token }),
  moderationList: (token, kind, status = "pending") =>
    allPages(`/api/moderation/${kind}/?status=${status}`, token), // kind: "categories" | "questions"
  moderationApprove: (token, kind, id) =>
    request(`/api/moderation/${kind}/${id}/approve/`, { method: "POST", authToken: token }),
  moderationReject: (token, kind, id, note) =>
    request(`/api/moderation/${kind}/${id}/reject/`, { method: "POST", authToken: token, body: { note } }),

  // ---- Games ----
  createGame: (token, { mode, categories, questions_per_category }) =>
    request("/api/games/", { method: "POST", authToken: token, body: { mode, categories, questions_per_category } }),
  gameSnapshot: (code) => request(`/api/games/${code.toUpperCase()}/`),
  // Host-private answer for the open cell (Handoff #6 §F1). Knox-authed and
  // host-checked server-side; works pre-reveal — that's its whole point. The
  // answer never rides a snapshot or WS payload before reveal (rule 5).
  gameAnswer: (token, code) => request(`/api/games/${code.toUpperCase()}/answer/`, { authToken: token }),
  // Game history + post-game report (Handoff #6 §G2), host's own games only.
  gameHistory: (token) => allPages("/api/games/history/", token),
  gameReport: (token, code) => request(`/api/games/${code.toUpperCase()}/report/`, { authToken: token }),
  joinGame: (code, name, participantToken) =>
    request(`/api/games/${code.toUpperCase()}/join/`, {
      method: "POST",
      body: participantToken ? { name, participant_token: participantToken } : { name },
    }),
};