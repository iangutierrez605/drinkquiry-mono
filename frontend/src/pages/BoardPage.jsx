import { useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { QRCodeSVG } from "qrcode.react";
import { api, mediaUrl } from "../lib/api";
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
  // §F (#11): `removed` — a kicked PLAYER's seat token in this browser (they
  // opened the board too) dies with code 4003; the board is read-only, so it
  // just falls back to unauthenticated polling like any other bad token.
  const usePolling = !seat?.token || ws.authFailed || ws.removed;

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

  // Flash on new buzzes + play THE GAME'S sound when toggled on (§H #13:
  // the sound is a host-chosen, per-game field now — snapshot.buzz_sound —
  // so every buzz makes the same noise on every surface). Browser autoplay
  // policy needs a user gesture ON THIS DEVICE before audio, so sound stays
  // behind a small enable control, default OFF, no persistence. Sound is
  // display-only side data (rule 1): nothing gates on it.
  const [soundOn, setSoundOn] = useState(false);
  const soundOnRef = useRef(soundOn);
  soundOnRef.current = soundOn;
  const gameSoundRef = useRef(1);
  gameSoundRef.current = game?.buzz_sound ?? 1;

  const playGameBuzz = () => {
    if (!soundOnRef.current) return;
    playBuzz(gameSoundRef.current);
  };

  // WS mode: consume the hook's existing lastBuzz output (rule 2 — the hook
  // itself is untouched).
  useEffect(() => {
    if (usePolling || !ws.lastBuzz) return;
    playGameBuzz();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ws.lastBuzz, usePolling]);

  // Both modes flash on buzz-count growth; in POLLING mode that same
  // snapshot diff (§B4's house technique) also triggers the game's sound —
  // there is no lastBuzz without a socket.
  const prevBuzzCount = useRef(0);
  const [flash, setFlash] = useState(false);
  const buzzes = game?.current_cell?.buzzes;
  const buzzCount = buzzes?.length ?? 0;
  useEffect(() => {
    if (buzzCount > prevBuzzCount.current) {
      if (usePolling && buzzes?.length) {
        playGameBuzz();
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
  // §H (#11): venue branding, straight from the snapshot (C2 — polling
  // boards get it free). Null unless the host's creator plan is live.
  const brand = game.brand ?? null;
  // §I (#13): tournament identity, straight from the snapshot (same C2
  // free ride). Null for every plain game — the entire block below no-ops.
  const tournament = game.tournament ?? null;
  const players = game.participants.filter((p) => p.role === "player");
  const hostSeat = game.participants.find((p) => p.role === "host");
  // §F: verdict straight from the snapshot (rule 1) — WS and polling boards
  // both get it, since it's a snapshot field, not an event.
  const judgment = cell?.last_judgment ?? null;
  const verdictOnScreen = judgment != null || revealedAnswer != null;

  return (
    <div className={`tv ${flash ? "tv--flash" : ""}`}>
      <header className="tv__head">
        {/* §F (#13): the identity block — wordmark + venue logo (+ §I's
            tournament chip) live together so they scale as one unit. */}
        <div className="tv__identity">
          <span className="wordmark wordmark--small">DRINKQUIRY</span>
          {/* §H (#11): the small persistent in-play logo — present but never
              competing with the game (the lobby block below is the big one).
              §F (#13): scaled up (see .brandlogo--tvhead). */}
          {brand?.logo && game.status !== "lobby" && (
            <img src={mediaUrl(brand.logo)} alt={brand.name || "Venue logo"} className="brandlogo brandlogo--tvhead" />
          )}
          {/* §I (#13): the compact in-play tournament chip — name + round,
              never competing with the board. */}
          {tournament && game.status !== "lobby" && (
            <span className="tv__tournamentchip">
              🏆 {tournament.name} · R{tournament.round_number}
            </span>
          )}
        </div>
        <span className="codechip codechip--tv">
          Join: <strong>{game.code}</strong>
        </span>
      </header>

      {/* §H (#13): the audio-ENABLE control must stay on the board DEVICE —
          browser autoplay law: audio needs a gesture on the device that
          plays it, so a host's laptop click can NEVER legally unlock this
          TV's speakers. But it moves out of the header's "weird spot" to a
          quiet fixed corner icon, and it no longer offers any CHOICE (the
          host picked the game's sound at creation) — it is purely "this TV
          may make noise". Default OFF, display-only (rule 1). */}
      <button
        type="button"
        className={`tv__soundcorner ${soundOn ? "tv__soundcorner--on" : ""}`}
        onClick={() => {
          // The click is the gesture that unlocks WebAudio.
          const next = !soundOn;
          if (next) ensureAudio();
          setSoundOn(next);
        }}
        title={soundOn ? "This TV may make noise — tap to mute" : "Tap once and this TV may make noise"}
        aria-label={soundOn ? "TV sound on" : "TV sound off"}
      >
        {soundOn ? "🔊" : "🔇"}
      </button>

      {game.status === "lobby" && (
        <div className="tv__lobby">
          {/* §H (#11): the paid surface — "tonight's trivia at THE KINGS
              ARMS" right where the room is looking. §F (#13): roughly
              doubled, inside the identity wrapper §I's tournament block
              shares. */}
          {(brand || tournament) && (
            <div className="tv__identity tv__identity--lobby">
              {/* §I3 (#13): the tournament block sits ABOVE the venue brand
                  — big name, ROUND N, and the hosted-by line (location is
                  server-resolved with the brand/display-name fallback, so
                  this stays a dumb renderer). When a tournament is present
                  the brand's own "tonight's trivia at" line is suppressed —
                  hosted-by already says whose night it is; the LOGO still
                  shows. */}
              {tournament && (
                <div className="tv__tournament">
                  <div className="tv__tournamentname">{tournament.name}</div>
                  <div className="tv__tournamentround">ROUND {tournament.round_number}</div>
                  {tournament.location && (
                    <p className="tv__brandline">
                      hosted by <strong>{tournament.location}</strong>
                    </p>
                  )}
                </div>
              )}
              {brand && (
                <div className="tv__brand">
                  {brand.logo && <img src={mediaUrl(brand.logo)} alt="" className="brandlogo brandlogo--tvlobby" />}
                  {brand.name && !tournament && (
                    <p className="tv__brandline">
                      tonight's trivia at <strong>{brand.name}</strong>
                    </p>
                  )}
                </div>
              )}
            </div>
          )}
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

      {game.status === "active" && !cell && (
        <>
          <BoardGrid game={game} />
          {/* §H3 (Handoff #10): every cell played — a QUIET line, no
              takeover, no animation (the winner moment owns big moments).
              cells_remaining is a snapshot field, so polling boards get it
              too (C2). */}
          {game.cells_remaining === 0 && (
            <p className="tv__allplayed">All questions played — waiting for the host to wrap up.</p>
          )}
        </>
      )}

      {/* §G1 (Handoff #9): a CORRECT verdict hands the whole screen to the
          moment — full overlay with the winner's name as the star, the
          answer beneath it (§G2's card treatment), and the drink line
          rendered LIVE (the drink is assigned while this is up). It lives
          exactly as long as the judgment marker does; #8's clearing points
          (close, reset, explicit reopen, next buzz, next cell) already
          handle every exit, so there is zero backend change and both WS and
          polling boards get it (snapshot-only inputs, §C2). */}
      {game.status === "active" && cell && judgment?.correct && (
        <div className="tv__takeover" role="status">
          <div className="tv__takeoverEyebrow">✔ CORRECT</div>
          <div className="tv__takeoverName">{judgment.name}</div>
          {revealedAnswer != null && (
            <div className="tv__answercard tv__answercard--takeover">
              <span className="tv__answercardLabel">the answer</span>
              <span className="tv__answercardText">{revealedAnswer}</span>
            </div>
          )}
          {cell.drink_assignment && (
            <div className="tv__drinkline">
              {cell.drink_assignment.from_name} sends {cell.drink_assignment.amount}{" "}
              drink{cell.drink_assignment.amount === 1 ? "" : "s"} to {cell.drink_assignment.to_name} 🍺
            </div>
          )}
        </div>
      )}

      {game.status === "active" && cell && (
        <div className={`tv__question ${revealedAnswer != null ? "tv__question--revealed" : ""}`}>
          {/* §F (#8): with a verdict (or a revealed answer) on screen, the
              board LEADS with it and the buzzer-lock chip disappears — no
              green→red flash stealing the moment. §G (#9): a CORRECT verdict
              is now the full takeover above, so only the WRONG banner
              renders here (wrong answers shouldn't own the room). */}
          {judgment && !judgment.correct && (
            <div className="tv__verdict tv__verdict--wrong">✘ WRONG — {judgment.name}</div>
          )}
          {/* §G2: the revealed answer gets a home (near-black card, gold
              border + glow) while everything around it dims via the
              --revealed modifier — pop by SUBTRACTION, not more size. Used
              for the host's explicit "nobody got it" reveal here, and the
              same card renders inside the §G1 takeover. */}
          {revealedAnswer != null && (
            <div className="tv__answercard">
              <span className="tv__answercardLabel">the answer</span>
              <span className="tv__answercardText">{revealedAnswer}</span>
            </div>
          )}
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
