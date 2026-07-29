import { useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { QRCodeSVG } from "qrcode.react";
import { api } from "../lib/api";
import { loadSeat } from "../lib/storage";
import { useGameSocket } from "../lib/useGameSocket";
import { ensureAudio, playBuzz } from "../lib/sounds";
import { BoardGrid, FinalStandings, MediaBlock } from "../components/shared";

/**
 * Read-only projected view. It never sends actions.
 *
 * Preferred transport: the WebSocket, using any participant token already in
 * this browser's localStorage for this code (e.g. the host opened the board
 * in a second tab). Fallback: poll GET /api/games/{code}/ (no auth), so a TV
 * on a different device still works with zero setup.
 */
export default function BoardPage() {
  const { code = "" } = useParams();
  const upper = code.toUpperCase();
  const seat = useMemo(() => loadSeat(upper), [upper]);

  const ws = useGameSocket(upper, seat?.token || null);
  const [polled, setPolled] = useState(null);
  const [pollError, setPollError] = useState(null);
  const usePolling = !seat?.token || ws.authFailed;

  useEffect(() => {
    if (!usePolling) return undefined;
    let alive = true;
    const tick = async () => {
      try {
        const snap = await api.gameSnapshot(upper);
        if (alive) {
          setPolled(snap);
          setPollError(null);
        }
      } catch {
        if (alive) setPollError(`Can't find game ${upper}.`);
      }
    };
    tick();
    const id = setInterval(tick, 1500);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [usePolling, upper]);

  const game = usePolling ? polled : ws.game;
  // Reveal (Handoff #6 §F2): both transports now derive it from the
  // snapshot's persisted `revealed_answer` (rule 1 — components render the
  // snapshot). In WS mode the hook's event-driven value stays as a fallback
  // so boards talking to an older backend (no snapshot field) don't regress.
  const revealedAnswer = game?.revealed_answer ?? (usePolling ? null : ws.revealedAnswer);

  // Flash on new buzzes + (§I) play the buzzing team's sound when toggled on.
  // Browser autoplay policy needs a user gesture before audio, so sound is
  // behind a small toggle, default OFF, no persistence. Sound is display-only
  // side data (rule 1): nothing gates on it.
  const [soundOn, setSoundOn] = useState(false);
  const soundOnRef = useRef(soundOn);
  soundOnRef.current = soundOn;
  const participantsRef = useRef(null);
  participantsRef.current = game?.participants ?? null;

  const playForParticipant = (participantId, fallbackOrder) => {
    if (!soundOnRef.current) return;
    const p = participantsRef.current?.find((x) => x.id === participantId);
    playBuzz(p?.buzzer_sound ?? fallbackOrder ?? 1);
  };

  // WS mode: consume the hook's existing lastBuzz output (rule 2 — the hook
  // itself is untouched).
  useEffect(() => {
    if (usePolling || !ws.lastBuzz) return;
    playForParticipant(ws.lastBuzz.participant_id, ws.lastBuzz.order);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ws.lastBuzz, usePolling]);

  // Both modes flash on buzz-count growth; in POLLING mode that same
  // snapshot diff (§B4's house technique) also identifies the newest buzz so
  // its team's sound can play — there is no lastBuzz without a socket.
  const prevBuzzCount = useRef(0);
  const [flash, setFlash] = useState(false);
  const buzzes = game?.current_cell?.buzzes;
  const buzzCount = buzzes?.length ?? 0;
  useEffect(() => {
    if (buzzCount > prevBuzzCount.current) {
      if (usePolling && buzzes?.length) {
        const newest = buzzes[buzzes.length - 1];
        playForParticipant(newest.participant_id, buzzes.length);
      }
      setFlash(true);
      const t = setTimeout(() => setFlash(false), 450);
      prevBuzzCount.current = buzzCount;
      return () => clearTimeout(t);
    }
    prevBuzzCount.current = buzzCount;
    return undefined;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [buzzCount, usePolling]);

  if (pollError && !game)
    return (
      <div className="tv tv--center">
        <div className="wordmark">DRINKQUIRY</div>
        <p className="tv__error">{pollError}</p>
      </div>
    );
  if (!game)
    return (
      <div className="tv tv--center">
        <div className="wordmark">DRINKQUIRY</div>
        <p>Tuning in to {upper}…</p>
      </div>
    );

  const cell = game.current_cell;
  const players = game.participants.filter((p) => p.role === "player");
  const hostSeat = game.participants.find((p) => p.role === "host");
  // §F: verdict straight from the snapshot (rule 1) — WS and polling boards
  // both get it, since it's a snapshot field, not an event.
  const judgment = cell?.last_judgment ?? null;
  const verdictOnScreen = judgment != null || revealedAnswer != null;

  return (
    <div className={`tv ${flash ? "tv--flash" : ""}`}>
      <header className="tv__head">
        <span className="wordmark wordmark--small">DRINKQUIRY</span>
        <button
          type="button"
          className={`btn btn--ghost tv__soundtoggle ${soundOn ? "tv__soundtoggle--on" : ""}`}
          onClick={() => {
            // The click is the gesture that unlocks WebAudio.
            const next = !soundOn;
            if (next) ensureAudio();
            setSoundOn(next);
          }}
          title={soundOn ? "Buzzer sounds on" : "Buzzer sounds off"}
        >
          {soundOn ? "🔊 sound on" : "🔇 sound off"}
        </button>
        <span className="codechip codechip--tv">
          Join: <strong>{game.code}</strong>
        </span>
      </header>

      {game.status === "lobby" && (
        <div className="tv__lobby">
          <p className="tv__lede">Grab a phone per team and join with code</p>
          <div className="tv__bigcode">{game.code}</div>
          {/* §H4: QR straight to this game's buzzer page. Origin-derived —
              never a hardcoded domain — and rendered locally (SVG, no network
              calls) on a white tile with quiet-zone padding so it scans from
              a couch against the chalkboard background. */}
          <div className="tv__qr" aria-label="Scan to join as a buzzer">
            <QRCodeSVG
              value={`${window.location.origin}/game/buzzer/${game.code}`}
              size={172}
              marginSize={2}
              bgColor="#ffffff"
              fgColor="#1e2621"
            />
            <span className="tv__qrhint">scan to grab a buzzer</span>
          </div>
          <p className="tv__teamcount">
            {players.length}
            {game.max_players ? `/${game.max_players}` : ""} teams
          </p>
          <div className="tv__joiners">
            {players.length === 0 ? (
              <span className="tv__waiting">Waiting for teams…</span>
            ) : (
              players.map((p) => (
                <span key={p.id} className={`tv__joiner ${p.connected ? "" : "tv__joiner--off"}`}>
                  {p.name}
                </span>
              ))
            )}
          </div>
        </div>
      )}

      {game.status === "active" && !cell && <BoardGrid game={game} />}

      {game.status === "active" && cell && (
        <div className="tv__question">
          {/* §F: with a verdict (or a revealed answer) on screen, the board
              LEADS with it and the buzzer-lock chip disappears entirely — no
              green→red flash stealing the moment (the lock is implied). The
              verdict + answer render before the question text so the payoff
              owns the top of the screen. */}
          {judgment && (
            <div className={`tv__verdict ${judgment.correct ? "tv__verdict--right" : "tv__verdict--wrong"}`}>
              {judgment.correct ? "✔ CORRECT" : "✘ WRONG"} — {judgment.name}
            </div>
          )}
          {revealedAnswer != null && <div className="tv__answer">{revealedAnswer}</div>}
          <div className="tv__qtop">
            <span className="valuechip valuechip--tv">
              {game.mode === "drinks" ? `${cell.value} DRINKS` : `${cell.value} POINTS`}
            </span>
            {!verdictOnScreen && (
              <span className={`buzzstate buzzstate--tv ${game.buzzer_open ? "buzzstate--open" : "buzzstate--locked"}`}>
                {game.buzzer_open ? "🟢 BUZZERS OPEN" : "🔒 BUZZERS LOCKED"}
              </span>
            )}
          </div>
          <p className="tv__qtext">{cell.question_text}</p>
          <MediaBlock cell={cell} autoPlay />
          {/* §G attribution: who sent the drink where. */}
          {cell.drink_assignment && (
            <div className="tv__drinkline">
              {cell.drink_assignment.from_name} sends {cell.drink_assignment.amount}{" "}
              drink{cell.drink_assignment.amount === 1 ? "" : "s"} to {cell.drink_assignment.to_name} 🍺
            </div>
          )}
          {!verdictOnScreen && cell.buzzes.length > 0 && (
            <ol className="tv__buzzes">
              {cell.buzzes.map((b, i) => (
                <li key={b.participant_id}>
                  <span className="tv__buzzorder">{i + 1}</span> {b.name}
                </li>
              ))}
            </ol>
          )}
        </div>
      )}

      {game.status === "finished" && <FinalStandings game={game} />}

      <footer className="tv__scores">
        {/* §G: the host is a valid drink target — show them in DRINK tallies
            (labeled, only once they've actually taken one) but never in
            score displays. */}
        {game.mode === "drinks" && hostSeat && hostSeat.drinks_taken > 0 && (
          <div className="tv__score tv__score--host">
            <span className="tv__scorename">🎤 {hostSeat.name}</span>
            <span className="tv__scoreval tv__scoreval--drinks">→🍻{hostSeat.drinks_taken}</span>
          </div>
        )}
        {players.map((p) => (
          <div key={p.id} className="tv__score">
            <span className="tv__scorename">{p.name}</span>
            {game.mode === "points" ? (
              <span className="tv__scoreval">{p.score}</span>
            ) : (
              <span className="tv__scoreval tv__scoreval--drinks">
                🍺→{p.drinks_given} · →🍻{p.drinks_taken}
              </span>
            )}
          </div>
        ))}
      </footer>
    </div>
  );
}
