import { useEffect, useRef, useState } from "react";
import { loadAgeAck, saveAgeAck } from "../lib/storage";

/**
 * §H (Handoff #9): first-visit age notice, rendered at the App level on ALL
 * routes — including the buzzer (buzzer holders are precisely the people
 * drinking) and the board (a TV sees it once, someone clicks it, done).
 *
 * It's a good-faith notice, not enforcement — there's no meaningful
 * client-side age verification and we don't pretend otherwise. Confirmed
 * once → dq_age_ack_v1 in localStorage, never shown again on that browser
 * (the key is versioned so a future copy change can re-prompt).
 *
 * Focus behavior: the primary button is autofocused; Escape deliberately
 * does nothing — the modal must be answered, not dismissed around.
 */
export default function AgeGate() {
  const [acked, setAcked] = useState(() => !!loadAgeAck());
  const [declined, setDeclined] = useState(false);
  const primaryRef = useRef(null);

  useEffect(() => {
    if (!acked) primaryRef.current?.focus();
  }, [acked, declined]);

  if (acked) return null;

  const confirm = () => {
    saveAgeAck();
    setAcked(true);
  };

  return (
    <div className="agegate" role="dialog" aria-modal="true" aria-label="Age notice">
      <div className="agegate__panel panel">
        <h2 className="agegate__title">One thing first —</h2>
        {declined ? (
          <>
            <p className="agegate__body">
              No problem — come back with the grown-ups, or ask your host to run points mode.
            </p>
            <button type="button" className="btn btn--ghost" onClick={() => setDeclined(false)}>
              ← back
            </button>
          </>
        ) : (
          <>
            <p className="agegate__body">
              Drinkquiry is trivia with drinking-game references. Points mode is for everyone; drinks mode is for
              adults.
            </p>
            <div className="agegate__btns">
              <button type="button" ref={primaryRef} className="btn btn--primary btn--big" onClick={confirm}>
                I'm of legal drinking age
              </button>
              <button type="button" className="btn btn--ghost" onClick={() => setDeclined(true)}>
                I'm not
              </button>
            </div>
          </>
        )}
        <p className="agegate__note">Drink responsibly. 🍻</p>
      </div>
    </div>
  );
}
