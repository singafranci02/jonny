// Captures microphone audio as 16-bit PCM and hands it to the main thread in
// small frames. The AudioContext is created at 16kHz, so frames are already
// at the rate Whisper wants — no resampling here.
class PCMWorklet extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
    if (input && input[0]) {
      const float = input[0]; // mono, Float32, [-1, 1]
      const pcm = new Int16Array(float.length);
      for (let i = 0; i < float.length; i++) {
        const s = Math.max(-1, Math.min(1, float[i]));
        pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      this.port.postMessage(pcm.buffer, [pcm.buffer]);
    }
    return true;
  }
}
registerProcessor("pcm-worklet", PCMWorklet);
