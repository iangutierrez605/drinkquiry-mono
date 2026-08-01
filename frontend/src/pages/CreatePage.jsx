import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, errorText, quotaError } from "../lib/api";
import { clearAuth, loadAuth, onAuthChange } from "../lib/storage";
import CategoryPicker from "../components/CategoryPicker";
import { Toast, UsageMeterLine } from "../components/shared";

/**
 * /create — creator content management.
 *
 * Gated on profile.plan !== "free" (the server reports the *effective* plan,
 * so an expired paid plan already reads as "free" — no expiry math here).
 * Admin sets the plan by hand; Stripe writes the same field later.
 * Two forms (category, question) posting multipart FormData, plus a
 * "my content" list with moderation badges and owner-only edit/delete.
 * Quotas are enforced server-side; the meters here are cosmetic.
 */

// §F2 hard-reject ceilings (cosmetic mirror of the server's settings — the
// backend validators are the gate). Images over ~1 MB / 1920 px are
// auto-resized server-side, so a 10 MB phone photo is fine to send.
const MEDIA_LIMITS = { image: 10 * 1024 * 1024, audio: 8 * 1024 * 1024, video: 25 * 1024 * 1024 };
const MB = (bytes) => `${Math.round(bytes / 1024 / 1024)} MB`;
// §F3: storage meter values are bytes; format them where the entries are
// built (this file / ProfilePage), never inside the shared UsageMeterLine.
const fmtMB = (bytes) => `${Math.round((bytes / 1024 / 1024) * 10) / 10} MB`;

export default function CreatePage() {
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
          setAuth(null); // token expired — bounce to the login prompt
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
          <p className="footnote">Creating content needs a host account.</p>
          <Link className="btn btn--primary" to="/login?next=/create">
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

  if (profile.plan === "free")
    return (
      <Shell user={profile} token={auth.token}>
        <section className="panel panel--center upsell">
          <span className="upsell__emoji">🔒</span>
          <h2 className="h2">Creating custom content is a paid feature</h2>
          <p className="footnote">
            Coming soon. For now, an admin can enable creator access on your account — everything else about
            hosting games works without it.
          </p>
          <Link className="btn btn--ghost" to="/host">
            Back to hosting
          </Link>
        </section>
      </Shell>
    );

  return <CreatorWorkspace auth={auth} profile={profile} />;
}

function Shell({ user, token, children }) {
  return (
    <div className="page">
      {/* §F: identity + cross-links moved to the SiteNav. */}
      <h1 className="h1">Your content</h1>
      {children}
    </div>
  );
}

/* ---------------- Creator workspace ---------------- */

/**
 * §F7 (Handoff #15): one "my content" pager — a page-appending fetch over an
 * OWNER-ONLY server list (?mine=1; the old flow fetched EVERY visible row —
 * all 10,000+ public questions — just to client-filter down to the owner's,
 * C21). `count` is the server-truth owned total (feeds the usage meters);
 * refresh() rewinds to page 1 and refetches (create/delete/visibility
 * changes). Stale responses are sequence-guarded (§G).
 */
function useMinePager(auth, kind) {
  const [params, setParams] = useState({ page: 1, nonce: 0 });
  const [rows, setRows] = useState(null); // accumulated pages; null = first load
  const [count, setCount] = useState(0);
  const [hasNext, setHasNext] = useState(false);
  const [error, setError] = useState(null);
  const seq = useRef(0);

  useEffect(() => {
    const mySeq = ++seq.current;
    const call =
      kind === "categories"
        ? api.categoriesPage(auth.token, { mine: 1, page: params.page })
        : api.questionsPage(auth.token, { mine: 1, page: params.page });
    call
      .then((d) => {
        if (seq.current !== mySeq) return;
        setRows((prev) => (params.page === 1 ? d.results : [...(prev ?? []), ...d.results]));
        setCount(d.count);
        setHasNext(!!d.next);
        setError(null);
      })
      .catch((err) => seq.current === mySeq && setError(errorText(err)));
  }, [auth.token, kind, params]);

  // Stable identities (setParams itself is stable) so consumers can list
  // these in hook deps without re-firing.
  const more = useCallback(() => setParams((p) => ({ ...p, page: p.page + 1 })), []);
  const refresh = useCallback(() => setParams((p) => ({ page: 1, nonce: p.nonce + 1 })), []);
  return { rows, count, hasNext, error, more, refresh };
}

function CreatorWorkspace({ auth, profile }) {
  const [toast, setToast] = useState(null);
  const ownCats = useMinePager(auth, "categories");
  const ownQs = useMinePager(auth, "questions");
  const loadError = ownCats.error || ownQs.error;

  const refreshCats = ownCats.refresh;
  const refreshQs = ownQs.refresh;
  const refresh = useCallback(() => {
    refreshCats();
    refreshQs();
  }, [refreshCats, refreshQs]);

  return (
    <Shell user={profile} token={auth.token}>
      {loadError && <p className="formerror formerror--block">{loadError}</p>}

      <UsageMeters ownCats={ownCats} ownQs={ownQs} usage={profile.usage} />

      <CategoryForm auth={auth} onSaved={refresh} onToast={setToast} />
      <QuestionForm auth={auth} onSaved={refresh} onToast={setToast} />
      <BulkUpload auth={auth} isStaff={!!profile.is_staff} onImported={refresh} onToast={setToast} />

      <MyContent auth={auth} ownCats={ownCats} ownQs={ownQs} onChanged={refresh} onToast={setToast} />

      <Toast message={toast} tone="info" onDone={() => setToast(null)} />
    </Shell>
  );
}

/* ---------------- Usage meters ---------------- */

/**
 * "2 of 25 categories · 41 of 500 questions". §F7 (#15): `used` is now the
 * ?mine=1 pagers' server `count` — the TRUE owned totals even when only one
 * page is loaded (the old list-length reading breaks under pagination);
 * refresh() after create/delete refetches, so the meters stay live. Limits
 * come from the profile's usage block; limit === null means unlimited.
 */
function UsageMeters({ ownCats, ownQs, usage }) {
  const storage = usage?.storage; // §F3: bytes from the profile payload
  return (
    <UsageMeterLine
      entries={[
        { used: ownCats.count, block: usage?.categories, noun: "categories" },
        { used: ownQs.count, block: usage?.questions, noun: "questions" },
        storage && {
          used: fmtMB(storage.used),
          block: storage.limit == null ? { limit: null } : { limit: fmtMB(storage.limit) },
          noun: "media",
        },
      ].filter(Boolean)}
    />
  );
}

/* ---------------- Category form ---------------- */

function CategoryForm({ auth, onSaved, onToast }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [photo, setPhoto] = useState(null);
  const [visibility, setVisibility] = useState("private");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const submit = async (e) => {
    e.preventDefault();
    if (photo && photo.size > MEDIA_LIMITS.image) {
      setError(`Photo is ${MB(photo.size)} — the limit is ${MB(MEDIA_LIMITS.image)}.`);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const fd = new FormData();
      fd.append("name", name.trim());
      if (description.trim()) fd.append("description", description.trim());
      if (photo) fd.append("photo", photo);
      fd.append("visibility", visibility);
      const created = await api.createCategory(auth.token, fd);
      onToast(
        created.visibility === "public"
          ? `“${created.name}” created — awaiting moderation before others can use it.`
          : `“${created.name}” created — private, usable in your games right away.`,
      );
      setName("");
      setDescription("");
      setPhoto(null);
      onSaved();
    } catch (err) {
      setError(quotaMessage(err) ?? (err instanceof ApiError && err.status === 403 ? UPSELL_403 : errorText(err)));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel">
      <h2 className="h2">New category</h2>
      <form onSubmit={submit}>
        <label className="field">
          Name
          <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={100} placeholder="e.g. 90s One-Hit Wonders" />
        </label>
        <label className="field">
          Description <span className="field__hint">(optional)</span>
          <input value={description} onChange={(e) => setDescription(e.target.value)} maxLength={300} />
        </label>
        <label className="field">
          Cover photo <span className="field__hint">(optional, image ≤ 10 MB — big photos are resized automatically)</span>
          <input type="file" accept="image/*" onChange={(e) => setPhoto(e.target.files?.[0] ?? null)} />
        </label>
        <VisibilityPick value={visibility} onChange={setVisibility} />
        {error && <p className="formerror">{error}</p>}
        <button className="btn btn--primary" disabled={busy || !name.trim()}>
          {busy ? "Saving…" : "Create category"}
        </button>
      </form>
    </section>
  );
}

/* ---------------- Question form ---------------- */

function QuestionForm({ auth, onSaved, onToast }) {
  // §F6 (Handoff #10): a question can live in SEVERAL categories. §F7
  // (#15): the fetch-all checkbox list became the shared searchable
  // CategoryPicker — `picked` is a Map(id → {id, name}) captured at click
  // (the pinning rule), so a selection survives any search.
  const [picked, setPicked] = useState(() => new Map());
  const [questionText, setQuestionText] = useState("");
  const [answer, setAnswer] = useState("");
  const [difficulty, setDifficulty] = useState(3);
  const [mediaType, setMediaType] = useState("none");
  const [file, setFile] = useState(null);
  const [visibility, setVisibility] = useState("private");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const submit = async (e) => {
    e.preventDefault();
    if (mediaType !== "none") {
      if (!file) {
        setError(`Pick a ${mediaType} file, or set media to “none”.`);
        return;
      }
      if (file.size > MEDIA_LIMITS[mediaType]) {
        setError(`That ${mediaType} is ${MB(file.size)} — the limit is ${MB(MEDIA_LIMITS[mediaType])}.`);
        return;
      }
    }
    setBusy(true);
    setError(null);
    try {
      const fd = new FormData();
      // §F2: repeated `categories` entries — the API reads the list.
      [...picked.keys()].forEach((id) => fd.append("categories", id));
      fd.append("question_text", questionText.trim());
      fd.append("answer", answer.trim());
      fd.append("difficulty", String(difficulty));
      fd.append("media_type", mediaType);
      if (mediaType !== "none" && file) fd.append(mediaType, file);
      fd.append("visibility", visibility);
      await api.createQuestion(auth.token, fd);
      onToast("Question saved.");
      setQuestionText("");
      setAnswer("");
      setFile(null);
      setPicked(new Map());
      onSaved();
    } catch (err) {
      setError(quotaMessage(err) ?? (err instanceof ApiError && err.status === 403 ? UPSELL_403 : errorText(err)));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel">
      <h2 className="h2">New question</h2>
      <form onSubmit={submit}>
        <CategoryPicker
          auth={auth}
          value={picked}
          onChange={setPicked}
          legend="Categories"
          hint="(pick one or more — a question can live in several)"
        />
        <label className="field">
          Question
          <textarea value={questionText} onChange={(e) => setQuestionText(e.target.value)} required rows={3} maxLength={500} />
        </label>
        <label className="field">
          Answer <span className="field__hint">(only ever shown to the host)</span>
          <input value={answer} onChange={(e) => setAnswer(e.target.value)} required maxLength={300} />
        </label>
        <label className="field">
          Difficulty <span className="field__hint">1 = easiest = top row = lowest value</span>
          <div className="stepper">
            <input type="range" min={1} max={5} value={difficulty} onChange={(e) => setDifficulty(Number(e.target.value))} />
            <span className="stepper__value">{difficulty}</span>
          </div>
        </label>
        <label className="field">
          Media
          <select
            value={mediaType}
            onChange={(e) => {
              setMediaType(e.target.value);
              setFile(null);
            }}
          >
            <option value="none">None — text only</option>
            <option value="image">Image (≤ 10 MB, auto-resized)</option>
            <option value="audio">Audio (≤ 8 MB)</option>
            <option value="video">Video (≤ 25 MB)</option>
          </select>
        </label>
        {mediaType !== "none" && (
          <label className="field">
            {mediaType[0].toUpperCase() + mediaType.slice(1)} file
            <input type="file" accept={`${mediaType}/*`} onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
          </label>
        )}
        <VisibilityPick value={visibility} onChange={setVisibility} />
        {error && <p className="formerror">{error}</p>}
        <button
          className="btn btn--primary"
          disabled={busy || picked.size === 0 || !questionText.trim() || !answer.trim()}
        >
          {busy ? "Saving…" : "Add question"}
        </button>
      </form>
    </section>
  );
}

/* ---------------- Bulk CSV upload (Handoff #4 §G3) ---------------- */

function BulkUpload({ auth, isStaff, onImported, onToast }) {
  const [file, setFile] = useState(null);
  const [official, setOfficial] = useState(false);
  const [createCats, setCreateCats] = useState(false); // §F: default off = v1 behavior
  const [preview, setPreview] = useState(null); // successful dry-run result
  const [rowErrors, setRowErrors] = useState([]); // [{row, field, message}]
  const [busy, setBusy] = useState(false); // false | "checking" | "importing"
  const [error, setError] = useState(null);
  const fileInput = useRef(null);

  const buildForm = (f, dryRun, off, create) => {
    const fd = new FormData();
    fd.append("file", f);
    fd.append("dry_run", dryRun ? "true" : "false");
    if (off) fd.append("official", "true");
    if (create) fd.append("create_categories", "true");
    return fd;
  };

  // Row-error 400s carry {created: 0, errors: [...], skipped: [...]}; anything
  // else (file-level 400s, quota 403s) flattens to one message.
  const handleFailure = (err) => {
    if (err instanceof ApiError && err.status === 400 && Array.isArray(err.data?.errors)) {
      setRowErrors(err.data.errors);
      setPreview(null);
    } else {
      setError(quotaMessage(err) ?? errorText(err));
    }
  };

  const dryRun = async (f, off, create) => {
    setBusy("checking");
    setError(null);
    setPreview(null);
    setRowErrors([]);
    try {
      setPreview(await api.bulkQuestions(auth.token, buildForm(f, true, off, create)));
    } catch (err) {
      handleFailure(err);
    } finally {
      setBusy(false);
    }
  };

  // Dry run happens automatically on file selection…
  const pick = (f) => {
    setFile(f);
    setPreview(null);
    setRowErrors([]);
    setError(null);
    if (f) dryRun(f, official, createCats);
  };

  // …and re-runs when "official" flips, since category matching differs
  // (official uploads match official categories only)…
  const toggleOfficial = (on) => {
    setOfficial(on);
    if (file) dryRun(file, on, createCats);
  };

  // …and when "create missing categories" flips, since unknown names stop
  // being row errors and become planned creations (§F).
  const toggleCreateCats = (on) => {
    setCreateCats(on);
    if (file) dryRun(file, official, on);
  };

  const doImport = async () => {
    setBusy("importing");
    setError(null);
    try {
      const res = await api.bulkQuestions(auth.token, buildForm(file, false, official, createCats));
      const skippedNote = res.skipped.length
        ? ` (${res.skipped.length} duplicate${res.skipped.length === 1 ? "" : "s"} skipped)`
        : "";
      const catNote = res.categories_created
        ? ` and ${res.categories_created} new categor${res.categories_created === 1 ? "y" : "ies"}`
        : "";
      onToast(`Imported ${res.created} question${res.created === 1 ? "" : "s"}${catNote}${skippedNote}.`);
      setFile(null);
      setPreview(null);
      setRowErrors([]);
      if (fileInput.current) fileInput.current.value = "";
      onImported(); // usage meters + my-content list refresh
    } catch (err) {
      handleFailure(err); // e.g. quota consumed elsewhere since the dry run
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel">
      <h2 className="h2">Bulk upload</h2>
      <p className="footnote">
        One file instead of one form per question — up to 500 rows. Columns:{" "}
        <code>category,question_text,answer,difficulty,visibility</code>. A question can go in several
        categories at once — separate the names with a pipe: <code>TV|80s</code>. For media questions, upload a{" "}
        <strong>.zip</strong> with the CSV at its root plus the files, adding <code>media_type</code> and{" "}
        <code>media_file</code> (path inside the zip) columns.{" "}
        <a className="bulklink" href="/question-template.csv" download>
          Download CSV template
        </a>{" "}
        ·{" "}
        <a className="bulklink" href="/question-media-template.zip" download>
          Download media template
        </a>
      </p>
      <label className="field">
        CSV or zip file{" "}
        <span className="field__hint">
          (CSV: UTF-8, ≤ 1 MB. Zip: ≤ 200 MB, one CSV at the root. Media per file: images ≤ 10 MB —
          auto-resized — audio ≤ 8 MB, video ≤ 25 MB)
        </span>
        <input
          ref={fileInput}
          type="file"
          accept=".csv,.zip,text/csv,application/zip"
          onChange={(e) => pick(e.target.files?.[0] ?? null)}
        />
      </label>
      <label className="bulkofficial">
        <input type="checkbox" checked={createCats} onChange={(e) => toggleCreateCats(e.target.checked)} />
        Create missing categories (uses your category quota)
      </label>
      {isStaff && (
        <label className="bulkofficial">
          <input type="checkbox" checked={official} onChange={(e) => toggleOfficial(e.target.checked)} />
          Official content (owner-less, auto-approved, doesn't count against your quota)
        </label>
      )}

      {busy === "checking" && <p className="footnote">Checking the file…</p>}
      {error && <p className="formerror">{error}</p>}

      {rowErrors.length > 0 && (
        <>
          <p className="formerror">
            {rowErrors.length} problem{rowErrors.length === 1 ? "" : "s"} found — nothing was imported. Fix the rows
            below and re-select the file (row numbers match your spreadsheet, header = row 1).
          </p>
          <table className="bulkerrors">
            <thead>
              <tr>
                <th>Row</th>
                <th>Column</th>
                <th>Problem</th>
              </tr>
            </thead>
            <tbody>
              {rowErrors.map((e, i) => (
                <tr key={i}>
                  <td>{e.row}</td>
                  <td>{e.field}</td>
                  <td>{e.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {preview && (
        <>
          <p className="bulksummary">
            {preview.created} row{preview.created === 1 ? "" : "s"} ready
            {preview.categories_created > 0 &&
              `, will create ${preview.categories_created} new categor${
                preview.categories_created === 1 ? "y" : "ies"
              } (${preview.category_names.join(", ")})`}
            {preview.media_files > 0 && `, ${preview.media_files} with media`}
            {preview.skipped.length > 0 &&
              `, ${preview.skipped.length} skipped as duplicate${preview.skipped.length === 1 ? "" : "s"}`}
            {official && " — will be imported as official content"}
          </p>
          {preview.created > 0 ? (
            <button className="btn btn--primary" disabled={!!busy} onClick={doImport}>
              {busy === "importing"
                ? "Importing…"
                : `Import ${preview.created} question${preview.created === 1 ? "" : "s"}`}
            </button>
          ) : (
            <p className="footnote">Every row already exists — nothing to import.</p>
          )}
        </>
      )}
    </section>
  );
}

function VisibilityPick({ value, onChange }) {
  return (
    <fieldset className="vispick">
      <legend className="field__hint">Visibility</legend>
      <label className={`vischoice ${value === "private" ? "vischoice--on" : ""}`}>
        <input type="radio" name="visibility" checked={value === "private"} onChange={() => onChange("private")} />
        <strong>Private</strong>
        <span>Usable in your games immediately</span>
      </label>
      <label className={`vischoice ${value === "public" ? "vischoice--on" : ""}`}>
        <input type="radio" name="visibility" checked={value === "public"} onChange={() => onChange("public")} />
        <strong>Public</strong>
        <span>Goes to moderation; others can use it once approved</span>
      </label>
    </fieldset>
  );
}

/* ---------------- My content list ---------------- */

const STATUS_LABEL = {
  not_submitted: "Private",
  pending: "In review",
  approved: "Approved",
  rejected: "Rejected",
};

function ModerationBadge({ item }) {
  const status = item.moderation_status || "not_submitted";
  return (
    <span className={`modbadge modbadge--${status}`} title={item.moderation_note || ""}>
      {STATUS_LABEL[status] ?? status}
      {status === "rejected" && item.moderation_note ? " ⓘ" : ""}
    </span>
  );
}

function MyContent({ auth, ownCats, ownQs, onChanged, onToast }) {
  // §F7 (#15): both lists are ?mine=1 server pages (Load more appends) —
  // the old flow fetched EVERY visible row and client-filtered to the
  // owner's. Category labels ride each question row as `category_names`
  // (active names, server-provided) — no more id→name resolving against a
  // fetched-in-full category table.
  const act = async (fn, doneMsg) => {
    try {
      await fn();
      onToast(doneMsg);
      onChanged();
    } catch (err) {
      onToast(errorText(err));
    }
  };

  const cats = ownCats.rows ?? [];
  const questions = ownQs.rows ?? [];

  return (
    <section className="panel">
      <h2 className="h2">My content</h2>

      {ownCats.rows != null && ownQs.rows != null && ownCats.count === 0 && ownQs.count === 0 && (
        <p className="footnote">Nothing yet — your categories and questions will show up here.</p>
      )}

      {cats.map((c) => (
        <div key={c.id} className="ownrow">
          <div className="ownrow__main">
            <strong>{c.name}</strong>
            <span className="ownrow__meta">
              category · {c.usable_question_count} usable question{c.usable_question_count === 1 ? "" : "s"}
            </span>
          </div>
          <ModerationBadge item={c} />
          {c.visibility === "private" ? (
            <button
              className="btn btn--ghost btn--sm"
              onClick={() =>
                act(() => api.updateCategory(auth.token, c.id, { visibility: "public" }), "Submitted for review.")
              }
            >
              Make public
            </button>
          ) : (
            <button
              className="btn btn--ghost btn--sm"
              onClick={() =>
                act(() => api.updateCategory(auth.token, c.id, { visibility: "private" }), "Now private.")
              }
            >
              Make private
            </button>
          )}
          {/* §F5: deleting a category no longer touches its questions —
              they stay in your list and can be recategorized. */}
          <DeleteButton
            label="category (its questions stay in your list)"
            onDelete={() => act(() => api.deleteCategory(auth.token, c.id), `Deleted “${c.name}” — its questions are still yours.`)}
          />
        </div>
      ))}
      {ownCats.hasNext && (
        <div className="loadmore loadmore--tight">
          <span className="listmeta">
            {cats.length} of {ownCats.count} categories
          </span>
          <button className="btn btn--ghost btn--sm" onClick={ownCats.more}>
            More categories
          </button>
        </div>
      )}

      {questions.map((q) => (
        <div key={q.id} className="ownrow">
          <div className="ownrow__main">
            <span className="ownrow__q">{q.question_text}</span>
            <span className="ownrow__meta">
              {/* §F6/§F7: the row's ACTIVE category names, straight from the
                  serializer (a category you delete drops out here). */}
              {(q.category_names ?? []).join(" · ") || "no live categories"} · difficulty {q.difficulty} ·{" "}
              {q.media_type === "none" ? "text" : q.media_type}
            </span>
          </div>
          <ModerationBadge item={q} />
          {q.visibility === "private" ? (
            <button
              className="btn btn--ghost btn--sm"
              onClick={() =>
                act(() => api.updateQuestion(auth.token, q.id, { visibility: "public" }), "Submitted for review.")
              }
            >
              Make public
            </button>
          ) : (
            <button
              className="btn btn--ghost btn--sm"
              onClick={() =>
                act(() => api.updateQuestion(auth.token, q.id, { visibility: "private" }), "Now private.")
              }
            >
              Make private
            </button>
          )}
          <DeleteButton
            label="question"
            onDelete={() => act(() => api.deleteQuestion(auth.token, q.id), "Question deleted.")}
          />
        </div>
      ))}
      {ownQs.hasNext && (
        <div className="loadmore loadmore--tight">
          <span className="listmeta">
            {questions.length} of {ownQs.count} questions
          </span>
          <button className="btn btn--ghost btn--sm" onClick={ownQs.more}>
            More questions
          </button>
        </div>
      )}
    </section>
  );
}

function DeleteButton({ label, onDelete }) {
  const [arming, setArming] = useState(false);
  useEffect(() => {
    if (!arming) return undefined;
    const t = setTimeout(() => setArming(false), 3500);
    return () => clearTimeout(t);
  }, [arming]);
  return arming ? (
    <button className="btn btn--danger btn--sm" onClick={onDelete} title={`Really delete this ${label}`}>
      Confirm delete
    </button>
  ) : (
    <button className="btn btn--ghost btn--sm" onClick={() => setArming(true)}>
      Delete
    </button>
  );
}

const UPSELL_403 = "Your account doesn't have creator access yet — an admin can enable it.";

/** Friendly text for the backend's structured quota 403s ({code: "quota_*"}). */
function quotaMessage(err) {
  const q = quotaError(err);
  if (!q) return null;
  if (q.code === "quota_storage") {
    // §F3: numbers are bytes — format MB here, at the message, not upstream.
    if (q.limit === 0) return "Your plan doesn't include media storage — upgrade to a creator account to unlock it.";
    const add = q.requested != null ? `This upload adds ${fmtMB(q.requested)} of media, but you` : "You";
    return `${add}'ve used ${fmtMB(q.used)} of your plan's ${fmtMB(q.limit)} media storage. Delete some media first.`;
  }
  if (q.limit === 0) return "Your plan doesn't include custom content — upgrade to a creator account to unlock it.";
  if (q.requested != null) {
    // Batch 403 from bulk upload: say how far the file overshoots the plan,
    // in the right noun for the quota that tripped (§F: categories or questions).
    const left = Math.max(0, q.limit - q.used);
    if (q.code === "quota_categories") {
      return `This file needs ${q.requested} new categor${q.requested === 1 ? "y" : "ies"} but your plan has ${left} left (${q.used} of ${q.limit} used). Untick "Create missing categories", trim the file, or delete some categories first.`;
    }
    return `This file has ${q.requested} question${q.requested === 1 ? "" : "s"} but your plan has ${left} left (${q.used} of ${q.limit} used). Trim the file or delete some questions first.`;
  }
  return `You've hit your plan's limit (${q.used} of ${q.limit} used). Upgrade for more, or delete something first.`;
}
