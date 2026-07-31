import { useEffect, useState } from "react";
import { Link, NavLink, useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import { clearAuth, loadAuth, onAuthChange } from "../lib/storage";

/**
 * §F2 (Handoff #9): the site-wide navbar. Mounted once at the App level on
 * every route EXCEPT the two game surfaces (/board/:code is a clean shared
 * TV; /game/buzzer/:code is a full-screen party surface for people who may
 * have no account — chrome there is noise).
 *
 * Auth state comes from dq_auth via storage.js, kept live by the
 * dq-auth-changed event (saveAuth/clearAuth announce; api.js announces when
 * a dead Knox token is dropped). The Moderate link only renders when the
 * PROFILE says is_staff — cosmetic as ever (rule 4): every /api/moderation/*
 * endpoint is IsAdminUser server-side. The profile fetch is cached in module
 * state per token so route changes don't refetch.
 */

// token -> is_staff, remembered across mounts/routes for this tab's lifetime.
const staffCache = new Map();

export default function SiteNav() {
  const [auth, setAuth] = useState(loadAuth());
  const [isStaff, setIsStaff] = useState(auth ? staffCache.get(auth.token) === true : false);
  const navigate = useNavigate();

  useEffect(() => onAuthChange(() => setAuth(loadAuth())), []);

  useEffect(() => {
    if (!auth?.token) {
      setIsStaff(false);
      return undefined;
    }
    if (staffCache.has(auth.token)) {
      setIsStaff(staffCache.get(auth.token) === true);
      return undefined;
    }
    let alive = true;
    api
      .profile(auth.token)
      .then((p) => {
        staffCache.set(auth.token, !!p.is_staff);
        if (alive) setIsStaff(!!p.is_staff);
      })
      .catch(() => {}); // the link is optional sugar; 401s already clear auth
    return () => {
      alive = false;
    };
  }, [auth?.token]);

  const logout = () => {
    // Best-effort server-side revoke (§F4 — the token really dies); the Knox
    // token may already be expired, hence the swallow. clearAuth announces,
    // so this nav and any mounted page fall back to signed-out immediately.
    if (auth?.token) api.logout(auth.token).catch(() => {});
    clearAuth();
    navigate("/");
  };

  const navClass = ({ isActive }) => `sitenav__link ${isActive ? "sitenav__link--on" : ""}`;

  return (
    <nav className="sitenav">
      <Link to="/" className="wordmark sitenav__wordmark">
        DRINKQUIRY
      </Link>
      <div className="sitenav__links">
        {/* §G2 (Handoff #12): useful discovery logged-out AND logged-in. */}
        <NavLink to="/categories" className={navClass}>
          Categories
        </NavLink>
        <NavLink to="/host" className={navClass}>
          Host
        </NavLink>
        <NavLink to="/create" className={navClass}>
          Make questions
        </NavLink>
        <NavLink to="/profile" className={navClass}>
          Profile
        </NavLink>
        {isStaff && (
          <NavLink to="/moderate" className={navClass}>
            Moderate
          </NavLink>
        )}
      </div>
      <div className="sitenav__auth">
        {auth ? (
          <>
            <span className="sitenav__user" title={auth.user?.email}>
              {/* §I (Handoff #12): NEVER the full email — a blank display
                  name falls back to the email's local-part ("sam", not
                  "sam@gmail.com"). The full address stays in the tooltip. */}
              {auth.user?.display_name || (auth.user?.email || "").split("@")[0]}
            </span>
            <button type="button" className="btn btn--ghost btn--sm" onClick={logout}>
              Log out
            </button>
          </>
        ) : (
          <Link className="btn btn--ghost btn--sm" to="/login">
            Log in
          </Link>
        )}
      </div>
    </nav>
  );
}
