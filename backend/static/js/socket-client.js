/* Same-origin Engine.IO v4 / Socket.IO event subset used by NexAlert.
 * WebSocket only; persistence and acknowledgements remain HTTP/API-backed.
 */
(function (global) {
  "use strict";
  function NexSocket(options) {
    this.handlers = new Map();
    this.connected = false;
    this.stopped = false;
    this.retry = 250;
    this.options = options || {};
    this.queue = [];
    this.connect();
  }
  NexSocket.prototype.on = function (event, callback) {
    if (!this.handlers.has(event)) this.handlers.set(event, []);
    this.handlers.get(event).push(callback);
    return this;
  };
  NexSocket.prototype.fire = function (event, data) {
    (this.handlers.get(event) || []).forEach(callback => {
      try { callback(data); } catch (error) { setTimeout(() => { throw error; }); }
    });
  };
  NexSocket.prototype.connect = function () {
    if (this.stopped) return;
    const scheme = global.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${scheme}//${global.location.host}/socket.io/?EIO=4&transport=websocket`;
    try { this.ws = new WebSocket(url); }
    catch (error) { this.fire("connect_error", error); this.schedule(); return; }
    this.ws.onmessage = event => this.receive(String(event.data));
    this.ws.onerror = () => this.fire("connect_error", new Error("Live socket unavailable"));
    this.ws.onclose = () => {
      const wasConnected = this.connected;
      this.connected = false;
      if (wasConnected) this.fire("disconnect", "transport close");
      this.schedule();
    };
  };
  NexSocket.prototype.receive = function (packet) {
    packet.split("\x1e").forEach(message => {
      if (!message) return;
      const type = message[0];
      if (type === "0") { // Engine.IO open
        this.sendRaw("40");
      } else if (type === "2") { // Engine.IO ping
        this.sendRaw("3" + message.slice(1));
      } else if (message.startsWith("40")) {
        this.connected = true;
        this.retry = 250;
        this.fire("connect");
        while (this.queue.length) this.sendRaw(this.queue.shift());
      } else if (message.startsWith("41")) {
        this.connected = false;
        this.fire("disconnect", "server disconnect");
      } else if (message.startsWith("42")) {
        try {
          const decoded = JSON.parse(message.slice(2));
          if (Array.isArray(decoded) && typeof decoded[0] === "string") this.fire(decoded[0], decoded[1]);
        } catch (_) { /* Ignore malformed/unrelated event frames. */ }
      } else if (message.startsWith("44")) {
        this.fire("connect_error", new Error(message.slice(2) || "Socket connection rejected"));
      }
    });
  };
  NexSocket.prototype.sendRaw = function (packet) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(packet);
  };
  NexSocket.prototype.emit = function (event, data) {
    const packet = "42" + JSON.stringify([event, data]);
    if (this.connected) this.sendRaw(packet); else this.queue.push(packet);
    return this;
  };
  NexSocket.prototype.disconnect = function () {
    this.stopped = true;
    if (this.ws) this.ws.close();
  };
  NexSocket.prototype.schedule = function () {
    if (this.stopped || this.timer) return;
    const delay = this.retry;
    this.retry = Math.min(10000, this.retry * 2);
    this.timer = setTimeout(() => { this.timer = null; this.connect(); }, delay);
  };
  global.io = function (options) { return new NexSocket(options); };
})(window);
