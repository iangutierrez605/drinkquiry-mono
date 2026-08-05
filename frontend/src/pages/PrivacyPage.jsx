import { Link } from "react-router-dom";
import { SUPPORT_EMAIL } from "../lib/api";

/*
 * ============================================================================
 * OWNER: PLACEHOLDER — have your counsel review and replace this BEFORE the
 * public launch. This describes what the product actually collects today; it
 * is NOT legal advice and was not written by a lawyer. (§F6, Handoff #17 —
 * flagged in CHANGES.md too.)
 * ============================================================================
 */

/**
 * §F6 (Handoff #17): /privacy — public static page under ChromeLayout.
 * Plain voice (rule 7's exception). Kept accurate to the code: account =
 * email + display name (+ optional date of birth), gameplay records, media
 * uploads; transactional email only; no ads, no analytics, no third-party
 * trackers (the §F6.4 decision — stdout logs are the observability story).
 */
export default function PrivacyPage() {
  return (
    <div className="page legalpage">
      <h1 className="h1">Privacy Policy</h1>
      <p className="footnote">Placeholder policy — under review. Last updated: August 2026.</p>

      <section className="panel legalpage__section">
        <h2 className="h2">1. What we collect</h2>
        <p>
          <strong>Host accounts:</strong> an email address, a display name, a password (stored hashed, never
          readable by us), and — only if you choose to give it — a date of birth. <strong>Gameplay
          records:</strong> the games you host and their results, including board layout, team names typed by
          players, scores, and drink tallies. <strong>Your content:</strong> the categories and questions you
          write, and any photos, audio or video you upload with them. <strong>Players without
          accounts:</strong> just the team name they type and their buzzes and scores for that game.
        </p>
      </section>

      <section className="panel legalpage__section">
        <h2 className="h2">2. What we use it for</h2>
        <p>
          To run the service: signing you in, building your boards, keeping score, showing tournament
          standings, and sending transactional email (password resets and the like). We don't sell your data,
          and we don't use it for advertising.
        </p>
      </section>

      <section className="panel legalpage__section">
        <h2 className="h2">3. Cookies and local storage</h2>
        <p>
          We use your browser's local storage for first-party essentials only: your sign-in session, your seat
          in a game you've joined, and remembering that you've seen the age notice. No advertising or
          analytics trackers, and no third-party cookies.
        </p>
      </section>

      <section className="panel legalpage__section">
        <h2 className="h2">4. Who can see what</h2>
        <p>
          People in a game with you see the game: team names, scores, and the board's questions during play.
          Categories you keep private stay private to your account; categories you share publicly are visible
          to everyone after review. Uploaded media is served to the people your content is visible to. We use
          service providers to host the service and deliver email; they process data on our behalf.
        </p>
      </section>

      <section className="panel legalpage__section">
        <h2 className="h2">5. Retention and deletion</h2>
        <p>
          Account data and gameplay history stay while your account exists. Email{" "}
          <a className="supportlink" href={`mailto:${SUPPORT_EMAIL}`}>
            {SUPPORT_EMAIL}
          </a>{" "}
          to delete your account and its content, or to ask what we hold about you.
        </p>
      </section>

      <section className="panel legalpage__section">
        <h2 className="h2">6. Age</h2>
        <p>
          Drinkquiry is intended for adults of legal drinking age; the drinks mode is strictly for them. We
          don't knowingly collect personal information from children.
        </p>
      </section>

      <section className="panel legalpage__section">
        <h2 className="h2">7. Changes and contact</h2>
        <p>
          If this policy changes, the date above changes with it. Questions or requests:{" "}
          <a className="supportlink" href={`mailto:${SUPPORT_EMAIL}`}>
            {SUPPORT_EMAIL}
          </a>
          . See also our <Link to="/terms">Terms of Service</Link>.
        </p>
      </section>
    </div>
  );
}
