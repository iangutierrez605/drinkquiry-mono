import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, errorText } from "../lib/api";
import { clearAuth, loadAuth } from "../lib/storage";
import { ModerationLink, Toast, UsageMeterLine } from "../components/shared";

const fmtMB = (bytes) => `${Math.round((bytes / 1024 / 1024) * 10) / 10} MB`;
import { AuthScreen } from "./HostPage";

/**
 * /profile (Handoff #6 §G3): the signed-in host's identity + plan block and
 * their game history. Knox-gated exactly like /host — same stored dq_auth,
 * same login screen (imported, not forked). Selecting a finished game loads
 * its report (participants, tallies, winners highlighted, every question with
 * its answer). Non-finished games have no report body: the backend's chosen
 * rule is 409-until-finished, so we don't even request one.
 */
export default function ProfilePage() {
  const [auth, setAuth] = useState(loadAuth());
  if (!auth) return <AuthScreen onAuthed={setAuth} />;
  return (
    <ProfileBody
      auth={auth}
      onAuthGone={() => setAuth(null)} // Knox token died mid-visit → login form
      onLogout={() => {
        api.logout(auth.token).catch(() => {});
        clearAuth();
        setAuth(null);
      }}
    />
  );
}

function ProfileBody({ auth, onAuthGone, onLogout }) {
  const [profile, setProfile] = useState(null);
  const [games, setGames] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [toast, setToast] = useState(null);

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
      <header className="pagehead">
        <Link to="/" className="wordmark wordmark--small wordmark--link">
          DRINKQUIRY
        </Link>
        <div className="pagehead__right">
          <span className="pagehead__user">{profile?.display_name || auth.user?.display_name || auth.user?.email}</span>
          {profile?.is_staff && <ModerationLink token={auth.token} />}
          <Link className="btn btn--ghost" to="/create">
            Your content
          </Link>
          <Link className="btn btn--ghost" to="/host">
            Host a game
          </Link>
          <button className="btn btn--ghost" onClick={onLogout}>
            Log out
          </button>
        </div>
      </header>

      <h1 className="h1">Your profile</h1>
      {loadError && <p className="formerror formerror--block">{loadError}</p>}

      {profile && (
        <section className="panel">
          <h2 className="h2">Account</h2>
          <div className="idblock">
            <div className="idblock__name">{profile.display_name || profile.email}</div>
            <div className="idblock__mail">{profile.email}</div>
            <span className={`planchip ${profile.plan !== "free" ? "planchip--paid" : ""}`}>
              {profile.plan} plan
            </span>
          </div>
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

/* ---------------- One game row + expandable report ---------------- */

function HistoryRow({ game, token, onToast }) {
  const [open, setOpen] = useState(false);
  const [report, setReport] = useState(null);
  const [busy, setBusy] = useState(false);
  const finished = game.status === "finished";

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
