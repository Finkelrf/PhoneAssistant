// Microphone -> 16 kHz 16-bit mono, in 80 ms blocks (1280 samples), for the server's wake word listener.
class Pcm16k extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.step = (options.processorOptions.rate || sampleRate) / 16000;   // input samples per output sample
    this.pos = 0;                 // where the next output sample falls, in input samples from the block start
    // low-pass before dropping samples (about 6 kHz), so higher sounds don't fold into the speech band
    this.alpha = this.step > 1 ? 1 - Math.exp(-2 * Math.PI * 6000 / (16000 * this.step)) : 1;
    this.y = 0;
    this.buf = new Float32Array(128);
    this.out = new Int16Array(1280);
    this.n = 0;
  }
  process(inputs) {
    const raw = inputs[0] && inputs[0][0];
    if (!raw) return true;
    if (this.buf.length !== raw.length) this.buf = new Float32Array(raw.length);
    const ch = this.buf;
    for (let k = 0; k < raw.length; k++) { this.y += this.alpha * (raw[k] - this.y); ch[k] = this.y; }
    while (this.pos < ch.length) {
      const i = Math.floor(this.pos), f = this.pos - i;
      const a = ch[i], b = i + 1 < ch.length ? ch[i + 1] : ch[i];
      const v = Math.max(-1, Math.min(1, a + (b - a) * f));
      this.out[this.n++] = v < 0 ? v * 0x8000 : v * 0x7fff;
      if (this.n === this.out.length) {
        this.port.postMessage(this.out.buffer, [this.out.buffer]);
        this.out = new Int16Array(1280);
        this.n = 0;
      }
      this.pos += this.step;
    }
    this.pos -= ch.length;
    return true;
  }
}
registerProcessor("pcm16k", Pcm16k);
