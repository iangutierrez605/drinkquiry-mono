import { Link } from "react-router-dom";
import { SUPPORT_EMAIL } from "../lib/api";

/*
 * ============================================================================
 * OWNER: PLACEHOLDER — have your counsel review and replace this BEFORE the
 * public launch. This is clearly-structured scaffolding covering the honest
 * basics of how the product works today; it is NOT legal advice and was not
 * written by a lawyer. (§F6, Handoff #17 — flagged in CHANGES.md too.)
 * ============================================================================
 */

/**
 * §F6 (Handoff #17): /terms — public static page under ChromeLayout, same
 * shell as /how-to-play. Rule 7's exception applies: the voice here is
 * deliberately PLAIN, not bar-night. Linked from the register form's
 * consent footnote (AuthScreen) and the landing footer.
 */
export default function TermsPage() {
  return (
    <div className="page legalpage">
      <h1 className="h1">Terms of Service</h1>
      <p className="footnote">Placeholder terms — under review. Last updated: August 2026.</p>

      <section className="panel legalpage__section">
        <h2 className="h2">1. What Drinkquiry is</h2>
        <p>
          Drinkquiry is a trivia hosting service: hosts build boards of trivia categories, project a board on a
          shared screen, and players use their phones as buzzers. Games can be played in a points mode or in a
          drinks mode that references drinking. Only hosts need accounts; players join games with a code and a
          team name.
        </p>
      </section>

      <section className="panel legalpage__section">
        <h2 className="h2">2. Age and responsible play</h2>
        <p>
          Drinkquiry contains drinking-game references. By creating an account or playing in drinks mode, you
          confirm that you are of legal drinking age in your jurisdiction. Points mode is available for
          all-ages play at the host's discretion. Never encourage anyone to drink more than they want to, and
          please drink responsibly — it's a quiz, not a contest. You are responsible for complying with the
          laws that apply where you play, including licensing rules if you run games at a venue.
        </p>
      </section>

      <section className="panel legalpage__section">
        <h2 className="h2">3. Your account</h2>
        <p>
          You need an account to host. Keep your password to yourself; you are responsible for activity under
          your account. Provide accurate information and let us know if you believe your account has been
          accessed by someone else.
        </p>
      </section>

      <section className="panel legalpage__section">
        <h2 className="h2">4. Your content</h2>
        <p>
          Hosts can write their own categories and questions and upload photos, audio and video to go with
          them. Content you upload remains yours. By uploading it, you give us the permission needed to store
          it and display it to the people you play with (and to everyone, if you choose to share a category
          publicly). Only upload content you have the right to use, and nothing unlawful, hateful, or that
          invades someone's privacy.
        </p>
      </section>

      <section className="panel legalpage__section">
        <h2 className="h2">5. Moderation and termination</h2>
        <p>
          Publicly shared content passes through review, and anything on the service can be flagged. We may
          remove content or suspend or terminate accounts that break these terms or put other users at risk,
          and we may decline service at our discretion. You can stop using the service at any time; contact us
          to delete your account.
        </p>
      </section>

      <section className="panel legalpage__section">
        <h2 className="h2">6. No warranty</h2>
        <p>
          The service is provided "as is", without warranties of any kind. We don't promise it will be
          uninterrupted or error-free, and to the fullest extent the law allows, we are not liable for
          indirect or consequential damages arising from your use of it. Nothing in these terms limits
          liability that cannot lawfully be limited.
        </p>
      </section>

      <section className="panel legalpage__section">
        <h2 className="h2">7. Changes and contact</h2>
        <p>
          We may update these terms as the product evolves; the date above tells you when they last changed.
          Questions? Email{" "}
          <a className="supportlink" href={`mailto:${SUPPORT_EMAIL}`}>
            {SUPPORT_EMAIL}
          </a>
          . See also our <Link to="/privacy">Privacy Policy</Link>.
        </p>
      </section>
    </div>
  );
}
