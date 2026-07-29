import { BrowserRouter, Link, Route, Routes, useNavigate } from "react-router-dom";
import { useState } from "react";
import HostPage from "./pages/HostPage";
import CreatePage from "./pages/CreatePage";
import BoardPage from "./pages/BoardPage";
import BuzzerPage from "./pages/BuzzerPage";
import ModeratePage from "./pages/ModeratePage";
import ProfilePage from "./pages/ProfilePage";

function Landing() {
  const [code, setCode] = useState("");
  const navigate = useNavigate();
  const valid = /^[A-Z0-9]{6}$/.test(code);
  return (
    <div className="page page--center">
      <div className="wordmark wordmark--hero">DRINKQUIRY</div>
      <p className="tagline">Jeopardy night, but the stakes are beverages.</p>
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
          />
        </label>
        <button className="btn btn--primary" disabled={!valid}>
          {code && !valid ? "Codes are 6 letters/numbers" : "Join"}
        </button>
        <div className="landing__or">or</div>
        <Link className="btn btn--gold" to="/host">
          Host a game
        </Link>
      </form>
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
