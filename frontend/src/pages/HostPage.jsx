import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, errorText, quotaError } from "../lib/api";
import { displayUrl } from "../lib/displayUrl";
import { useDebounced } from "../lib/hooks";
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
import { ensureAudio, playBuzz } from "../lib/sounds";
import AuthScreen from "../components/AuthScreen";
import CategoryPicker from "../components/CategoryPicker";
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
  // #13 fix: arriving with ?tournament= is an explicit "create ANOTHER
  // game" intent — a tournament runs many games, so the stored active game
  // must not gate the create screen (it used to: after game 1, "+ game in
  // round N" landed the host back in game 1's console with no way to make
  // game 2). The params are cleared on create/resume so the wrapper then
  // falls through to the new game's console; the previous game stays
  // resumable (its seat is stored per code) from the tournament page,
  // /profile, or the create screen's resume panel.
  const [searchParams, setSearchParams] = useSearchParams();
  const tournamentCreate = searchParams.has("tournament");
  const clearParams = () => setSearchParams({}, { replace: true });
  const navigate = useNavigate();

  if (!auth) return <AuthScreen onAuthed={setAuth} />;
  if (!active || tournamentCreate)
    return (
      <CreateScreen
        auth={auth}
        onCreated={(code, token, participantId, tournamentId) => {
          // The seat is ALWAYS saved — the create response already issued
          // it, and the bracket's "Host console" resumes from it.
          saveSeat(code, { token, participantId, role: "host" });
          if (tournamentId) {
            // §F4a (Handoff #17): a tournament create returns you to the
            // BRACKET, not this game's console — the page reloads on mount
            // and the fresh card appears "in the lobby" with its Host
            // console button. This closes the owner's "never shows
            // anything" loop: the populated bracket is what you see after
            // every create. NOT saveHostGame/setActive: the console is one
            // resumeHostGame away, and a plain game the host had active
            // stays their active game. clearParams first so back-button
            // lands on a clean /host, not a re-armed ?tournament= create.
            clearParams();
            navigate(`/tournaments/${tournamentId}`);
            return;
          }
          saveHostGame(code);
          clearParams();
          setActive({ code, token });
        }}
        onResumed={(a) => {
          clearParams();
          setActive(a);
        }}
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
  // §F6 (Handoff #15): the create grid is search-and-page shaped — ONE
  // server page at a time (debounced ?search=, Load more appends), never
  // the fetch-all sweep (C21: 40+ requests / 2,000 cards at current scale).
  const [catSearch, setCatSearch] = useState("");
  const debouncedCatSearch = useDebounced(catSearch);
  const [catParams, setCatParams] = useState({ search: "", page: 1 }); // atomic: search change resets page
  const [catRows, setCatRows] = useState(null); // accumulated pages; null = first load
  const [catCount, setCatCount] = useState(0);
  const [catHasNext, setCatHasNext] = useState(false);
  const [catLoading, setCatLoading] = useState(true);
  const catSeq = useRef(0); // stale-response guard (§G)
  // §G3 (Handoff #10): themes filter the category grid and offer a one-tap
  // "Pick for me". Pure discovery/selection sugar — the picked Map stays the
  // one selection state, the create request still sends category_ids, and
  // the server's shortage refusal (§F3) remains the real gate (rule 4).
  // §F6 (#15): the strip shows the first ~12 + a server-side theme search.
  const [themes, setThemes] = useState(null);
  const [themeSearch, setThemeSearch] = useState("");
  const debouncedThemeSearch = useDebounced(themeSearch);
  const themeSeq = useRef(0);
  const [activeTheme, setActiveTheme] = useState(null); // null = "All categories"
  // §I: unfinished games this host can jump back into. Discoverability lives
  // here (the "no active game" screen) AND on /profile; both share
  // resumeHostGame above.
  const [unfinished, setUnfinished] = useState(null);
  const [resumeBusy, setResumeBusy] = useState(null);
  const [mode, setMode] = useState("drinks");
  const [perCategory, setPerCategory] = useState(5);
  // §H (#13): the game's ONE buzz sound — a host choice at creation (the
  // preview tap is the WebAudio gesture). Server validates 1–4 (rule 4).
  const [buzzSound, setBuzzSound] = useState(1);
  // §I4 (#13): arriving from a tournament control room carries
  // ?tournament=ID&round=N — the create request then attaches the game.
  // The banner is display; the SERVER re-validates ownership/liveness/
  // finished on create (rule 4), so a stale or hand-typed URL just errors.
  const [searchParams] = useSearchParams();
  const tournamentId = Number(searchParams.get("tournament")) || null;
  const roundNumber = tournamentId ? Number(searchParams.get("round")) || 1 : null;
  const [tournamentInfo, setTournamentInfo] = useState(null); // {name, location} | "error" | null
  // §F6 (#15) SELECTION PINNING: picked categories live in a Map(id →
  // {id, name, usable_question_count}) captured AT CLICK — never derived
  // from whatever page/search/theme the grid currently shows (§G trap:
  // "selection ≠ current page"). Searching away and back can't lose a pick.
  const [picked, setPicked] = useState(() => new Map());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  // Full profile: usage.games_this_month feeds the (cosmetic) meter — limit
  // null means unlimited (today's default) and we show nothing — and is_staff
  // drives the moderation link. Server enforces both either way.
  const [profile, setProfile] = useState(null);
  const gamesUsage = profile?.usage?.games_this_month ?? null;

  useEffect(() => {
    api
      .profile(auth.token)
      .then(setProfile)
      .catch(() => {}); // meter/link are optional; creation errors surface on their own
    api
      .gameHistory(auth.token)
      .then((games) => setUnfinished(games.filter((g) => g.status !== "finished")))
      .catch(() => setUnfinished([])); // the panel is optional sugar
  }, [auth.token]);

  // Debounced grid search → params (page resets); identity guard skips the
  // mount no-op so the initial render fires exactly one fetch.
  useEffect(() => {
    setCatParams((p) => (p.search === debouncedCatSearch ? p : { search: debouncedCatSearch, page: 1 }));
  }, [debouncedCatSearch]);

  useEffect(() => {
    const mySeq = ++catSeq.current;
    setCatLoading(true);
    api
      .categoriesPage(auth.token, { search: catParams.search, page: catParams.page })
      .then((d) => {
        if (catSeq.current !== mySeq) return;
        setCatRows((prev) => (catParams.page === 1 ? d.results : [...(prev ?? []), ...d.results]));
        setCatCount(d.count);
        setCatHasNext(!!d.next);
        setCatLoading(false);
      })
      .catch((err) => {
        if (catSeq.current !== mySeq) return;
        setCatLoading(false);
        setError(errorText(err));
      });
  }, [auth.token, catParams]);

  // Theme strip: server-side search (§F6). The unfiltered call feeds the
  // "first ~12" default view; typing re-queries. Failure = no strip (sugar).
  useEffect(() => {
    const mySeq = ++themeSeq.current;
    api
      .themes(auth.token, debouncedThemeSearch)
      .then((rows) => themeSeq.current === mySeq && setThemes(rows))
      .catch(() => themeSeq.current === mySeq && setThemes([]));
  }, [auth.token, debouncedThemeSearch]);

  // §I4: the tournament banner data (name + venue). Separate keyed effect —
  // it only runs when arriving with ?tournament=, and its failure just makes
  // the banner say so (create still sends the ids; the server is the gate).
  useEffect(() => {
    if (!tournamentId) return undefined;
    let alive = true;
    api
      .tournament(auth.token, tournamentId)
      .then((t) => alive && setTournamentInfo(t))
      .catch(() => alive && setTournamentInfo("error"));
    return () => {
      alive = false;
    };
  }, [auth.token, tournamentId]);

  const resume = async (code) => {
    setResumeBusy(code);
    try {
      onResumed(await resumeHostGame(auth.token, code));
    } catch (err) {
      setError(errorText(err));
      setResumeBusy(null);
    }
  };

  // Capture display data AT CLICK (§F6 pinning) — a pick made from any
  // page, search or theme survives every later view change. Cap of 8 kept.
  const capture = (cat) => ({ id: cat.id, name: cat.name, usable_question_count: cat.usable_question_count ?? 0 });
  const togglePick = (cat) =>
    setPicked((prev) => {
      const next = new Map(prev);
      if (next.has(cat.id)) next.delete(cat.id);
      else if (next.size < 8) next.set(cat.id, capture(cat));
      return next;
    });

  // §G3 "Pick for me": up to 5 of the theme's categories that can actually
  // fill a column (usable count >= perCategory), highest counts first. 5
  // because the owner said "up to 5"; the manual grid still allows up to 8.
  // REPLACES the current selection — it's the one-tap "give me a board".
  const pickForMe = (theme) => {
    const picks = theme.categories
      .filter((c) => (c.usable_question_count ?? 0) >= perCategory)
      .sort((a, b) => (b.usable_question_count ?? 0) - (a.usable_question_count ?? 0))
      .slice(0, 5);
    setPicked(new Map(picks.map((c) => [c.id, capture(c)])));
  };

  // §F2 (Handoff #17): "Use every question" — a host who wrote exactly 8
  // questions shouldn't have to count them and drag a slider. The button
  // sets the stepper to the SMALLEST picked category's usable count,
  // clamped to the server's 1–10 (rule 1: the serializer's validator and
  // the shortage 409 stay the real gates if counts move between page-load
  // and create). Counts come from the picked Map's captured
  // usable_question_count (§F6 #15 pinning), so this follows searches,
  // themes and pages for free. The `short` flags and auto-suggest filter
  // already key off perCategory — they follow the new value untouched.
  const pickedMinUsable =
    picked.size > 0 ? Math.min(...[...picked.values()].map((c) => c.usable_question_count ?? 0)) : null;
  const useEveryQuestion = () => {
    if (pickedMinUsable == null) return;
    setPerCategory(Math.min(10, Math.max(1, pickedMinUsable)));
  };

  // Filtering is a VIEW, not a reset: switching themes never touches the
  // picked Map — categories chosen under another filter stay chosen (and
  // render in the pinned row even when the current view hides them).
  // Theme active → the grid shows the theme's OWN category list (staff-
  // curated, bounded, already per-user counted), with the search box
  // narrowing it client-side; no theme → the server page(s).
  const gridCats = activeTheme
    ? activeTheme.categories.filter(
        (c) => !debouncedCatSearch || c.name.toLowerCase().includes(debouncedCatSearch.toLowerCase()),
      )
    : (catRows ?? []);

  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await api.createGame(auth.token, {
        mode,
        categories: [...picked.keys()],
        questions_per_category: perCategory,
        buzz_sound: buzzSound,
        // §I4: attach when arriving from a tournament (both or neither —
        // the server enforces the pairing and re-checks ownership).
        ...(tournamentId ? { tournament: tournamentId, round_number: roundNumber } : {}),
      });
      const code = res.game.code;
      const hostParticipant = res.game.participants.find((p) => p.role === "host");
      // §F4a: the 4th argument tells the wrapper this create carried a
      // tournament attach — the wrapper owns the where-to-go decision
      // (console vs bracket); this screen stays dumb about navigation.
      onCreated(code, res.participant_token, hostParticipant?.id, tournamentId);
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

      {tournamentId && (
        <section className="panel tournamentbanner">
          {tournamentInfo === "error" ? (
            <p className="formerror">
              Couldn't load that tournament — creating will double-check it.{" "}
              <Link to="/tournaments">Back to tournaments</Link>
            </p>
          ) : (
            <>
              <p className="tournamentbanner__line">
                🏆 This game joins <strong>{tournamentInfo?.name ?? `tournament #${tournamentId}`}</strong> as{" "}
                <strong>round {roundNumber}</strong>
                {tournamentInfo?.location ? ` at ${tournamentInfo.location}` : ""}.
              </p>
              {/* §I5: the difficulty NUDGE, v1 — a hint, not machinery
                  (auto-hard-mode per round is §M). */}
              {roundNumber > 1 && (
                <p className="footnote">
                  Later rounds usually want a harder board — a fresh category set or more questions per
                  category does it.
                </p>
              )}
              <Link className="btn btn--ghost btn--sm" to={`/tournaments/${tournamentId}`}>
                Back to the tournament
              </Link>
            </>
          )}
        </section>
      )}

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
            <span>Classic quiz-show scoring: +value right, −value wrong</span>
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
          <button
            type="button"
            className="btn btn--ghost btn--sm stepper__all"
            disabled={pickedMinUsable == null}
            title={
              pickedMinUsable == null
                ? "Pick your categories first — this matches the smallest one"
                : "Set the board to the smallest picked category's question count"
            }
            onClick={useEveryQuestion}
          >
            Use every question
          </button>
        </div>
        {pickedMinUsable != null && (
          <p className="footnote">
            Smallest picked category has {pickedMinUsable} usable question{pickedMinUsable === 1 ? "" : "s"}.
            {pickedMinUsable > 10 && " Boards cap at 10 — the TV board gets cramped past that."}
          </p>
        )}
      </section>

      {/* §H (#13): the buzz sound is now a HOST choice — one sound for the
          whole game (every phone and the TV play it). Tapping an option
          selects it AND previews it: the tap is the user gesture WebAudio's
          autoplay policy wants, so the preview always sounds here. No sound
          choice exists anywhere else — the board's corner icon is only
          "this TV may make noise" (autoplay law), and phones just play. */}
      <section className="panel">
        <h2 className="h2">
          Buzz sound <span className="field__hint">(tap to hear — everyone's buzzer plays this one)</span>
        </h2>
        <div className="soundpick">
          {[
            { id: 1, emoji: "🚨", label: "Classic buzzer" },
            { id: 2, emoji: "🛎️", label: "Ding-ding" },
            { id: 3, emoji: "📯", label: "Honk" },
            { id: 4, emoji: "🎺", label: "Triple beep" },
          ].map((s) => (
            <button
              key={s.id}
              type="button"
              className={`soundcard ${buzzSound === s.id ? "soundcard--on" : ""}`}
              onClick={() => {
                ensureAudio(); // the click is the gesture
                playBuzz(s.id);
                setBuzzSound(s.id);
              }}
            >
              <span className="soundcard__emoji">{s.emoji}</span>
              <strong>{s.label}</strong>
            </button>
          ))}
        </div>
      </section>

      <section className="panel">
        <h2 className="h2">
          Categories <span className="field__hint">(pick 1–8 — search finds packs beyond the first page)</span>
        </h2>
        {/* §F6 (#15): the strip shows the FIRST ~12 themes; the search box
            queries the server for the rest. The active theme's chip is
            pinned into view even when a search would hide it. */}
        {themes != null && (themes.length > 0 || themeSearch !== "") && (
          <div className="themestrip">
            <button
              type="button"
              className={`themechip ${activeTheme == null ? "themechip--on" : ""}`}
              onClick={() => setActiveTheme(null)}
            >
              All categories
            </button>
            {activeTheme && !themes.slice(0, 12).some((t) => t.id === activeTheme.id) && (
              <button
                type="button"
                className="themechip themechip--on"
                title={activeTheme.description || ""}
                onClick={() => setActiveTheme(null)}
              >
                {activeTheme.name}
              </button>
            )}
            {themes.slice(0, 12).map((t) => {
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
            {themes.length > 12 && (
              <span className="themestrip__more">+{themes.length - 12} more — search:</span>
            )}
            <input
              className="themestrip__search"
              type="search"
              placeholder="Search themes…"
              value={themeSearch}
              onChange={(e) => setThemeSearch(e.target.value)}
              aria-label="Search themes"
            />
            {themes.length === 0 && themeSearch !== "" && (
              <span className="themestrip__more">no themes match</span>
            )}
            {activeTheme && (
              <span className="themestrip__actions">
                <button type="button" className="btn btn--gold btn--sm" onClick={() => pickForMe(activeTheme)}>
                  ✨ Pick for me
                </button>
                <span className="themestrip__count">{picked.size} selected</span>
              </span>
            )}
          </div>
        )}
        {/* §F6 pinned selections — always visible ABOVE the results, no
            matter what page/search/theme the grid shows. Deselect works from
            here or from a matching grid card. */}
        {picked.size > 0 && (
          <div className="pinnedrow">
            <span className="pinnedrow__label">On your board ({picked.size})</span>
            {[...picked.values()].map((cat) => {
              const short = (cat.usable_question_count ?? 0) < perCategory;
              return (
                <button
                  key={cat.id}
                  type="button"
                  className={`pinnedchip ${short ? "pinnedchip--short" : ""}`}
                  title={short ? `Only ${cat.usable_question_count} usable questions; ${perCategory} needed` : "Tap to remove"}
                  onClick={() => togglePick(cat)}
                >
                  {cat.name}
                  {short && " ⚠️"} ✕
                </button>
              );
            })}
          </div>
        )}
        <input
          className="listsearch"
          type="search"
          placeholder={activeTheme ? `Search inside ${activeTheme.name}…` : "Search categories…"}
          value={catSearch}
          onChange={(e) => setCatSearch(e.target.value)}
          aria-label="Search categories"
        />
        {!activeTheme && catRows == null && !error && <p>Loading…</p>}
        <div className="catgrid">
          {gridCats.map((cat) => {
            const short = (cat.usable_question_count ?? 0) < perCategory;
            const on = picked.has(cat.id);
            return (
              <button
                key={cat.id}
                className={`catcard ${on ? "catcard--on" : ""} ${short ? "catcard--short" : ""}`}
                onClick={() => togglePick(cat)}
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
        {!activeTheme && catRows != null && (
          <div className="loadmore">
            <span className="listmeta">
              Showing {catRows.length} of {catCount}
            </span>
            {catHasNext && (
              <button
                type="button"
                className="btn btn--ghost btn--sm"
                disabled={catLoading}
                onClick={() => setCatParams((p) => ({ ...p, page: p.page + 1 }))}
              >
                {catLoading ? "Loading…" : "Load more"}
              </button>
            )}
          </div>
        )}
        {activeTheme && gridCats.length === 0 && (
          <p className="footnote">No categories in this theme match “{debouncedCatSearch}”.</p>
        )}
        {!activeTheme && catRows != null && catCount === 0 && (
          <p className="footnote">
            {catParams.search ? (
              <>Nothing matches “{catParams.search}” — try fewer words.</>
            ) : (
              <>
                No categories visible. Run <code>seed_demo</code> on the backend or create content in <code>/admin/</code>.
              </>
            )}
          </p>
        )}
      </section>

      {error && <p className="formerror formerror--block">{error}</p>}

      {gamesUsage?.limit != null && (
        <p className="footnote">
          {Math.max(0, gamesUsage.limit - gamesUsage.used)} of {gamesUsage.limit} games left on your plan this month.
        </p>
      )}

      <button className="btn btn--primary btn--big" disabled={busy || picked.size === 0} onClick={create}>
        {busy ? "Creating…" : `Create ${mode} game (${picked.size} ${picked.size === 1 ? "category" : "categories"})`}
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
      {/* §F4b (Handoff #17): the console finally knows its tournament — the
          other half of closing the loop (§F4a returns you to the bracket
          after create; this gets you back from a resumed console). The
          snapshot's tournament block gained `id` for exactly this link
          (pinned-shape amendment, additive — see games/serializers.py).
          The id guard is deploy-order insurance (C9): a WS snapshot from a
          not-yet-restarted backend renders the line, just link-less. */}
      {game.tournament && (
        <div className="tourneyline">
          🏆 <strong>{game.tournament.name}</strong> — Round {game.tournament.round_number}
          {game.tournament.id != null && (
            <>
              {" · "}
              <Link className="tourneyline__link" to={`/tournaments/${game.tournament.id}`}>
                back to the bracket
              </Link>
            </>
          )}
        </div>
      )}
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
            {/* §F9 (#15): full-host display strings — drinkquiry.com/… on
                prod, localhost:5173/… in dev — derived, never hardcoded.
                DISPLAY only; the render paths are untouched. */}
            Each team opens <code>{displayUrl(`/game/buzzer/${game.code}`)}</code> on one phone. Project{" "}
            <code>{displayUrl(`/board/${game.code}`)}</code> on the TV.
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
                  <span>
                    {p.name} {p.connected ? "🟢" : "⚪"}
                  </span>
                  {/* §F (#11): kick from the lobby — wrong-code joiners and
                      troll names die here. Server re-validates (rule 4). */}
                  <RemovePlayerButton name={p.name} onRemove={() => send("remove_player", { participant_id: p.id })} />
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
          {/* §F (#11): the ScoreStrip-adjacent removal surface — the table
              that left after round one gets kicked from here. ScoreStrip
              itself stays shared/untouched (buzzers and TVs render it too);
              this row is host-only chrome. Snapshot-driven: a removed seat
              vanishes from `participants`, so the row cleans itself. */}
          <div className="manageteams">
            {players.map((p) => (
              <span key={p.id} className="manageteams__row">
                <span className="manageteams__name">{p.name}</span>
                <RemovePlayerButton
                  name={p.name}
                  onRemove={() => send("remove_player", { participant_id: p.id })}
                />
              </span>
            ))}
          </div>
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
 *
 * §F (Handoff #16): each column header also grows a "swap category"
 * affordance — the shared CategoryPicker in single-select mode, categories
 * already on the board offered-but-disabled ("on this board"), and an
 * explicit confirm so a stray tap can't torch a column. The server rebuilds
 * the whole column (same draw + value scaling as creation) and returns it
 * in the board-detail shape; we patch that ONE column in place, with the
 * same stale-guard style the rest of the preview uses (busy flags gate the
 * buttons; responses land into setBoard functionally). Lobby-only like the
 * cell replace — the button is cosmetic, the endpoint is the gate (rule 4).
 */
function LobbyPreview({ auth, code, onToast }) {
  const [board, setBoard] = useState(null);
  const [error, setError] = useState(null);
  const [openCols, setOpenCols] = useState({}); // column id -> bool
  const [busyCell, setBusyCell] = useState(null);
  const [swapCol, setSwapCol] = useState(null); // column id with the picker open
  const [swapPick, setSwapPick] = useState(() => new Map()); // CategoryPicker value (≤1 entry)
  const [swapBusy, setSwapBusy] = useState(false);

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

  const openSwap = (columnId) => {
    // One picker at a time; reopening always starts with a clean pick.
    setSwapPick(new Map());
    setSwapCol((cur) => (cur === columnId ? null : columnId));
  };

  const swapCategory = async (columnId) => {
    const picked = [...swapPick.values()][0];
    if (!picked) return;
    setSwapBusy(true);
    try {
      const updated = await api.replaceColumnCategory(auth.token, code, columnId, picked.id);
      // Patch the ONE returned column in place — new category, fresh cells.
      setBoard((b) => ({
        ...b,
        columns: b.columns.map((col) => (col.id === updated.id ? updated : col)),
      }));
      setSwapCol(null);
      setSwapPick(new Map());
      setOpenCols((o) => ({ ...o, [columnId]: true })); // show the fresh draw
      onToast?.(`Column swapped to ${picked.name}.`);
    } catch (err) {
      // 409: started / already on the board / deleted / too thin — the
      // server's message says which; the board state is untouched.
      onToast?.(errorText(err));
    } finally {
      setSwapBusy(false);
    }
  };

  if (error) return <section className="panel"><p className="footnote">Couldn't load the board preview: {error}</p></section>;
  if (!board) return <section className="panel"><p className="footnote">Loading your board…</p></section>;

  // §F: categories already on the board are offered-but-disabled in the
  // picker (cosmetic — the endpoint 409s duplicates regardless).
  const onBoardIds = new Set(board.columns.map((col) => col.category_id));

  return (
    <section className="panel preview">
      <h2 className="h2">Your board — peek &amp; swap before you start</h2>
      <p className="footnote">
        Only you can see this. Don't like a question? Replace redraws that slot from the same
        category (favoring ones you haven't played before). Want a different category entirely?
        Swap the whole column. Locked once the game starts.
      </p>
      {board.columns.map((col) => {
        const open = !!openCols[col.id];
        const swapping = swapCol === col.id;
        return (
          <div key={col.id} className="preview__col">
            <div className="preview__colhead">
              <button
                type="button"
                className={`previewcat ${open ? "previewcat--on" : ""}`}
                onClick={() => setOpenCols((o) => ({ ...o, [col.id]: !open }))}
              >
                <span>{col.category_name}</span>
                <span className="previewcat__chev">{open ? "▾" : "▸"}</span>
              </button>
              <button
                type="button"
                className={`btn btn--ghost btn--sm previewswap__toggle ${swapping ? "previewswap__toggle--on" : ""}`}
                title={`Swap ${col.category_name} for a different category`}
                onClick={() => openSwap(col.id)}
              >
                {swapping ? "✕ keep it" : "⇄ category"}
              </button>
            </div>
            {swapping && (
              <div className="previewswap">
                <CategoryPicker
                  auth={auth}
                  value={swapPick}
                  onChange={setSwapPick}
                  single
                  disabledIds={onBoardIds}
                  disabledNote="on this board"
                  legend={`Swap “${col.category_name}” for…`}
                  hint="the whole column redraws from the new category"
                />
                <div className="previewswap__actions">
                  <button
                    type="button"
                    className="btn btn--primary btn--sm"
                    disabled={swapBusy || swapPick.size === 0}
                    onClick={() => swapCategory(col.id)}
                  >
                    {swapBusy ? "Swapping…" : "Swap column"}
                  </button>
                  <button
                    type="button"
                    className="btn btn--ghost btn--sm"
                    disabled={swapBusy}
                    onClick={() => openSwap(col.id)}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}
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

/**
 * §F (Handoff #11): inline-confirm kick — the standing DeleteButton /
 * confirmrow pattern (arm, 3.5s auto-disarm), reused not reinvented. The
 * click sends `remove_player`; everything after that re-renders from the
 * snapshot (the seat vanishes from `participants`), so there's no local
 * "removed" state to manage.
 */
function RemovePlayerButton({ name, onRemove }) {
  const [arming, setArming] = useState(false);
  useEffect(() => {
    if (!arming) return undefined;
    const t = setTimeout(() => setArming(false), 3500);
    return () => clearTimeout(t);
  }, [arming]);
  return arming ? (
    <button
      type="button"
      className="btn btn--danger btn--sm"
      title={`Really remove ${name} from the game`}
      onClick={() => {
        onRemove();
        setArming(false);
      }}
    >
      Confirm remove
    </button>
  ) : (
    <button type="button" className="btn btn--ghost btn--sm" onClick={() => setArming(true)}>
      ✕ remove
    </button>
  );
}

function Wordmark({ small }) {
  return <div className={`wordmark ${small ? "wordmark--small" : ""}`}>DRINKQUIRY</div>;
}
