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

// --- §F2 (Handoff #21): the THUNDER FUCKED sting ---------------------------
// ~5 s, ORIGINAL, fully synthesized (§A.6 licensing hard rule: the app never
// ships, plays or imitates the actual song — the venue's own speakers do the
// music; this is the reveal's drum-roll). Same WebAudio, zero files, zero
// deps — the four buzz voices' mechanism, just longer: a low rolling rumble
// (detuned saws through a swept lowpass), crackle bursts of filtered noise,
// and a rising sine sweep that "strikes" right at the end, which is the
// moment HostPage auto-fires the reveal.

export const THUNDER_STING_MS = 5000;

/**
 * Play the sting on THIS device (the host screen). Returns the sting length
 * in ms (0 when audio can't start — callers fall back to the manual Reveal
 * button either way, so a muted laptop still runs the show).
 */
export function playThunderSting() {
  const c = audioContext();
  if (!c) return 0;
  if (c.state === "suspended") {
    c.resume().catch(() => {});
    if (c.state === "suspended") return 0;
  }
  try {
    const t0 = c.currentTime;
    const total = THUNDER_STING_MS / 1000;

    // Rolling rumble: two detuned saws under a closing-then-opening filter.
    const filter = c.createBiquadFilter();
    filter.type = "lowpass";
    filter.frequency.setValueAtTime(90, t0);
    filter.frequency.exponentialRampToValueAtTime(700, t0 + total - 0.4);
    const rumbleAmp = c.createGain();
    rumbleAmp.gain.setValueAtTime(0.0001, t0);
    rumbleAmp.gain.exponentialRampToValueAtTime(0.16, t0 + 0.5);
    rumbleAmp.gain.setValueAtTime(0.16, t0 + total - 0.6);
    rumbleAmp.gain.exponentialRampToValueAtTime(0.0001, t0 + total);
    filter.connect(rumbleAmp).connect(c.destination);
    for (const detune of [-7, 6]) {
      const osc = c.createOscillator();
      osc.type = "sawtooth";
      osc.frequency.setValueAtTime(38, t0);
      osc.frequency.linearRampToValueAtTime(52, t0 + total);
      osc.detune.setValueAtTime(detune, t0);
      osc.connect(filter);
      osc.start(t0);
      osc.stop(t0 + total + 0.05);
    }

    // Crackle: short noise bursts, quickening toward the strike.
    const noiseLength = 0.3;
    const buffer = c.createBuffer(1, Math.ceil(c.sampleRate * noiseLength), c.sampleRate);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < channel.length; i += 1) channel[i] = Math.random() * 2 - 1;
    [0.4, 1.3, 2.1, 2.8, 3.4, 3.9, 4.3].forEach((offset, i) => {
      const source = c.createBufferSource();
      source.buffer = buffer;
      const band = c.createBiquadFilter();
      band.type = "bandpass";
      band.frequency.setValueAtTime(900 + i * 350, t0 + offset);
      band.Q.value = 0.8;
      const amp = c.createGain();
      amp.gain.setValueAtTime(0.0001, t0 + offset);
      amp.gain.exponentialRampToValueAtTime(0.12 + i * 0.012, t0 + offset + 0.02);
      amp.gain.exponentialRampToValueAtTime(0.0001, t0 + offset + noiseLength);
      source.connect(band).connect(amp).connect(c.destination);
      source.start(t0 + offset);
      source.stop(t0 + offset + noiseLength + 0.05);
    });

    // The strike: a rising sweep that lands exactly at the sting's end.
    const sweep = c.createOscillator();
    sweep.type = "sine";
    sweep.frequency.setValueAtTime(160, t0 + total - 1.1);
    sweep.frequency.exponentialRampToValueAtTime(1240, t0 + total - 0.05);
    const sweepAmp = c.createGain();
    sweepAmp.gain.setValueAtTime(0.0001, t0 + total - 1.1);
    sweepAmp.gain.exponentialRampToValueAtTime(0.24, t0 + total - 0.15);
    sweepAmp.gain.exponentialRampToValueAtTime(0.0001, t0 + total + 0.2);
    sweep.connect(sweepAmp).connect(c.destination);
    sweep.start(t0 + total - 1.1);
    sweep.stop(t0 + total + 0.3);
    return THUNDER_STING_MS;
  } catch {
    return 0; // best-effort, never break the show
  }
}

// --- #21.1 (owner-directed): the OFFICIAL Thunder recordings ---------------
// Two fixed app assets in frontend/public/ — the owner's own parody, one
// recording for every game everywhere (deliberately app-wide, not a profile
// upload):
//   /thunder-sting.mp3  — the reveal build; plays on the HOST screen at the
//                         ⚡ splash, and its real duration times the reveal
//   /thunder-chug.mp3   — the countdown riff; plays on the TV while the
//                         clock runs (wagers cap at 30 s, so ~32 s of audio
//                         covers every game — no looping)
// NEITHER file ships in this delta (the recording is the owner's to drop
// in; see CHANGES §N). Absence is a first-class state: loadTrack resolves
// null on 404/decode failure and every caller falls back to today's
// behavior — the synthesized sting, a silent countdown. This is the ONE
// sanctioned retirement of the "no audio files" rule, for official assets
// only; per-host uploads stay out.

export const THUNDER_STING_URL = "/thunder-sting.mp3";
export const THUNDER_CHUG_URL = "/thunder-chug.mp3";

const trackCache = new Map(); // url → Promise<AudioBuffer|null> (decode once)

/**
 * Fetch + decode an official track once per page. Resolves null when the
 * file is missing, unfetchable, or undecodable — callers treat null as
 * "use the fallback", never as an error.
 */
export function loadTrack(url) {
  const c = audioContext();
  if (!c) return Promise.resolve(null);
  if (!trackCache.has(url)) {
    trackCache.set(
      url,
      fetch(url)
        .then((r) => (r.ok ? r.arrayBuffer() : Promise.reject(new Error("missing"))))
        .then((bytes) => c.decodeAudioData(bytes))
        .catch(() => null),
    );
  }
  return trackCache.get(url);
}

/**
 * Play `buffer` starting `offset` seconds in (the C-5 seek: a late board
 * lands IN the song at the right spot), optionally for `playFor` seconds
 * with a short fade at the end (the countdown's hit-zero moment). Returns
 * a stop() that fades out ~250 ms — safe to call repeatedly.
 */
export function playTrackFrom(buffer, { offset = 0, playFor = null, level = 0.9 } = {}) {
  const c = audioContext();
  if (!c || !buffer) return () => {};
  if (c.state === "suspended") {
    c.resume().catch(() => {});
    if (c.state === "suspended") return () => {};
  }
  try {
    const source = c.createBufferSource();
    source.buffer = buffer;
    const amp = c.createGain();
    const t0 = c.currentTime;
    amp.gain.setValueAtTime(level, t0);
    source.connect(amp).connect(c.destination);
    const startAt = Math.min(Math.max(0, offset), Math.max(0, buffer.duration - 0.05));
    source.start(t0, startAt);
    if (playFor != null) {
      const end = t0 + Math.max(0.1, playFor);
      amp.gain.setValueAtTime(level, Math.max(t0, end - 0.35));
      amp.gain.exponentialRampToValueAtTime(0.0001, end + 0.05);
      source.stop(end + 0.1);
    }
    let stopped = false;
    return () => {
      if (stopped) return;
      stopped = true;
      try {
        const now = c.currentTime;
        amp.gain.cancelScheduledValues(now);
        amp.gain.setValueAtTime(Math.max(amp.gain.value, 0.0001), now);
        amp.gain.exponentialRampToValueAtTime(0.0001, now + 0.25);
        source.stop(now + 0.3);
      } catch {
        /* already ended */
      }
    };
  } catch {
    return () => {};
  }
}

/**
 * The host screen's sting: the OFFICIAL recording when present (its real
 * duration times the auto-reveal), else null so the caller runs the
 * synthesized fallback. Resolves {ms, stop} | null.
 */
export async function playOfficialSting() {
  const buffer = await loadTrack(THUNDER_STING_URL);
  if (!buffer) return null;
  const stop = playTrackFrom(buffer, { offset: 0 });
  return { ms: Math.round(buffer.duration * 1000), stop };
}
