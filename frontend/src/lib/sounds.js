// Buzzer sounds (Handoff #7 §I): 4 short, clearly distinct sounds, < 1 s,
// synthesized with WebAudio — no audio files, no new deps, no licensing.
// Used by BOTH surfaces: the buzzer page plays the player's OWN sound on
// press (the tap is the user gesture, so autoplay policy is satisfied) and
// the board plays the buzzing team's sound when a new buzz lands (behind a
// default-off toggle, since boards need a gesture before audio may start).
//
// Display-only by design (architecture rule 1): nothing here reads or gates
// game state — callers hand in the participant's `buzzer_sound` (1–4) from
// the server snapshot / join response, never a locally guessed number.

let ctx = null;

function audioContext() {
  if (typeof window === "undefined") return null;
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) return null;
  if (!ctx) ctx = new Ctx();
  return ctx;
}

/**
 * Create/resume the AudioContext from within a user gesture (buzz press,
 * sound-toggle click). Safe to call repeatedly.
 */
export function ensureAudio() {
  const c = audioContext();
  if (c && c.state === "suspended") c.resume().catch(() => {});
  return !!c;
}

/** One enveloped oscillator note. Times are relative to ctx.currentTime. */
function note(c, { type, freq, start = 0, length = 0.2, gain = 0.22, glideTo = null }) {
  const osc = c.createOscillator();
  const amp = c.createGain();
  const t0 = c.currentTime + start;
  osc.type = type;
  osc.frequency.setValueAtTime(freq, t0);
  if (glideTo) osc.frequency.exponentialRampToValueAtTime(glideTo, t0 + length);
  amp.gain.setValueAtTime(0.0001, t0);
  amp.gain.exponentialRampToValueAtTime(gain, t0 + 0.015); // fast attack, no click
  amp.gain.exponentialRampToValueAtTime(0.0001, t0 + length); // decay to silence
  osc.connect(amp).connect(c.destination);
  osc.start(t0);
  osc.stop(t0 + length + 0.05);
}

// The four voices. All well under 1 s and deliberately unalike in timbre AND
// contour so they stay tellable-apart across a noisy bar's TV speakers.
const SOUNDS = {
  1: (c) => {
    // Classic game-show buzzer: harsh square blast.
    note(c, { type: "square", freq: 196, length: 0.4, gain: 0.2 });
    note(c, { type: "square", freq: 98, length: 0.4, gain: 0.12 });
  },
  2: (c) => {
    // Bright rising ding-ding.
    note(c, { type: "sine", freq: 523, length: 0.18, gain: 0.3 });
    note(c, { type: "sine", freq: 784, start: 0.14, length: 0.32, gain: 0.3 });
  },
  3: (c) => {
    // Falling sawtooth honk.
    note(c, { type: "sawtooth", freq: 440, glideTo: 165, length: 0.45, gain: 0.18 });
  },
  4: (c) => {
    // Triple beep.
    note(c, { type: "triangle", freq: 880, length: 0.09, gain: 0.3 });
    note(c, { type: "triangle", freq: 880, start: 0.13, length: 0.09, gain: 0.3 });
    note(c, { type: "triangle", freq: 1175, start: 0.26, length: 0.16, gain: 0.3 });
  },
};

/**
 * Play buzzer sound `n` (1–4; anything else wraps into range). No-ops when
 * WebAudio is unavailable or the context can't start (no gesture yet).
 */
export function playBuzz(n) {
  const c = audioContext();
  if (!c) return;
  if (c.state === "suspended") {
    // Only resolves if a gesture already unlocked audio; otherwise stay silent.
    c.resume().catch(() => {});
    if (c.state === "suspended") return;
  }
  const idx = ((Math.trunc(Number(n) || 1) - 1) % 4 + 4) % 4 + 1;
  try {
    SOUNDS[idx](c);
  } catch {
    /* audio is best-effort, never break the page */
  }
}
