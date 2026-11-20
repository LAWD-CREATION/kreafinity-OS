// Client of the daemon's API: REST requests and the live event stream.
//
// The daemon recognizes this browser as belonging to the user (same account,
// same origin, X-Kreafinity-Client header): no secret is needed. A token is
// only used when that is not possible (it is then kept in localStorage).

const TOKEN_KEY = "kreafinity.token";

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export class Api {
  constructor() {
    this.token = localStorage.getItem(TOKEN_KEY);
  }

  setToken(token) {
    this.token = token || null;
    if (this.token) localStorage.setItem(TOKEN_KEY, this.token);
    else localStorage.removeItem(TOKEN_KEY);
  }

  async request(method, path, body) {
    const headers = { "X-Kreafinity-Client": "web" };
    if (this.token) headers.Authorization = `Bearer ${this.token}`;
    const options = { method, headers, cache: "no-store" };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch(path, options);
    } catch (error) {
      throw new ApiError(0, "network", error.message);
    }
    if (response.status === 204) return null;
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      const code = data?.error?.code ?? (response.status === 422 ? "invalid" : "unknown");
      const message = data?.error?.message ?? data?.detail?.[0]?.msg ?? response.statusText;
      throw new ApiError(response.status, code, message);
    }
    return data;
  }

  get(path) { return this.request("GET", path); }
  post(path, body) { return this.request("POST", path, body); }
  put(path, body) { return this.request("PUT", path, body); }
  patch(path, body) { return this.request("PATCH", path, body); }
  delete(path) { return this.request("DELETE", path); }
}

/** Live events over a WebSocket, reconnecting automatically. */
export class EventStream {
  /** `panel`: this page is a compact assistant panel, able to show a conversation on request. */
  constructor(api, { onEvent, onStatus, panel = false }) {
    this.api = api;
    this.onEvent = onEvent;
    this.onStatus = onStatus;
    this.panel = panel;
    this.delay = 1000;
    this.closed = false;
  }

  connect() {
    const query = this.panel ? "?panel=1" : "";
    const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/events${query}`;
    const socket = new WebSocket(url);
    this.socket = socket;
    socket.addEventListener("open", () => {
      socket.send(JSON.stringify({ type: "auth", token: this.api.token }));
    });
    socket.addEventListener("message", (message) => {
      let event;
      try {
        event = JSON.parse(message.data);
      } catch {
        return;
      }
      if (event.type === "ready") {
        this.delay = 1000;
        this.onStatus(true);
      }
      this.onEvent(event);
      if (event.type === "resync_required") socket.close();
    });
    socket.addEventListener("close", () => {
      this.onStatus(false);
      if (this.closed) return;
      setTimeout(() => this.connect(), this.delay);
      this.delay = Math.min(this.delay * 2, 10000);
    });
  }

  close() {
    this.closed = true;
    this.socket?.close();
  }
}
