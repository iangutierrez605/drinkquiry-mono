import { BrowserRouter, Link, Route, Routes, useNavigate } from "react-router-dom";
import { useState } from "react";
import HostPage from "./pages/HostPage";
import CreatePage from "./pages/CreatePage";
import BoardPage from "./pages/BoardPage";
import BuzzerPage from "./pages/BuzzerPage";
import ModeratePage from "./pages/ModeratePage";
import ProfilePage from "./pages/ProfilePage";

/**
 * §H5 (Handoff #8): a real landing page. The join-code input is still the
 * #1 job and rules the fold; below it, a short how-it-works strip and a
 * footer for the stranger who wandered in. No backend involvement.
 */
function Landing() {
  const [code, setCode] = useState("");
  const navigate = useNavigate();
  const valid = /^[A-Z0-9]{6}$/.test(code);
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
            <span>The Jeopardy grid, the buzz order, the big reveals — cast it or plug it in.</span>
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

      <footer className="landingfoot">
        <span className="wordmark wordmark--small">DRINKQUIRY</span>
        <nav className="landingfoot__nav">
          <Link to="/host">Host</Link>
          <Link to="/create">Make questions</Link>
          <Link to="/profile">Profile</Link>
        </nav>
        <span className="landingfoot__note">Please drink responsibly — it's a quiz, not a contest. 🍻</span>
      </footer>
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="/host" element={<HostPage />} />
        <Route path="/create" element={<CreatePage />} />
        <Route path="/moderate" element={<ModeratePage />} />
        <Route path="/profile" element={<ProfilePage />} />
        <Route path="/board/:code" element={<BoardPage />} />
        <Route path="/game/buzzer/:code" element={<BuzzerPage />} />
      </Routes>
    </BrowserRouter>
  );
}
