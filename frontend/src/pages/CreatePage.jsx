import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, errorText, quotaError } from "../lib/api";
import { clearAuth, loadAuth, onAuthChange } from "../lib/storage";
import CategoryPicker from "../components/CategoryPicker";
import { Toast, UsageMeterLine } from "../components/shared";

/**
 * /create — creator content management.
 *
 * Gated on CAPABILITY, not plan (§F3, Handoff #19). plan ≠ capability
 * (§A.1): Stripe writes ENTITLEMENTS, never `plan` (#18 ruling — the
 * manual/ops lane and the billing lane must never fight), so every buyer
 * stays plan:"free" forever and a plan-only read locks out every paying
 * customer. The door opens for a manual paid plan, ANY entitlement (active
 * OR lapsed — lapsed buyers must still see their kept-safe content; C-3),
 * or a §J1 limit override. The server chokes stay the truth.
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

// §F5 (#19): entitlement-kind display labels + days-left, duplicated from
// ProfilePage/ManageUsersPage (the existing house precedent — three tiny
// copies beat a new shared module mid-handoff; noted in CHANGES).
const KIND_LABELS = {
  party_pack: "Party Game",
  big_pack: "Big Game",
  venue: "Venue",
  tournament_pass: "Tournament Pass",
  venue_tournament: "Venue Tournament",
};

function daysLeft(iso) {
  if (!iso) return null;
  const ms = new Date(iso).getTime() - Date.now();
  return Math.max(0, Math.ceil(ms / 86400000));
}

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

  // §F5 (#19): pack budgets live in profile.usage.entitlements — refetch
  // after authoring changes so the meter strip moves (the §F3 owner
  // retest: "its budget meter moves"). Silent-fail: cosmetic meters only.
  const refreshProfile = useCallback(() => {
    if (!auth) return;
    api
      .profile(auth.token)
      .then(setProfile)
      .catch(() => {});
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

  // §F3 (#19): any capability lane opens the workspace — manual plan,
  // entitlements (active OR lapsed, C-3: writes on lapsed packs 403
  // informatively server-side with pack_inactive), or §J1 overrides
  // raising the free plan's 0-limits. Locked-out is ONLY no-plan,
  // no-entitlement-ever, no-overrides.
  const usage = profile.usage || {};
  const openLimit = (block) => !!block && (block.limit === null || block.limit > 0);
  const canEnter =
    profile.plan !== "free" ||
    (usage.entitlements?.length ?? 0) > 0 ||
    openLimit(usage.questions) ||
    openLimit(usage.categories);

  if (!canEnter)
    return (
      <Shell user={profile} token={auth.token}>
        <section className="panel panel--center upsell">
          <span className="upsell__emoji">🔒</span>
          <h2 className="h2">Creating custom content is a paid feature</h2>
          <p className="footnote">
            Grab a game pack or the Venue plan to build your own categories and questions — hosting
            from the free library needs no plan at all.
          </p>
          <div className="upsell__actions">
            <Link className="btn btn--primary" to="/pricing">
              See packs &amp; plans
            </Link>
            <Link className="btn btn--ghost" to="/host">
              Back to hosting
            </Link>
          </div>
        </section>
      </Shell>
    );

  return <CreatorWorkspace auth={auth} profile={profile} onUsageChanged={refreshProfile} />;
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

function CreatorWorkspace({ auth, profile, onUsageChanged }) {
  const [toast, setToast] = useState(null);
  const ownCats = useMinePager(auth, "categories");
  const ownQs = useMinePager(auth, "questions");
  const loadError = ownCats.error || ownQs.error;
  const entitlements = profile.usage?.entitlements || [];

  const refreshCats = ownCats.refresh;
  const refreshQs = ownQs.refresh;
  const refresh = useCallback(() => {
    refreshCats();
    refreshQs();
    onUsageChanged(); // §F5 (#19): pack budgets ride the profile
  }, [refreshCats, refreshQs, onUsageChanged]);

  return (
    <Shell user={profile} token={auth.token}>
      {loadError && <p className="formerror formerror--block">{loadError}</p>}

      <PackMeterStrip entitlements={entitlements} />
      <UsageMeters ownCats={ownCats} ownQs={ownQs} usage={profile.usage} />

      <CategoryForm
        auth={auth}
        entitlements={entitlements}
        accountOpen={
          !!profile.usage?.categories &&
          (profile.usage.categories.limit === null || profile.usage.categories.limit > 0)
        }
        onSaved={refresh}
        onToast={setToast}
      />
      <QuestionForm auth={auth} entitlements={entitlements} onSaved={refresh} onToast={setToast} />
      <BulkUpload auth={auth} isStaff={!!profile.is_staff} onImported={refresh} onToast={setToast} />

      <MyContent
        auth={auth}
        ownCats={ownCats}
        ownQs={ownQs}
        entitlements={entitlements}
        onChanged={refresh}
        onToast={setToast}
      />

      <Toast message={toast} tone="info" onDone={() => setToast(null)} />
    </Shell>
  );
}

/* ---------------- Pack meter strip (§F5, Handoff #19) ---------------- */

/**
 * "Your pack lives HERE, with THIS budget" — one line per ACTIVE
 * entitlement from profile.usage.entitlements (packs: budget + days left;
 * venue: active-question meter). Cosmetic only: the server chokes stay the
 * truth, these numbers just stop a buyer wondering where their purchase
 * went (the owner's own bug-3 confusion is the spec).
 */
function PackMeterStrip({ entitlements }) {
  const active = (entitlements || []).filter((e) => e.is_active);
  if (!active.length) return null;
  return (
    <div className="packstrip">
      {active.map((e) => {
        const bits = [];
        if (e.question_limit != null) bits.push(`${e.questions_used}/${e.question_limit} questions`);
        // §F5 (#20): the category pair, straight from the same usage row.
        if (e.category_limit != null) bits.push(`${e.categories_used}/${e.category_limit} categories`);
        if (e.active_questions)
          bits.push(`${e.active_questions.used}/${e.active_questions.limit} active questions`);
        const left = daysLeft(e.active_until);
        if (left != null) bits.push(`${left} day${left === 1 ? "" : "s"} left`);
        return (
          <span key={e.id} className="packstrip__item">
            <strong>{KIND_LABELS[e.kind] || e.kind}</strong>
            {bits.length ? ` — ${bits.join(" · ")}` : ""}
          </span>
        );
      })}
    </div>
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

function CategoryForm({ auth, entitlements, accountOpen, onSaved, onToast }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [photo, setPhoto] = useState(null);
  const [visibility, setVisibility] = useState("private");
  // #19.1: WHERE the category lives — "account" or a pack entitlement id.
  // The backend has taken `entitlement: <id>` since #18; this form just
  // never offered it, which left pure pack buyers (account lane = 0) with
  // nowhere to create — the owner's exact report. Bound kinds are the
  // rows with a question budget; venue rows are account-scoped already.
  const packs = (entitlements || []).filter((e) => e.is_active && e.question_limit != null);
  const [home, setHome] = useState(() =>
    accountOpen || !packs.length ? "account" : String(packs[0].id),
  );
  const packHome = home !== "account";
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
      if (packHome) {
        // Bound categories are private by definition (serializer rule);
        // sending no visibility lets the server default stand.
        fd.append("entitlement", home);
      } else {
        fd.append("visibility", visibility);
      }
      const created = await api.createCategory(auth.token, fd);
      const packLabel = packHome
        ? KIND_LABELS[packs.find((p) => String(p.id) === home)?.kind] || "pack"
        : null;
      onToast(
        packHome
          ? `“${created.name}” created inside your ${packLabel} pack — questions there use its budget.`
          : created.visibility === "public"
            ? `“${created.name}” created — awaiting moderation before others can use it.`
            : `“${created.name}” created — private, usable in your games right away.`,
      );
      setName("");
      setDescription("");
      setPhoto(null);
      onSaved();
    } catch (err) {
      setError(forbidden(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel">
      <h2 className="h2">New category</h2>
      <form onSubmit={submit}>
        {packs.length > 0 && (
          <label className="field">
            Where does this category live?
            <select value={home} onChange={(e) => setHome(e.target.value)}>
              <option value="account">My library (account plan)</option>
              {packs.map((p) => {
                const left = daysLeft(p.active_until);
                // §F5 (#20): the option says how much category room the
                // pack has left — the same numbers as the strip above.
                const cats =
                  p.category_limit != null ? ` · ${p.categories_used}/${p.category_limit} categories` : "";
                return (
                  <option key={p.id} value={String(p.id)}>
                    {KIND_LABELS[p.kind] || p.kind} pack{cats}
                    {left != null ? ` · ${left} day${left === 1 ? "" : "s"} left` : ""}
                  </option>
                );
              })}
            </select>
          </label>
        )}
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
        {packHome ? (
          <p className="footnote">Pack categories stay private to your account — the pack is your night, not the library.</p>
        ) : (
          <VisibilityPick value={visibility} onChange={setVisibility} />
        )}
        {error && <p className="formerror">{error}</p>}
        <button className="btn btn--primary" disabled={busy || !name.trim()}>
          {busy ? "Saving…" : "Create category"}
        </button>
      </form>
    </section>
  );
}

/* ---------------- Question form ---------------- */

function QuestionForm({ auth, entitlements, onSaved, onToast }) {
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
      setError(forbidden(err));
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
        {(() => {
          // §F5(c) (#19): one line when the selection includes a pack-bound
          // category. Cosmetic — mixed scopes still 400 server-side.
          const boundId = [...picked.values()].map((c) => c.entitlement).find((e) => e != null);
          if (boundId == null) return null;
          const ent = (entitlements || []).find((e) => e.id === boundId);
          return (
            <p className="packhint">
              {ent && ent.question_limit != null
                ? `Counts against your ${KIND_LABELS[ent.kind] || ent.kind} budget (${ent.questions_used}/${ent.question_limit} used).`
                : "Counts against your pack's question budget."}
            </p>
          );
        })()}
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

function MyContent({ auth, ownCats, ownQs, entitlements, onChanged, onToast }) {
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
            {c.entitlement != null && (
              // §F5(b) (#19): bound categories wear a "pack" chip; the
              // tooltip names which pack (CategorySerializer exposes the
              // entitlement id — #18).
              <span
                className="badge badge--pack"
                title={(() => {
                  const ent = (entitlements || []).find((e) => e.id === c.entitlement);
                  return ent ? `${KIND_LABELS[ent.kind] || ent.kind} pack category` : "Pack category";
                })()}
              >
                pack
              </span>
            )}
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

// §F3 (#19): pricing-aware, and ONLY a fallback — a 403 that carries a
// server `detail` (pack_inactive, plan_required, …) must surface that
// informative copy, not this generic line (the old constant masked #18's
// structured messages; a §F3(d) sibling of the stale "admin can enable"
// string, listed in CHANGES).
const UPSELL_403 =
  "This needs a game pack or the Venue plan — see the Pricing page. Hosting from the free library is always free.";
const forbidden = (err) =>
  quotaMessage(err) ??
  (err instanceof ApiError && err.status === 403 && !err.data?.detail ? UPSELL_403 : errorText(err));

/** Friendly text for the backend's structured quota 403s ({code: "quota_*"}). */
function quotaMessage(err) {
  const q = quotaError(err);
  if (!q) return null;
  if (q.code === "quota_storage") {
    // §F3: numbers are bytes — format MB here, at the message, not upstream.
    if (q.limit === 0)
      return "Your current plan doesn't include media storage — a game pack or the Venue plan unlocks it (see the Pricing page).";
    const add = q.requested != null ? `This upload adds ${fmtMB(q.requested)} of media, but you` : "You";
    return `${add}'ve used ${fmtMB(q.used)} of your plan's ${fmtMB(q.limit)} media storage. Delete some media first.`;
  }
  if (q.limit === 0) {
    // §F3(b) (#19): a pack buyer creating into an UNBOUND category is
    // correctly denied by the ACCOUNT lane (free = 0 questions); the hint
    // routes them to the lane they paid for — their pack's bound category.
    const base =
      "Your current plan doesn't include custom content — a game pack or the Venue plan unlocks it (see the Pricing page).";
    return q.code === "quota_questions"
      ? `${base} (Pack owners: add questions inside your pack's own category.)`
      : base;
  }
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
