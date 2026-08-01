import { useEffect, useRef, useState } from "react";
import { api, errorText, SUPPORT_EMAIL } from "../lib/api";
import { displayUrl } from "../lib/displayUrl";
import { saveAuth } from "../lib/storage";

// §F2 (Handoff #12): the Turnstile SITE key reaches the SPA at build time
// (the VITE_API_BASE pattern). Absent → no script, no widget, no token sent —
// the site runs exactly as before. The backend independently requires/skips
// the token on its own TURNSTILE_SECRET_KEY (rule 4: the server is the gate).
const TURNSTILE_SITE_KEY = import.meta.env.VITE_TURNSTILE_SITE_KEY || "";
const TURNSTILE_SRC = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";

/**
 * §F1 (Handoff #9): the shared login/register screen, extracted from
 * HostPage.jsx (it was imported by ProfilePage too — first shared component
 * of this shape, so it finally gets its own file). Behavior is unchanged:
 * register/login toggle, writes dq_auth via storage.js (which now announces
 * the change to the SiteNav), calls onAuthed. Also used verbatim by /login.
 *
 * §K1 adds the "Forgot password?" path: a tiny email form whose submit
 * ALWAYS reads as success ("if that account exists…") — the server never
 * reveals whether the email is registered, and neither do we.
 */
export default function AuthScreen({ onAuthed }) {
  const [mode, setMode] = useState("login"); // login | register | forgot
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [dob, setDob] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [forgotSent, setForgotSent] = useState(false);
  // §F1: the decoy field. Humans never see it (off-screen, aria-hidden,
  // tabIndex -1); form-filling bots love a field named "website". Sent as-is
  // — the SERVER rejects a non-empty value (this render is cosmetic, rule 4).
  const [website, setWebsite] = useState("");
  // §F2: the solved-challenge token (empty when the widget is absent/unsolved).
  const [turnstileToken, setTurnstileToken] = useState("");
  const turnstileRef = useRef(null);

  // §F2: load + render the Turnstile widget for REGISTER only, and only when
  // a site key was baked in. Login stays widget-free (its throttle is its
  // defense — no friction for returning users). Hooks before the forgot-mode
  // early return below (C11).
  useEffect(() => {
    if (!TURNSTILE_SITE_KEY || mode !== "register") return undefined;
    let widgetId = null;
    let alive = true;
    const render = () => {
      if (!alive || !window.turnstile || !turnstileRef.current) return;
      widgetId = window.turnstile.render(turnstileRef.current, {
        sitekey: TURNSTILE_SITE_KEY,
        callback: (t) => setTurnstileToken(t),
        "expired-callback": () => setTurnstileToken(""),
        "error-callback": () => setTurnstileToken(""),
      });
    };
    const existing = document.querySelector("script[data-dq-turnstile]");
    if (window.turnstile) render();
    else if (existing) existing.addEventListener("load", render);
    else {
      const s = document.createElement("script");
      s.src = TURNSTILE_SRC;
      s.async = true;
      s.defer = true;
      s.setAttribute("data-dq-turnstile", "1");
      s.addEventListener("load", render);
      document.head.appendChild(s);
    }
    return () => {
      alive = false;
      if (existing) existing.removeEventListener("load", render);
      if (widgetId != null && window.turnstile) {
        try {
          window.turnstile.remove(widgetId);
        } catch {
          /* widget already gone */
        }
      }
      setTurnstileToken("");
    };
  }, [mode]);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (mode === "forgot") {
        await api.passwordForgot(email);
        setForgotSent(true);
        return;
      }
      if (mode === "register") {
        await api.register({
          email,
          password,
          display_name: displayName,
          ...(dob ? { date_of_birth: dob } : {}),
          // §F1: usually "" (which the server waves through); a bot-filled
          // value earns the vague 400 server-side.
          website,
          // §F2: only sent when the widget rendered and was solved.
          ...(turnstileToken ? { turnstile_token: turnstileToken } : {}),
        });
      }
      const res = await api.login(email, password); // Knox basic-auth login
      const authed = { token: res.token, user: res.user };
      saveAuth(authed);
      onAuthed(authed);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  if (mode === "forgot")
    return (
      <div className="page page--center">
        <Wordmark />
        <form className="panel authform" onSubmit={submit}>
          <h2 className="h2">Reset your password</h2>
          {forgotSent ? (
            <>
              <p>If that account exists, a reset link is on its way to {email}.</p>
              <p className="footnote">Check spam too. The link expires after a while.</p>
            </>
          ) : (
            <>
              <label className="field">
                Email
                <input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  required
                  autoComplete="email"
                  autoFocus
                />
              </label>
              {error && <p className="formerror">{error}</p>}
              <button className="btn btn--primary" disabled={busy}>
                {busy ? "…" : "Send reset link"}
              </button>
            </>
          )}
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={() => {
              setMode("login");
              setForgotSent(false);
              setError(null);
            }}
          >
            ← Back to login
          </button>
        </form>
      </div>
    );

  return (
    <div className="page page--center">
      <Wordmark />
      <form className="panel authform" onSubmit={submit}>
        <div className="tabs">
          <button type="button" className={mode === "login" ? "tab tab--on" : "tab"} onClick={() => setMode("login")}>
            Log in
          </button>
          <button type="button" className={mode === "register" ? "tab tab--on" : "tab"} onClick={() => setMode("register")}>
            Create account
          </button>
        </div>
        {mode === "register" && (
          <label className="field">
            Display name
            <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} required maxLength={50} />
          </label>
        )}
        <label className="field">
          Email
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="email" />
        </label>
        <label className="field">
          Password
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            autoComplete={mode === "login" ? "current-password" : "new-password"}
          />
        </label>
        {mode === "register" && (
          <label className="field">
            Date of birth <span className="field__hint">(optional)</span>
            <input type="date" value={dob} onChange={(e) => setDob(e.target.value)} />
          </label>
        )}
        {mode === "register" && (
          /* §F1: the honeypot. Off-screen absolute positioning (NOT
             display:none alone — some form-fillers skip hidden inputs),
             aria-hidden, out of the tab order, autocomplete off. Humans
             never touch it; the server rejects a filled value. */
          <div className="hp-field" aria-hidden="true">
            <label htmlFor="dq-website">Website</label>
            <input
              id="dq-website"
              name="website"
              type="text"
              tabIndex={-1}
              autoComplete="off"
              value={website}
              onChange={(e) => setWebsite(e.target.value)}
            />
          </div>
        )}
        {mode === "register" && TURNSTILE_SITE_KEY && (
          /* §F2: the challenge renders here only when a site key is baked
             into the build. */
          <div ref={turnstileRef} className="turnstile-slot" />
        )}
        {error && <p className="formerror">{error}</p>}
        <button className="btn btn--primary" disabled={busy}>
          {busy ? "…" : mode === "login" ? "Log in" : "Create account & log in"}
        </button>
        {mode === "login" && (
          <button type="button" className="btn btn--ghost btn--sm authform__forgot" onClick={() => setMode("forgot")}>
            Forgot password?
          </button>
        )}
      </form>
      <p className="footnote">
        {/* §F9 (#15): full-host display string, derived — never hardcoded. */}
        Players don't need accounts — only the host logs in. Buzzers join at <code>{displayUrl("/game/buzzer/CODE")}</code>.
      </p>
      {/* §H2 (Handoff #12): login trouble is THE support moment. */}
      <p className="footnote">
        Stuck? <a className="supportlink" href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a>
      </p>
    </div>
  );
}

function Wordmark() {
  return <div className="wordmark">DRINKQUIRY</div>;
}
