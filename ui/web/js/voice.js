// Talking to Kreafinity: the microphone (the daemon turns speech into text)
// and answers read aloud (the daemon's Piper voice).

import { ApiError } from "./api.js";
import { t } from "./i18n.js";

/** Longest recording, in seconds (the daemon refuses more than 120). */
const MAX_SECONDS = 60;

export class Voice {
  /**
   * `button`: the microphone button. `onText(text)`: what was said, to send.
   * `onState()`: called when listening/speaking starts or stops.
   */
  constructor(api, { button, onText, toast, openSettings }) {
    this.api = api;
    this.button = button;
    this.onText = onText;
    this.toast = toast;
    this.openSettings = openSettings;
    this.info = null;
    this.recorder = null;
    this.audio = null;
    button.addEventListener("click", () => this.toggle());
  }

  async refresh() {
    try {
      this.update(await this.api.get("/api/voice"));
    } catch {
      this.update(null);
    }
  }

  update(info) {
    this.info = info;
    this.button.hidden = !info || info.input === "off" || !navigator.mediaDevices?.getUserMedia || !window.MediaRecorder;
  }

  /** Whether answers should be read aloud (`spoken`: the question was said aloud). */
  shouldSpeak(spoken) {
    if (!this.info?.speaking) return false;
    return this.info.reply === "always" || (this.info.reply === "voice" && spoken);
  }

  get listening() {
    return this.recorder?.state === "recording";
  }

  async toggle() {
    this.stopSpeaking();
    if (this.listening) return this.recorder.stop();
    if (!this.info?.listening) {
      this.toast(t(this.info?.preparing ? "voice.preparing" : "voice.not_ready"), "warning");
      if (!this.info?.preparing) this.openSettings();
      return;
    }
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      return this.toast(t("voice.no_microphone"), "error");
    }
    const type = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/webm"].find(
      (candidate) => window.MediaRecorder?.isTypeSupported?.(candidate),
    );
    const recorder = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
    const chunks = [];
    recorder.addEventListener("dataavailable", (event) => event.data.size && chunks.push(event.data));
    recorder.addEventListener("stop", () => {
      stream.getTracks().forEach((track) => track.stop());
      clearTimeout(this.limit);
      this.setState("busy");
      this.transcribe(new Blob(chunks, { type: recorder.mimeType || "audio/webm" }));
    });
    this.recorder = recorder;
    recorder.start();
    this.limit = setTimeout(() => this.listening && recorder.stop(), MAX_SECONDS * 1000);
    this.setState("listening");
  }

  async transcribe(blob) {
    try {
      const headers = { "X-Kreafinity-Client": "web", "Content-Type": blob.type };
      if (this.api.token) headers.Authorization = `Bearer ${this.api.token}`;
      const response = await fetch("/api/voice/transcribe", { method: "POST", headers, body: blob });
      const data = await response.json().catch(() => null);
      if (!response.ok) throw new ApiError(response.status, data?.error?.code ?? "unknown", data?.error?.message ?? response.statusText);
      const text = (data?.text ?? "").trim();
      if (text) this.onText(text);
      else this.toast(t("voice.nothing_heard"), "warning");
    } catch (error) {
      this.toast(t("voice.failed", { message: error.message }), "error");
    } finally {
      this.setState("idle");
    }
  }

  /** Read `text` aloud (an answer). */
  async speak(text) {
    this.stopSpeaking();
    try {
      const headers = { "X-Kreafinity-Client": "web", "Content-Type": "application/json" };
      if (this.api.token) headers.Authorization = `Bearer ${this.api.token}`;
      const response = await fetch("/api/voice/speak", { method: "POST", headers, body: JSON.stringify({ text }) });
      if (!response.ok) return; // nothing to read, or no voice: stay silent
      const url = URL.createObjectURL(await response.blob());
      this.audio = new Audio(url);
      this.audio.addEventListener("ended", () => URL.revokeObjectURL(url));
      await this.audio.play();
    } catch {
      /* the browser refused to play sound: the answer is still on screen */
    }
  }

  stopSpeaking() {
    if (this.audio) {
      this.audio.pause();
      this.audio = null;
    }
  }

  setState(state) {
    this.button.classList.toggle("listening", state === "listening");
    this.button.classList.toggle("busy", state === "busy");
    this.button.disabled = state === "busy";
    this.button.setAttribute("aria-label", t(`voice.${state === "listening" ? "stop" : "start"}`));
    this.button.title = this.button.getAttribute("aria-label");
  }
}
