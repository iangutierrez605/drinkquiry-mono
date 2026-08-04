import { useCallback, useEffect, useState } from "react";
import { api, errorText } from "../lib/api";
import StaffGate from "../components/StaffGate";
import CategoryPicker from "../components/CategoryPicker";
import { Toast } from "../components/shared";

/**
 * /manage/themes — §G4's staff-curated theme table, its own page since the
 * §F3 (Handoff #16) split of the six-tab /moderate. The body is the old
 * ThemesTab + ThemeRow + ThemeForm lifted VERBATIM — unpaginated list
 * (staff-curated scale), inline create/edit with the shared searchable
 * CategoryPicker (§F7 #15), soft delete with confirm. Only the wrapper
 * changed (StaffGate). Everything is IsAdminUser server-side.
 */

export default function ManageThemesPage() {
  return (
    <StaffGate title="Themes" next="/manage/themes">
      {({ auth }) => <ThemesBody auth={auth} />}
    </StaffGate>
  );
}

function ThemesBody({ auth }) {
  const [toast, setToast] = useState(null);
  return (
    <>
      <Themes auth={auth} onToast={setToast} />
      <Toast message={toast} tone="info" onDone={() => setToast(null)} />
    </>
  );
}

/**
 * Staff-curated tag table: a theme groups categories so hosts can find "all
 * music categories" in one tap on /host. List + inline create/edit (name,
 * description, categories via the shared searchable CategoryPicker — §F7
 * #15, no more fetch-all option list) + soft delete with confirm — the
 * library page's row/form patterns, reused. Unpaginated (staff-curated
 * scale). Everything is IsAdminUser server-side.
 */
function Themes({ auth, onToast }) {
  const [themes, setThemes] = useState(null);
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
  }, [refresh]);

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
        <ThemeRow key={theme.id} auth={auth} theme={theme} onToast={onToast} onChanged={refresh} />
      ))}
    </>
  );
}

function ThemeRow({ auth, theme, onToast, onChanged }) {
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
function ThemeForm({ auth, theme, onCancel, onDone }) {
  const [name, setName] = useState(theme?.name ?? "");
  const [description, setDescription] = useState(theme?.description ?? "");
  // §F7 (#15): membership through the shared CategoryPicker; edit mode
  // seeds its Map from the serializer's `category_details` ({id, name},
  // ACTIVE members only). Side effect worth naming: saving a theme now
  // normalizes membership to active categories — the old form re-sent
  // soft-deleted member ids, which the serializer's active-only queryset
  // REJECTED, 400ing every edit of a theme with a dead member. Dropping
  // them matches what every active surface already displays.
  const [picked, setPicked] = useState(
    () => new Map((theme?.category_details ?? []).map((c) => [c.id, { id: c.id, name: c.name }])),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const body = {
      name: name.trim(),
      description: description.trim(),
      categories: [...picked.keys()],
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
      <CategoryPicker auth={auth} value={picked} onChange={setPicked} legend="Categories in this theme" />
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
