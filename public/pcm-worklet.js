// Captures microphone audio and downsamples it to 16kHz PCM16 for the Mac's
// Whisper — CORRECTLY, from whatever rate the browser actually runs at
// (usually 48000, sometimes 44100). Never assume the context honored a
// requested rate: feeding 48k audio labelled as 16k gives Whisper sped-up
// gibberish, which reads as "it doesn't understand me at all".
class PCMWorklet extends AudioWorkletProcessor {
  constructor() {
    super();
    // `sampleRate` is the AudioWorklet global: the REAL context rate.
    this.ratio = sampleRate / 16000;
    this.pos = 0; // fractional read position across block boundaries
    this.tail = new Float32Array(0);
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch || !ch.length) return true;

    // stitch leftover samples from the previous block onto this one
    const input = new Float32Array(this.tail.length + ch.length);
    input.set(this.tail, 0);
    input.set(ch, this.tail.length);

    // linear-interpolation resample to 16k (stateful across blocks)
    const out = [];
    let pos = this.pos;
    while (pos + 1 < input.length) {
      const i = Math.floor(pos);
      const frac = pos - i;
      out.push(input[i] * (1 - frac) + input[i + 1] * frac);
      pos += this.ratio;
    }
    const keepFrom = Math.min(Math.floor(pos), input.length);
    this.tail = input.slice(keepFrom);
    this.pos = pos - keepFrom;

    if (out.length) {
      const pcm = new Int16Array(out.length);
      for (let i = 0; i < out.length; i++) {
        const s = Math.max(-1, Math.min(1, out[i]));
        pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      this.port.postMessage(pcm.buffer, [pcm.buffer]);
    }
    return true;
  }
}
registerProcessor("pcm-worklet", PCMWorklet);
