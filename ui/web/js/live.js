// Live conversation: talk freely, Kreafinity answers aloud, sentence by sentence.
//
// The browser listens continuously (with echo cancellation) and detects
// speech by its loudness: when the user stops talking, the sentence is
// transcribed on the computer (Whisper, through the daemon) and sent. The
// answer is read aloud as it is written (Piper), one sentence at a time.
// Speaking while Kreafinity talks interrupts it (and stops the request).

import { el, icon } from "./dom.js";
import { t } from "./i18n.js";

const SAMPLE_RATE = 16000; // sent to the speech recognizer
const PRE_ROLL = 0.4; // seconds kept before speech is detected
const SPEECH_START = 0.18; // seconds of loud sound to start a sentence
const SILENCE_END = 0.9; // seconds of quiet to end it
const BARGE_IN = 0.35; // seconds of speech to interrupt the answer
const MAX_SENTENCE = 30; // seconds
const MIN_LEVEL = 0.012; // RMS below which it is always quiet

/** A float32 mono signal at `rate` -> 16-bit PCM WAV at 16 kHz. */
export function encodeWav(samples, rate) {
  const ratio = rate / SAMPLE_RATE;
  const length = Math.floor(samples.length / ratio);
  const buffer = new ArrayBuffer(44 + length * 2);
  const view = new DataView(buffer);
  const text = (offset, value) => [...value].forEach((c, i) => view.setUint8(offset + i, c.charCodeAt(0)));
  text(0, "RIFF");
  view.setUint32(4, 36 + length * 2, true);
  text(8, "WAVEfmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, SAMPLE_RATE, true);
  view.setUint32(28, SAMPLE_RATE * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  text(36, "data");
  view.setUint32(40, length * 2, true);
  for (let i = 0; i < length; i++) {
    // Average the samples of each output sample (a simple low-pass).
    const start = Math.floor(i * ratio);
    const end = Math.max(start + 1, Math.floor((i + 1) * ratio));
    let sum = 0;
    for (let j = start; j < end; j++) sum += samples[j] ?? 0;
    const value = Math.max(-1, Math.min(1, sum / (end - start)));
    view.setInt16(44 + i * 2, value < 0 ? value * 0x8000 : value * 0x7fff, true);
  }
  return new Blob([buffer], { type: "audio/wav" });
}

/** Text to read aloud: no markdown, no code. */
export function speakable(text) {
  return text
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/^\s*[-*+]\s+/gm, "")
    .replace(/[*_~>|]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

/** Cuts streamed text into sentences to read aloud as soon as they are complete. */
export class SentenceSplitter {
  constructor() {
    this.buffer = "";
    this.inCode = false;
  }

  /** New text; returns the sentences now complete. */
  push(text) {
    this.buffer += text;
    const sentences = [];
    for (;;) {
      if (this.inCode) {
        const end = this.buffer.indexOf("```");
        if (end < 0) return sentences;
        this.buffer = this.buffer.slice(end + 3);
        this.inCode = false;
        continue;
      }
      const fence = this.buffer.indexOf("```");
      const match = /[.!?…:;](?=\s)|\n/.exec(fence >= 0 ? this.buffer.slice(0, fence) : this.buffer);
      if (match) {
        const sentence = speakable(this.buffer.slice(0, match.index + 1));
        this.buffer = this.buffer.slice(match.index + 1);
        if (sentence.length > 1) sentences.push(sentence);
        continue;
      }
      if (fence >= 0) {
        const before = speakable(this.buffer.slice(0, fence));
        if (before.length > 1) sentences.push(before);
        this.buffer = this.buffer.slice(fence + 3);
        this.inCode = true;
        continue;
      }
      return sentences;
    }
  }

  /** What is left at the end of the answer. */
  flush() {
    const rest = this.inCode ? "" : speakable(this.buffer);
    this.buffer = "";
    this.inCode = false;
    return rest.length > 1 ? [rest] : [];
  }
}

export class LiveConversation {
  /**
   * `send(text)`: send a spoken message (resolves when it was accepted).
   * `cancel()`: stop the running request. `running()`: whether one runs.
   */
  constructor(api, { panel, toast, send, cancel, running }) {
    this.api = api;
    this.panel = panel;
    this.toast = toast;
    this.send = send;
    this.cancel = cancel;
    this.running = running;
    this.active = false;
    this.state = "idle";
  }

  async start() {
    if (this.active) return;
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
      });
    } catch {
      this.toast(t("voice.no_microphone"), "error");
      return;
    }
    this.active = true;
    this.stream = stream;
    this.context = new AudioContext();
    await this.context.resume().catch(() => {});
    const source = this.context.createMediaStreamSource(stream);
    // ScriptProcessor: deprecated but available everywhere (Chromium, Qt WebEngine, WebKitGTK).
    this.processor = this.context.createScriptProcessor(2048, 1, 1);
    this.processor.onaudioprocess = (event) => this.onAudio(event.inputBuffer.getChannelData(0));
    source.connect(this.processor);
    this.processor.connect(this.context.destination); // needed for the processor to run (it outputs silence)
    this.rate = this.context.sampleRate;
    this.frameSeconds = 2048 / this.rate;
    this.preRoll = [];
    this.recording = null;
    this.loud = 0;
    this.quiet = 0;
    this.noise = MIN_LEVEL;
    this.muted = false;
    this.queue = [];
    this.splitter = new SentenceSplitter();
    this.render();
    this.setState("listening");
    this.warmUp();
  }

  /** Load the speech recognizer now (a short silence), not at the first sentence. */
  warmUp() {
    const headers = { "X-Kreafinity-Client": "web", "Content-Type": "audio/wav" };
    if (this.api.token) headers.Authorization = `Bearer ${this.api.token}`;
    const silence = encodeWav(new Float32Array(Math.round(this.rate * 0.3)), this.rate);
    fetch("/api/voice/transcribe", { method: "POST", headers, body: silence }).catch(() => {});
  }

  stop() {
    if (!this.active) return;
    this.active = false;
    this.stopSpeaking();
    this.processor?.disconnect();
    this.stream?.getTracks().forEach((track) => track.stop());
    this.context?.close().catch(() => {});
    this.panel.hidden = true;
    this.panel.replaceChildren();
    this.state = "idle";
  }

  toggleMute() {
    this.muted = !this.muted;
    this.recording = null;
    this.render();
    this.setState(this.state);
  }

  render() {
    this.orb = el("div", { class: "live-orb" }, el("span", { class: "logo-mark" }));
    this.status = el("p", { class: "live-status", role: "status" });
    this.panel.replaceChildren(
      this.orb,
      this.status,
      el("div", { class: "live-actions" },
        el("button", {
          class: `button small ${this.muted ? "primary" : "ghost"}`, type: "button", onclick: () => this.toggleMute(),
        }, icon("mic"), el("span", {}, t(this.muted ? "live.unmute" : "live.mute"))),
        el("button", { class: "button small danger-ghost", type: "button", onclick: () => this.stop() },
          icon("close"), el("span", {}, t("live.end")))),
    );
    this.panel.hidden = false;
  }

  setState(state) {
    this.state = state;
    this.panel.dataset.state = state;
    if (this.status) this.status.textContent = t(this.muted ? "live.muted" : `live.${state}`);
  }

  // --- Listening ------------------------------------------------------------------------

  onAudio(input) {
    if (!this.active) return;
    const frame = new Float32Array(input);
    let sum = 0;
    for (const value of frame) sum += value * value;
    const level = Math.sqrt(sum / frame.length);
    this.orb?.style.setProperty("--level", Math.min(1, level * 12).toFixed(3));
    if (this.muted || this.state === "transcribing") return;
    const speakingNow = this.state === "speaking";
    // The voice of Kreafinity may leak into the microphone: be stricter then.
    const threshold = Math.max(MIN_LEVEL, this.noise * 3) * (speakingNow ? 2.5 : 1);
    const loud = level > threshold;
    if (!loud && !this.recording) this.noise = this.noise * 0.95 + level * 0.05; // follow the room
    this.loud = loud ? this.loud + this.frameSeconds : 0;
    this.quiet = loud ? 0 : this.quiet + this.frameSeconds;

    if (this.recording) {
      this.recording.push(frame);
      const length = this.recording.length * this.frameSeconds;
      if (this.quiet >= SILENCE_END || length >= MAX_SENTENCE) this.endSentence();
      return;
    }
    this.preRoll.push(frame);
    while (this.preRoll.length * this.frameSeconds > PRE_ROLL) this.preRoll.shift();
    const needed = this.state === "speaking" || this.state === "thinking" ? BARGE_IN : SPEECH_START;
    if (this.loud >= needed) {
      if (this.state !== "listening") this.interrupt();
      this.recording = [...this.preRoll];
      this.preRoll = [];
      this.setState("hearing");
    }
  }

  /** The user talks over the answer: stop speaking and stop the request. */
  interrupt() {
    this.stopSpeaking();
    this.splitter = new SentenceSplitter();
    if (this.running()) this.cancel();
  }

  async endSentence() {
    const frames = this.recording;
    this.recording = null;
    this.quiet = 0;
    const samples = new Float32Array(frames.reduce((n, f) => n + f.length, 0));
    let offset = 0;
    for (const frame of frames) {
      samples.set(frame, offset);
      offset += frame.length;
    }
    this.setState("transcribing");
    let text = "";
    try {
      const headers = { "X-Kreafinity-Client": "web", "Content-Type": "audio/wav" };
      if (this.api.token) headers.Authorization = `Bearer ${this.api.token}`;
      const response = await fetch("/api/voice/transcribe", { method: "POST", headers, body: encodeWav(samples, this.rate) });
      const data = await response.json().catch(() => null);
      if (!response.ok) throw new Error(data?.error?.message ?? response.statusText);
      text = (data?.text ?? "").trim();
    } catch (error) {
      this.toast(t("voice.failed", { message: error.message }), "error");
    }
    if (!this.active) return;
    if (!text) return this.setState("listening");
    this.setState("thinking");
    try {
      await this.send(text);
    } catch {
      this.setState("listening");
    }
  }

  // --- Speaking -------------------------------------------------------------------------

  /** Text of the answer, as it streams. */
  onText(text) {
    if (!this.active || this.state === "hearing") return;
    for (const sentence of this.splitter.push(text)) this.say(sentence);
  }

  /** The answer is complete (or the request stopped). */
  onAnswerEnd() {
    if (!this.active) return;
    for (const sentence of this.splitter.flush()) this.say(sentence);
    this.answerDone = true;
    if (!this.queue.length && !this.audio && this.state !== "hearing" && this.state !== "transcribing") {
      this.setState("listening");
    }
  }

  say(sentence) {
    this.answerDone = false;
    // Ask for the voice at once (in parallel), play in order.
    const audio = this.fetchVoice(sentence);
    this.queue.push(audio);
    if (!this.audio) this.playNext();
  }

  async fetchVoice(text) {
    const headers = { "X-Kreafinity-Client": "web", "Content-Type": "application/json" };
    if (this.api.token) headers.Authorization = `Bearer ${this.api.token}`;
    try {
      const response = await fetch("/api/voice/speak", { method: "POST", headers, body: JSON.stringify({ text }) });
      return response.ok ? URL.createObjectURL(await response.blob()) : null;
    } catch {
      return null;
    }
  }

  async playNext() {
    const next = this.queue.shift();
    if (!next) {
      this.audio = null;
      if (this.answerDone && this.state === "speaking") this.setState("listening");
      return;
    }
    const url = await next;
    if (!this.active || !url) return this.playNext();
    const audio = new Audio(url);
    this.audio = audio;
    audio.addEventListener("ended", () => {
      URL.revokeObjectURL(url);
      if (this.audio === audio) this.playNext();
    });
    if (this.state !== "hearing") this.setState("speaking");
    try {
      await audio.play();
    } catch {
      this.audio = null;
      this.playNext();
    }
  }

  stopSpeaking() {
    this.queue = [];
    if (this.audio) {
      this.audio.pause();
      this.audio = null;
    }
  }
}
