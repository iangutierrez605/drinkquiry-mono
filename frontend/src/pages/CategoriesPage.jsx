import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, errorText, mediaUrl } from "../lib/api";
import { useDebounced } from "../lib/hooks";
import { loadAuth, onAuthChange } from "../lib/storage";

/**
 * §G2 (Handoff #12): /categories — the logged-out marketing funnel. A browse
 * grid over GET /api/categories/public/ (anonymous, unthrottled): photo when
 * set, name, description, "N questions". Cards are NOT selectable — this is
 * a shop window, not the create screen; the CTA banner routes to /host,
 * which shows the AuthScreen when logged out (the funnel already exists).
 * Inside ChromeLayout (SiteNav on top); the AgeGate still sits above
 * everything — deliberate, it's a drinking product.
 *
 * §F5 (Handoff #15): search-and-page shaped. The grid renders ONE server
 * page at a time (debounced server-side ?search=, Load more appends) —
 * never the fetch-everything allPages sweep that would mean 40+ sequential
 * requests and a 2,000-card render at the current corpus (C21). Fetches
 * carry a sequence number so a stale slow response can never clobber a
 * newer one (§G top trap).
 */
export default function CategoriesPage() {
  // Hooks first, early returns after (C11).
  const [auth, setAuth] = useState(loadAuth());
  const [search, setSearch] = useState("");
  const debounced = useDebounced(search);
  // search+page move ATOMICALLY (one state, one fetch effect) so a search
  // change can't race its own page reset into a duplicate request.
  const [params, setParams] = useState({ search: "", page: 1 });
  const [rows, setRows] = useState(null); // accumulated pages; null = first load
  const [count, setCount] = useState(0);
  const [hasNext, setHasNext] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const seq = useRef(0);

  useEffect(() => onAuthChange(() => setAuth(loadAuth())), []);

  // Debounced input → params (page resets to 1). The identity guard skips
  // the initial no-op so mount fires exactly one fetch.
  useEffect(() => {
    setParams((p) => (p.search === debounced ? p : { search: debounced, page: 1 }));
  }, [debounced]);

  useEffect(() => {
    const mySeq = ++seq.current; // stale-response guard (§G)
    setLoading(true);
    api
      .publicCategoriesPage({ search: params.search, page: params.page })
      .then((d) => {
        if (seq.current !== mySeq) return;
        setRows((prev) => (params.page === 1 ? d.results : [...(prev ?? []), ...d.results]));
        setCount(d.count);
        setHasNext(!!d.next);
        setError(null);
        setLoading(false);
      })
      .catch((err) => {
        if (seq.current !== mySeq) return;
        setError(errorText(err));
        setLoading(false);
      });
  }, [params]);

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

  return (
    <div className="page">
      <h1 className="h1">Categories</h1>
      <p className="footnote browseintro">
        Every game is built from packs like these — official ones plus creations from hosts on the
        creator plan. Questions stay under wraps until game night, obviously.
      </p>
      {cta}
      <input
        className="listsearch"
        type="search"
        placeholder="Search categories…"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        aria-label="Search categories"
      />
      {error && <p className="formerror formerror--block">{error}</p>}
      {rows == null && !error && <p className="footnote">Loading…</p>}
      {rows != null && count === 0 && !loading && (
        <p className="footnote">
          {params.search
            ? `Nothing matches “${params.search}” — try fewer words.`
            : "Nothing here yet — the official packs are on their way. 🍺"}
        </p>
      )}
      {rows?.length > 0 && (
        <>
          <div className="browsegrid">
            {rows.map((cat) => (
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
          <div className="loadmore">
            <span className="listmeta">
              Showing {rows.length} of {count}
            </span>
            {hasNext && (
              <button
                className="btn btn--ghost"
                disabled={loading}
                onClick={() => setParams((p) => ({ ...p, page: p.page + 1 }))}
              >
                {loading ? "Loading…" : "Load more"}
              </button>
            )}
          </div>
        </>
      )}
    </div>
  );
}
