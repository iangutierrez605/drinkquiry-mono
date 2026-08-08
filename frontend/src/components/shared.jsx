import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, mediaUrl } from "../lib/api";

/**
 * Header link to /moderate with the pending-count badge (Handoff #4 §F3).
 * Render only when the profile says is_staff — that gate is cosmetic; the
 * server enforces IsAdminUser on every moderation endpoint.
 */
export function ModerationLink({ token }) {
  const [counts, setCounts] = useState(null);
  useEffect(() => {
    let alive = true;
    api
      .moderationCounts(token)
      .then((c) => alive && setCounts(c))
      .catch(() => {}); // badge is optional; the queue page surfaces real errors
    return () => {
      alive = false;
    };
  }, [token]);
  const pending = counts ? (counts.categories ?? 0) + (counts.questions ?? 0) : 0;
  return (
    <Link className="btn btn--ghost" to="/moderate">
      Moderation
      {pending > 0 && <span className="pendingbadge">{pending}</span>}
    </Link>
  );
}

/** Header link to /profile (Handoff #6 §G3) — shown next to ModerationLink. */
export function ProfileLink() {
  return (
    <Link className="btn btn--ghost" to="/profile">
      Profile
    </Link>
  );
}

/**
 * "Plan usage: 2 of 25 categories · 41 of 500 questions" — the single meter
 * renderer shared by /create and /profile (Handoff #6 §G3: extracted rather
 * than forked). Each entry is {used, block, noun}; block comes straight from
 * the profile's usage payload and block.limit === null means unlimited.
 * Entries with a null/undefined `used` are skipped.
 */
export function UsageMeterLine({ entries }) {
  const parts = entries
    .filter((e) => e.used != null)
    .map((e) => (e.block?.limit == null ? `${e.used} ${e.noun}` : `${e.used} of ${e.block.limit} ${e.noun}`));
  if (parts.length === 0) return null;
  return <p className="footnote">Plan usage: {parts.join(" · ")}</p>;
}

/** Transient error toast; auto-dismisses. */
export function Toast({ message, onDone, tone = "error" }) {
  useEffect(() => {
    if (!message) return undefined;
    const t = setTimeout(onDone, 4200);
    return () => clearTimeout(t);
  }, [message, onDone]);
  if (!message) return null;
  return (
    <div className={`toast toast--${tone}`} role="alert" onClick={onDone}>
      {message}
    </div>
  );
}

export function ConnectionDot({ connected }) {
  return (
    <span className={`conn ${connected ? "conn--on" : "conn--off"}`}>
      <span className="conn__dot" />
      {connected ? "Live" : "Reconnecting…"}
    </span>
  );
}

/** Per-team standings. Mode decides which numbers matter. */
export function ScoreStrip({ game, selfId }) {
  const players = game.participants.filter((p) => p.role === "player");
  return (
    <div className="scorestrip">
      {players.map((p) => (
        <div key={p.id} className={`scorecard ${p.id === selfId ? "scorecard--self" : ""} ${p.connected ? "" : "scorecard--offline"}`}>
          <div className="scorecard__name">{p.name}</div>
          {game.mode === "points" ? (
            <div className="scorecard__stat">
              <strong>{p.score}</strong>
              <span>pts</span>
            </div>
          ) : (
            <div className="scorecard__drinks">
              <span title="Drinks given">🍺→ {p.drinks_given}</span>
              <span title="Drinks taken">→🍻 {p.drinks_taken}</span>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

/**
 * The game grid. Pure renderer of `columns` from the snapshot.
 * If `onOpenCell` is given (host), hidden cells are clickable.
 *
 * §G (Handoff #11): a won cell shows the winning TEAM's name (still in the
 * gold --won treatment so it reads at TV distance); a played cell nobody got
 * keeps —. Names resolve CLIENT-side (house style — see how HostPage finds
 * `winner`) from `answered_by` against participants + §F's former_players;
 * an id that resolves to nothing (pre-#11 games whose winner was hard-deleted
 * by ancient tooling — answered_by is SET_NULL) falls back to ✓.
 */
export function BoardGrid({ game, onOpenCell, compact = false }) {
  const cols = game.columns;
  const nameById = new Map(
    [...game.participants, ...(game.former_players ?? [])].map((p) => [p.id, p.name]),
  );
  return (
    <div className={`board ${compact ? "board--compact" : ""}`} style={{ "--cols": cols.length }}>
      {cols.map((col) => (
        <div className="board__col" key={col.id}>
          <div className="board__cat">
            {col.category_photo && <img src={mediaUrl(col.category_photo)} alt="" className="board__catphoto" />}
            <span>{col.category_name}</span>
          </div>
          {col.cells.map((cell) => {
            const clickable = onOpenCell && cell.state === "hidden" && game.status === "active" && !game.current_cell;
            const winnerName = cell.answered_correctly ? nameById.get(cell.answered_by) : null;
            return (
              <button
                key={cell.id}
                type="button"
                className={`cell cell--${cell.state} ${cell.answered_correctly ? "cell--won" : ""}`}
                disabled={!clickable}
                onClick={clickable ? () => onOpenCell(cell.id) : undefined}
              >
                {cell.state === "answered" ? (
                  winnerName ? (
                    <span className="cell__done cell__done--name">{winnerName}</span>
                  ) : (
                    <span className="cell__done">{cell.answered_correctly ? "✓" : "—"}</span>
                  )
                ) : (
                  <span className="cell__value">
                    {game.mode === "drinks" ? `${cell.value} 🍺` : cell.value}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      ))}
    </div>
  );
}

/** Question media (image/audio/video), URLs resolved against the API base. */
export function MediaBlock({ cell, autoPlay = false }) {
  if (!cell || cell.media_type === "none") return null;
  if (cell.media_type === "image" && cell.image)
    return <img className="qmedia qmedia--img" src={mediaUrl(cell.image)} alt="Question media" />;
  if (cell.media_type === "audio" && cell.audio)
    return <audio className="qmedia" src={mediaUrl(cell.audio)} controls autoPlay={autoPlay} />;
  if (cell.media_type === "video" && cell.video)
    return <video className="qmedia qmedia--video" src={mediaUrl(cell.video)} controls autoPlay={autoPlay} />;
  return null;
}

/** Ordered buzz list. */
export function BuzzList({ buzzes, selfId, children }) {
  if (!buzzes?.length) return <div className="buzzlist buzzlist--empty">No buzzes yet</div>;
  return (
    <ol className="buzzlist">
      {buzzes.map((b, i) => (
        <li key={b.participant_id} className={`buzzlist__row ${b.participant_id === selfId ? "buzzlist__row--self" : ""}`}>
          <span className="buzzlist__order">{i + 1}</span>
          <span className="buzzlist__name">{b.name}</span>
          {children ? children(b, i) : null}
        </li>
      ))}
    </ol>
  );
}

/** Final standings once status === "finished". */
export function FinalStandings({ game }) {
  const players = [...game.participants.filter((p) => p.role === "player")];
  players.sort((a, b) => (game.mode === "points" ? b.score - a.score : b.drinks_given - a.drinks_given));
  return (
    <div className="final">
      <h2 className="final__title">Final standings</h2>
      <ol className="final__list">
        {players.map((p, i) => (
          <li key={p.id} className={`final__row ${i === 0 ? "final__row--winner" : ""}`}>
            <span className="final__rank">{i + 1}</span>
            <span className="final__name">{p.name}</span>
            <span className="final__stat">
              {game.mode === "points" ? `${p.score} pts` : `gave ${p.drinks_given} · took ${p.drinks_taken}`}
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}

/**
 * §F2 (Handoff #21): the drift-proof chug countdown — every screen computes
 * remaining LOCALLY from the snapshot's server anchor (chug.started_at +
 * chug.seconds; C-5: zero per-second broadcasts). Returns whole seconds
 * remaining (clamped at 0) while stage is "running", else null. Client
 * clock skew shifts all screens in this room identically at worst — the TV
 * is the referee's face anyway.
 */
export function useChugRemaining(chug) {
  const running = chug?.stage === "running" && chug.started_at && chug.seconds != null;
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return undefined;
    const id = setInterval(() => setNow(Date.now()), 200);
    return () => clearInterval(id);
  }, [running]);
  if (!running) return null;
  const elapsed = (now - new Date(chug.started_at).getTime()) / 1000;
  return Math.max(0, Math.ceil(chug.seconds - elapsed));
}
