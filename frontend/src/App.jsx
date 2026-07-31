import { BrowserRouter, Link, Outlet, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { useEffect, useState } from "react";
import { api, SUPPORT_EMAIL } from "./lib/api";
import HostPage from "./pages/HostPage";
import CategoriesPage from "./pages/CategoriesPage";
import CreatePage from "./pages/CreatePage";
import BoardPage from "./pages/BoardPage";
import BuzzerPage from "./pages/BuzzerPage";
import ModeratePage from "./pages/ModeratePage";
import ProfilePage from "./pages/ProfilePage";
import TournamentsPage from "./pages/TournamentsPage";
import TournamentDetailPage from "./pages/TournamentDetailPage";
import ResetPasswordPage from "./pages/ResetPasswordPage";
import AuthScreen from "./components/AuthScreen";
import SiteNav from "./components/SiteNav";
import AgeGate from "./components/AgeGate";

/**
 * §H5 (Handoff #8): a real landing page. The join-code input is still the
 * #1 job and rules the fold; below it, a short how-it-works strip and a
 * footer for the stranger who wandered in. No backend involvement.
 */
function Landing() {
  const [code, setCode] = useState("");
  // §G2 (Handoff #12): a light category teaser — same public endpoint as
  // /categories, sliced client-side. Fetch failure/empty just hides the
  // strip (the landing's existing structure stays).
  const [teaser, setTeaser] = useState([]);
  const navigate = useNavigate();
  const valid = /^[A-Z0-9]{6}$/.test(code);
  useEffect(() => {
    let alive = true;
    api
      .publicCategories()
      .then((cats) => alive && setTeaser(cats.slice(0, 4)))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);
  return (
    <div className="landingpage">
      <section className="landinghero">
        <div className="wordmark wordmark--hero">DRINKQUIRY</div>
        <p className="tagline">Bar trivia where the losers drink.</p>
        <form
          className="panel landing"
          onSubmit={(e) => {
            e.preventDefault();
            if (valid) navigate(`/game/buzzer/${code}`);
          }}
        >
          <label className="field">
            Got a game code? Join as a buzzer
            <input
              value={code}
              onChange={(e) => setCode(e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 6))}
              placeholder="AB3XKQ"
              maxLength={6}
              className="codeinput"
              inputMode="text"
              autoCapitalize="characters"
            />
          </label>
          <button className="btn btn--primary btn--big" disabled={!valid}>
            {code && !valid ? "Codes are 6 letters/numbers" : "Join the game"}
          </button>
          <div className="landing__or">or</div>
          <Link className="btn btn--gold" to="/host">
            Host a game
          </Link>
        </form>
      </section>

      <section className="howworks">
        <h2 className="howworks__title">How a night goes</h2>
        <div className="howworks__strip">
          <div className="howstep">
            <span className="howstep__emoji">💻</span>
            <strong>Host on a laptop</strong>
            <span>Pick categories, get a 6-letter code. Your screen quietly shows every answer.</span>
          </div>
          <div className="howstep__arrow">→</div>
          <div className="howstep">
            <span className="howstep__emoji">📺</span>
            <strong>Board on the TV</strong>
            <span>The trivia grid, the buzz order, the big reveals — cast it or plug it in.</span>
          </div>
          <div className="howstep__arrow">→</div>
          <div className="howstep">
            <span className="howstep__emoji">📱</span>
            <strong>Phones are buzzers</strong>
            <span>One phone per team, no app, no accounts. Answer right, pick who drinks.</span>
          </div>
        </div>
        <p className="howworks__foot">
          Points mode included for the well-behaved. Bring your own beverages and judgment.
        </p>
      </section>

      {teaser.length > 0 && (
        <section className="teaser">
          <h2 className="howworks__title">What's on the board</h2>
          <div className="teaser__strip">
            {teaser.map((cat) => (
              <div key={cat.id} className="catcard catcard--browse catcard--teaser">
                <span className="catcard__name">{cat.name}</span>
                <span className="catcard__count">
                  {cat.question_count} question{cat.question_count === 1 ? "" : "s"}
                </span>
              </div>
            ))}
          </div>
          <Link className="teaser__more" to="/categories">
            browse all →
          </Link>
        </section>
      )}

      <footer className="landingfoot">
        <span className="wordmark wordmark--small">DRINKQUIRY</span>
        {/* §F3: the footer's Host/Make questions/Profile links moved to the
            SiteNav above — keeping both would be the same list twice. */}
        <span className="landingfoot__note">Please drink responsibly — it's a quiz, not a contest. 🍻</span>
      </footer>
    </div>
  );
}

/**
 * §F3 (Handoff #9): /login — the SiteNav's auth corner lands here. Renders
 * the shared AuthScreen; after auth, honor ?next= (e.g. a deep link that
 * bounced someone here), else the classic destination: /host.
 */
function LoginPage() {
  const navigate = useNavigate();
  const next = new URLSearchParams(useLocation().search).get("next");
  return <AuthScreen onAuthed={() => navigate(next || "/host")} />;
}

/**
 * §F (Handoff #9): chrome layout. The SiteNav renders on every routed page
 * that nests under this layout; the two game surfaces (/board/:code TV and
 * /game/buzzer/:code phone) sit OUTSIDE it — the TV is a clean shared
 * display and the buzzer is a full-screen party surface for people who may
 * have no account (the owner asked "except the game board"; extending the
 * exception to the buzzer follows the same logic — a one-line revert if
 * unwanted: move its Route inside this layout).
 */
function ChromeLayout() {
  return (
    <>
      <SiteNav />
      <Outlet />
      {/* §H1 (Handoff #12): every chrome page gets the support footer for
          free; the TV board and buzzer stay chrome-free (they're outside
          this layout already — a bar's TV doesn't need a mailto). */}
      <footer className="sitefoot">
        Drinkquiry · <a className="supportlink" href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a>
      </footer>
    </>
  );
}

/**
 * §J2 (Handoff #12): the catch-all 404. Stateless (C11 trivially holds);
 * links home + to the browse page, plus §H's support address.
 */
function NotFound() {
  return (
    <div className="page page--center notfound">
      <div className="wordmark">DRINKQUIRY</div>
      <h1 className="h1">That page doesn't exist</h1>
      <p>Wrong turn at the bar. These work:</p>
      <div className="notfound__links">
        <Link className="btn btn--primary" to="/">
          Home
        </Link>
        <Link className="btn btn--ghost" to="/categories">
          Browse categories
        </Link>
      </div>
      <p className="footnote">
        Sure it should be here?{" "}
        <a className="supportlink" href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a>
      </p>
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      {/* §H: the age gate renders above EVERYTHING, all routes — including
          the game surfaces. One confirm per browser (localStorage). */}
      <AgeGate />
      <Routes>
        <Route element={<ChromeLayout />}>
          <Route path="/" element={<Landing />} />
          <Route path="/categories" element={<CategoriesPage />} />
          <Route path="/login" element={<LoginPage />} />
          <Route path="/reset-password" element={<ResetPasswordPage />} />
          <Route path="/host" element={<HostPage />} />
          <Route path="/create" element={<CreatePage />} />
          <Route path="/moderate" element={<ModeratePage />} />
          <Route path="/profile" element={<ProfilePage />} />
          {/* §I (Handoff #13): tournaments — inside ChromeLayout (SiteNav),
              auth-funneled by the pages themselves like /host. */}
          <Route path="/tournaments" element={<TournamentsPage />} />
          <Route path="/tournaments/:id" element={<TournamentDetailPage />} />
          {/* §J2: catch-all — LAST inside the chrome so real routes win.
              The two chrome-free game routes below still match before it
              at the top level. */}
          <Route path="*" element={<NotFound />} />
        </Route>
        <Route path="/board/:code" element={<BoardPage />} />
        <Route path="/game/buzzer/:code" element={<BuzzerPage />} />
      </Routes>
    </BrowserRouter>
  );
}
