import { Link } from "react-router-dom";

/**
 * §F4 (Handoff #16): /how-to-play — the ways-to-play explainer the owner
 * asked for ("so people who log in can clearly see all the different ways
 * to play"). Deliberately PUBLIC and in the top row: a bar owner evaluating
 * the product should read this before making an account. Static content +
 * deep links into the existing flows — no backend involvement, no auth.
 *
 * Copy stays LIMIT-AGNOSTIC on purpose (no team caps, no plan numbers):
 * those live in server settings, and a marketing page quoting them is a
 * page that drifts from reality the day a setting changes.
 */
export default function HowToPlayPage() {
  return (
    <div className="page howplay">
      <h1 className="h1">How to play</h1>
      <p className="howplay__lede">
        Every Drinkquiry night runs the same way: the host builds a board of trivia categories,
        the board goes on a TV, and every team's phone becomes a buzzer — no app, and players
        never need accounts (only the host does). From there, pick your format.
      </p>

      <div className="howplay__ways">
        <section className="panel howplay__way">
          <span className="howplay__emoji" aria-hidden="true">🏆</span>
          <h2 className="h2">Run a tournament</h2>
          <p className="howplay__tag">For bars &amp; venues — a whole evening, or a season</p>
          <p>
            A tournament is rounds of ordinary games under one name. Run round one across as many
            boards as your room needs, and when the games finish, advance the winners (or the top
            two) by team name — they simply join the next round's game and type the same name.
            You confirm every advancement, the bracket math is done for you, and the standings
            live on one control screen.
          </p>
          <p className="footnote">
            Great for trivia nights that keep a crowd coming back week after week.
          </p>
          {/* §F7 (Handoff #18): matches the /tournaments upsell (both
              change together, per the #17 note). Billing exists now. */}
          <p className="footnote">
            Tournaments run on a <Link to="/pricing">Tournament Pass</Link> (one
            bracket, your own questions) or the Venue plan for weekly nights.
            Hosting single games from the free library needs no plan at all.
          </p>
          <div className="howplay__cta">
            <Link className="btn btn--gold" to="/tournaments">
              Set up a tournament
            </Link>
          </div>
        </section>

        <section className="panel howplay__way">
          <span className="howplay__emoji" aria-hidden="true">📺</span>
          <h2 className="h2">Host a single game</h2>
          <p className="howplay__tag">For bars &amp; venues — one board, one winner</p>
          <p>
            The classic night: pick your categories (or grab a ready-made theme), put the board
            on the TV, and read out the six-letter join code. Each table opens it on one phone
            and that phone is their team's buzzer. First buzz answers; get it right in drinks
            mode and you choose who drinks. Points mode included for the well-behaved.
          </p>
          <p className="footnote">
            Your customers don't need accounts — they just type a team name and play.
          </p>
          <div className="howplay__cta">
            <Link className="btn btn--gold" to="/host">
              Host a game
            </Link>
          </div>
        </section>

        <section className="panel howplay__way">
          <span className="howplay__emoji" aria-hidden="true">🎉</span>
          <h2 className="h2">Make it personal</h2>
          <p className="howplay__tag">For friends — pre-games, parties, reunions</p>
          <p>
            The board doesn't have to be pub trivia. Write your own categories about the people
            in the room — “Whose forehead is this?”, “Things Dave has broken”, “Group chat:
            quote or misquote?” — with photo, audio or video questions if you like. Build them
            once, then host exactly like a bar would, minus the bar.
          </p>
          <p className="footnote">
            Your categories stay private to you unless you choose to share them.
          </p>
          <div className="howplay__cta">
            <Link className="btn btn--gold" to="/create">
              Make your categories
            </Link>
            <Link className="btn btn--ghost" to="/host">
              then host it
            </Link>
          </div>
        </section>
      </div>

      <p className="howplay__foot footnote">
        Not sure what to put on a board? <Link className="howplay__browse" to="/categories">Browse the ready-made categories</Link> —
        and please drink responsibly. 🍻
      </p>
    </div>
  );
}
