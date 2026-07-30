import { useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, errorText } from "../lib/api";

/**
 * §K1 (Handoff #9): /reset-password?uid=…&token=… — the landing spot for the
 * emailed link. New-password form; on success the server has already revoked
 * every Knox session for the account, so we send them to /login to sign in
 * fresh. Bad/expired tokens surface the server's generic 400 message (which
 * deliberately never says whether the account exists).
 */
export default function ResetPasswordPage() {
  const [params] = useSearchParams();
  const uid = params.get("uid") || "";
  const token = params.get("token") || "";
  const linkOk = useMemo(() => uid.length > 0 && token.length > 0, [uid, token]);

  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [done, setDone] = useState(false);
  const navigate = useNavigate();

  const submit = async (e) => {
    e.preventDefault();
    if (password !== confirm) {
      setError("Those passwords don't match.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.passwordReset(uid, token, password);
      setDone(true);
      setTimeout(() => navigate("/login"), 1800);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page page--center">
      <div className="wordmark">DRINKQUIRY</div>
      <div className="panel authform">
        <h2 className="h2">Pick a new password</h2>
        {!linkOk ? (
          <>
            <p>This reset link is incomplete — open the link from the email again, or request a new one.</p>
            <Link className="btn btn--ghost" to="/login">
              Back to login
            </Link>
          </>
        ) : done ? (
          <p>Password reset ✔ — taking you to the login…</p>
        ) : (
          <form onSubmit={submit}>
            <label className="field">
              New password
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                autoComplete="new-password"
                autoFocus
              />
            </label>
            <label className="field">
              New password, again
              <input
                type="password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                required
                autoComplete="new-password"
              />
            </label>
            {error && <p className="formerror">{error}</p>}
            <button className="btn btn--primary" disabled={busy}>
              {busy ? "…" : "Set new password"}
            </button>
            <p className="footnote">Resetting signs out every device that was logged in.</p>
          </form>
        )}
      </div>
    </div>
  );
}
