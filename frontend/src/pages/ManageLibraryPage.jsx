import { useEffect, useRef, useState } from "react";
import { api, errorText } from "../lib/api";
import { useDebounced } from "../lib/hooks";
import StaffGate, { Owner, UsageBadge } from "../components/StaffGate";
import { Toast } from "../components/shared";

/**
 * /manage/library — §I5's staff question library, its own page since the
 * §F3 (Handoff #16) split of the six-tab /moderate (the owner's "one at a
 * time" dropdown destinations). The body is the old LibraryTab lifted
 * VERBATIM — same server pagination (25/page, never allPages), the same
 * §F8 searchable category pick, the same stale-response seq guards, the
 * same revise/soft-delete row flows. Only the wrapper changed: StaffGate
 * carries the auth/staff funnel all four staff pages share.
 */

export default function ManageLibraryPage() {
  return (
    <StaffGate title="Question library" next="/manage/library">
      {({ auth }) => <LibraryBody auth={auth} />}
    </StaffGate>
  );
}

function LibraryBody({ auth }) {
  const [toast, setToast] = useState(null);
  return (
    <>
      <Library auth={auth} onToast={setToast} />
      <Toast message={toast} tone="info" onDone={() => setToast(null)} />
    </>
  );
}

/**
 * §F8 (#15): a compact SINGLE category pick for the library filters —
 * type-to-search over the server's ?search= (top 8 matches), tap to pick;
 * the pick renders as a chip with ✕ to clear. `value` is {id, name} | null.
 * Replaces a <select> that fetched every visible category for its options.
 */
function CategoryFilterPick({ auth, value, onChange }) {
  const [search, setSearch] = useState("");
  const debounced = useDebounced(search);
  const [rows, setRows] = useState(null); // null = closed; [] = no match
  const seq = useRef(0);

  useEffect(() => {
    if (!debounced.trim()) {
      setRows(null);
      return;
    }
    const mySeq = ++seq.current; // stale-response guard
    api
      .categoriesPage(auth.token, { search: debounced })
      .then((d) => seq.current === mySeq && setRows(d.results.slice(0, 8)))
      .catch(() => seq.current === mySeq && setRows([]));
  }, [auth.token, debounced]);

  if (value)
    return (
      <button type="button" className="pinnedchip" title="Clear the category filter" onClick={() => onChange(null)}>
        {value.name} ✕
      </button>
    );

  return (
    <span className="filterpick">
      <input
        type="search"
        placeholder="all — type to filter"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        aria-label="Filter by category"
      />
      {rows != null && (
        <span className="filterpick__results">
          {rows.length === 0 && <span className="filterpick__none">no match</span>}
          {rows.map((c) => (
            <button
              key={c.id}
              type="button"
              className="filterpick__opt"
              onClick={() => {
                onChange({ id: c.id, name: c.name });
                setSearch("");
                setRows(null);
              }}
            >
              {c.name}
            </button>
          ))}
        </span>
      )}
    </span>
  );
}

/**
 * Every question on the site — search + filters + REAL pagination (the
 * backend pages at 25; this page never uses allPages, that's the whole
 * "thousands of questions" point). Per-row: inline Edit (→ revise/, which
 * soft-deletes the old row and creates a new approved one — the row swaps
 * in place) and Delete (soft, inline confirm). ?deleted=only shows the
 * graveyard, read-only.
 */
function Library({ auth, onToast }) {
  const [search, setSearch] = useState("");
  const debouncedSearch = useDebounced(search);
  // §F8 (#15): the category filter is a SEARCHABLE pick ({id, name} | null)
  // now — the old <select> loaded EVERY visible category to offer options
  // (its "small list; allPages is fine HERE" claim died at 2,000 rows, C21).
  const [category, setCategory] = useState(null);
  const [status, setStatus] = useState("all");
  const [deleted, setDeleted] = useState("active");
  const [ordering, setOrdering] = useState("-created_at");
  const [page, setPage] = useState(1);
  const [data, setData] = useState(null); // {results, count, next, previous}
  const [error, setError] = useState(null);
  const seq = useRef(0);

  // Any filter change resets to page 1.
  useEffect(() => {
    setPage(1);
  }, [debouncedSearch, category, status, deleted, ordering]);

  useEffect(() => {
    const mySeq = ++seq.current; // stale-response guard for fast typists
    api
      .moderationLibrary(auth.token, {
        search: debouncedSearch,
        category: category?.id ?? "",
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
          <CategoryFilterPick auth={auth} value={category} onChange={setCategory} />
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
