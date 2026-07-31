import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, errorText, quotaError } from "../lib/api";
import { loadAuth, onAuthChange } from "../lib/storage";
import AuthScreen from "../components/AuthScreen";

/**
 * §I4 (Handoff #13): /tournaments — the host's tournament list + create.
 *
 * Plan gating is the SERVER's quota choke point (free's tournaments limit is
 * 0 → structured quota_tournaments 403); this page renders honestly off the
 * profile's own usage meter — limit 0 shows the upsell instead of the form,
 * so a staff limit_overrides grant on a free account just works (the meter
 * reflects overrides). The list itself is ungated: any signed-in host can
 * GET it (a free account's is simply empty).
 */
export default function TournamentsPage() {
  // Hooks first, early returns after (C11); the auth funnel is the house
  // pattern (HostPage/ProfilePage) — same stored dq_auth, same AuthScreen.
  const [auth, setAuth] = useState(loadAuth());
  useEffect(() => onAuthChange(() => setAuth(loadAuth())), []);
  if (!auth) return <AuthScreen onAuthed={setAuth} />;
  return <TournamentsBody auth={auth} />;
}

function TournamentsBody({ auth }) {
  const [rows, setRows] = useState(null); // null = loading
  const [meter, setMeter] = useState(undefined); // usage.tournaments | undefined while loading
  const [loadError, setLoadError] = useState(null);
  const [name, setName] = useState("");
  const [location, setLocation] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState(null);

  const reload = useCallback(() => {
    let alive = true;
    Promise.all([api.tournaments(auth.token), api.profile(auth.token)])
      .then(([list, profile]) => {
        if (!alive) return;
        setRows(list);
        setMeter(profile?.usage?.tournaments ?? null);
        setLoadError(null);
      })
      .catch((err) => alive && setLoadError(errorText(err)));
    return () => {
      alive = false;
    };
  }, [auth.token]);
  useEffect(reload, [reload]);

  const create = async (e) => {
    e.preventDefault();
    setBusy(true);
    setFormError(null);
    try {
      await api.createTournament(auth.token, { name: name.trim(), location: location.trim() });
      setName("");
      setLocation("");
      reload();
    } catch (err) {
      const q = quotaError(err);
      setFormError(
        q
          ? `You've used all ${q.limit} tournaments on your plan (${q.used} live). Finish or delete one first.`
          : errorText(err), // duplicate live name lands here as name: [...]
      );
    } finally {
      setBusy(false);
    }
  };

  if (loadError)
    return (
      <div className="page">
        <h1 className="h1">Tournaments</h1>
        <p className="formerror formerror--block">{loadError}</p>
      </div>
    );
  if (rows === null || meter === undefined)
    return (
      <div className="page">
        <h1 className="h1">Tournaments</h1>
        <p>Loading…</p>
      </div>
    );

  // limit 0 = the plan gate (the server's own meter, overrides included).
  const gated = meter?.limit === 0;

  return (
    <div className="page">
      <h1 className="h1">Tournaments</h1>
      <p className="footnote">
        A tournament is rounds of ordinary games under one name — run heats, advance the best, crown a champion.
        Every game still gets its own code, board and buzzers.
      </p>

      {gated ? (
        <section className="panel panel--center upsell">
          <span className="upsell__emoji">🏆</span>
          <h2 className="h2">Tournaments are a creator feature</h2>
          <p className="footnote">
            Coming soon. For now, an admin can enable creator access on your account — everything else about
            hosting single games works without it.
          </p>
          <Link className="btn btn--ghost" to="/host">
            Back to hosting
          </Link>
        </section>
      ) : (
        <section className="panel">
          <h2 className="h2">New tournament</h2>
          <form className="tournamentform" onSubmit={create}>
            <label className="field">
              Name
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                maxLength={80}
                placeholder="Summer Fest Trivia Tournament"
                required
              />
            </label>
            <label className="field">
              Venue / location <span className="field__hint">(optional — shows on the TV as "hosted by …")</span>
              <input
                value={location}
                onChange={(e) => setLocation(e.target.value)}
                maxLength={120}
                placeholder="Ian's Bar Venue"
              />
            </label>
            {formError && <p className="formerror">{formError}</p>}
            <button className="btn btn--gold" disabled={busy || !name.trim()}>
              {busy ? "Creating…" : "Create tournament"}
            </button>
            {meter?.limit != null && (
              <p className="footnote">
                {meter.used} of {meter.limit} tournaments in use.
              </p>
            )}
          </form>
        </section>
      )}

      <section className="panel">
        <h2 className="h2">Your tournaments</h2>
        {rows.length === 0 ? (
          <p className="footnote">Nothing yet{gated ? "" : " — create one above"}.</p>
        ) : (
          <ul className="tournamentlist">
            {rows.map((t) => (
              <li key={t.id}>
                <Link className="tournamentrow" to={`/tournaments/${t.id}`}>
                  <strong className="tournamentrow__name">{t.name}</strong>
                  <span className="tournamentrow__meta">
                    {t.location ? `${t.location} · ` : ""}
                    {t.finished_at
                      ? `finished ${new Date(t.finished_at).toLocaleDateString()}`
                      : `started ${new Date(t.created_at).toLocaleDateString()}`}
                  </span>
                  {t.finished_at ? (
                    <span className="badge">finished</span>
                  ) : (
                    <span className="badge badge--live">live</span>
                  )}
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
