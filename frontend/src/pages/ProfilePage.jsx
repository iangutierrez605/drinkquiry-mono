import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, errorText, mediaUrl } from "../lib/api";
import { loadAuth, onAuthChange, saveAuth } from "../lib/storage";
import { Toast, UsageMeterLine } from "../components/shared";
import AuthScreen from "../components/AuthScreen";

const fmtMB = (bytes) => `${Math.round((bytes / 1024 / 1024) * 10) / 10} MB`;
import { resumeHostGame } from "./HostPage";

/**
 * /profile (Handoff #6 §G3): the signed-in host's identity + plan block and
 * their game history. Knox-gated exactly like /host — same stored dq_auth,
 * same login screen (imported, not forked; it lives in components/ since
 * §F1). Selecting a finished game loads its report (participants, tallies,
 * winners highlighted, every question with its answer). Non-finished games
 * have no report body: the backend's chosen rule is 409-until-finished, so
 * we don't even request one. §K2 adds the change-password panel.
 */
export default function ProfilePage() {
  const [auth, setAuth] = useState(loadAuth());
  // §F: nav logout / dead-token cleanup flips this page to the login screen.
  useEffect(() => onAuthChange(() => setAuth(loadAuth())), []);
  // §I (Handoff #12): stable identity — saving a display name announces an
  // auth change (saveAuth), which re-renders this page; an inline arrow here
  // would hand ProfileBody a fresh onAuthGone and re-trigger its load effect
  // on every save.
  const authGone = useCallback(() => setAuth(null), []); // Knox token died mid-visit → login form
  if (!auth) return <AuthScreen onAuthed={setAuth} />;
  return <ProfileBody auth={auth} onAuthGone={authGone} />;
}

function ProfileBody({ auth, onAuthGone }) {
  const [profile, setProfile] = useState(null);
  const [games, setGames] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [toast, setToast] = useState(null);

  // §I (Handoff #12): kill navbar staleness. Every successful profile PATCH
  // (display-name save, branding save/clear) returns the USER object
  // directly (profile serializer) — merge it into the stored auth, keeping
  // the token (login returned {token, user}; updateProfile does NOT). The
  // existing AUTH_EVENT plumbing makes the SiteNav flip instantly.
  const applyFreshProfile = useCallback(
    (fresh) => {
      setProfile(fresh);
      saveAuth({ token: auth.token, user: fresh });
    },
    [auth.token],
  );

  useEffect(() => {
    let alive = true;
    Promise.all([api.profile(auth.token), api.gameHistory(auth.token)])
      .then(([p, g]) => {
        if (!alive) return;
        setProfile(p);
        setGames(g); // allPages: newest-first straight from the server
        setLoadError(null);
      })
      .catch((err) => {
        if (!alive) return;
        if (err?.status === 401) onAuthGone(); // request() already cleared dq_auth
        else setLoadError(errorText(err));
      });
    return () => {
      alive = false;
    };
  }, [auth.token, onAuthGone]);

  return (
    <div className="page">
      {/* §F: the old pagehead (identity, Moderation/Your content/Host links,
          Log out) lives in the SiteNav now. */}
      <h1 className="h1">Your profile</h1>
      {loadError && <p className="formerror formerror--block">{loadError}</p>}

      {profile && (
        <section className="panel">
          <h2 className="h2">Account</h2>
          <div className="idblock">
            {/* §I: same rule as the navbar — never render the full email as
                a name; a blank display name reads as the local-part. */}
            <div className="idblock__name">
              {profile.display_name || (profile.email || "").split("@")[0]}
            </div>
            <div className="idblock__mail">{profile.email}</div>
            {/* §F4 (#20): EFFECTIVE standing, not the raw plan field —
                buyers stay plan:"free" forever (§A.1), so a paying Big
                Game buyer must not read "free plan" here. Precedence:
                manual plan name → best ACTIVE entitlement (venue kinds,
                else the soonest-expiring pack) → free. Cosmetic only; the
                capability gates were already right. ManageUsers keeps the
                raw plan column on purpose (ops needs the manual lane). */}
            {(() => {
              const standing = effectiveStanding(profile);
              return (
                <span className={`planchip ${standing.paid ? "planchip--paid" : ""}`}>
                  {standing.label}
                </span>
              );
            })()}
          </div>
          <DisplayNameEdit
            token={auth.token}
            profile={profile}
            onSaved={applyFreshProfile}
            onToast={setToast}
          />
          <UsageMeterLine
            entries={[
              { used: profile.usage?.games_this_month?.used, block: profile.usage?.games_this_month, noun: "games this month" },
              { used: profile.usage?.categories?.used, block: profile.usage?.categories, noun: "categories" },
              { used: profile.usage?.questions?.used, block: profile.usage?.questions, noun: "questions" },
              // §F3 storage: bytes in the payload — formatted HERE, where the
              // entries are built, never inside the shared UsageMeterLine.
              profile.usage?.storage && {
                used: fmtMB(profile.usage.storage.used),
                block:
                  profile.usage.storage.limit == null
                    ? { limit: null }
                    : { limit: fmtMB(profile.usage.storage.limit) },
                noun: "media",
              },
            ].filter(Boolean)}
          />
        </section>
      )}

      {profile && (
        <BrandingPanel
          token={auth.token}
          profile={profile}
          onSaved={applyFreshProfile} /* §I: branding saves refresh the cached nav user too */
          onToast={setToast}
        />
      )}

      {/* §F7 (Handoff #18): billing — entitlement meters, purchases, and
          the Stripe portal hand-off. Renders nothing for accounts with no
          billing history (most users, forever). */}
      {profile && (
        <BillingPanel
          token={auth.token}
          entitlements={profile.usage?.entitlements || []}
          onToast={setToast}
        />
      )}

      {profile && <ChangePasswordPanel token={auth.token} onToast={setToast} />}

      <section className="panel">
        <h2 className="h2">Game history</h2>
        {games == null && !loadError && <p className="footnote">Loading…</p>}
        {games?.length === 0 && (
          <p className="footnote">
            No games yet — <Link to="/host">host your first one</Link>.
          </p>
        )}
        <ul className="history">
          {games?.map((g) => (
            <HistoryRow key={g.code} game={g} token={auth.token} onToast={setToast} />
          ))}
        </ul>
      </section>

      <Toast message={toast} onDone={() => setToast(null)} />
    </div>
  );
}

/* ---------------- §H (Handoff #11): venue branding ---------------- */

/* ---------------- §F7 (Handoff #18): billing panel ---------------- */

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

/**
 * §F4 (#20): what the idblock chip should SAY. A manual plan keeps its
 * honest name; else the best ACTIVE entitlement speaks — venue kinds as
 * "Venue · active", else the soonest-expiring pack as "Big Game pack ·
 * 23 days left" (KIND_LABELS + daysLeft reused); else "free plan".
 */
function effectiveStanding(profile) {
  if (profile.plan !== "free") return { label: `${profile.plan} plan`, paid: true };
  const active = (profile.usage?.entitlements || []).filter((e) => e.is_active);
  const venue = active.find((e) => e.kind === "venue" || e.kind === "venue_tournament");
  if (venue) return { label: `${KIND_LABELS[venue.kind] || venue.kind} · active`, paid: true };
  const packs = active
    .filter((e) => e.active_until)
    .sort((a, b) => new Date(a.active_until) - new Date(b.active_until));
  if (packs.length) {
    const left = daysLeft(packs[0].active_until);
    return {
      label: `${KIND_LABELS[packs[0].kind] || packs[0].kind} pack · ${left} day${left === 1 ? "" : "s"} left`,
      paid: true,
    };
  }
  return { label: "free plan", paid: false };
}

/**
 * Entitlement meters (packs: questions used / budget + days left; venue:
 * active questions / 100), the purchase list, and Manage billing → Stripe's
 * portal. Money copy is PLAIN (rule 8). A buyer is never rendered as a
 * "creator" — the plan chip above keeps showing the honest manual plan
 * (free for most buyers); what they bought shows HERE, by its product name.
 */
function BillingPanel({ token, entitlements, onToast }) {
  const [status, setStatus] = useState(null); // /billing/status — lazy
  const [statusError, setStatusError] = useState(null);
  const [portalBusy, setPortalBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    // Only fetch the purchase/subscription detail when there's any billing
    // history to show — the profile's own entitlements list is the cheap
    // signal (empty for accounts that never bought).
    if (!entitlements.length) return undefined;
    api
      .billingStatus(token)
      .then((s) => alive && setStatus(s))
      .catch((err) => alive && setStatusError(errorText(err)));
    return () => {
      alive = false;
    };
  }, [token, entitlements.length]);

  if (!entitlements.length) return null;

  const openPortal = async () => {
    setPortalBusy(true);
    try {
      const { url } = await api.billingPortal(token);
      window.location.assign(url);
    } catch (err) {
      onToast(
        err?.data?.code === "billing_not_configured"
          ? "Billing isn't switched on for this server."
          : errorText(err),
      );
      setPortalBusy(false);
    }
  };

  const subs = status?.subscriptions || [];
  const purchases = (status?.purchases || []).filter((p) => p.status !== "pending");

  return (
    <section className="panel billing-panel">
      <h2 className="h2">Billing</h2>

      {entitlements.map((ent) => {
        const label = KIND_LABELS[ent.kind] || ent.kind;
        const left = daysLeft(ent.active_until);
        return (
          <div key={ent.id} className={`entrow ${ent.is_active ? "" : "entrow--lapsed"}`}>
            <div className="entrow__head">
              <strong>{label}</strong>
              {ent.is_active ? (
                ent.active_until ? (
                  <span className="badge">{left} day{left === 1 ? "" : "s"} left</span>
                ) : (
                  <span className="badge">active</span>
                )
              ) : (
                <span className="badge badge--muted">ended</span>
              )}
            </div>
            {ent.question_limit != null && (
              <div className="entrow__meter">
                {ent.questions_used} / {ent.question_limit} questions used
                {ent.game_limit != null ? ` · up to ${ent.game_limit} games` : ""}
              </div>
            )}
            {ent.active_questions && (
              <div className="entrow__meter">
                {ent.active_questions.used} / {ent.active_questions.limit} active questions
                {" — archiving frees a slot; archived questions keep forever"}
              </div>
            )}
            {!ent.is_active && ent.kind !== "venue" && ent.kind !== "venue_tournament" && (
              <div className="footnote">
                Everything you made is kept safe, read-only. Reactivating this
                pack brings hosting and editing back — email {SUPPORT_EMAIL} to
                reactivate.
              </div>
            )}
          </div>
        );
      })}

      {subs.map((s) => (
        <div key={s.product_key} className="entrow">
          <div className="entrow__head">
            <strong>{s.name} subscription</strong>
            <span className="badge">{s.status}</span>
          </div>
          {s.status === "past_due" && s.grace_period_ends_at && (
            <div className="formerror">
              Your last payment didn't go through. Access continues until{" "}
              {new Date(s.grace_period_ends_at).toLocaleDateString()} while it
              retries — update your card via Manage billing below.
            </div>
          )}
          {s.cancel_at_period_end && s.current_period_end && (
            <div className="footnote">
              Cancels at the end of the current period (
              {new Date(s.current_period_end).toLocaleDateString()}). Your
              content stays yours.
            </div>
          )}
        </div>
      ))}

      {purchases.length > 0 && (
        <ul className="billing-purchases">
          {purchases.map((p, i) => (
            <li key={i} className="footnote">
              {p.name} — {p.status}
              {p.amount_total != null
                ? ` — $${(p.amount_total / 100).toFixed(2)} ${String(p.currency || "").toUpperCase()}`
                : ""}
              {p.purchased_at ? ` — ${new Date(p.purchased_at).toLocaleDateString()}` : ""}
            </li>
          ))}
        </ul>
      )}
      {statusError && <p className="formerror">{statusError}</p>}

      <div className="billing-panel__actions">
        <button className="btn btn--sm" onClick={openPortal} disabled={portalBusy}>
          {portalBusy ? "Opening…" : "Manage billing"}
        </button>
        <span className="footnote">
          Card details, past invoices and cancellation are handled in Stripe's
          secure portal. Receipt emails come from us with every charge.
        </span>
      </div>
    </section>
  );
}

/**
 * Creator-plan feature: a brand name + logo that every game this account
 * hosts carries (TV lobby, in-play header, buzzer join line — all served
 * from the snapshot's `brand`). Free plans see the standard upsell card;
 * the server is the real gate on writes (rule 4). Logo uploads ride the
 * same media pipeline + storage quota as category photos.
 */
/* ---------------- §I (Handoff #12): display-name edit ---------------- */

function DisplayNameEdit({ token, profile, onSaved, onToast }) {
  const [name, setName] = useState(profile.display_name ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      // No backend change needed — display_name is already writable on the
      // existing profile PATCH. onSaved is applyFreshProfile: the fresh user
      // lands in the stored auth and the SiteNav flips instantly (§I).
      const fresh = await api.updateProfile(token, { display_name: name.trim() });
      onSaved(fresh);
      onToast("Name saved ✔");
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const dirty = name.trim() !== (profile.display_name ?? "");
  return (
    <div className="nameedit">
      <label className="field">
        Display name{" "}
        <span className="field__hint">(what the navbar shows — blank falls back to your email's name part)</span>
        <input value={name} maxLength={50} onChange={(e) => setName(e.target.value)} />
      </label>
      {error && <p className="formerror">{error}</p>}
      <button className="btn btn--primary btn--sm" disabled={busy || !dirty} onClick={save}>
        {busy ? "Saving…" : "Save name"}
      </button>
    </div>
  );
}

function BrandingPanel({ token, profile, onSaved, onToast }) {
  const [name, setName] = useState(profile.brand_name ?? "");
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  // §F3(c) (#19): plan ≠ capability (§A.1) — the Venue promise is "your
  // branding on every screen", and Venue buyers stay plan:"free" forever.
  // Widened to: manual paid plan OR any ACTIVE venue-kind entitlement
  // (packs don't include branding). Mirrors the server gate exactly
  // (ProfileView write + snapshot serve, both widened this handoff).
  const canBrand =
    profile.plan !== "free" ||
    (profile.usage?.entitlements || []).some(
      (e) => e.is_active && (e.kind === "venue" || e.kind === "venue_tournament"),
    );

  if (!canBrand)
    return (
      <section className="panel panel--center upsell">
        <span className="upsell__emoji">🏷️</span>
        <p>
          <strong>Brand your games.</strong> The Venue plan puts your name and logo on the TV, the
          lobby and every buzzer — see the Pricing page.
        </p>
      </section>
    );

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      let fresh;
      if (file) {
        const fd = new FormData();
        fd.append("brand_name", name);
        fd.append("brand_logo", file);
        fresh = await api.updateProfile(token, fd);
      } else {
        fresh = await api.updateProfile(token, { brand_name: name });
      }
      onSaved(fresh);
      setFile(null);
      onToast("Branding saved ✔ — it's on your games' screens from the next snapshot.");
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const clearLogo = async () => {
    setBusy(true);
    setError(null);
    try {
      const fresh = await api.updateProfile(token, { brand_logo_clear: true });
      onSaved(fresh);
      onToast("Logo cleared — its storage quota is freed.");
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel">
      <h2 className="h2">Branding</h2>
      <p className="footnote">
        Your name and logo show on the TV lobby next to the join code, in the in-play header, and
        on every buzzer's join screen — for every game you host.
      </p>
      <label className="field">
        Brand name <span className="field__hint">(e.g. THE KINGS ARMS — blank to remove)</span>
        <input value={name} maxLength={60} onChange={(e) => setName(e.target.value)} />
      </label>
      <div className="brandpanel__logo">
        {profile.brand_logo && !file && (
          <img src={mediaUrl(profile.brand_logo)} alt="Your brand logo" className="brandlogo brandlogo--preview" />
        )}
        {file && <img src={URL.createObjectURL(file)} alt="New logo preview" className="brandlogo brandlogo--preview" />}
        <label className="field">
          Logo <span className="field__hint">(PNG/JPG/WebP, resized automatically, counts toward your media storage)</span>
          <input type="file" accept="image/*" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        </label>
      </div>
      {error && <p className="formerror">{error}</p>}
      <div className="pwform__btns">
        <button className="btn btn--primary btn--sm" disabled={busy} onClick={save}>
          {busy ? "Saving…" : "Save branding"}
        </button>
        {profile.brand_logo && (
          <button className="btn btn--ghost btn--sm" disabled={busy} onClick={clearLogo}>
            Clear logo
          </button>
        )}
      </div>
    </section>
  );
}

/* ---------------- §K2: change password ---------------- */

function ChangePasswordPanel({ token, onToast }) {
  const [open, setOpen] = useState(false);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const submit = async (e) => {
    e.preventDefault();
    if (next !== confirm) {
      setError("Those new passwords don't match.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.passwordChange(token, current, next);
      // The server kept THIS session's token and revoked every other one,
      // then emailed a heads-up — nothing to store client-side.
      onToast("Password changed ✔ — other sessions were signed out.");
      setOpen(false);
      setCurrent("");
      setNext("");
      setConfirm("");
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel">
      <h2 className="h2">Password</h2>
      {!open ? (
        <button type="button" className="btn btn--ghost" onClick={() => setOpen(true)}>
          Change password…
        </button>
      ) : (
        <form className="pwform" onSubmit={submit}>
          <label className="field">
            Current password
            <input
              type="password"
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
              required
              autoComplete="current-password"
              autoFocus
            />
          </label>
          <label className="field">
            New password
            <input
              type="password"
              value={next}
              onChange={(e) => setNext(e.target.value)}
              required
              autoComplete="new-password"
            />
          </label>
          <label className="field">
            New password, again
            <input
              type="password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              required
              autoComplete="new-password"
            />
          </label>
          {error && <p className="formerror">{error}</p>}
          <div className="pwform__btns">
            <button className="btn btn--primary" disabled={busy}>
              {busy ? "…" : "Change password"}
            </button>
            <button type="button" className="btn btn--ghost" disabled={busy} onClick={() => setOpen(false)}>
              Cancel
            </button>
          </div>
          <p className="footnote">This device stays signed in; every other session is signed out, and you'll get an email heads-up.</p>
        </form>
      )}
    </section>
  );
}

/* ---------------- One game row + expandable report ---------------- */

function HistoryRow({ game, token, onToast }) {
  const [open, setOpen] = useState(false);
  const [report, setReport] = useState(null);
  const [busy, setBusy] = useState(false);
  const [resuming, setResuming] = useState(false);
  const navigate = useNavigate();
  const finished = game.status === "finished";

  // §I: unfinished games get Resume — the server re-issues the host seat
  // token (works from a brand-new browser), we store it and land on /host,
  // where the normal connect flow reattaches with state intact.
  const resume = async (e) => {
    e.stopPropagation();
    setResuming(true);
    try {
      await resumeHostGame(token, game.code);
      navigate("/host");
    } catch (err) {
      setResuming(false);
      onToast(errorText(err));
    }
  };

  const toggle = useCallback(async () => {
    const next = !open;
    setOpen(next);
    // 409 route (§G2): non-finished games have no report body — don't fetch.
    if (!next || !finished || report || busy) return;
    setBusy(true);
    try {
      setReport(await api.gameReport(token, game.code));
    } catch (err) {
      setOpen(false);
      onToast(errorText(err));
    } finally {
      setBusy(false);
    }
  }, [open, finished, report, busy, token, game.code, onToast]);

  const when = new Date(game.finished_at || game.created_at).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });

  return (
    <li className="history__item">
      <button type="button" className={`historyrow ${open ? "historyrow--on" : ""}`} onClick={toggle}>
        <span className="historyrow__code">{game.code}</span>
        <span className="historyrow__meta">
          {game.mode === "drinks" ? "🍺 drinks" : "💯 points"} · {when} ·{" "}
          {finished ? "finished" : game.status === "active" ? "abandoned mid-game" : "never started"} ·{" "}
          {game.participant_count} team{game.participant_count === 1 ? "" : "s"}
        </span>
        <span className="historyrow__winners">
          {game.winners.length > 0 ? `🏆 ${game.winners.join(" & ")}` : finished ? "no winner" : "—"}
        </span>
        {!finished && (
          <span
            role="button"
            tabIndex={0}
            className="btn btn--gold btn--sm"
            onClick={resume}
            onKeyDown={(e) => e.key === "Enter" && resume(e)}
          >
            {resuming ? "Resuming…" : "▶ Resume"}
          </span>
        )}
      </button>
      {open && !finished && (
        <p className="footnote history__note">
          This game was never finished, so there's no report — standings and answers are only recorded at finish.
        </p>
      )}
      {open && finished && (busy || !report) && <p className="footnote history__note">Loading report…</p>}
      {open && finished && report && <Report report={report} />}
    </li>
  );
}

function Report({ report }) {
  const winnerIds = new Set(report.winners.map((w) => w.id));
  const players = report.participants.filter((p) => p.role === "player");
  return (
    <div className="report">
      <h2 className="h2">Final tallies</h2>
      <ol className="final__list final__list--report">
        {players
          .slice()
          .sort((a, b) => b.score - a.score)
          .map((p) => (
            <li key={p.id} className={`final__row ${winnerIds.has(p.id) ? "final__row--winner" : ""}`}>
              <span className="final__name">
                {winnerIds.has(p.id) && "🏆 "}
                {p.name}
                {/* §F (#11): the report keeps kicked seats (full history) —
                    badge them so the tallies read right. */}
                {p.removed && <span className="removedbadge">removed</span>}
              </span>
              <span className="final__stat">
                {report.mode === "points" ? `${p.score} pts` : `gave ${p.drinks_given} · took ${p.drinks_taken}`}
              </span>
            </li>
          ))}
      </ol>

      <h2 className="h2">The board</h2>
      {report.columns.map((col) => (
        <div key={col.id} className="reportcol">
          <div className="reportcol__cat">{col.category_name}</div>
          <ul className="reportq__list">
            {col.questions.map((q) => (
              <li key={q.id} className="reportq">
                <div className="reportq__top">
                  <span className="reportq__value">{report.mode === "drinks" ? `${q.value} 🍺` : q.value}</span>
                  <span className="reportq__text">{q.question_text}</span>
                </div>
                <div className="reportq__bottom">
                  <span className="reportq__answer">{q.answer}</span>
                  <span className="reportq__played">
                    {q.state !== "answered"
                      ? "not played"
                      : q.answered_correctly && q.answered_by_name
                        ? `won by ${q.answered_by_name}`
                        : "no correct answer"}
                  </span>
                </div>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}
