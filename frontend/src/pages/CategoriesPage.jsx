import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, errorText, mediaUrl } from "../lib/api";
import { loadAuth, onAuthChange } from "../lib/storage";

/**
 * §G2 (Handoff #12): /categories — the logged-out marketing funnel. A browse
 * grid over GET /api/categories/public/ (anonymous, unthrottled): photo when
 * set, name, description, "N questions". Cards are NOT selectable — this is
 * a shop window, not the create screen; the CTA banner routes to /host,
 * which shows the AuthScreen when logged out (the funnel already exists).
 * Inside ChromeLayout (SiteNav on top); the AgeGate still sits above
 * everything — deliberate, it's a drinking product.
 */
export default function CategoriesPage() {
  // Hooks first, early returns after (C11).
  const [cats, setCats] = useState(null); // null = loading
  const [error, setError] = useState(null);
  const [auth, setAuth] = useState(loadAuth());
  useEffect(() => onAuthChange(() => setAuth(loadAuth())), []);
  useEffect(() => {
    let alive = true;
    api
      .publicCategories()
      .then((rows) => alive && (setCats(rows), setError(null)))
      .catch((err) => alive && setError(errorText(err)));
    return () => {
      alive = false;
    };
  }, []);

  const cta = (
    <section className="panel panel--center ctabanner">
      {auth ? (
        <>
          <h2 className="h2">Host a game with these</h2>
          <p>Pick your categories, get a code, put the board on the TV.</p>
          <Link className="btn btn--gold" to="/host">
            Host a game
          </Link>
        </>
      ) : (
        <>
          <h2 className="h2">Like what you see?</h2>
          <p>Make a free account and host tonight's game — players just need their phones.</p>
          <Link className="btn btn--gold" to="/host">
            Make a free account &amp; host
          </Link>
        </>
      )}
    </section>
  );

  if (error)
    return (
      <div className="page">
        <h1 className="h1">Categories</h1>
        <p className="formerror formerror--block">{error}</p>
      </div>
    );

  return (
    <div className="page">
      <h1 className="h1">Categories</h1>
      <p className="footnote browseintro">
        Every game is built from packs like these — official ones plus creations from hosts on the
        creator plan. Questions stay under wraps until game night, obviously.
      </p>
      {cta}
      {cats == null && <p className="footnote">Loading…</p>}
      {cats?.length === 0 && (
        <p className="footnote">Nothing here yet — the official packs are on their way. 🍺</p>
      )}
      {cats?.length > 0 && (
        <div className="browsegrid">
          {cats.map((cat) => (
            <div key={cat.id} className="catcard catcard--browse">
              {cat.photo && (
                <img className="catcard__photo" src={mediaUrl(cat.photo)} alt="" loading="lazy" />
              )}
              <span className="catcard__name">{cat.name}</span>
              {cat.description && <span className="catcard__desc">{cat.description}</span>}
              <span className="catcard__count">
                {cat.question_count} question{cat.question_count === 1 ? "" : "s"}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
