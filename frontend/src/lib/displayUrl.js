// §F4/§F9 (Handoff #15): instructional strings that used to show a bare
// path ("open /game/buzzer/CODE") now show the full host — drinkquiry.com/…
// on prod, localhost:5173/… in dev — derived from window.location at render
// time, NEVER hardcoded (the BoardPage QR set this precedent with
// window.location.origin). `host` rather than `origin` on purpose: the owner
// wants "drinkquiry.com/game/buzzer/CODE" without the protocol noise.
// DISPLAY ONLY — router paths, <Link to=…> and fetch URLs never go through
// here.
export const displayUrl = (path) => `${window.location.host}${path}`;
