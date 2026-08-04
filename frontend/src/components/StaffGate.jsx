import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, errorText } from "../lib/api";
import { clearAuth, loadAuth, onAuthChange } from "../lib/storage";

/**
 * §F3 (Handoff #16): the staff-guard wrapper for the split admin pages —
 * /moderate (queues), /manage/users, /manage/library, /manage/themes all
 * render inside this so a non-staff user hitting a /manage/* URL directly
 * never sees admin UI. It is EXACTLY the outer gate the old six-tab
 * ModeratePage ran (log-in funnel with ?next=, profile fetch with the
 * 401-clears-auth rule, the staff-only upsell), lifted out so four pages
 * share one copy. As ever the gate is COSMETIC (rule 4): every
 * /api/moderation/* endpoint stays IsAdminUser server-side.
 *
 * Props: title (the page h1), next (the login redirect, e.g.
 * "/manage/users"), children — a FUNCTION ({auth, profile}) => body,
 * called only once the profile confirms is_staff.
 */
export default function StaffGate({ title, next, children }) {
  const [auth, setAuth] = useState(loadAuth());
  const [profile, setProfile] = useState(null);
  const [profileError, setProfileError] = useState(null);
  // Nav logout / dead-token cleanup flips these pages immediately.
  useEffect(() => onAuthChange(() => setAuth(loadAuth())), []);

  useEffect(() => {
    if (!auth) return undefined;
    let alive = true;
    api
      .profile(auth.token)
      .then((p) => alive && setProfile(p))
      .catch((err) => {
        if (!alive) return;
        if (err instanceof ApiError && err.status === 401) {
          clearAuth();
          setAuth(null);
        } else {
          setProfileError(errorText(err));
        }
      });
    return () => {
      alive = false;
    };
  }, [auth]);

  if (!auth)
    return (
      <Shell title={title}>
        <section className="panel panel--center">
          <h2 className="h2">Log in first</h2>
          <p className="footnote">This area needs a staff account.</p>
          <Link className="btn btn--primary" to={`/login?next=${next}`}>
            Go to login
          </Link>
        </section>
      </Shell>
    );

  if (profileError)
    return (
      <Shell title={title}>
        <p className="formerror formerror--block">{profileError}</p>
      </Shell>
    );
  if (!profile)
    return (
      <Shell title={title}>
        <p>Loading your profile…</p>
      </Shell>
    );

  // Same pattern as /create's upsell card: friendly panel, no details leaked.
  if (!profile.is_staff)
    return (
      <Shell title={title}>
        <section className="panel panel--center upsell">
          <span className="upsell__emoji">🛡️</span>
          <h2 className="h2">Staff only</h2>
          <p className="footnote">This area is for site staff. Everything else is over on hosting.</p>
          <Link className="btn btn--ghost" to="/host">
            Back to hosting
          </Link>
        </section>
      </Shell>
    );

  return <Shell title={title}>{children({ auth, profile })}</Shell>;
}

function Shell({ title, children }) {
  return (
    <div className="page page--wide">
      {/* Wide shell for all four staff pages: the Library and Users rows
          want the room on desktop; item 2's containment rules keep them
          honest at phone width. */}
      <h1 className="h1">{title}</h1>
      {children}
    </div>
  );
}

/* The two card chips BOTH staff surfaces render (the moderation queue's
   cards and the library rows) — shared here so the §F3 page split doesn't
   fork them. */

export function Owner({ item }) {
  const who = item.owner_display_name || item.owner_email || "official";
  return (
    <span className="modcard__owner" title={item.owner_email || ""}>
      by {who}
    </span>
  );
}

/** §J1: "used in N games / questions asked N times" chip on review cards. */
export function UsageBadge({ count, noun = "time" }) {
  return (
    <span className="usagebadge" title="How often this has appeared in games (all hosts)">
      🎲 {noun === "game" ? `in ${count} game${count === 1 ? "" : "s"}` : `played ${count} ${noun}${count === 1 ? "" : "s"}`}
    </span>
  );
}
