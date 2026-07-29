import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, errorText, mediaUrl } from "../lib/api";
import { clearAuth, loadAuth } from "../lib/storage";
import { Toast } from "../components/shared";

/**
 * /moderate — staff review queue (Handoff #4 §F3).
 *
 * Shows each pending item exactly as players will see it (media rendered via
 * mediaUrl(), answers visible) with Approve / Reject-with-note. The is_staff
 * gate here is cosmetic: every /api/moderation/* endpoint is IsAdminUser
 * server-side. Plain REST + refresh — no WS involvement.
 */

export default function ModeratePage() {
  const [auth, setAuth] = useState(loadAuth());
  const [profile, setProfile] = useState(null);
  const [profileError, setProfileError] = useState(null);

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
          <Link className="btn btn--primary" to="/host">
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
    <div className="page">
      <header className="pagehead">
        <Link to="/" className="wordmark wordmark--small wordmark--link">
          DRINKQUIRY
        </Link>
        <div className="pagehead__right">
          {user && <span className="pagehead__user">{user.display_name || user.email}</span>}
          <Link className="btn btn--ghost" to="/create">
            Your content
          </Link>
          <Link className="btn btn--ghost" to="/host">
            Host a game
          </Link>
        </div>
      </header>
      <h1 className="h1">Moderation queue</h1>
      {children}
    </div>
  );
}

/* ---------------- Queue ---------------- */

function ReviewQueue({ auth, profile }) {
  const [tab, setTab] = useState("questions"); // "questions" | "categories" | "flagged"
  const [items, setItems] = useState(null); // pending items for the active tab
  const [counts, setCounts] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [toast, setToast] = useState(null);

  const refresh = useCallback(async () => {
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
  }, [auth.token, tab]);

  useEffect(() => {
    setItems(null);
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
      </div>

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
        <span className="modcard__cat">{item.category_name}</span>
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
 */
function SimilarMatches({ auth, questionId }) {
  const [similar, setSimilar] = useState(null);
  useEffect(() => {
    let alive = true;
    api
      .moderationSimilar(auth.token, questionId)
      .then((r) => alive && setSimilar(r.similar))
      .catch(() => alive && setSimilar([])); // the aid is optional; approve/reject still work
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
              {m.category_name} · played {m.usage_count}×
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
        <span className="modcard__cat">{item.category_name}</span>
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
