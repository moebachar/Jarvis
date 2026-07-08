// Mic capture for remote/tunneled voice. Runs on the audio render thread.
//
// The AudioContext runs at the device's NATIVE rate (typically 48 kHz) — forcing it to 16 kHz
// can stall getUserMedia on real hardware — so we resample down to 16 kHz here (streaming linear
// interpolation, phase preserved across render quanta) and pack the result into Int16 blocks for
// the main thread to stream over the WebSocket. `sampleRate` is the context rate in this scope.
class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._outRate = 16000;
    this._step = this._outRate / sampleRate; // output samples produced per input sample (≈1/3 at 48k)
    this._phase = 0;
    this._prev = 0;
    this._buf = new Int16Array(1024); // ~64 ms at 16 kHz
    this._n = 0;
  }
  _emit(sample) {
    let s = sample;
    if (s > 1) s = 1; else if (s < -1) s = -1;
    this._buf[this._n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
    if (this._n === this._buf.length) {
      const out = this._buf.slice(0); // copy; transfer its buffer (zero-copy to main thread)
      this.port.postMessage(out.buffer, [out.buffer]);
      this._n = 0;
    }
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true; // keep the node alive between mic frames
    for (let i = 0; i < ch.length; i++) {
      const cur = ch[i];
      this._phase += this._step;
      while (this._phase >= 1) {
        this._phase -= 1;
        // Residual _phase is in OUTPUT-sample units; divide by _step to get the fraction
        // between _prev and cur in input-sample units. (At an integer ratio like 48k→16k,
        // _phase is 0 here → t=1 → exact decimation.)
        const t = 1 - this._phase / this._step;
        this._emit(this._prev + (cur - this._prev) * t);
      }
      this._prev = cur;
    }
    return true; // MUST return true or the node is garbage-collected
  }
}
registerProcessor("capture", CaptureProcessor);
