import { useEffect, useRef, useState } from "react";
import { Link, NavLink, useLocation, useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import { clearAuth, loadAuth, onAuthChange } from "../lib/storage";

/**
 * §F2 (Handoff #9): the site-wide navbar. Mounted once at the App level on
 * every route EXCEPT the two game surfaces (/board/:code is a clean shared
 * TV; /game/buzzer/:code is a full-screen party surface for people who may
 * have no account — chrome there is noise).
 *
 * §F1/§F2 (Handoff #16): the row now splits "doing" from "account/admin".
 * The main row keeps the discovery links (Categories · Host · Tournaments ·
 * Make questions · How to play — §F4's new page) and is identical logged in
 * or out; Profile ONLY exists inside the username dropdown, so it appears
 * strictly after login (the owner's ask). The dropdown also carries the
 * four §F3 admin destinations when the profile says is_staff — /moderate
 * (queues), /manage/users, /manage/library, /manage/themes — and Log out
 * (moved off the bar; flagged as a taste call in CHANGES). The staff gate
 * here is cosmetic as ever (rule 4): every /api/moderation/* endpoint is
 * IsAdminUser server-side, and the pages themselves re-check via StaffGate.
 *
 * Auth state comes from dq_auth via storage.js, kept live by the
 * dq-auth-changed event (saveAuth/clearAuth announce; api.js announces when
 * a dead Knox token is dropped). The profile fetch is cached in module
 * state per token so route changes don't refetch.
 */

// token -> is_staff, remembered across mounts/routes for this tab's lifetime.
const staffCache = new Map();

export default function SiteNav() {
  const [auth, setAuth] = useState(loadAuth());
  const [isStaff, setIsStaff] = useState(auth ? staffCache.get(auth.token) === true : false);
  const navigate = useNavigate();
  // §F1 (Handoff #17): the Log in button carries the user's place, so a
  // browser on /categories comes BACK to /categories after auth (LoginPage
  // honors ?next=, guarded there against off-site values). On /login and
  // /reset-password there's no place worth preserving — plain /login. The
  // two chrome-less game surfaces have no nav, so a next=/board/... can't
  // occur by construction.
  const { pathname, search } = useLocation();
  const onAuthRoute = pathname === "/login" || pathname === "/reset-password";
  const loginTo = onAuthRoute ? "/login" : `/login?next=${encodeURIComponent(pathname + search)}`;

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
      .catch(() => {}); // the links are optional sugar; 401s already clear auth
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
        {/* §I (Handoff #13): the tournament control room. Visible to all
            signed-in states — the page itself upsells free accounts (the
            server's meter is the gate, so overrides just work). */}
        <NavLink to="/tournaments" className={navClass}>
          Tournaments
        </NavLink>
        <NavLink to="/create" className={navClass}>
          Make questions
        </NavLink>
        {/* §F4 (Handoff #16): the ways-to-play explainer — public, so a bar
            owner evaluating the product sees it before signing up. */}
        <NavLink to="/how-to-play" className={navClass}>
          How to play
        </NavLink>
      </div>
      <div className="sitenav__auth">
        {auth ? (
          <UserMenu auth={auth} isStaff={isStaff} onLogout={logout} />
        ) : (
          <Link className="btn btn--ghost btn--sm" to={loginTo}>
            Log in
          </Link>
        )}
      </div>
    </nav>
  );
}

/**
 * §F1 (Handoff #16): the username dropdown — Profile, the staff
 * destinations, Log out. Built by hand (no new dependency): closes on
 * outside pointerdown, on Escape (focus returns to the trigger), and on
 * any item click; first item takes focus when the menu opens; the route
 * changing closes it too (belt for browser back). Contained on phones by
 * CSS (right-anchored, viewport-capped width, its own scroll past 60vh) —
 * item 2's containment rule applied from birth.
 */
function UserMenu({ auth, isStaff, onLogout }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);
  const triggerRef = useRef(null);
  const menuRef = useRef(null);
  const location = useLocation();

  // Any navigation closes the menu (item clicks close it themselves; this
  // also catches back/forward).
  useEffect(() => {
    setOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    if (!open) return undefined;
    // Focus lands on the first item so keyboard users are IN the menu.
    menuRef.current?.querySelector("a, button")?.focus();
    const onPointerDown = (e) => {
      if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false);
    };
    const onKeyDown = (e) => {
      if (e.key === "Escape") {
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const close = () => setOpen(false);

  return (
    <div className="usermenu" ref={rootRef}>
      <button
        type="button"
        ref={triggerRef}
        className={`usermenu__trigger ${open ? "usermenu__trigger--open" : ""}`}
        aria-haspopup="menu"
        aria-expanded={open}
        title={auth.user?.email}
        onClick={() => setOpen((o) => !o)}
      >
        {/* §I (Handoff #12): NEVER the full email — a blank display name
            falls back to the email's local-part ("sam", not
            "sam@gmail.com"). The full address stays in the tooltip. */}
        <span className="usermenu__name">
          {auth.user?.display_name || (auth.user?.email || "").split("@")[0]}
        </span>
        <span className="usermenu__chev" aria-hidden="true">
          {open ? "▴" : "▾"}
        </span>
      </button>
      {open && (
        <div className="usermenu__menu" role="menu" ref={menuRef}>
          <Link className="usermenu__item" role="menuitem" to="/profile" onClick={close}>
            Profile
          </Link>
          {isStaff && (
            <>
              <div className="usermenu__rule" role="separator" />
              {/* §F3: the four single-purpose staff destinations — "one at
                  a time", per the owner. */}
              <Link className="usermenu__item" role="menuitem" to="/moderate" onClick={close}>
                Moderate
              </Link>
              <Link className="usermenu__item" role="menuitem" to="/manage/users" onClick={close}>
                Manage users
              </Link>
              <Link className="usermenu__item" role="menuitem" to="/manage/library" onClick={close}>
                Manage library
              </Link>
              <Link className="usermenu__item" role="menuitem" to="/manage/themes" onClick={close}>
                Manage themes
              </Link>
            </>
          )}
          <div className="usermenu__rule" role="separator" />
          <button
            type="button"
            className="usermenu__item usermenu__item--btn"
            role="menuitem"
            onClick={() => {
              close();
              onLogout();
            }}
          >
            Log out
          </button>
        </div>
      )}
    </div>
  );
}
