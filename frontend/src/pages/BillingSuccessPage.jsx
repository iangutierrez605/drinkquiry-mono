import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, SUPPORT_EMAIL } from "../lib/api";
import { loadAuth, onAuthChange } from "../lib/storage";
import AuthScreen from "../components/AuthScreen";

/**
 * §F7 (Handoff #18): /billing/success — where Stripe sends the browser back.
 *
 * THIS PAGE GRANTS NOTHING. Fulfillment happens only in the WEBHOOK (rule
 * 1 / §F3); all this page does is poll GET /billing/status/?session=… until
 * the caller's own purchase reads paid, then link onward. The webhook
 * usually lands within seconds; the patient fallback copy covers the rare
 * slow race honestly instead of pretending.
 *
 * Voice: money copy plain (rule 8).
 */
export default function BillingSuccessPage() {
  const [auth, setAuth] = useState(loadAuth());
  useEffect(() => onAuthChange(() => setAuth(loadAuth())), []);
  if (!auth) return <AuthScreen onAuthed={setAuth} />;
  return <SuccessBody auth={auth} />;
}

function SuccessBody({ auth }) {
  const [params] = useSearchParams();
  const sessionId = params.get("session_id") || "";
  const [phase, setPhase] = useState(sessionId ? "waiting" : "no-session");
  const [session, setSession] = useState(null);
  const tries = useRef(0);

  useEffect(() => {
    if (!sessionId) return undefined;
    let live = true;
    let timer = null;
    const poll = async () => {
      tries.current += 1;
      try {
        const status = await api.billingStatus(auth.token, sessionId);
        if (!live) return;
        setSession(status.session);
        if (status.session?.paid) {
          setPhase("paid");
          return;
        }
        if (status.session?.status === "failed") {
          setPhase("failed");
          return;
        }
      } catch {
        /* transient — keep polling */
      }
      if (!live) return;
      if (tries.current >= 15) {
        setPhase("slow"); // ~45s of patience, then the honest fallback
        return;
      }
      timer = setTimeout(poll, 3000);
    };
    poll();
    return () => {
      live = false;
      if (timer) clearTimeout(timer);
    };
  }, [auth.token, sessionId]);

  if (phase === "no-session") {
    return (
      <div className="page billing-success">
        <h1>Hmm — nothing to confirm</h1>
        <p className="footnote">
          This page expects to be reached from checkout. Your profile's
          billing panel always shows the current state of your purchases.
        </p>
        <Link className="btn btn--primary" to="/profile">Go to your profile</Link>
      </div>
    );
  }

  if (phase === "paid") {
    const subscription = session?.product_key?.includes("monthly");
    return (
      <div className="page billing-success">
        <h1>You're in 🍻</h1>
        <p>
          Payment confirmed. {subscription
            ? "Your subscription is active — your new allowances are live right now."
            : "Your pack is ready, and a starter category is already waiting in your library."}
        </p>
        <p className="footnote">
          A confirmation email with your receipt is on its way — keep it for
          your records.
        </p>
        <div className="billing-success-actions">
          <Link className="btn btn--primary" to="/create">Start adding questions</Link>
          <Link className="btn btn--ghost" to="/profile">See it on your profile</Link>
        </div>
      </div>
    );
  }

  if (phase === "failed") {
    return (
      <div className="page billing-success">
        <h1>That payment didn't go through</h1>
        <p className="footnote">
          No charge was completed. You can try again from the pricing page —
          nothing was lost.
        </p>
        <Link className="btn btn--primary" to="/pricing">Back to pricing</Link>
      </div>
    );
  }

  if (phase === "slow") {
    return (
      <div className="page billing-success">
        <h1>Almost there…</h1>
        <p>
          Your payment is being confirmed. This usually takes seconds, but
          some payment methods take longer. It's safe to leave this page —
          your purchase appears on your profile the moment it lands, and
          you'll get a confirmation email.
        </p>
        <p className="footnote">
          Still nothing after a while? Email {SUPPORT_EMAIL} and we'll sort it
          out.
        </p>
        <Link className="btn btn--ghost" to="/profile">Check your profile</Link>
      </div>
    );
  }

  return (
    <div className="page billing-success">
      <h1>Confirming your payment…</h1>
      <p className="footnote">One moment — checking with our payment processor.</p>
    </div>
  );
}
