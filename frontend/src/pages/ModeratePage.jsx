import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, errorText, mediaUrl } from "../lib/api";
import StaffGate, { Owner, UsageBadge } from "../components/StaffGate";
import { Toast } from "../components/shared";

/**
 * /moderate — the staff REVIEW QUEUES (Handoff #4 §F3, grown every handoff
 * since; §F3 of Handoff #16 split the old six-tab page into four dropdown
 * destinations, per the owner's "one at a time"). What lives HERE is the
 * tightly-coupled review surface: the pending Questions/Categories queues
 * (SERVER-PAGED since #15 — the fetch-all sweep here is what detonated
 * #14c at 500 pending) and the Flagged worklist (its own unpaginated
 * endpoint, kept near-empty by design). The Library, Users and Themes tabs
 * moved to /manage/library, /manage/users and /manage/themes — each its
 * own page under the same StaffGate wrapper, which now carries the
 * auth/staff funnel this file used to open with.
 *
 * Duplicate-check lines arrive ONE BATCH PER PAGE (§F2 #15), not per card.
 * The is_staff gate is cosmetic: every /api/moderation/* endpoint is
 * IsAdminUser server-side. Plain REST + refresh — no WS involvement.
 */

export default function ModeratePage() {
  return (
    <StaffGate title="Moderation" next="/moderate">
      {({ auth }) => <ReviewQueue auth={auth} />}
    </StaffGate>
  );
}

/* ---------------- Queue ---------------- */

// Mirrors the server's page sizes so the pager can say "page X of Y":
// questions ride LibraryPagination (25), categories the global PAGE_SIZE
// (50) — same house pattern as the library page's hardcoded 25.
const QUEUE_PAGE_SIZE = { questions: 25, categories: 50 };

function ReviewQueue({ auth }) {
  const [tab, setTab] = useState("questions"); // questions | categories | flagged
  const [page, setPage] = useState(1); // paged queue tabs only
  // Paged tabs: the DRF envelope {results, count, next, previous};
  // flagged: normalized to {results} (its endpoint's own shape).
  const [data, setData] = useState(null);
  const [counts, setCounts] = useState(null);
  // §F2 (#15): the §K1 duplicate-check aid, ONE BATCH PER PAGE — id → rows;
  // a missing key means "still checking". Replaces the per-card fetches and
  // the #14c 6-slot gate entirely (superseded: with one request per page
  // there is no herd left to throttle).
  const [similarByQ, setSimilarByQ] = useState({});
  const [loadError, setLoadError] = useState(null);
  const [toast, setToast] = useState(null);
  const seq = useRef(0); // stale-response guard (tab hops, page hops, slow batches)
  const pagedTab = tab === "questions" || tab === "categories";

  // Changing tab always restarts at page 1.
  useEffect(() => {
    setPage(1);
  }, [tab]);

  const refresh = useCallback(async () => {
    const mySeq = ++seq.current;
    try {
      // §K2: the Flagged tab has its own endpoint; the counts payload keeps
      // its pinned pre-#8 shape, so the tab count comes from the list itself.
      const [list, c] = await Promise.all([
        tab === "flagged"
          ? api.moderationFlags(auth.token)
          : api.moderationPage(tab, auth.token, { page }),
        api.moderationCounts(auth.token),
      ]);
      if (seq.current !== mySeq) return;
      setData(list);
      setCounts(c);
      setLoadError(null);
      // Question-bearing tabs get their duplicate-check lines page-at-a-time.
      if (tab === "questions" || tab === "flagged") {
        setSimilarByQ({});
        const ids = (list.results ?? []).map((it) => it.id);
        // The endpoint caps a batch at 50; the flagged worklist is
        // unpaginated so chunk defensively (queue pages are 25/50 anyway).
        for (let i = 0; i < ids.length; i += 50) {
          const chunk = ids.slice(i, i + 50);
          try {
            const res = await api.moderationSimilarBatch(auth.token, chunk);
            if (seq.current !== mySeq) return; // page/tab moved on mid-batch
            setSimilarByQ((m) => {
              const next = { ...m };
              for (const id of chunk) next[id] = res[String(id)] ?? [];
              return next;
            });
          } catch {
            if (seq.current !== mySeq) return;
            // The aid is optional; approve/reject still work.
            setSimilarByQ((m) => {
              const next = { ...m };
              for (const id of chunk) next[id] = [];
              return next;
            });
          }
        }
      }
    } catch (err) {
      if (seq.current === mySeq) setLoadError(errorText(err));
    }
  }, [auth.token, tab, page]);

  useEffect(() => {
    setData(null);
    setLoadError(null);
    refresh();
  }, [refresh]);

  // §F8 action flow: with server pages, optimistic removal + background
  // refresh would leave a 24-card page 1 while a card from page 2 goes
  // unseen — so an action REFETCHES the current page; and when the LAST
  // item of a page > 1 is acted on, step back a page instead of rendering
  // an empty one (the page-state change triggers the refetch).
  const onActed = (id, message) => {
    if (message) setToast(message);
    const remaining = (data?.results ?? []).filter((it) => it.id !== id).length;
    if (pagedTab && remaining === 0 && page > 1) setPage((p) => p - 1);
    else refresh();
  };

  const onConflict = (id) => {
    // 409: someone else already actioned it — resync says so.
    onActed(id, "Already reviewed by someone else — removed from your queue.");
  };

  const items = data?.results ?? null;
  const count = pagedTab ? (data?.count ?? 0) : (items?.length ?? 0);
  const pageCount = pagedTab ? Math.max(1, Math.ceil(count / QUEUE_PAGE_SIZE[tab])) : 1;

  return (
    <>
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
          <QuestionCard
            key={item.id}
            auth={auth}
            item={item}
            similar={similarByQ[item.id]}
            onActed={onActed}
            onConflict={onConflict}
            onToast={setToast}
          />
        ) : tab === "categories" ? (
          <CategoryCard key={item.id} auth={auth} item={item} onActed={onActed} onConflict={onConflict} onToast={setToast} />
        ) : (
          <FlaggedCard
            key={item.id}
            auth={auth}
            item={item}
            similar={similarByQ[item.id]}
            onActed={onActed}
            onConflict={onConflict}
            onToast={setToast}
          />
        ),
      )}

      {pagedTab && data && count > 0 && (
        <div className="libpager">
          <button className="btn btn--ghost btn--sm" disabled={!data.previous} onClick={() => setPage((p) => p - 1)}>
            ← Prev
          </button>
          <span className="libpager__where">
            page {page} of {pageCount} · {count} pending
          </span>
          <button className="btn btn--ghost btn--sm" disabled={!data.next} onClick={() => setPage((p) => p + 1)}>
            Next →
          </button>
        </div>
      )}

      <Toast message={toast} tone="info" onDone={() => setToast(null)} />
    </>
  );
}

/* ---------------- Cards ---------------- */
/* Owner and UsageBadge moved to components/StaffGate.jsx (§F3 #16) — the
   library page renders them too, so the split shares one copy. */

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

function QuestionCard({ auth, item, similar, onActed, onConflict, onToast }) {
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
      <SimilarMatches similar={similar} />
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

/**
 * §K1: nearest existing approved questions — a review AID, nothing here
 * acts. PRESENTATIONAL since #15 (§F2): the rows arrive via prop from the
 * queue's ONE batch-per-page request (/similar/batch/), so this component
 * fetches nothing. `similar` undefined = the page's batch hasn't landed
 * yet; [] = none found (or the optional aid failed — approve/reject work
 * regardless).
 *
 * History: the per-card fetch this replaces detonated #14c (50 concurrent
 * /similar calls per queue page, doubled by reloads → Postgres' connection
 * cap) and was patched with a 6-slot client gate. The batch endpoint
 * SUPERSEDES that gate — one request per page leaves no herd to throttle —
 * so the gate machinery is deleted, not kept dormant.
 */
function SimilarMatches({ similar }) {
  if (similar === undefined) return <p className="footnote">Checking for lookalikes…</p>;
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
function FlaggedCard({ auth, item, similar, onActed, onConflict, onToast }) {
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
      <SimilarMatches similar={similar} />
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
