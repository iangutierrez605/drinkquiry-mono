import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, errorText, mediaUrl } from "../lib/api";
import { clearAuth, loadAuth, onAuthChange } from "../lib/storage";
import { Toast } from "../components/shared";

/**
 * /moderate — staff review queue (Handoff #4 §F3, grown every handoff since).
 *
 * Tabs: the pending Questions/Categories queues and the Flagged worklist
 * (small lists, allPages), plus Handoff #9's Library (§I5 — ALL questions,
 * searchable, properly paginated) and Users (§J3 — plans, demo expiries,
 * per-user allowance overrides). The is_staff gate here is cosmetic: every
 * /api/moderation/* endpoint is IsAdminUser server-side. Plain REST +
 * refresh — no WS involvement.
 */

export default function ModeratePage() {
  const [auth, setAuth] = useState(loadAuth());
  const [profile, setProfile] = useState(null);
  const [profileError, setProfileError] = useState(null);
  // §F: nav logout / dead-token cleanup flips this page immediately.
  useEffect(() => onAuthChange(() => setAuth(loadAuth())), []);

  useEffect(() => {
    if (!auth) return;
    let alive = true;
    api
      .profile(auth.token)
      .then((p) => alive && setProfile(p))
      .catch((err) => {
        if (!alive) return;
        if (err instanceof ApiError && err.status === 401) {
          clearAuth();
          setAuth(null);
        } else {
          setProfileError(errorText(err));
        }
      });
    return () => {
      alive = false;
    };
  }, [auth]);

  if (!auth)
    return (
      <Shell>
        <section className="panel panel--center">
          <h2 className="h2">Log in first</h2>
          <p className="footnote">Moderation needs a staff account.</p>
          <Link className="btn btn--primary" to="/login?next=/moderate">
            Go to login
          </Link>
        </section>
      </Shell>
    );

  if (profileError)
    return (
      <Shell user={auth.user}>
        <p className="formerror formerror--block">{profileError}</p>
      </Shell>
    );
  if (!profile)
    return (
      <Shell user={auth.user}>
        <p>Loading your profile…</p>
      </Shell>
    );

  // Same pattern as /create's upsell card: friendly panel, no details leaked.
  if (!profile.is_staff)
    return (
      <Shell user={profile}>
        <section className="panel panel--center upsell">
          <span className="upsell__emoji">🛡️</span>
          <h2 className="h2">Staff only</h2>
          <p className="footnote">The moderation queue is for site staff. Everything else is over on hosting.</p>
          <Link className="btn btn--ghost" to="/host">
            Back to hosting
          </Link>
        </section>
      </Shell>
    );

  return <ReviewQueue auth={auth} profile={profile} />;
}

function Shell({ user, children }) {
  return (
    <div className="page page--wide">
      {/* §F: identity + cross-links moved to the SiteNav. Wide shell: the
          Library and Users tables want the room. */}
      <h1 className="h1">Moderation</h1>
      {children}
    </div>
  );
}

/* ---------------- Queue ---------------- */

function ReviewQueue({ auth, profile }) {
  const [tab, setTab] = useState("questions"); // questions | categories | flagged | library | users | themes
  const [items, setItems] = useState(null); // pending items for the active QUEUE tab
  const [counts, setCounts] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [toast, setToast] = useState(null);
  const queueTab = tab === "questions" || tab === "categories" || tab === "flagged";

  const refresh = useCallback(async () => {
    if (!queueTab) return; // §I5/§J3: Library and Users manage their own data
    try {
      // §K2: the Flagged tab has its own endpoint; the counts payload keeps
      // its pinned pre-#8 shape, so the tab count comes from the list itself.
      const [list, c] = await Promise.all([
        tab === "flagged"
          ? api.moderationFlags(auth.token).then((r) => r.results)
          : api.moderationList(auth.token, tab),
        api.moderationCounts(auth.token),
      ]);
      setItems(list);
      setCounts(c);
      setLoadError(null);
    } catch (err) {
      setLoadError(errorText(err));
    }
  }, [auth.token, tab, queueTab]);

  useEffect(() => {
    setItems(null);
    setLoadError(null);
    refresh();
  }, [refresh]);

  // On action: card leaves the list, counts drop, and a background refresh
  // keeps us honest if another reviewer is working the same queue.
  const onActed = (id, message) => {
    setItems((list) => (list ?? []).filter((it) => it.id !== id));
    setCounts((c) => (c ? { ...c, [tab]: Math.max(0, (c[tab] ?? 1) - 1) } : c));
    if (message) setToast(message);
  };

  const onConflict = (id) => {
    // 409: someone else already actioned it — drop the card and resync.
    onActed(id, "Already reviewed by someone else — removed from your queue.");
    refresh();
  };

  return (
    <Shell user={profile}>
      <div className="tabs">
        <button className={`tab ${tab === "questions" ? "tab--on" : ""}`} onClick={() => setTab("questions")}>
          Questions{counts ? ` (${counts.questions})` : ""}
        </button>
        <button className={`tab ${tab === "categories" ? "tab--on" : ""}`} onClick={() => setTab("categories")}>
          Categories{counts ? ` (${counts.categories})` : ""}
        </button>
        <button className={`tab ${tab === "flagged" ? "tab--on" : ""}`} onClick={() => setTab("flagged")}>
          Flagged{items && tab === "flagged" ? ` (${items.length})` : ""}
        </button>
        <button className={`tab ${tab === "library" ? "tab--on" : ""}`} onClick={() => setTab("library")}>
          Library
        </button>
        <button className={`tab ${tab === "users" ? "tab--on" : ""}`} onClick={() => setTab("users")}>
          Users
        </button>
        <button className={`tab ${tab === "themes" ? "tab--on" : ""}`} onClick={() => setTab("themes")}>
          Themes
        </button>
      </div>

      {tab === "library" && <LibraryTab auth={auth} onToast={setToast} />}
      {tab === "users" && <UsersTab auth={auth} onToast={setToast} />}
      {tab === "themes" && <ThemesTab auth={auth} onToast={setToast} />}

      {queueTab && (
        <>
          {loadError && <p className="formerror formerror--block">{loadError}</p>}
          {!items && !loadError && <p>Loading the queue…</p>}
          {items?.length === 0 && (
            <section className="panel panel--center">
              <p className="footnote">
                {tab === "flagged" ? "No open flags — the regulars approve. 🍻" : "Nothing pending — the chalkboard is clean. 🧽"}
              </p>
            </section>
          )}

          {items?.map((item) =>
            tab === "questions" ? (
              <QuestionCard key={item.id} auth={auth} item={item} onActed={onActed} onConflict={onConflict} onToast={setToast} />
            ) : tab === "categories" ? (
              <CategoryCard key={item.id} auth={auth} item={item} onActed={onActed} onConflict={onConflict} onToast={setToast} />
            ) : (
              <FlaggedCard key={item.id} auth={auth} item={item} onActed={onActed} onConflict={onConflict} onToast={setToast} />
            ),
          )}
        </>
      )}

      <Toast message={toast} tone="info" onDone={() => setToast(null)} />
    </Shell>
  );
}

/* ---------------- Cards ---------------- */

function Owner({ item }) {
  const who = item.owner_display_name || item.owner_email || "official";
  return (
    <span className="modcard__owner" title={item.owner_email || ""}>
      by {who}
    </span>
  );
}

function ReviewActions({ auth, kind, item, onActed, onConflict, onToast }) {
  const [rejecting, setRejecting] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const run = async (fn, doneMsg) => {
    setBusy(true);
    try {
      await fn();
      onActed(item.id, doneMsg);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) onConflict(item.id);
      else onToast(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  if (rejecting)
    return (
      <div className="modcard__reject">
        <label className="field">
          Why is this being rejected? <span className="field__hint">(the owner sees this note)</span>
          <textarea value={note} onChange={(e) => setNote(e.target.value)} rows={2} maxLength={500} autoFocus />
        </label>
        <div className="modcard__actions">
          <button
            className="btn btn--danger btn--sm"
            disabled={busy || !note.trim()}
            onClick={() => run(() => api.moderationReject(auth.token, kind, item.id, note.trim()), "Rejected with note.")}
          >
            {busy ? "Rejecting…" : "Reject with note"}
          </button>
          <button className="btn btn--ghost btn--sm" disabled={busy} onClick={() => setRejecting(false)}>
            Cancel
          </button>
        </div>
      </div>
    );

  return (
    <div className="modcard__actions">
      <button
        className="btn btn--good btn--sm"
        disabled={busy}
        onClick={() => run(() => api.moderationApprove(auth.token, kind, item.id), "Approved — now publicly listed.")}
      >
        {busy ? "Approving…" : "Approve"}
      </button>
      <button className="btn btn--ghost btn--sm" disabled={busy} onClick={() => setRejecting(true)}>
        Reject…
      </button>
    </div>
  );
}

function QuestionCard({ auth, item, onActed, onConflict, onToast }) {
  return (
    <section className="panel modcard">
      <div className="modcard__meta">
        {/* §F4: a question can live in several categories now. */}
        <span className="modcard__cat">{(item.category_names ?? []).join(" · ")}</span>
        <span>difficulty {item.difficulty}</span>
        <span>{item.visibility}</span>
        {item.usage_count != null && <UsageBadge count={item.usage_count} />}
        <Owner item={item} />
        <span className="modcard__when">{new Date(item.created_at).toLocaleString()}</span>
      </div>
      <p className="modcard__q">{item.question_text}</p>
      <p className="modcard__a">
        Answer: <strong>{item.answer}</strong>
      </p>
      {/* Render media exactly as players will see it — the point of the in-app queue. */}
      {item.media_type === "image" && item.image && (
        <img className="qmedia qmedia--img" src={mediaUrl(item.image)} alt="Question media under review" />
      )}
      {item.media_type === "audio" && item.audio && <audio className="qmedia" src={mediaUrl(item.audio)} controls />}
      {item.media_type === "video" && item.video && (
        <video className="qmedia qmedia--video" src={mediaUrl(item.video)} controls />
      )}
      <SimilarMatches auth={auth} questionId={item.id} />
      <ReviewActions auth={auth} kind="questions" item={item} onActed={onActed} onConflict={onConflict} onToast={onToast} />
    </section>
  );
}

function CategoryCard({ auth, item, onActed, onConflict, onToast }) {
  return (
    <section className="panel modcard">
      <div className="modcard__meta">
        <span>category</span>
        <span>{item.visibility}</span>
        {item.usage_count != null && <UsageBadge count={item.usage_count} noun="game" />}
        <Owner item={item} />
        <span className="modcard__when">{new Date(item.created_at).toLocaleString()}</span>
      </div>
      <div className="modcard__cathead">
        {item.photo && <img className="modcard__photo" src={mediaUrl(item.photo)} alt="" />}
        <p className="modcard__q">{item.name}</p>
      </div>
      {item.description && <p className="footnote">{item.description}</p>}
      <p className="footnote">
        {item.usable_question_count} usable question{item.usable_question_count === 1 ? "" : "s"} — questions are
        reviewed separately on the other tab.
      </p>
      <ReviewActions auth={auth} kind="categories" item={item} onActed={onActed} onConflict={onConflict} onToast={onToast} />
    </section>
  );
}


/** §J1: "used in N games / questions asked N times" chip on review cards. */
function UsageBadge({ count, noun = "time" }) {
  return (
    <span className="usagebadge" title="How often this has appeared in games (all hosts)">
      🎲 {noun === "game" ? `in ${count} game${count === 1 ? "" : "s"}` : `played ${count} ${noun}${count === 1 ? "" : "s"}`}
    </span>
  );
}

/**
 * §K1: nearest existing approved questions, fetched per card (its own
 * endpoint keeps the queue list snappy). A review AID — nothing here acts.
 * #14c: the fetches are GATED. A full queue page mounts up to 50 cards
 * (PAGE_SIZE) and HTTP/2 fires every per-card /similar simultaneously —
 * a thundering herd that pinned 50+ DB connections at once and, with a
 * mid-load reload stacking a second herd (the server keeps working on
 * abandoned requests), hit Postgres' 100-connection cap. At most
 * MAX_CONCURRENT_SIMILAR run at a time; cards still fill progressively
 * top-to-bottom, and the whole page finishes FASTER because the calls no
 * longer fight each other for CPU and connections.
 */
const MAX_CONCURRENT_SIMILAR = 6;
let similarInFlight = 0;
const similarQueue = [];
function acquireSimilarSlot() {
  if (similarInFlight < MAX_CONCURRENT_SIMILAR) {
    similarInFlight += 1;
    return Promise.resolve();
  }
  // Full: wait. The slot is handed over directly in releaseSimilarSlot()
  // (no decrement/increment gap), so the cap can never be overshot.
  return new Promise((resolve) => similarQueue.push(resolve));
}
function releaseSimilarSlot() {
  const next = similarQueue.shift();
  if (next) next(); // pass the slot straight to the next waiter
  else similarInFlight -= 1;
}

function SimilarMatches({ auth, questionId }) {
  const [similar, setSimilar] = useState(null);
  useEffect(() => {
    let alive = true;
    (async () => {
      await acquireSimilarSlot();
      if (!alive) {
        releaseSimilarSlot(); // unmounted while queued (e.g. tab switch)
        return;
      }
      try {
        const r = await api.moderationSimilar(auth.token, questionId);
        if (alive) setSimilar(r.similar);
      } catch {
        if (alive) setSimilar([]); // the aid is optional; approve/reject still work
      } finally {
        releaseSimilarSlot();
      }
    })();
    return () => {
      alive = false;
    };
  }, [auth.token, questionId]);

  if (!similar) return <p className="footnote">Checking for lookalikes…</p>;
  if (similar.length === 0) return <p className="footnote">No similar approved questions found. ✨</p>;
  return (
    <div className="similar">
      <span className="similar__title">Possible duplicates already approved:</span>
      <ul className="similar__list">
        {similar.map((m) => (
          <li key={m.id} className="similar__row">
            <span className="similar__score">{Math.round(m.score * 100)}%</span>
            <span className="similar__text">
              {m.question_text} <em className="similar__answer">→ {m.answer}</em>
            </span>
            <span className="similar__usage">
              {(m.category_names ?? []).join(" · ")} · played {m.usage_count}×
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * §K2: one flagged question in the "Flagged" tab — the question with its
 * open reports, §J1 usage and §K1 lookalikes, resolved by Dismiss (keep) or
 * Reject-with-note. The copy documents what rejection actually does; games
 * already containing the question keep their copy.
 */
function FlaggedCard({ auth, item, onActed, onConflict, onToast }) {
  const [rejecting, setRejecting] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const resolve = async (action, resolveNote) => {
    setBusy(true);
    try {
      await api.moderationFlagResolve(auth.token, item.id, action, resolveNote);
      onActed(item.id, action === "dismiss" ? "Flags dismissed — the question stays." : "Rejected — unlisted from public play.");
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) onConflict(item.id);
      else onToast(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel modcard modcard--flagged">
      <div className="modcard__meta">
        <span className="modcard__cat">{(item.category_names ?? []).join(" · ")}</span>
        <span className="flagcount">🚩 {item.report_count} open flag{item.report_count === 1 ? "" : "s"}</span>
        <span className={`modbadge modbadge--${item.moderation_status}`}>{item.moderation_status}</span>
        {item.usage_count != null && <UsageBadge count={item.usage_count} />}
        <Owner item={item} />
      </div>
      <p className="modcard__q">{item.question_text}</p>
      <p className="modcard__a">
        Answer: <strong>{item.answer}</strong>
      </p>
      <ul className="flagreasons">
        {item.reports.map((r) => (
          <li key={r.id}>
            <strong>{r.reporter_display_name || r.reporter_email}</strong>
            {r.reason ? `: ${r.reason}` : " (no reason given)"}
          </li>
        ))}
      </ul>
      <SimilarMatches auth={auth} questionId={item.id} />
      {rejecting ? (
        <div className="modcard__reject">
          <label className="field">
            Why is this being rejected? <span className="field__hint">(the owner sees this note)</span>
            <textarea value={note} onChange={(e) => setNote(e.target.value)} rows={2} maxLength={500} autoFocus />
          </label>
          <div className="modcard__actions">
            <button
              className="btn btn--danger btn--sm"
              disabled={busy || !note.trim()}
              onClick={() => resolve("reject", note.trim())}
            >
              {busy ? "Rejecting…" : "Reject with note"}
            </button>
            <button className="btn btn--ghost btn--sm" disabled={busy} onClick={() => setRejecting(false)}>
              Cancel
            </button>
          </div>
          <p className="footnote">
            Rejecting unlists it from public categories and future game builds. Games already built
            keep their copy of the question.
          </p>
        </div>
      ) : (
        <div className="modcard__actions">
          <button className="btn btn--good btn--sm" disabled={busy} onClick={() => resolve("dismiss")}>
            {busy ? "Working…" : "Dismiss flags (keep it)"}
          </button>
          <button className="btn btn--ghost btn--sm" disabled={busy} onClick={() => setRejecting(true)}>
            Reject…
          </button>
        </div>
      )}
    </section>
  );
}

/* ---------------- §I5 (Handoff #9): the Library tab ---------------- */

/** Debounce a value; the library search waits ~300ms of quiet. */
function useDebounced(value, ms = 300) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return debounced;
}

/**
 * Every question on the site — search + filters + REAL pagination (the
 * backend pages at 25; this tab never uses allPages, that's the whole
 * "thousands of questions" point). Per-row: inline Edit (→ revise/, which
 * soft-deletes the old row and creates a new approved one — the row swaps
 * in place) and Delete (soft, inline confirm). ?deleted=only shows the
 * graveyard, read-only.
 */
function LibraryTab({ auth, onToast }) {
  const [search, setSearch] = useState("");
  const debouncedSearch = useDebounced(search);
  const [category, setCategory] = useState("");
  const [status, setStatus] = useState("all");
  const [deleted, setDeleted] = useState("active");
  const [ordering, setOrdering] = useState("-created_at");
  const [page, setPage] = useState(1);
  const [data, setData] = useState(null); // {results, count, next, previous}
  const [error, setError] = useState(null);
  const [categories, setCategories] = useState(null);
  const seq = useRef(0);

  // Category options for the filter select (small list; allPages is fine HERE).
  useEffect(() => {
    api.categories(auth.token).then(setCategories).catch(() => setCategories([]));
  }, [auth.token]);

  // Any filter change resets to page 1.
  useEffect(() => {
    setPage(1);
  }, [debouncedSearch, category, status, deleted, ordering]);

  useEffect(() => {
    const mySeq = ++seq.current; // stale-response guard for fast typists
    api
      .moderationLibrary(auth.token, {
        search: debouncedSearch,
        category,
        status,
        deleted,
        ordering,
        page,
      })
      .then((d) => {
        if (seq.current !== mySeq) return;
        setData(d);
        setError(null);
      })
      .catch((err) => seq.current === mySeq && setError(errorText(err)));
  }, [auth.token, debouncedSearch, category, status, deleted, ordering, page]);

  const pageSize = 25;
  const pageCount = data ? Math.max(1, Math.ceil(data.count / pageSize)) : 1;

  const replaceRow = (oldId, newRow) =>
    setData((d) => ({ ...d, results: d.results.map((r) => (r.id === oldId ? newRow : r)) }));

  const markDeleted = (row) => replaceRow(row.id, { ...row, deleted_at: new Date().toISOString() });

  return (
    <>
      <section className="panel libfilters">
        <input
          className="libfilters__search"
          placeholder="Search question text or answers…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <label className="libfilters__field">
          Category
          <select value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="">all</option>
            {categories?.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </label>
        <label className="libfilters__field">
          Status
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="all">all</option>
            <option value="approved">approved</option>
            <option value="pending">pending</option>
            <option value="rejected">rejected</option>
            <option value="not_submitted">not submitted</option>
          </select>
        </label>
        <label className="libfilters__field">
          Deleted
          <select value={deleted} onChange={(e) => setDeleted(e.target.value)}>
            <option value="active">hide deleted</option>
            <option value="only">deleted only</option>
            <option value="all">everything</option>
          </select>
        </label>
        <label className="libfilters__field">
          Order
          <select value={ordering} onChange={(e) => setOrdering(e.target.value)}>
            <option value="-created_at">newest first</option>
            <option value="created_at">oldest first</option>
            <option value="-usage_count">most played</option>
            <option value="usage_count">least played</option>
          </select>
        </label>
      </section>

      {error && <p className="formerror formerror--block">{error}</p>}
      {!data && !error && <p>Loading the library…</p>}
      {data?.results.length === 0 && (
        <section className="panel panel--center">
          <p className="footnote">No questions match those filters.</p>
        </section>
      )}

      {data?.results.map((row) => (
        <LibraryRow
          key={`${row.id}-${row.deleted_at || "live"}`}
          auth={auth}
          row={row}
          onToast={onToast}
          onRevised={(newRow) => replaceRow(row.id, newRow)}
          onDeleted={() => markDeleted(row)}
        />
      ))}

      {data && data.count > 0 && (
        <div className="libpager">
          <button className="btn btn--ghost btn--sm" disabled={!data.previous} onClick={() => setPage((p) => p - 1)}>
            ← Prev
          </button>
          <span className="libpager__where">
            page {page} of {pageCount} · {data.count} question{data.count === 1 ? "" : "s"}
          </span>
          <button className="btn btn--ghost btn--sm" disabled={!data.next} onClick={() => setPage((p) => p + 1)}>
            Next →
          </button>
        </div>
      )}
    </>
  );
}

function LibraryRow({ auth, row, onToast, onRevised, onDeleted }) {
  const [editing, setEditing] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [busy, setBusy] = useState(false);
  const isDeleted = !!row.deleted_at;

  const del = async () => {
    setBusy(true);
    try {
      await api.moderationDelete(auth.token, row.id);
      onDeleted();
      onToast("Deleted — gone from listings and future games; past games keep their copy.");
    } catch (err) {
      onToast(err?.status === 409 ? "Already deleted by someone else." : errorText(err));
      if (err?.status === 409) onDeleted();
    } finally {
      setBusy(false);
      setConfirmDelete(false);
    }
  };

  return (
    <section className={`panel librow ${isDeleted ? "librow--deleted" : ""}`}>
      <div className="librow__main">
        <div className="librow__meta">
          <span className="modcard__cat">{(row.category_names ?? []).join(" · ")}</span>
          <span className={`modbadge modbadge--${row.moderation_status}`}>{row.moderation_status}</span>
          {isDeleted && <span className="modbadge modbadge--deleted">deleted</span>}
          {isDeleted && row.replaced_by && <span className="librow__superseded">superseded by #{row.replaced_by}</span>}
          <span>difficulty {row.difficulty}</span>
          {row.usage_count != null && <UsageBadge count={row.usage_count} />}
          <Owner item={row} />
          <span className="modcard__when">
            {isDeleted
              ? `deleted ${new Date(row.deleted_at).toLocaleString()}`
              : new Date(row.created_at).toLocaleDateString()}
          </span>
        </div>
        <p className="librow__q" title={row.question_text}>
          {row.question_text}
        </p>
        <p className="librow__a">→ {row.answer}</p>
      </div>
      {!isDeleted && !editing && (
        <div className="librow__actions">
          <button className="btn btn--ghost btn--sm" onClick={() => setEditing(true)}>
            ✏️ Edit
          </button>
          {confirmDelete ? (
            <span className="confirmrow">
              <button className="btn btn--danger btn--sm" disabled={busy} onClick={del}>
                {busy ? "Deleting…" : "Confirm delete"}
              </button>
              <button className="btn btn--ghost btn--sm" disabled={busy} onClick={() => setConfirmDelete(false)}>
                Cancel
              </button>
            </span>
          ) : (
            <button className="btn btn--ghost btn--sm" onClick={() => setConfirmDelete(true)}>
              🗑 Delete
            </button>
          )}
        </div>
      )}
      {editing && (
        <ReviseForm
          auth={auth}
          row={row}
          onCancel={() => setEditing(false)}
          onDone={(newRow) => {
            setEditing(false);
            onRevised(newRow);
            onToast("Revised — the old version was retired; new games draw the new text.");
          }}
          onToast={onToast}
        />
      )}
    </section>
  );
}

function ReviseForm({ auth, row, onCancel, onDone, onToast }) {
  const [text, setText] = useState(row.question_text);
  const [answer, setAnswer] = useState(row.answer);
  const [difficulty, setDifficulty] = useState(row.difficulty);
  const [visibility, setVisibility] = useState(row.visibility);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const newRow = await api.moderationRevise(auth.token, row.id, {
        question_text: text,
        answer,
        difficulty: Number(difficulty),
        visibility,
      });
      onDone(newRow);
    } catch (err) {
      if (err?.status === 409) {
        onToast("Someone else already revised or deleted this one — refresh the library.");
        onCancel();
      } else {
        setError(errorText(err));
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="reviseform" onSubmit={submit}>
      <label className="field">
        Question text
        <textarea value={text} onChange={(e) => setText(e.target.value)} rows={2} required />
      </label>
      <label className="field">
        Answer
        <input value={answer} onChange={(e) => setAnswer(e.target.value)} maxLength={500} required />
      </label>
      <div className="reviseform__row">
        <label className="field">
          Difficulty
          <select value={difficulty} onChange={(e) => setDifficulty(e.target.value)}>
            {[1, 2, 3, 4, 5].map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          Visibility
          <select value={visibility} onChange={(e) => setVisibility(e.target.value)}>
            <option value="public">public</option>
            <option value="private">private</option>
          </select>
        </label>
      </div>
      {error && <p className="formerror">{error}</p>}
      <div className="modcard__actions">
        <button className="btn btn--primary btn--sm" disabled={busy}>
          {busy ? "Saving…" : "Save as new version"}
        </button>
        <button type="button" className="btn btn--ghost btn--sm" disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </div>
      <p className="footnote">
        Saving retires the current version (games already built keep it) and publishes this text as a new,
        already-approved question.
      </p>
    </form>
  );
}

/* ---------------- §J3 (Handoff #9): the Users tab ---------------- */

const OVERRIDE_FIELDS = [
  { key: "games_per_month", label: "games / month", usageKey: "games_this_month" },
  { key: "categories", label: "categories", usageKey: "categories" },
  { key: "questions", label: "questions", usageKey: "questions" },
  { key: "storage_bytes", label: "storage (bytes)", usageKey: "storage" },
];

function UsersTab({ auth, onToast }) {
  const [search, setSearch] = useState("");
  const debouncedSearch = useDebounced(search);
  const [plan, setPlan] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const seq = useRef(0);

  useEffect(() => {
    setPage(1);
  }, [debouncedSearch, plan]);

  useEffect(() => {
    const mySeq = ++seq.current;
    api
      .adminUsers(auth.token, { search: debouncedSearch, plan, page })
      .then((d) => {
        if (seq.current !== mySeq) return;
        setData(d);
        setError(null);
      })
      .catch((err) => seq.current === mySeq && setError(errorText(err)));
  }, [auth.token, debouncedSearch, plan, page]);

  const pageCount = data ? Math.max(1, Math.ceil(data.count / 25)) : 1;

  return (
    <>
      <section className="panel libfilters">
        <input
          className="libfilters__search"
          placeholder="Search email or display name…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <label className="libfilters__field">
          Plan
          <select value={plan} onChange={(e) => setPlan(e.target.value)}>
            <option value="">all</option>
            <option value="free">free</option>
            <option value="creator">creator</option>
          </select>
        </label>
      </section>

      <p className="footnote">
        Billing: not wired yet — there is no payment system, so plans are set manually right here. Demo access:
        creator + an expiry (it lapses back to free automatically).
      </p>

      {error && <p className="formerror formerror--block">{error}</p>}
      {!data && !error && <p>Loading users…</p>}
      {data?.results.length === 0 && (
        <section className="panel panel--center">
          <p className="footnote">Nobody matches that.</p>
        </section>
      )}

      {data?.results.map((u) => (
        <UserRow
          key={u.id}
          auth={auth}
          user={u}
          onToast={onToast}
          onSaved={(fresh) =>
            setData((d) => ({ ...d, results: d.results.map((r) => (r.id === fresh.id ? fresh : r)) }))
          }
        />
      ))}

      {data && data.count > 0 && (
        <div className="libpager">
          <button className="btn btn--ghost btn--sm" disabled={!data.previous} onClick={() => setPage((p) => p - 1)}>
            ← Prev
          </button>
          <span className="libpager__where">
            page {page} of {pageCount} · {data.count} user{data.count === 1 ? "" : "s"}
          </span>
          <button className="btn btn--ghost btn--sm" disabled={!data.next} onClick={() => setPage((p) => p + 1)}>
            Next →
          </button>
        </div>
      )}
    </>
  );
}

/** ISO datetime → the value a <input type="date"> wants (or ""). */
const isoToDateInput = (iso) => (iso ? iso.slice(0, 10) : "");

function UserRow({ auth, user, onToast, onSaved }) {
  const [open, setOpen] = useState(false);
  const [plan, setPlan] = useState(user.plan);
  const [expiry, setExpiry] = useState(isoToDateInput(user.plan_expires_at));
  // overrides: key -> {unlimited: bool, value: string} — "" means "no override".
  const [overrides, setOverrides] = useState(() => {
    const state = {};
    for (const f of OVERRIDE_FIELDS) {
      const v = user.limit_overrides?.[f.key];
      state[f.key] =
        v === undefined ? { unlimited: false, value: "" } : v === null ? { unlimited: true, value: "" } : { unlimited: false, value: String(v) };
    }
    return state;
  });
  const [busy, setBusy] = useState(false);
  // §H (#11): brand oversight — staff can blank a bad name (rides the normal
  // Save PATCH) and clear a bad logo (its own immediate PATCH below). Both
  // keys are on the server's whitelist; staff never UPLOAD a logo.
  const [brandName, setBrandName] = useState(user.brand_name ?? "");
  const [clearArming, setClearArming] = useState(false);

  const lapsed = user.plan !== "free" && user.effective_plan === "free";

  const clearLogo = async () => {
    setBusy(true);
    setClearArming(false);
    try {
      const fresh = await api.adminUserPatch(auth.token, user.id, { brand_logo_clear: true });
      onSaved(fresh);
      onToast(`Cleared ${fresh.email}'s brand logo.`);
    } catch (err) {
      onToast(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    setBusy(true);
    try {
      const builtOverrides = {};
      for (const f of OVERRIDE_FIELDS) {
        const o = overrides[f.key];
        if (o.unlimited) builtOverrides[f.key] = null;
        else if (o.value !== "") builtOverrides[f.key] = Number(o.value);
      }
      const fresh = await api.adminUserPatch(auth.token, user.id, {
        plan,
        plan_expires_at: expiry ? new Date(`${expiry}T23:59:59`).toISOString() : null,
        limit_overrides: builtOverrides,
        brand_name: brandName, // §H: staff may blank/fix a venue's brand name
      });
      onSaved(fresh);
      onToast(`Saved ${fresh.email} — plan ${fresh.plan}${fresh.plan_expires_at ? " (expiring)" : ""}.`);
    } catch (err) {
      onToast(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel userrow">
      <button type="button" className="userrow__head" onClick={() => setOpen((o) => !o)}>
        <span className="userrow__id">
          <strong>{user.display_name || user.email}</strong>
          <span className="userrow__mail">{user.email}</span>
        </span>
        <span className="userrow__chips">
          {user.is_staff && <span className="modbadge modbadge--staff">staff</span>}
          <span className={`planchip ${user.effective_plan !== "free" ? "planchip--paid" : ""}`}>
            {user.plan}
            {lapsed ? " (lapsed → free)" : ""}
          </span>
          {Object.keys(user.limit_overrides || {}).length > 0 && <span className="modbadge">custom limits</span>}
          <span className="userrow__chev">{open ? "▾" : "▸"}</span>
        </span>
      </button>

      {open && (
        <div className="userrow__body">
          <p className="footnote">
            Joined {new Date(user.date_joined).toLocaleDateString()} · staff status is read-only here (Django admin
            only) · billing isn't wired — this panel IS the payment override.
          </p>
          <div className="userrow__plan">
            <label className="field">
              Plan
              <select value={plan} onChange={(e) => setPlan(e.target.value)}>
                <option value="free">free</option>
                <option value="creator">creator</option>
              </select>
            </label>
            <label className="field">
              Plan expires <span className="field__hint">(blank = never)</span>
              <input type="date" value={expiry} onChange={(e) => setExpiry(e.target.value)} />
            </label>
          </div>
          <p className="footnote">Demo access: creator + an expiry — it lapses to free automatically.</p>

          <h3 className="h2">Allowances</h3>
          <div className="userrow__overrides">
            {OVERRIDE_FIELDS.map((f) => {
              const o = overrides[f.key];
              const usage = user.usage?.[f.usageKey];
              return (
                <div key={f.key} className="overridefield">
                  <span className="overridefield__label">{f.label}</span>
                  <input
                    type="number"
                    min="0"
                    disabled={o.unlimited}
                    value={o.value}
                    placeholder={usage?.limit == null ? "plan: unlimited" : `plan: ${usage.limit}`}
                    onChange={(e) =>
                      setOverrides((s) => ({ ...s, [f.key]: { ...s[f.key], value: e.target.value } }))
                    }
                  />
                  <label className="overridefield__unlimited">
                    <input
                      type="checkbox"
                      checked={o.unlimited}
                      onChange={(e) =>
                        setOverrides((s) => ({
                          ...s,
                          [f.key]: { unlimited: e.target.checked, value: "" },
                        }))
                      }
                    />
                    unlimited
                  </label>
                  <span className="overridefield__usage">{usage ? `${usage.used} used` : ""}</span>
                </div>
              );
            })}
          </div>
          <p className="footnote">
            The number shown grey is the plan default; typing a value overrides it for this user only (and survives
            plan changes). Blank = plan default.
          </p>
          <h3 className="h2">Branding</h3>
          <div className="userrow__brand">
            <label className="field">
              Brand name <span className="field__hint">(shown on their game's TV — blank to remove)</span>
              <input value={brandName} maxLength={60} onChange={(e) => setBrandName(e.target.value)} />
            </label>
            {user.brand_logo ? (
              <div className="userrow__brandlogo">
                <img src={mediaUrl(user.brand_logo)} alt="Brand logo" className="brandlogo brandlogo--thumb" />
                {clearArming ? (
                  <button type="button" className="btn btn--danger btn--sm" disabled={busy} onClick={clearLogo}>
                    Confirm clear
                  </button>
                ) : (
                  <button type="button" className="btn btn--ghost btn--sm" onClick={() => setClearArming(true)}>
                    Clear logo
                  </button>
                )}
              </div>
            ) : (
              <span className="footnote">No logo uploaded.</span>
            )}
          </div>
          <button className="btn btn--primary btn--sm" disabled={busy} onClick={save}>
            {busy ? "Saving…" : "Save"}
          </button>
        </div>
      )}
    </section>
  );
}

/* ---------------- §G4 (Handoff #10): the Themes tab ---------------- */

/**
 * Staff-curated tag table: a theme groups categories so hosts can find "all
 * music categories" in one tap on /host. List + inline create/edit (name,
 * description, category multi-select fed by api.categories) + soft delete
 * with confirm — the Library tab's row/form patterns, reused. Unpaginated
 * (staff-curated scale). Everything is IsAdminUser server-side.
 */
function ThemesTab({ auth, onToast }) {
  const [themes, setThemes] = useState(null);
  const [categories, setCategories] = useState(null);
  const [error, setError] = useState(null);
  const [creating, setCreating] = useState(false);

  const refresh = useCallback(() => {
    api
      .moderationThemes(auth.token)
      .then((list) => {
        setThemes(list);
        setError(null);
      })
      .catch((err) => setError(errorText(err)));
  }, [auth.token]);

  useEffect(() => {
    refresh();
    api.categories(auth.token).then(setCategories).catch(() => setCategories([]));
  }, [auth.token, refresh]);

  return (
    <>
      <p className="footnote">
        Themes are the host screen's one-tap category groups ("all music categories"). Official-only
        for now — creators can't author themes yet. Game creation itself never sees themes; they only
        filter and pre-select the category grid.
      </p>
      {error && <p className="formerror formerror--block">{error}</p>}
      {!themes && !error && <p>Loading themes…</p>}

      {creating ? (
        <section className="panel">
          <h2 className="h2">New theme</h2>
          <ThemeForm
            auth={auth}
            categories={categories ?? []}
            onCancel={() => setCreating(false)}
            onDone={() => {
              setCreating(false);
              onToast("Theme created.");
              refresh();
            }}
          />
        </section>
      ) : (
        <button className="btn btn--primary btn--sm" onClick={() => setCreating(true)}>
          + New theme
        </button>
      )}

      {themes?.length === 0 && (
        <section className="panel panel--center">
          <p className="footnote">No themes yet — group some categories to give hosts a head start.</p>
        </section>
      )}
      {themes?.map((theme) => (
        <ThemeRow
          key={theme.id}
          auth={auth}
          theme={theme}
          categories={categories ?? []}
          onToast={onToast}
          onChanged={refresh}
        />
      ))}
    </>
  );
}

function ThemeRow({ auth, theme, categories, onToast, onChanged }) {
  const [editing, setEditing] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [busy, setBusy] = useState(false);

  const del = async () => {
    setBusy(true);
    try {
      await api.deleteTheme(auth.token, theme.id);
      onToast(`Deleted “${theme.name}” — hosts no longer see it. Its categories are untouched.`);
      onChanged();
    } catch (err) {
      // 409: someone else beat us to it — refreshing drops the row anyway.
      onToast(err?.status === 409 ? "Already deleted by someone else." : errorText(err));
      if (err?.status === 409) onChanged();
    } finally {
      setBusy(false);
      setConfirmDelete(false);
    }
  };

  return (
    <section className="panel librow">
      <div className="librow__main">
        <div className="librow__meta">
          <span className="modcard__cat">theme</span>
          <span>
            {theme.category_names.length} categor{theme.category_names.length === 1 ? "y" : "ies"}
          </span>
          <span className="modcard__when">{new Date(theme.created_at).toLocaleDateString()}</span>
        </div>
        <p className="librow__q">{theme.name}</p>
        {theme.description && <p className="footnote">{theme.description}</p>}
        <p className="librow__a">{theme.category_names.join(" · ") || "no categories yet"}</p>
      </div>
      {!editing && (
        <div className="librow__actions">
          <button className="btn btn--ghost btn--sm" onClick={() => setEditing(true)}>
            ✏️ Edit
          </button>
          {confirmDelete ? (
            <span className="confirmrow">
              <button className="btn btn--danger btn--sm" disabled={busy} onClick={del}>
                {busy ? "Deleting…" : "Confirm delete"}
              </button>
              <button className="btn btn--ghost btn--sm" disabled={busy} onClick={() => setConfirmDelete(false)}>
                Cancel
              </button>
            </span>
          ) : (
            <button className="btn btn--ghost btn--sm" onClick={() => setConfirmDelete(true)}>
              🗑 Delete
            </button>
          )}
        </div>
      )}
      {editing && (
        <ThemeForm
          auth={auth}
          theme={theme}
          categories={categories}
          onCancel={() => setEditing(false)}
          onDone={() => {
            setEditing(false);
            onToast("Theme saved.");
            onChanged();
          }}
        />
      )}
    </section>
  );
}

/** Shared create/edit form. `theme` present = edit (PATCH), absent = create. */
function ThemeForm({ auth, theme, categories, onCancel, onDone }) {
  const [name, setName] = useState(theme?.name ?? "");
  const [description, setDescription] = useState(theme?.description ?? "");
  const [selected, setSelected] = useState(() => (theme?.categories ?? []).map(String));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const body = {
      name: name.trim(),
      description: description.trim(),
      categories: selected.map(Number),
    };
    try {
      if (theme) await api.updateTheme(auth.token, theme.id, body);
      else await api.createTheme(auth.token, body);
      onDone();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="reviseform" onSubmit={submit}>
      <label className="field">
        Name
        <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={100} />
      </label>
      <label className="field">
        Description <span className="field__hint">(optional)</span>
        <input value={description} onChange={(e) => setDescription(e.target.value)} maxLength={300} />
      </label>
      <fieldset className="field catpick">
        <legend>Categories in this theme</legend>
        <div className="catpick__list">
          {categories.map((c) => {
            const on = selected.includes(String(c.id));
            return (
              <label key={c.id} className={`catpick__item ${on ? "catpick__item--on" : ""}`}>
                <input
                  type="checkbox"
                  checked={on}
                  onChange={() =>
                    setSelected((sel) =>
                      on ? sel.filter((x) => x !== String(c.id)) : [...sel, String(c.id)],
                    )
                  }
                />
                {c.name}
                {c.owner === null ? " (official)" : ""}
              </label>
            );
          })}
        </div>
      </fieldset>
      {error && <p className="formerror">{error}</p>}
      <div className="modcard__actions">
        <button className="btn btn--primary btn--sm" disabled={busy || !name.trim()}>
          {busy ? "Saving…" : theme ? "Save theme" : "Create theme"}
        </button>
        <button type="button" className="btn btn--ghost btn--sm" disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}
