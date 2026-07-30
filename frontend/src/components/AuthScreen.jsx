import { useState } from "react";
import { api, errorText } from "../lib/api";
import { saveAuth } from "../lib/storage";

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
        Players don't need accounts — only the host logs in. Buzzers join at <code>/game/buzzer/CODE</code>.
      </p>
    </div>
  );
}

function Wordmark() {
  return <div className="wordmark">DRINKQUIRY</div>;
}
