import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, errorText, quotaError } from "../lib/api";
import {
  clearHostGame,
  loadAuth,
  loadHostGame,
  loadSeat,
  onAuthChange,
  saveHostGame,
  saveSeat,
} from "../lib/storage";
import { useGameSocket } from "../lib/useGameSocket";
import AuthScreen from "../components/AuthScreen";
import {
  BoardGrid,
  BuzzList,
  ConnectionDot,
  FinalStandings,
  MediaBlock,
  ScoreStrip,
  Toast,
} from "../components/shared";

// §F1 (Handoff #9): AuthScreen moved to src/components/AuthScreen.jsx (it
// was never host-specific — /profile imported it, /login renders it too).
// Re-exported here so nothing that still imports it from HostPage breaks.
export { default as AuthScreen } from "../components/AuthScreen";

/**
 * §I (Handoff #8): resume a game from ANY browser — the server re-issues the
 * host's seat token (host-private endpoint), we store it under the normal
 * keys, and the standard connect flow takes over. Exported so /profile's
 * Resume buttons share the exact same path instead of forking it.
 */
export async function resumeHostGame(authToken, code) {
  const res = await api.gameHostSeat(authToken, code);
  const seat = {
    token: res.participant_token,
    participantId: res.participant.id,
    name: res.participant.name,
    role: "host",
  };
  saveSeat(code, seat);
  saveHostGame(code);
  return { code: code.toUpperCase(), token: seat.token };
}

export default function HostPage() {
  const [auth, setAuth] = useState(loadAuth());
  // §F: the SiteNav's logout (or a dead Knox token dropped by api.js) clears
  // dq_auth and announces — flip straight back to the login screen.
  useEffect(() => onAuthChange(() => setAuth(loadAuth())), []);
  // active = {code, token} once a game is created or resumed
  const [active, setActive] = useState(() => {
    const code = loadHostGame();
    if (!code) return null;
    const seat = loadSeat(code);
    return seat?.token ? { code, token: seat.token } : null;
  });

  if (!auth) return <AuthScreen onAuthed={setAuth} />;
  if (!active)
    return (
      <CreateScreen
        auth={auth}
        onCreated={(code, token, participantId) => {
          saveSeat(code, { token, participantId, role: "host" });
          saveHostGame(code);
          setActive({ code, token });
        }}
        onResumed={setActive}
      />
    );
  return (
    <HostGame
      code={active.code}
      token={active.token}
      auth={auth}
      onLeave={() => {
        clearHostGame();
        setActive(null);
      }}
    />
  );
}

/* ---------------- Auth ----------------
   The AuthScreen itself lives in src/components/AuthScreen.jsx since §F1;
   see the re-export at the top of this file. */

/* ---------------- Game creation ---------------- */

function CreateScreen({ auth, onCreated, onResumed }) {
  const [categories, setCategories] = useState(null);
  // §G3 (Handoff #10): themes filter the category grid and offer a one-tap
  // "Pick for me". Pure discovery/selection sugar — `selected` stays the one
  // selection state, the create request still sends category_ids, and the
  // server's shortage refusal (§F3) remains the real gate (rule 4).
  const [themes, setThemes] = useState(null);
  const [activeTheme, setActiveTheme] = useState(null); // null = "All categories"
  // §I: unfinished games this host can jump back into. Discoverability lives
  // here (the "no active game" screen) AND on /profile; both share
  // resumeHostGame above.
  const [unfinished, setUnfinished] = useState(null);
  const [resumeBusy, setResumeBusy] = useState(null);
  const [mode, setMode] = useState("drinks");
  const [perCategory, setPerCategory] = useState(5);
  const [selected, setSelected] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  // Full profile: usage.games_this_month feeds the (cosmetic) meter — limit
  // null means unlimited (today's default) and we show nothing — and is_staff
  // drives the moderation link. Server enforces both either way.
  const [profile, setProfile] = useState(null);
  const gamesUsage = profile?.usage?.games_this_month ?? null;

  useEffect(() => {
    api
      .categories(auth.token)
      .then(setCategories)
      .catch((err) => setError(errorText(err)));
    api
      .themes(auth.token)
      .then(setThemes)
      .catch(() => setThemes([])); // the strip is optional sugar
    api
      .profile(auth.token)
      .then(setProfile)
      .catch(() => {}); // meter/link are optional; creation errors surface on their own
    api
      .gameHistory(auth.token)
      .then((games) => setUnfinished(games.filter((g) => g.status !== "finished")))
      .catch(() => setUnfinished([])); // the panel is optional sugar
  }, [auth.token]);

  const resume = async (code) => {
    setResumeBusy(code);
    try {
      onResumed(await resumeHostGame(auth.token, code));
    } catch (err) {
      setError(errorText(err));
      setResumeBusy(null);
    }
  };

  const toggle = (id) =>
    setSelected((sel) => (sel.includes(id) ? sel.filter((x) => x !== id) : sel.length < 8 ? [...sel, id] : sel));

  // §G3 "Pick for me": up to 5 of the theme's categories that can actually
  // fill a column (usable count >= perCategory), highest counts first. 5
  // because the owner said "up to 5"; the manual grid still allows up to 8.
  // REPLACES the current selection — it's the one-tap "give me a board".
  const pickForMe = (theme) => {
    const picks = theme.categories
      .filter((c) => (c.usable_question_count ?? 0) >= perCategory)
      .sort((a, b) => (b.usable_question_count ?? 0) - (a.usable_question_count ?? 0))
      .slice(0, 5)
      .map((c) => c.id);
    setSelected(picks);
  };

  // Filtering is a VIEW, not a reset: switching themes never touches
  // `selected` — categories chosen under another filter stay chosen.
  const themeCategoryIds = activeTheme ? new Set(activeTheme.categories.map((c) => c.id)) : null;
  const visibleCategories = themeCategoryIds
    ? (categories ?? []).filter((c) => themeCategoryIds.has(c.id))
    : categories;

  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await api.createGame(auth.token, {
        mode,
        categories: selected,
        questions_per_category: perCategory,
      });
      const code = res.game.code;
      const hostParticipant = res.game.participants.find((p) => p.role === "host");
      onCreated(code, res.participant_token, hostParticipant?.id);
    } catch (err) {
      const q = quotaError(err);
      setError(
        q
          ? `You've used all ${q.limit} games included in your plan this month (${q.used} created). Upgrade for unlimited hosting.`
          : errorText(err), // 400 lists every short category
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page">
      {/* §F: the old pagehead (user name, Moderation/Profile/Your content
          links, Log out) moved wholesale into the SiteNav — one identity
          bar per page, not two. */}
      <h1 className="h1">New game</h1>

      {unfinished?.length > 0 && (
        <section className="panel resumepanel">
          <h2 className="h2">Pick up where you left off</h2>
          <ul className="resumelist">
            {unfinished.map((g) => (
              <li key={g.code} className="resumerow">
                <span className="historyrow__code">{g.code}</span>
                <span className="historyrow__meta">
                  {g.mode === "drinks" ? "🍺 drinks" : "💯 points"} ·{" "}
                  {g.status === "lobby" ? "still in the lobby" : "mid-game"} · {g.participant_count} team
                  {g.participant_count === 1 ? "" : "s"}
                </span>
                <button
                  className="btn btn--gold btn--sm"
                  disabled={resumeBusy === g.code}
                  onClick={() => resume(g.code)}
                >
                  {resumeBusy === g.code ? "Resuming…" : "▶ Resume"}
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="panel">
        <h2 className="h2">Mode</h2>
        <div className="modepick">
          <button className={`modecard ${mode === "drinks" ? "modecard--on" : ""}`} onClick={() => setMode("drinks")}>
            <span className="modecard__emoji">🍺</span>
            <strong>Drinks</strong>
            <span>Answer right, pick who drinks the cell's value</span>
          </button>
          <button className={`modecard ${mode === "points" ? "modecard--on" : ""}`} onClick={() => setMode("points")}>
            <span className="modecard__emoji">💯</span>
            <strong>Points</strong>
            <span>Classic Jeopardy: +value right, −value wrong</span>
          </button>
        </div>
      </section>

      <section className="panel">
        <h2 className="h2">Questions per category</h2>
        <div className="stepper">
          <input
            type="range"
            min={1}
            max={10}
            value={perCategory}
            onChange={(e) => setPerCategory(Number(e.target.value))}
          />
          <span className="stepper__value">{perCategory}</span>
        </div>
      </section>

      <section className="panel">
        <h2 className="h2">
          Categories <span className="field__hint">(pick 1–8)</span>
        </h2>
        {themes && themes.length > 0 && (
          <div className="themestrip">
            <button
              type="button"
              className={`themechip ${activeTheme == null ? "themechip--on" : ""}`}
              onClick={() => setActiveTheme(null)}
            >
              All categories
            </button>
            {themes.map((t) => {
              // A theme is "thin" when NO visible category in it can fill a
              // column at the current per-category setting.
              const thin = !t.categories.some((c) => (c.usable_question_count ?? 0) >= perCategory);
              const on = activeTheme?.id === t.id;
              return (
                <button
                  key={t.id}
                  type="button"
                  className={`themechip ${on ? "themechip--on" : ""}`}
                  disabled={thin}
                  title={thin ? "Not enough questions yet" : t.description || ""}
                  onClick={() => setActiveTheme(on ? null : t)}
                >
                  {t.name}
                  {thin && <span className="themechip__hint"> — not enough questions yet</span>}
                </button>
              );
            })}
            {activeTheme && (
              <span className="themestrip__actions">
                <button type="button" className="btn btn--gold btn--sm" onClick={() => pickForMe(activeTheme)}>
                  ✨ Pick for me
                </button>
                <span className="themestrip__count">{selected.length} selected</span>
              </span>
            )}
          </div>
        )}
        {!categories && !error && <p>Loading…</p>}
        <div className="catgrid">
          {visibleCategories?.map((cat) => {
            const short = (cat.usable_question_count ?? 0) < perCategory;
            const on = selected.includes(cat.id);
            return (
              <button
                key={cat.id}
                className={`catcard ${on ? "catcard--on" : ""} ${short ? "catcard--short" : ""}`}
                onClick={() => toggle(cat.id)}
                title={short ? `Only ${cat.usable_question_count} usable questions; ${perCategory} needed` : cat.description || ""}
              >
                <span className="catcard__name">{cat.name}</span>
                <span className="catcard__count">
                  {cat.usable_question_count} question{cat.usable_question_count === 1 ? "" : "s"}
                  {short && " ⚠️"}
                </span>
              </button>
            );
          })}
        </div>
        {categories?.length === 0 && (
          <p className="footnote">
            No categories visible. Run <code>seed_demo</code> on the backend or create content in <code>/admin/</code>.
          </p>
        )}
      </section>

      {error && <p className="formerror formerror--block">{error}</p>}

      {gamesUsage?.limit != null && (
        <p className="footnote">
          {Math.max(0, gamesUsage.limit - gamesUsage.used)} of {gamesUsage.limit} games left on your plan this month.
        </p>
      )}

      <button className="btn btn--primary btn--big" disabled={busy || selected.length === 0} onClick={create}>
        {busy ? "Creating…" : `Create ${mode} game (${selected.length} ${selected.length === 1 ? "category" : "categories"})`}
      </button>
    </div>
  );
}

/* ---------------- Live host panel ---------------- */

function HostGame({ code, token, auth, onLeave }) {
  const { game, connected, authFailed, lastError, revealedAnswer, clearError, send } = useGameSocket(code, token);
  const [confirmFinish, setConfirmFinish] = useState(false);
  const [flagToast, setFlagToast] = useState(null);

  const openCell = game?.current_cell;
  const players = game?.participants.filter((p) => p.role === "player") ?? [];

  const openCellAction = useCallback((cellId) => send("open_cell", { cell_id: cellId }), [send]);

  // §F1 (Handoff #6): host-private answer, fetched over the Knox side channel
  // whenever the snapshot shows a newly opened cell. It is DISPLAY data only —
  // every enabled/disabled decision still derives from the snapshot (rule 1) —
  // and a fetch failure must not break the panel: judging keeps working and
  // the WS answer_reveal remains the host's fallback. The answer is tagged
  // with its cell id so a slow response for a closed cell can never render.
  const [hostAnswer, setHostAnswer] = useState(null); // {cellId, answer}
  const [hostAnswerError, setHostAnswerError] = useState(null);
  const openCellId = openCell?.id ?? null;
  const fetchHostAnswer = useCallback(
    async (cellId) => {
      setHostAnswerError(null);
      try {
        const res = await api.gameAnswer(auth.token, code);
        // question_id feeds §K2's flag button — host-private, never from the
        // public snapshot (which deliberately carries no question ids).
        setHostAnswer({ cellId, answer: res.answer, questionId: res.question_id });
      } catch (err) {
        setHostAnswer(null);
        setHostAnswerError(errorText(err));
      }
    },
    [auth?.token, code],
  );
  useEffect(() => {
    if (openCellId == null) {
      // Cell closed: clear the private answer immediately.
      setHostAnswer(null);
      setHostAnswerError(null);
      return;
    }
    fetchHostAnswer(openCellId);
  }, [openCellId, fetchHostAnswer]);

  // Reveal state, preferring the snapshot's persisted field (§F2 — rule 1
  // done properly); the hook's event-driven value stays as the fallback for
  // older backends that don't send `revealed_answer` yet.
  const shownReveal = game?.revealed_answer ?? revealedAnswer;

  useEffect(() => {
    if (authFailed) onLeave(); // stale seat: back to creation
  }, [authFailed, onLeave]);

  if (!game)
    return (
      <div className="page page--center">
        <Wordmark />
        <p>Connecting to game {code}…</p>
      </div>
    );

  // Judge state, derived only from the snapshot.
  const cellOnBoard = openCell ? game.columns.flatMap((c) => c.cells).find((c) => c.id === openCell.id) : null;
  const judgedCorrect = cellOnBoard?.answered_correctly ?? false;
  const winnerId = cellOnBoard?.answered_by ?? null;
  const winner = winnerId ? game.participants.find((p) => p.id === winnerId) : null;
  // §G: everything derives from the snapshot (rule 1). The picker shows only
  // while nothing is assigned; the server's once-per-cell marker is the real
  // gate (rule 4) — these are cosmetic.
  const drinksAssigned = openCell?.drinks_assigned ?? false;
  const drinkAttribution = openCell?.drink_assignment ?? null;
  const needsDrinkAssign = game.mode === "drinks" && judgedCorrect && openCell && !drinksAssigned;
  const hostSeat = game.participants.find((p) => p.role === "host");

  return (
    <div className="page page--wide">
      <header className="pagehead">
        {/* §F: wordmark lives in the SiteNav now; this bar keeps the
            game-specific chrome (code, connection, board link, finish). */}
        <div className="pagehead__mid">
          <span className="codechip">
            Join code <strong>{game.code}</strong>
          </span>
          <ConnectionDot connected={connected} />
        </div>
        <div className="pagehead__right">
          <Link className="btn btn--ghost" to={`/board/${game.code}`} target="_blank" rel="noreferrer">
            Open board ↗
          </Link>
          {game.status !== "finished" ? (
            confirmFinish ? (
              <span className="confirmrow">
                <button
                  className="btn btn--danger"
                  onClick={() => {
                    send("finish_game");
                    setConfirmFinish(false);
                  }}
                >
                  Confirm finish
                </button>
                <button className="btn btn--ghost" onClick={() => setConfirmFinish(false)}>
                  Cancel
                </button>
              </span>
            ) : (
              <button className="btn btn--ghost" onClick={() => setConfirmFinish(true)}>
                Finish game
              </button>
            )
          ) : (
            <button className="btn btn--ghost" onClick={onLeave}>
              New game
            </button>
          )}
        </div>
      </header>

      {game.status === "lobby" && (
        <section className="lobby">
          <p className="lobby__lede">Send buzzers to</p>
          <div className="lobby__code">{game.code}</div>
          <p className="lobby__hint">
            Each team opens <code>/game/buzzer/{game.code}</code> on one phone. Project <code>/board/{game.code}</code> on the TV.
          </p>
          <div className="panel">
            <h2 className="h2">
              Teams joined ({players.length}
              {game.max_players ? `/${game.max_players}` : ""})
            </h2>
            {game.max_players != null && players.length >= game.max_players && (
              <p className="footnote">Table's full — extra players share a teammate's buzzer.</p>
            )}
            {players.length === 0 && <p className="footnote">Waiting for buzzers…</p>}
            <ul className="lobbylist">
              {players.map((p) => (
                <li key={p.id} className={p.connected ? "" : "lobbylist--offline"}>
                  {p.name} {p.connected ? "🟢" : "⚪"}
                </li>
              ))}
            </ul>
          </div>
          <LobbyPreview auth={auth} code={game.code} onToast={setFlagToast} />
          <button className="btn btn--primary btn--big" disabled={players.length === 0} onClick={() => send("start_game")}>
            Start game
          </button>
        </section>
      )}

      {!connected && (
        <div className="connbanner" role="status">
          Reconnecting to the game — controls are paused. Your game state is safe on the server.
        </div>
      )}

      {game.status === "active" && (
        <>
          <ScoreStrip game={game} />
          {!openCell && (
            <>
              {/* §H2 (Handoff #10): the board is fully played — say so and
                  bring the finish action to where the host is looking. The
                  condition derives entirely from the snapshot (rule 1):
                  cells_remaining hits 0 only after the LAST close_cell. The
                  confirm state is the SAME confirmFinish the header uses —
                  reused, not forked; the header's Finish button stays too. */}
              {game.cells_remaining === 0 ? (
                <div className="boarddone" role="status">
                  <p className="boarddone__title">That's the whole board 🎉</p>
                  <p className="boarddone__sub">Finish the game to crown the winner.</p>
                  {confirmFinish ? (
                    <span className="confirmrow">
                      <button
                        className="btn btn--danger"
                        onClick={() => {
                          send("finish_game");
                          setConfirmFinish(false);
                        }}
                      >
                        Confirm finish
                      </button>
                      <button className="btn btn--ghost" onClick={() => setConfirmFinish(false)}>
                        Cancel
                      </button>
                    </span>
                  ) : (
                    <button className="btn btn--gold btn--big" onClick={() => setConfirmFinish(true)}>
                      🏁 Finish game
                    </button>
                  )}
                </div>
              ) : (
                <p className="hostprompt">Click a cell to open its question.</p>
              )}
              <BoardGrid game={game} onOpenCell={openCellAction} />
            </>
          )}

          {openCell && (
            <section className="hostq">
              <div className="hostq__main">
                <div className="hostq__meta">
                  <span className="valuechip">
                    {game.mode === "drinks" ? `${openCell.value} drinks` : `${openCell.value} pts`}
                  </span>
                  <span className={`buzzstate ${game.buzzer_open ? "buzzstate--open" : "buzzstate--locked"}`}>
                    {game.buzzer_open ? "BUZZER OPEN" : "BUZZER LOCKED"}
                  </span>
                </div>
                <p className="hostq__text">{openCell.question_text}</p>
                <MediaBlock cell={openCell} />
                {shownReveal == null &&
                  (hostAnswer?.cellId === openCell.id ? (
                    <div className="answercard answercard--private">
                      <span>Answer (only you see this)</span>
                      <strong>{hostAnswer.answer}</strong>
                    </div>
                  ) : hostAnswerError ? (
                    <div className="answercard answercard--private">
                      <span>Answer (only you see this)</span>
                      <p className="answercard__err">Couldn't load it — you can still judge, or reveal over the room screen.</p>
                      <button type="button" className="btn btn--sm" onClick={() => fetchHostAnswer(openCell.id)}>
                        Retry
                      </button>
                    </div>
                  ) : (
                    <div className="answercard answercard--private">
                      <span>Answer (only you see this)</span>
                      <p className="answercard__err">Loading…</p>
                    </div>
                  ))}
                {shownReveal != null && (
                  <div className="answercard">
                    <span>Answer</span>
                    <strong>{shownReveal}</strong>
                  </div>
                )}
                {/* §K2: the moment the host sees a stinker live is the moment
                    to flag it. Filing never touches the question — it stays
                    playable until a moderator reviews it. */}
                {hostAnswer?.cellId === openCell.id && hostAnswer.questionId && (
                  <FlagButton
                    token={auth.token}
                    questionId={hostAnswer.questionId}
                    onToast={setFlagToast}
                  />
                )}
              </div>

              <div className="hostq__side">
                <h2 className="h2">Buzz order</h2>
                <BuzzList buzzes={openCell.buzzes}>
                  {(b) =>
                    !judgedCorrect && (
                      <span className="judgebtns">
                        <button
                          className="btn btn--good btn--sm"
                          onClick={() => send("judge", { participant_id: b.participant_id, correct: true })}
                        >
                          ✓ Right
                        </button>
                        <button
                          className="btn btn--danger btn--sm"
                          onClick={() => send("judge", { participant_id: b.participant_id, correct: false })}
                        >
                          ✗ Wrong
                        </button>
                      </span>
                    )
                  }
                </BuzzList>

                {needsDrinkAssign && (
                  <div className="drinkassign">
                    <h2 className="h2">
                      {winner
                        ? `${winner.name} answered right — THEIR phone picks who drinks ${openCell.value}`
                        : "Who drinks?"}
                    </h2>
                    <p className="footnote">
                      Dead phone or distracted winner? Assign it from here — first assignment wins,
                      one per question.
                    </p>
                    <div className="drinkassign__btns">
                      {/* §G: every seat is a target — the winner themselves
                          (drinking your own win is allowed) and the HOST. */}
                      {game.participants.map((p) => (
                        <button
                          key={p.id}
                          className="btn btn--drink"
                          onClick={() => send("assign_drinks", { to_participant_id: p.id })}
                        >
                          🍺 {p.role === "host" ? `${p.name} (you)` : p.id === winnerId ? `${p.name} (the winners)` : p.name}{" "}
                          drinks {openCell.value}
                        </button>
                      ))}
                    </div>
                  </div>
                )}
                {game.mode === "drinks" && drinksAssigned && (
                  <div className="drinkassign drinkassign--done">
                    <h2 className="h2">Drinks assigned ✔</h2>
                    {drinkAttribution && (
                      <p className="drinkassign__line">
                        {drinkAttribution.from_name} → {drinkAttribution.to_name} 🍺×{drinkAttribution.amount}
                      </p>
                    )}
                    <p className="footnote">One assignment per question — close the cell to keep playing.</p>
                  </div>
                )}
                {game.mode === "points" && judgedCorrect && winner && (
                  <p className="footnote">✓ {winner.name} +{openCell.value}. Close the cell to continue.</p>
                )}

                <div className="hostctl">
                  {!game.buzzer_open && !judgedCorrect && (
                    <button className="btn btn--primary" onClick={() => send("open_buzzer")}>
                      Open buzzer
                    </button>
                  )}
                  {game.buzzer_open && (
                    <button className="btn" onClick={() => send("lock_buzzer")}>
                      Lock buzzer
                    </button>
                  )}
                  <button className="btn btn--ghost" onClick={() => send("reset_buzzer")}>
                    Reset buzzes
                  </button>
                  {shownReveal == null && (
                    <button className="btn btn--ghost" onClick={() => send("reveal_answer")}>
                      Reveal answer
                    </button>
                  )}
                  <button className="btn btn--gold" onClick={() => send("close_cell")}>
                    Close cell
                  </button>
                </div>
              </div>
            </section>
          )}
        </>
      )}

      {game.status === "finished" && (
        <>
          <FinalStandings game={game} />
          <ScoreStrip game={game} />
        </>
      )}

      <Toast message={lastError} onDone={clearError} />
      <Toast message={flagToast} tone="info" onDone={() => setFlagToast(null)} />
    </div>
  );
}

/**
 * §K2: "flag this question" — shown on the host's open-cell view (they just
 * saw the bad question live) and in the lobby preview rows. POST-only; a 409
 * means this host already has an open flag on it, which we surface as done.
 */
export function FlagButton({ token, questionId, onToast, small = true }) {
  const [state, setState] = useState("idle"); // idle | busy | done
  if (state === "done") return <span className="flagdone">🚩 flagged for review</span>;
  return (
    <button
      type="button"
      className={`btn btn--ghost ${small ? "btn--sm" : ""} flagbtn`}
      disabled={state === "busy"}
      onClick={async () => {
        setState("busy");
        try {
          await api.reportQuestion(token, questionId);
          setState("done");
          onToast?.("Flagged — the question stays playable until a moderator reviews it.");
        } catch (err) {
          if (err?.status === 409) {
            setState("done"); // already flagged by us — same outcome
            onToast?.("You'd already flagged this one — it's in the review queue.");
          } else {
            setState("idle");
            onToast?.(errorText(err));
          }
        }
      }}
    >
      {state === "busy" ? "Flagging…" : "🚩 flag this question"}
    </button>
  );
}

/**
 * §J3: the host's lobby preview — per-category expandable lists of the drawn
 * questions (host-private board detail over REST; the WS lobby snapshot
 * carries no questions, and must not) with per-row Replace + flag. Replace
 * redraws the one cell server-side (same category, closest difficulty,
 * prefer-unused, excluding what's already on the board) and is refused with
 * a 409 once the game has started.
 */
function LobbyPreview({ auth, code, onToast }) {
  const [board, setBoard] = useState(null);
  const [error, setError] = useState(null);
  const [openCols, setOpenCols] = useState({}); // column id -> bool
  const [busyCell, setBusyCell] = useState(null);

  useEffect(() => {
    let alive = true;
    api
      .gameBoard(auth.token, code)
      .then((b) => alive && setBoard(b))
      .catch((err) => alive && setError(errorText(err)));
    return () => {
      alive = false;
    };
  }, [auth.token, code]);

  const replace = async (cellId) => {
    setBusyCell(cellId);
    try {
      const updated = await api.replaceCell(auth.token, code, cellId);
      setBoard((b) => ({
        ...b,
        columns: b.columns.map((col) => ({
          ...col,
          cells: col.cells.map((c) => (c.id === updated.id ? updated : c)),
        })),
      }));
    } catch (err) {
      onToast?.(errorText(err)); // 409: started, or nothing left to swap in
    } finally {
      setBusyCell(null);
    }
  };

  if (error) return <section className="panel"><p className="footnote">Couldn't load the board preview: {error}</p></section>;
  if (!board) return <section className="panel"><p className="footnote">Loading your board…</p></section>;

  return (
    <section className="panel preview">
      <h2 className="h2">Your board — peek &amp; swap before you start</h2>
      <p className="footnote">
        Only you can see this. Don't like a question? Replace redraws that slot from the same
        category (favoring ones you haven't played before). Locked once the game starts.
      </p>
      {board.columns.map((col) => {
        const open = !!openCols[col.id];
        return (
          <div key={col.id} className="preview__col">
            <button
              type="button"
              className={`previewcat ${open ? "previewcat--on" : ""}`}
              onClick={() => setOpenCols((o) => ({ ...o, [col.id]: !open }))}
            >
              <span>{col.category_name}</span>
              <span className="previewcat__chev">{open ? "▾" : "▸"}</span>
            </button>
            {open && (
              <ul className="preview__list">
                {col.cells.map((cell) => (
                  <li key={cell.id} className="previewq">
                    <div className="previewq__main">
                      <span className="previewq__value">{board.mode === "drinks" ? `${cell.value} 🍺` : cell.value}</span>
                      <span className="previewq__text">{cell.question_text}</span>
                    </div>
                    <div className="previewq__sub">
                      <span className="previewq__answer">{cell.answer}</span>
                      <span className="previewq__actions">
                        <FlagButton token={auth.token} questionId={cell.question_id} onToast={onToast} />
                        <button
                          type="button"
                          className="btn btn--sm"
                          disabled={busyCell === cell.id}
                          onClick={() => replace(cell.id)}
                        >
                          {busyCell === cell.id ? "Swapping…" : "↻ Replace"}
                        </button>
                      </span>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        );
      })}
    </section>
  );
}

function Wordmark({ small }) {
  return <div className={`wordmark ${small ? "wordmark--small" : ""}`}>DRINKQUIRY</div>;
}
