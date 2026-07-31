import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { api, errorText, ApiError, SUPPORT_EMAIL } from "../lib/api";
import { clearSeat, loadSeat, saveSeat } from "../lib/storage";
import { useGameSocket } from "../lib/useGameSocket";
import { ensureAudio, playBuzz } from "../lib/sounds";
import { FinalStandings, ScoreStrip, Toast } from "../components/shared";

export default function BuzzerPage() {
  const { code = "" } = useParams();
  const upper = code.toUpperCase();
  const [seat, setSeat] = useState(() => loadSeat(upper));

  const dropSeat = useCallback(() => {
    clearSeat(upper);
    setSeat(null);
  }, [upper]);

  if (!seat?.token || !seat?.participantId)
    return <JoinForm code={upper} existing={seat} onJoined={setSeat} onDropSeat={dropSeat} />;
  return <BuzzerLive code={upper} seat={seat} onBadSeat={dropSeat} />;
}

/* ---------------- Join ---------------- */

function JoinForm({ code, existing, onJoined, onDropSeat }) {
  const [name, setName] = useState(existing?.name || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [nameTaken, setNameTaken] = useState(false);
  const [fullLimit, setFullLimit] = useState(null); // §G: server said game_full
  // §H (#11): the brand line under the wordmark ("hosted by THE KINGS
  // ARMS"). Pre-join there's no socket yet, so one unauthenticated snapshot
  // fetch supplies it — optional sugar, a failure just shows nothing.
  const [brand, setBrand] = useState(null);
  // §I (#13): the join screen's compact tournament line — same snapshot
  // fetch, another display-only field.
  const [tournament, setTournament] = useState(null);
  useEffect(() => {
    let alive = true;
    api
      .gameSnapshot(code)
      .then((snap) => {
        if (!alive) return;
        setBrand(snap.brand ?? null);
        setTournament(snap.tournament ?? null);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [code]);
  const brandLine = brand?.name ? (
    <p className="joinbrand">
      hosted by <strong>{brand.name}</strong>
    </p>
  ) : null;
  // §I3 (#13): above the brand line at both render sites — teams should see
  // they're joining ROUND N of a named thing before they type.
  const tournamentLine = tournament ? (
    <p className="jointournament">
      🏆 {tournament.name} — Round {tournament.round_number}
    </p>
  ) : null;

  const join = async (useToken) => {
    setBusy(true);
    setError(null);
    setNameTaken(false);
    try {
      const res = await api.joinGame(code, name.trim(), useToken ? existing?.token : undefined);
      const seat = {
        token: res.participant_token,
        participantId: res.participant.id,
        name: res.participant.name,
        role: res.participant.role,
      };
      saveSeat(code, seat);
      onJoined(seat);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409 && err.data?.code === "game_full") {
        // Cosmetic only (rule 4): the server is the gate; we just say it nicely.
        setFullLimit(err.data.limit ?? 6);
      } else if (err instanceof ApiError && err.status === 400 && err.data?.name) {
        // Name taken: if we still hold a token for this code, offer reclaim.
        setNameTaken(true);
        setError(err.data.name.join(" "));
      } else if (err instanceof ApiError && err.status === 404) {
        // §H3 (Handoff #12): confused guests are the other support moment.
        setError(`No game with code ${code}. Double-check with the host — or email ${SUPPORT_EMAIL}.`);
      } else {
        setError(errorText(err));
      }
    } finally {
      setBusy(false);
    }
  };

  if (fullLimit != null)
    return (
      <div className="page page--center page--buzzer">
        <div className="wordmark">DRINKQUIRY</div>
        {tournamentLine}
        {brandLine}
        <p className="joincode">
          Game <strong>{code}</strong>
        </p>
        <div className="panel joinform">
          <h2 className="h2">Table's full! 🍻</h2>
          <p>
            This game already has its {fullLimit} teams. Huddle up with one of them and share their
            phone — one buzzer per team is the whole idea.
          </p>
          {existing?.token && (
            <button type="button" className="btn btn--gold" disabled={busy} onClick={() => join(true)}>
              We already had a seat — reclaim it
            </button>
          )}
          <button type="button" className="btn btn--ghost" onClick={() => setFullLimit(null)}>
            Back
          </button>
        </div>
      </div>
    );

  return (
    <div className="page page--center page--buzzer">
      <div className="wordmark">DRINKQUIRY</div>
      {tournamentLine}
      {brandLine}
      <p className="joincode">
        Game <strong>{code}</strong>
      </p>
      <form
        className="panel joinform"
        onSubmit={(e) => {
          e.preventDefault();
          join(false);
        }}
      >
        <label className="field">
          Team name
          {/* §H1: caps in the VALUE (not just the rendering) via
              toUpperCase() here, plus text-transform in CSS so it looks
              right even mid-composition. The server normalizes again —
              this is cosmetic (rule 4). */}
          <input
            className="capsinput"
            value={name}
            onChange={(e) => setName(e.target.value.toUpperCase())}
            required
            maxLength={50}
            placeholder="e.g. QUIZZY MCGUINNESS"
            autoFocus
          />
        </label>
        {error && <p className="formerror">{error}</p>}
        {nameTaken && existing?.token && (
          <button type="button" className="btn btn--gold" disabled={busy} onClick={() => join(true)}>
            That's us — reclaim our seat
          </button>
        )}
        <button className="btn btn--primary btn--big" disabled={busy || !name.trim()}>
          {busy ? "Joining…" : "Join game"}
        </button>
        {existing?.token && !nameTaken && (
          <button type="button" className="btn btn--ghost" onClick={onDropSeat}>
            Forget saved seat
          </button>
        )}
      </form>
    </div>
  );
}

/* ---------------- Live buzzer ---------------- */

function BuzzerLive({ code, seat, onBadSeat }) {
  const { game, connected, authFailed, removed, lastError, lastBuzz, clearError, send } =
    useGameSocket(code, seat.token);

  useEffect(() => {
    if (authFailed) onBadSeat(); // 4001: clear token, back to the name form
  }, [authFailed, onBadSeat]);

  // §F (Handoff #11): the host kicked this seat. Three detection paths (C2
  // thinking — WS error/close AND the snapshot itself): the structured
  // `player_removed` error, close code 4003 (both via the hook's `removed`),
  // or our participantId simply missing from a fresh snapshot — removed
  // seats are excluded from `participants` entirely.
  const kicked =
    removed || (game != null && !game.participants.some((p) => p.id === seat.participantId));
  useEffect(() => {
    if (kicked) clearSeat(code); // the token is dead server-side; drop it now
  }, [kicked, code]);

  // Haptic + confirmation on our own buzz echo.
  useEffect(() => {
    if (lastBuzz && lastBuzz.participant_id === seat.participantId && navigator.vibrate) {
      navigator.vibrate(80);
    }
  }, [lastBuzz, seat.participantId]);

  const self = useMemo(
    () => game?.participants.find((p) => p.id === seat.participantId),
    [game, seat.participantId],
  );
  const cell = game?.current_cell;
  // §F/§G: everything below derives from the snapshot (rule 1) — the verdict
  // marker, the once-per-cell assigned flag, and the winner check all come
  // from server state; the server re-validates every give_drink (rule 4).
  const judgment = cell?.last_judgment ?? null;
  const iWon = judgment?.correct === true && judgment.participant_id === seat.participantId;
  const iWhiffed = judgment?.correct === false && judgment.participant_id === seat.participantId;
  const canGiveDrink =
    game?.mode === "drinks" && iWon && cell && !cell.drinks_assigned && game.status === "active";
  const buzzes = cell?.buzzes ?? [];
  const myBuzzIndex = buzzes.findIndex((b) => b.participant_id === seat.participantId);
  const alreadyBuzzed = myBuzzIndex >= 0;
  const canBuzz = game?.status === "active" && !!cell && game.buzzer_open && !alreadyBuzzed;

  // Press feedback independent of network. The tap itself is the user
  // gesture, so playing here satisfies autoplay policy and gives tactile
  // feedback before the server round-trip. §H (#13): the sound is now THE
  // GAME'S (host-chosen snapshot.buzz_sound) — every phone makes the same
  // noise, as asked. Server state, never guessed locally (rule 1).
  const [pressed, setPressed] = useState(false);
  const pressTimer = useRef(null);
  const buzz = () => {
    if (!canBuzz) return;
    ensureAudio();
    playBuzz(game?.buzz_sound ?? 1);
    send("buzz");
    setPressed(true);
    clearTimeout(pressTimer.current);
    pressTimer.current = setTimeout(() => setPressed(false), 250);
  };

  // §F (#11): the kicked card wins over everything (all hooks above have
  // already run, so this early return is hook-order safe). No shaming copy —
  // hosts kick for lots of reasons.
  if (kicked)
    return (
      <div className="page page--center page--buzzer">
        <div className="wordmark">DRINKQUIRY</div>
        <div className="panel joinform kickedcard">
          <h2 className="h2">This buzzer was removed 🍃</h2>
          <p>
            The host removed this buzzer from game <strong>{code}</strong> — grab a teammate's
            phone, or rejoin with a new name.
          </p>
          <button type="button" className="btn btn--primary btn--big" onClick={onBadSeat}>
            Join again
          </button>
        </div>
      </div>
    );

  if (!game)
    return (
      <div className="page page--center page--buzzer">
        <div className="wordmark">DRINKQUIRY</div>
        <p>Connecting…</p>
      </div>
    );

  // §H4 (Handoff #10): once the game is FINISHED the dead buzz circle is
  // the wrong screen — render a proper finale instead (below). The socket
  // stays open (late snapshots still render); no new WS events.
  const finished = game.status === "finished";
  // Winner check, derived LOCALLY from the live snapshot exactly the way
  // FinalStandings ranks (points → score, drinks → drinks_given): the
  // snapshot has no winners field (that lives on history/report serializers)
  // and doesn't need one.
  const rankStat = (p) => (game.mode === "points" ? p.score : p.drinks_given);
  const playersAll = game.participants.filter((p) => p.role === "player");
  const topStat = playersAll.length ? Math.max(...playersAll.map(rankStat)) : 0;
  const iAmWinner = finished && self != null && playersAll.length > 0 && rankStat(self) === topStat;

  if (finished)
    return (
      <div className="page page--buzzer buzzover">
        <header className="buzzhead">
          <span className="buzzhead__team">{self?.name ?? seat.name}</span>
          <span className={`conn ${connected ? "conn--on" : "conn--off"}`}>
            <span className="conn__dot" />
            {connected ? code : "reconnecting"}
          </span>
        </header>
        {iAmWinner && <div className="buzzover__trophy">🏆</div>}
        <div className="buzzover__title">GAME OVER 🍻</div>
        {iAmWinner && <p className="buzzover__winner">Champions — that's you!</p>}
        <FinalStandings game={game} />
        {self && (
          <p className="buzzfinal">
            {game.mode === "points"
              ? `Final score: ${self.score} pts`
              : `You gave ${self.drinks_given} 🍺 and took ${self.drinks_taken} 🍻`}
          </p>
        )}
        <p className="footnote buzzover__foot">Thanks for playing — hand the phone back. 🍻</p>
        <Toast message={lastError} onDone={clearError} />
      </div>
    );

  let stateLabel;
  let stateClass;
  if (game.status === "lobby") {
    stateLabel = "Waiting for the host to start";
    stateClass = "idle";
  } else if (!cell) {
    stateLabel = "Watch the board…";
    stateClass = "idle";
  } else if (alreadyBuzzed) {
    stateLabel = `You buzzed ${ordinal(myBuzzIndex + 1)}`;
    stateClass = "buzzed";
  } else if (game.buzzer_open) {
    stateLabel = "BUZZ!";
    stateClass = "open";
  } else {
    stateLabel = "Locked — listen up";
    stateClass = "locked";
  }

  return (
    <div className="page page--buzzer">
      <header className="buzzhead">
        <span className="buzzhead__team">{self?.name ?? seat.name}</span>
        <span className={`conn ${connected ? "conn--on" : "conn--off"}`}>
          <span className="conn__dot" />
          {connected ? code : "reconnecting"}
        </span>
      </header>

      <div className="buzzstage">
        <button
          type="button"
          className={`bigbuzz bigbuzz--${stateClass} ${pressed ? "bigbuzz--pressed" : ""}`}
          disabled={!canBuzz}
          onPointerDown={buzz}
        >
          <span className="bigbuzz__label">{stateLabel}</span>
          {cell && !alreadyBuzzed && (
            <span className="bigbuzz__value">
              {game.mode === "drinks" ? `${cell.value} 🍺 on the line` : `${cell.value} pts on the line`}
            </span>
          )}
        </button>
      </div>

      {judgment && (
        <div className={`buzzverdict ${judgment.correct ? "buzzverdict--right" : "buzzverdict--wrong"}`}>
          {iWon
            ? "You got it! 🎉"
            : iWhiffed
              ? "Wrong — drink's coming 🍻"
              : judgment.correct
                ? `${judgment.name} got it`
                : `${judgment.name} whiffed — buzz in!`}
        </div>
      )}

      {canGiveDrink && (
        <div className="drinkpick">
          <h2 className="h2 drinkpick__title">Who drinks {cell.value} 🍺? Your call.</h2>
          <div className="drinkpick__btns">
            {game.participants.map((p) => (
              <button
                key={p.id}
                type="button"
                className="btn btn--drink"
                onClick={() => send("give_drink", { target_participant_id: p.id })}
              >
                🍺 {p.id === seat.participantId ? `${p.name} (us!)` : p.role === "host" ? `${p.name} (the host)` : p.name}
              </button>
            ))}
          </div>
        </div>
      )}

      {cell?.drink_assignment && (
        <p className="buzzdrinkline">
          {cell.drink_assignment.from_name} sends {cell.drink_assignment.amount} 🍺 to{" "}
          {cell.drink_assignment.to_participant_id === seat.participantId
            ? "YOU — drink up!"
            : cell.drink_assignment.to_name}
        </p>
      )}

      {cell && buzzes.length > 0 && !judgment && (
        <ol className="buzzorder">
          {buzzes.map((b, i) => (
            <li key={b.participant_id} className={b.participant_id === seat.participantId ? "buzzorder--self" : ""}>
              <span>{i + 1}.</span> {b.name}
            </li>
          ))}
        </ol>
      )}

      <footer className="buzzfoot">
        <ScoreStrip game={game} selfId={seat.participantId} />
      </footer>

      <Toast message={lastError} onDone={clearError} />
    </div>
  );
}

function ordinal(n) {
  const s = ["th", "st", "nd", "rd"];
  const v = n % 100;
  return n + (s[(v - 20) % 10] || s[v] || s[0]);
}
