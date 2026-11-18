// The celebration chime: made in the browser (Web Audio), no sound file. A rising bell arpeggio with a soft
// shimmer on top, ~1.6 s, quiet. Browsers allow it after the user has clicked on the page (Confirm & Initiate).
const SOUND_KEY = "crv.sound";

export function soundOn(): boolean {
  try { return localStorage.getItem(SOUND_KEY) !== "off"; } catch { return true; }
}

export function setSound(on: boolean) {
  try { localStorage.setItem(SOUND_KEY, on ? "on" : "off"); } catch { /* storage blocked: lasts for this page */ }
}

function bell(ctx: AudioContext, out: AudioNode, freq: number, at: number, length = 1.1, level = 0.22) {
  // a bell: the note plus two quieter partials, fast attack, long exponential decay
  for (const [ratio, gain] of [[1, 1], [2.01, 0.35], [3.02, 0.12]] as const) {
    const osc = ctx.createOscillator();
    const env = ctx.createGain();
    osc.type = "sine";
    osc.frequency.value = freq * ratio;
    env.gain.setValueAtTime(0.0001, at);
    env.gain.exponentialRampToValueAtTime(level * gain, at + 0.012);
    env.gain.exponentialRampToValueAtTime(0.0001, at + length);
    osc.connect(env).connect(out);
    osc.start(at);
    osc.stop(at + length + 0.05);
  }
}

export function playChime(): void {
  try {
    const Ctx = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    const master = ctx.createGain();
    master.gain.value = 0.55;
    master.connect(ctx.destination);
    const t = ctx.currentTime + 0.05;
    // C6 E6 G6 C7 -- then a little sparkle of high notes
    [1046.5, 1318.5, 1568.0, 2093.0].forEach((f, i) => bell(ctx, master, f, t + i * 0.11, 1.2));
    [2637.0, 3136.0, 3520.0].forEach((f, i) => bell(ctx, master, f, t + 0.5 + i * 0.07, 0.6, 0.07));
    window.setTimeout(() => ctx.close().catch(() => undefined), 2500);
  } catch {
    /* no audio on this device: the celebration stays silent */
  }
}

/** The review alert: two soft descending two-tone pulses (A5 -> E5), ~0.9 s, quieter than the chime. */
export function playAlert(): void {
  try {
    const Ctx = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    const master = ctx.createGain();
    master.gain.value = 0.35;
    master.connect(ctx.destination);
    const t = ctx.currentTime + 0.05;
    [[880, 0], [659.3, 0.18], [880, 0.46], [659.3, 0.64]].forEach(([freq, at]) => {
      const osc = ctx.createOscillator();
      const env = ctx.createGain();
      osc.type = "triangle";
      osc.frequency.value = freq;
      env.gain.setValueAtTime(0.0001, t + at);
      env.gain.exponentialRampToValueAtTime(0.25, t + at + 0.015);
      env.gain.exponentialRampToValueAtTime(0.0001, t + at + 0.16);
      osc.connect(env).connect(master);
      osc.start(t + at);
      osc.stop(t + at + 0.2);
    });
    window.setTimeout(() => ctx.close().catch(() => undefined), 1500);
  } catch {
    /* no audio on this device: the alert stays silent */
  }
}
