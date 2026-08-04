import { useEffect, useRef, useState } from "react";
import { api, errorText, mediaUrl } from "../lib/api";
import { useDebounced } from "../lib/hooks";
import StaffGate from "../components/StaffGate";
import { Toast } from "../components/shared";

/**
 * /manage/users — §J3's staff user management (plans, demo expiries,
 * per-user allowance overrides, §H brand oversight), its own page since the
 * §F3 (Handoff #16) split of the six-tab /moderate. The body is the old
 * UsersTab + UserRow lifted VERBATIM — server pagination (25/page), the
 * seq stale guard, the whitelist PATCH. Only the wrapper changed
 * (StaffGate). Staff status editing stays Django-admin-only, as before.
 */

export default function ManageUsersPage() {
  return (
    <StaffGate title="Manage users" next="/manage/users">
      {({ auth }) => <UsersBody auth={auth} />}
    </StaffGate>
  );
}

function UsersBody({ auth }) {
  const [toast, setToast] = useState(null);
  return (
    <>
      <Users auth={auth} onToast={setToast} />
      <Toast message={toast} tone="info" onDone={() => setToast(null)} />
    </>
  );
}

const OVERRIDE_FIELDS = [
  { key: "games_per_month", label: "games / month", usageKey: "games_this_month" },
  { key: "categories", label: "categories", usageKey: "categories" },
  { key: "questions", label: "questions", usageKey: "questions" },
  { key: "storage_bytes", label: "storage (bytes)", usageKey: "storage" },
];

function Users({ auth, onToast }) {
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
