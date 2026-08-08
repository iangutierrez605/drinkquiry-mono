import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, errorText } from "../lib/api";
import { loadAuth, onAuthChange } from "../lib/storage";
import AuthScreen from "../components/AuthScreen";
import { resumeHostGame } from "./HostPage";

/**
 * §I4 (Handoff #13): /tournaments/:id — the host's control room.
 *
 * Rounds render as columns: each shows its games (code, status, standings
 * once finished), the round's confirmed advancers, an Advance control
 * (top 1 / top 2 — advancement is host-CONFIRMED, server-COMPUTED, and
 * re-runnable until the next round starts in earnest), and "New game in
 * round N" which routes to the ordinary /host create screen carrying
 * ?tournament=ID&round=N. Games are still just games — their boards,
 * codes and buzzers work exactly as ever.
 */
export default function TournamentDetailPage() {
  const [auth, setAuth] = useState(loadAuth());
  useEffect(() => onAuthChange(() => setAuth(loadAuth())), []);
  if (!auth) return <AuthScreen onAuthed={setAuth} />;
  return <TournamentDetailBody auth={auth} />;
}

function TournamentDetailBody({ auth }) {
  const { id } = useParams();
  const navigate = useNavigate();
  const [t, setT] = useState(null); // the detail payload
  const [loadError, setLoadError] = useState(null);
  const [busy, setBusy] = useState(false);
  // Advance rejections (round incomplete / empty) are per-round, inline.
  const [roundErrors, setRoundErrors] = useState({});

  const reload = useCallback(() => {
    let alive = true;
    api
      .tournament(auth.token, id)
      .then((data) => {
        if (!alive) return;
        setT(data);
        setLoadError(null);
      })
      .catch((err) => alive && setLoadError(errorText(err)));
    return () => {
      alive = false;
    };
  }, [auth.token, id]);
  useEffect(reload, [reload]);

  const advance = async (round, perGame) => {
    setBusy(true);
    setRoundErrors((e) => ({ ...e, [round]: null }));
    try {
      await api.advanceRound(auth.token, id, round, perGame);
      reload();
    } catch (err) {
      // The pinned 409s (tournament_round_incomplete / _empty) carry a
      // human "detail" — errorText surfaces it as-is.
      setRoundErrors((e) => ({ ...e, [round]: errorText(err) }));
    } finally {
      setBusy(false);
    }
  };

  // #13 fix: a tournament is MANY games, and the host needs to jump
  // between their consoles. Same path as /profile's Resume: the server
  // re-issues the seat token, it's stored under the normal keys, and /host
  // renders that game's console.
  const hostGame = async (code) => {
    setBusy(true);
    try {
      await resumeHostGame(auth.token, code);
      navigate("/host");
    } catch (err) {
      setLoadError(errorText(err));
      setBusy(false);
    }
  };

  const finish = async () => {
    setBusy(true);
    try {
      await api.finishTournament(auth.token, id);
      reload();
    } catch (err) {
      setLoadError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  if (loadError)
    return (
      <div className="page">
        <h1 className="h1">Tournament</h1>
        <p className="formerror formerror--block">{loadError}</p>
        <Link className="btn btn--ghost" to="/tournaments">
          Back to tournaments
        </Link>
      </div>
    );
  if (!t)
    return (
      <div className="page">
        <h1 className="h1">Tournament</h1>
        <p>Loading…</p>
      </div>
    );

  const finished = Boolean(t.finished_at);
  // Group games by round (the API orders by round, then created).
  const byRound = new Map();
  for (const g of t.games) {
    if (!byRound.has(g.round_number)) byRound.set(g.round_number, []);
    byRound.get(g.round_number).push(g);
  }
  const rounds = [...byRound.keys()].sort((a, b) => a - b);
  const nextRound = rounds.length ? rounds[rounds.length - 1] + 1 : 1;
  const advancersFor = (round) => t.advancers.filter((a) => a.round_number === round);
  // §F2 (#20): the host routes qualifiers when a round splits into several
  // games (C-4; one game auto-targets server-side). Same reload-after-write
  // convention as advance — the server's payload is the truth.
  const setTarget = async (advancerId, gameCode) => {
    setBusy(true);
    try {
      await api.setAdvancerTarget(auth.token, id, advancerId, gameCode || null);
      reload();
    } catch (err) {
      setLoadError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page page--wide">
      <div className="tournamenthead">
        <div>
          <h1 className="h1">{t.name}</h1>
          <p className="footnote">
            {t.location ? `${t.location} · ` : ""}
            {finished
              ? `finished ${new Date(t.finished_at).toLocaleDateString()}`
              : `started ${new Date(t.created_at).toLocaleDateString()}`}
            {finished && <span className="badge"> finished</span>}
          </p>
        </div>
        <div className="tournamenthead__actions">
          {/* §F4b (#21), C-8: the Delete button is GONE (deliberately
              removed, not deferred — the server 409s owner deletes too).
              Deleting looked like the way to free a stuck pass, orphaned
              real history, and the pass bug is fixed at the source now. */}
          {!finished && <ArmButton label="Finish tournament" armedLabel="Really finish?" onFire={finish} disabled={busy} />}
          <Link className="btn btn--ghost btn--sm" to="/tournaments">
            All tournaments
          </Link>
        </div>
      </div>

      {finished && (
        <p className="footnote">
          This tournament is sealed — its games and results stay right here, but new games and advancement are
          closed. Reopen isn't a thing; start a new one.
        </p>
      )}

      {rounds.length === 0 && !finished && (
        <section className="panel panel--center">
          <h2 className="h2">No games yet</h2>
          <p className="footnote">Start round 1 — each game is an ordinary game with its own code and board.</p>
          {/* §F4c (Handoff #17): the one honest sentence that kills the
              unattached-by-accident mystery. The plain create screen stays
              nag-free on purpose — this is the page that cares. */}
          <p className="footnote">
            Games join this tournament only when you create them from these buttons — a game made from the
            plain Host screen stays a plain game.
          </p>
          <Link className="btn btn--gold" to={`/host?tournament=${t.id}&round=1`}>
            New game in round 1
          </Link>
        </section>
      )}

      <div className="roundcols">
        {rounds.map((round) => (
          <section key={round} className="roundcol">
            <h2 className="roundcol__title">Round {round}</h2>
            {byRound.get(round).map((g) => (
              <div key={g.code} className="tgamecard">
                <div className="tgamecard__head">
                  <span className="historyrow__code">{g.code}</span>
                  <span className="tgamecard__status">
                    {g.mode === "drinks" ? "🍺" : "💯"}{" "}
                    {g.status === "finished" ? "finished" : g.status === "lobby" ? "in the lobby" : "playing"}
                  </span>
                </div>
                {g.status !== "finished" && !finished && (
                  <button
                    type="button"
                    className="btn btn--primary btn--sm tgamecard__host"
                    disabled={busy}
                    onClick={() => hostGame(g.code)}
                  >
                    Host console
                  </button>
                )}
                {g.standings ? (
                  <ol className="tstandings">
                    {g.standings.map((s) => (
                      <li key={s.name} className="tstanding">
                        <span>
                          <span className="tstanding__rank">#{s.rank}</span> {s.name}
                        </span>
                        <strong>{s.score}</strong>
                      </li>
                    ))}
                  </ol>
                ) : (
                  <p className="footnote">Standings appear when the game finishes.</p>
                )}
                {/* §F2b (#20): who belongs in THIS lobby — the qualifiers
                    routed here, each with claim state. This list is the
                    truth teams re-join by if phones go missing (C-6). */}
                {g.status === "lobby" &&
                  (() => {
                    const quals = t.advancers.filter((a) => a.target_game === g.code);
                    if (!quals.length) return null;
                    return (
                      <ul className="quallist">
                        {quals.map((a) => (
                          <li key={a.id} className="qualrow">
                            <span>{a.name}</span>
                            {a.claimed ? (
                              <span className="qualrow__state qualrow__state--claimed">claimed ✓</span>
                            ) : (
                              <span className="qualrow__state">waiting</span>
                            )}
                          </li>
                        ))}
                      </ul>
                    );
                  })()}
              </div>
            ))}

            {advancersFor(round).length > 0 && (
              <div className="advancerbox">
                <strong>Going through:</strong>
                <ul className="advancerlist">
                  {advancersFor(round).map((a) => {
                    // §F2a (#20): with several next-round games the host
                    // routes each qualifier (C-4); with exactly one the
                    // server auto-targets and this just shows it.
                    const nextGames = byRound.get(round + 1) || [];
                    return (
                      <li key={a.id ?? `${a.source_game}-${a.name}`}>
                        {a.name} <span className="footnote">(#{a.rank} in {a.source_game})</span>
                        {a.claimed && <span className="qualrow__state qualrow__state--claimed"> claimed ✓</span>}
                        {!finished && !a.claimed && nextGames.length > 1 && (
                          <select
                            className="targetpick"
                            value={a.target_game || ""}
                            disabled={busy}
                            onChange={(e) => setTarget(a.id, e.target.value || null)}
                          >
                            <option value="">— ask your host —</option>
                            {nextGames.map((ng) => (
                              <option key={ng.code} value={ng.code}>
                                → {ng.code}
                              </option>
                            ))}
                          </select>
                        )}
                        {!a.claimed && nextGames.length <= 1 && a.target_game && (
                          <span className="footnote"> → {a.target_game}</span>
                        )}
                      </li>
                    );
                  })}
                </ul>
              </div>
            )}

            {!finished && (
              <div className="roundcol__actions">
                <div className="advancebtns">
                  <button className="btn btn--primary btn--sm" disabled={busy} onClick={() => advance(round, 1)}>
                    Advance winners
                  </button>
                  <button className="btn btn--ghost btn--sm" disabled={busy} onClick={() => advance(round, 2)}>
                    Advance top 2
                  </button>
                </div>
                {roundErrors[round] && <p className="formerror">{roundErrors[round]}</p>}
                <Link className="btn btn--ghost btn--sm" to={`/host?tournament=${t.id}&round=${round}`}>
                  + game in round {round}
                </Link>
              </div>
            )}
          </section>
        ))}

        {!finished && rounds.length > 0 && (
          <section className="roundcol roundcol--next">
            <h2 className="roundcol__title">Round {nextRound}</h2>
            <p className="footnote">
              Advance round {nextRound - 1} first, then build the next board — winning phones get a
              one-tap "take your seat" the moment it's in the lobby (joining by code always works too).
            </p>
            <Link className="btn btn--gold" to={`/host?tournament=${t.id}&round=${nextRound}`}>
              New game in round {nextRound}
            </Link>
          </section>
        )}
      </div>
    </div>
  );
}

/** Two-tap arm-then-fire (the CreatePage DeleteButton pattern, local copy). */
function ArmButton({ label, armedLabel, onFire, danger = false, disabled = false }) {
  const [arming, setArming] = useState(false);
  useEffect(() => {
    if (!arming) return undefined;
    const t = setTimeout(() => setArming(false), 3500);
    return () => clearTimeout(t);
  }, [arming]);
  return arming ? (
    <button className="btn btn--danger btn--sm" onClick={onFire} disabled={disabled}>
      {armedLabel}
    </button>
  ) : (
    <button className={`btn btn--sm ${danger ? "btn--ghost" : "btn--primary"}`} onClick={() => setArming(true)} disabled={disabled}>
      {label}
    </button>
  );
}
