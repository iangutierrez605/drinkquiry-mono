import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, errorText, SUPPORT_EMAIL } from "../lib/api";
import { loadAuth, onAuthChange } from "../lib/storage";
import AuthScreen from "../components/AuthScreen";

/**
 * §F7 (Handoff #18): /pricing — the store front.
 *
 * PUBLIC page; the catalog comes from GET /api/billing/products/ (display
 * strings only — the browser never sees a Stripe price id, and the server
 * re-resolves everything at checkout, rule 1). Buying funnels through the
 * house AuthScreen in place, then POST /checkout/ hands the browser to
 * Stripe's hosted page. A keyless server (billing_not_configured) renders
 * the cards with buying disabled and an honest note — dev keeps working.
 *
 * Voice: sales copy playful; money/legal lines plain (rule 8).
 */
export default function PricingPage() {
  const [auth, setAuth] = useState(loadAuth());
  const [products, setProducts] = useState(null); // null = loading
  const [loadError, setLoadError] = useState(null);
  const [buying, setBuying] = useState(null); // product key in flight
  const [buyError, setBuyError] = useState(null);
  const [wantsAuthFor, setWantsAuthFor] = useState(null); // product key awaiting sign-in

  useEffect(() => onAuthChange(() => setAuth(loadAuth())), []);
  useEffect(() => {
    let live = true;
    api
      .billingProducts()
      .then((rows) => live && setProducts(rows))
      .catch((err) => live && setLoadError(errorText(err)));
    return () => {
      live = false;
    };
  }, []);

  const buy = async (key) => {
    if (!auth) {
      setWantsAuthFor(key);
      return;
    }
    setBuying(key);
    setBuyError(null);
    try {
      const { url } = await api.checkout(auth.token, key);
      window.location.assign(url); // Stripe's hosted checkout takes it from here
    } catch (err) {
      if (err?.data?.code === "billing_not_configured") {
        setBuyError("Purchases aren't switched on for this server yet.");
      } else {
        setBuyError(errorText(err));
      }
      setBuying(null);
    }
  };

  if (wantsAuthFor) {
    return (
      <div className="page pricing-page">
        <h1>One quick sign-in first</h1>
        <p className="footnote">
          Your purchase attaches to your account — sign in or create one and
          we'll bring you right back.
        </p>
        <AuthScreen
          onAuthed={(fresh) => {
            setAuth(fresh);
            setWantsAuthFor(null);
          }}
        />
        <button className="btn btn--ghost btn--sm" onClick={() => setWantsAuthFor(null)}>
          ← Back to pricing
        </button>
      </div>
    );
  }

  const byKey = Object.fromEntries((products || []).map((p) => [p.key, p]));
  const order = ["party_game_50", "big_game_100", "tournament_pass", "venue_monthly", "venue_tournament_monthly"];

  return (
    <div className="page pricing-page">
      <h1>Pricing</h1>
      <p className="pricing-lede">
        The library of official categories is free to host, forever. These are
        for when the night should be <em>yours</em> — your questions, your
        people, your inside jokes on the big screen.
      </p>
      {loadError && <p className="formerror">{loadError}</p>}
      {buyError && <p className="formerror">{buyError}</p>}
      {products === null && !loadError && <p className="footnote">Loading the menu…</p>}
      {products !== null && (
        <div className="pricing-grid">
          {order.filter((k) => byKey[k]).map((key) => (
            <PricingCard
              key={key}
              product={byKey[key]}
              featured={key === "big_game_100"}
              buying={buying === key}
              onBuy={() => buy(key)}
            />
          ))}
          <div className="pricing-card pricing-card--custom">
            <h2>Custom Game</h2>
            <p className="pricing-price">Let's talk</p>
            <p className="pricing-blurb">
              Company party? Wedding? Something weirder? We'll build the whole
              night with you — questions, branding, the works.
            </p>
            <a
              className="btn btn--primary pricing-buy"
              href={`mailto:${SUPPORT_EMAIL}?subject=${encodeURIComponent("Custom Drinkquiry game")}`}
            >
              Email us
            </a>
          </div>
        </div>
      )}
      {/* §F6 (#20): how packs actually work — the four facts buyers ask
          about, in product voice (the PLAIN money copy stays below). */}
      <section className="packsexplainer">
        <h2 className="h2">How packs work</h2>
        <p>
          A pack is a question budget that lives inside its own private pack
          categories — your questions, kept in their own corner. Boards can
          mix and match: pack columns next to free-library columns on the
          same night, no ceremony. The one rule is that a single question
          picks a side — it lives in pack categories or in your regular
          ones, never both. Packs run for 30 days of hosting and editing;
          after that everything you wrote is kept safe and read-only, ready
          the moment you reactivate.
        </p>
      </section>
      <div className="pricing-fineprint">
        {/* Plain voice from here down (rule 8). */}
        <p>
          Prices are in USD. Payments are processed by Stripe; card details
          never touch our servers. One-time packs stay editable and hostable
          for 30 days from purchase — after that, everything you made is kept
          safe and read-only, and can be reactivated. Subscriptions renew
          monthly and can be cancelled any time from your profile's Manage
          billing button. See our <Link to="/terms">Terms</Link> for the
          purchase and refund details.
        </p>
      </div>
    </div>
  );
}

function PricingCard({ product, featured, buying, onBuy }) {
  const { name, price, interval, blurb, coming_soon: comingSoon } = product;
  return (
    <div className={`pricing-card${featured ? " pricing-card--featured" : ""}${comingSoon ? " pricing-card--soon" : ""}`}>
      {featured && <span className="pricing-badge">Best for most events</span>}
      {comingSoon && <span className="pricing-badge pricing-badge--soon">Coming soon</span>}
      <h2>{name}</h2>
      <p className="pricing-price">
        {price}
        {interval ? <span className="pricing-interval">/{interval}</span> : null}
      </p>
      <p className="pricing-blurb">{blurb}</p>
      <button className="btn btn--primary pricing-buy" onClick={onBuy} disabled={buying || comingSoon}>
        {comingSoon ? "Not yet" : buying ? "Opening checkout…" : interval ? "Subscribe" : "Buy"}
      </button>
    </div>
  );
}
