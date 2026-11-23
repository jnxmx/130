import { app } from "../../scripts/app.js";

let _assetsPromise = null;

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const existing = document.querySelector(`script[src="${src}"]`);
    if (existing) {
      if (existing.dataset.loaded === "1") return resolve();
      existing.addEventListener("load", () => resolve(), { once: true });
      existing.addEventListener("error", reject, { once: true });
      return;
    }
    const el = document.createElement("script");
    el.src = src;
    el.async = false;
    el.onload = () => {
      el.dataset.loaded = "1";
      resolve();
    };
    el.onerror = reject;
    document.head.appendChild(el);
  });
}

function loadStylesheet(href) {
  if (document.querySelector(`link[href="${href}"]`)) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = href;
  document.head.appendChild(link);
}

function ensureXtermAssets() {
  if (_assetsPromise) return _assetsPromise;
  loadStylesheet("/terminal/assets/xterm.css");
  _assetsPromise = (async () => {
    if (!window.Terminal) {
      await loadScript("/terminal/assets/xterm.js");
    }
    if (!window.FitAddon) {
      await loadScript("/terminal/assets/addon-fit.js");
    }
    if (!window.WebLinksAddon) {
      await loadScript("/terminal/assets/addon-web-links.js").catch(() => {});
    }
  })();
  return _assetsPromise;
}

function injectCustomStyles() {
  if (document.getElementById("comfy-web-terminal-styles")) return;
  const style = document.createElement("style");
  style.id = "comfy-web-terminal-styles";
  style.textContent = `
    .cwt-root {
      display: flex;
      width: 100%;
      height: 100%;
      min-height: 0;
      background: #14161a;
      color: #e5e7eb;
      position: relative;
      overflow: hidden;
    }
    .cwt-host {
      flex: 1;
      min-width: 0;
      min-height: 0;
      width: 100%;
      height: 100%;
      padding: 6px 8px;
      background: #14161a;
      overflow: hidden;
    }
    .cwt-host .xterm {
      height: 100%;
    }
    .cwt-host .xterm-viewport {
      overflow-y: auto !important;
    }
  `;
  document.head.appendChild(style);
}

class EmbeddedTerminalController {
  constructor(el) {
    this.el = el;
    this.activeSession = "default";
    this.term = null;
    this.fitAddon = null;
    this.ws = null;
    this.reconnectTimer = null;
    this.pingTimer = null;
    this.resizeObserver = null;
    this.intersectionObserver = null;
    this.disposed = false;
    this._fitTimeout = null;
  }

  async mount() {
    injectCustomStyles();
    this.el.style.width = "100%";
    this.el.style.height = "100%";
    this.el.style.display = "flex";
    this.el.style.flexDirection = "column";
    this.el.style.minHeight = "0";
    this.el.innerHTML = "";

    const root = document.createElement("div");
    root.className = "cwt-root";
    root.innerHTML = `<div class="cwt-host" data-role="host"></div>`;
    this.el.appendChild(root);

    this.root = root;
    this.hostEl = root.querySelector('[data-role="host"]');

    const stopIfTerminalFocused = (ev) => {
      if ((ev.ctrlKey || ev.metaKey) && ev.key === "`") {
        return;
      }
      ev.stopPropagation();
    };
    this.hostEl.addEventListener("keydown", stopIfTerminalFocused);
    this.hostEl.addEventListener("keyup", stopIfTerminalFocused);
    this.hostEl.addEventListener("keypress", stopIfTerminalFocused);

    try {
      await ensureXtermAssets();
    } catch (err) {
      this.hostEl.textContent = "Failed to load xterm assets";
      console.error("[ComfyUI-Web-Terminal] Asset load error:", err);
      return;
    }

    if (this.disposed) return;

    this.term = new window.Terminal({
      cursorBlink: true,
      scrollback: 10000,
      fontSize: 13,
      lineHeight: 1.2,
      fontFamily: '"JetBrains Mono", "Fira Code", Menlo, Monaco, Consolas, monospace',
      theme: {
        background: "#14161a",
        foreground: "#e5e7eb",
        cursor: "#93c5fd",
        selectionBackground: "rgba(147, 197, 253, 0.28)",
        black: "#1e222a",
        red: "#f87171",
        green: "#4ade80",
        yellow: "#facc15",
        blue: "#60a5fa",
        magenta: "#c084fc",
        cyan: "#22d3ee",
        white: "#e5e7eb",
        brightBlack: "#6b7280",
        brightRed: "#fca5a5",
        brightGreen: "#86efac",
        brightYellow: "#fde047",
        brightBlue: "#93c5fd",
        brightMagenta: "#d8b4fe",
        brightCyan: "#67e8f9",
        brightWhite: "#ffffff",
      },
    });

    this.fitAddon = new window.FitAddon.FitAddon();
    this.term.loadAddon(this.fitAddon);
    if (window.WebLinksAddon?.WebLinksAddon) {
      this.term.loadAddon(new window.WebLinksAddon.WebLinksAddon());
    }

    this.term.open(this.hostEl);

    this.term.attachCustomKeyEventHandler((ev) => {
      if (ev.type === "keydown" && (ev.ctrlKey || ev.metaKey)) {
        if (ev.key === "`") {
          return false;
        }
        if ((ev.key === "c" || ev.key === "C") && (ev.shiftKey || ev.metaKey || this.term.hasSelection())) {
          const sel = this.term.getSelection();
          if (sel) navigator.clipboard.writeText(sel).catch(() => {});
          return false;
        }
        if ((ev.key === "v" || ev.key === "V") && (ev.shiftKey || ev.metaKey || ev.ctrlKey)) {
          navigator.clipboard
            .readText()
            .then((txt) => {
              if (txt) this.send({ type: "input", data: txt });
            })
            .catch(() => {});
          return false;
        }
      }
      return true;
    });

    this.term.onData((data) => {
      this.send({ type: "input", data });
    });

    this.hostEl.addEventListener("mousedown", () => {
      setTimeout(() => this.term?.focus(), 0);
    });

    this.resizeObserver = new ResizeObserver(() => this.scheduleFit());
    this.resizeObserver.observe(this.hostEl);

    if (typeof IntersectionObserver !== "undefined") {
      this.intersectionObserver = new IntersectionObserver((entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          this.scheduleFit();
          setTimeout(() => this.term?.focus(), 40);
        }
      });
      this.intersectionObserver.observe(this.hostEl);
    }

    this.scheduleFit();
    this.connect();
  }

  scheduleFit() {
    clearTimeout(this._fitTimeout);
    this._fitTimeout = setTimeout(() => {
      if (this.disposed || !this.fitAddon || !this.term || !this.hostEl) return;
      if (this.hostEl.clientWidth < 40 || this.hostEl.clientHeight < 30) return;
      try {
        this.fitAddon.fit();
        this.send({ type: "resize", cols: this.term.cols, rows: this.term.rows });
      } catch {}
    }, 30);
  }

  wsUrl() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const cols = this.term?.cols || 120;
    const rows = this.term?.rows || 30;
    return `${proto}//${location.host}/ws/terminal?session=${encodeURIComponent(this.activeSession)}&cols=${cols}&rows=${rows}`;
  }

  send(obj) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(obj));
    }
  }

  connect() {
    if (this.disposed) return;
    clearTimeout(this.reconnectTimer);
    clearInterval(this.pingTimer);
    if (this.ws) {
      try {
        this.ws.onclose = null;
        this.ws.close();
      } catch {}
      this.ws = null;
    }

    const socket = new WebSocket(this.wsUrl());
    this.ws = socket;

    socket.onopen = () => {
      if (this.ws !== socket || this.disposed) return;
      this.scheduleFit();
      this.pingTimer = setInterval(() => this.send({ type: "ping" }), 25000);
    };

    socket.onmessage = (ev) => {
      if (this.ws !== socket || this.disposed) return;
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "history") {
          this.term?.reset();
          if (msg.data) this.term?.write(msg.data);
        } else if (msg.type === "output") {
          if (msg.data) this.term?.write(msg.data);
        } else if (msg.type === "restarted") {
          this.term?.reset();
        } else if (msg.type === "exit") {
          if (msg.message) this.term?.write(msg.message);
        }
      } catch {}
    };

    socket.onclose = () => {
      if (this.ws !== socket || this.disposed) return;
      clearInterval(this.pingTimer);
      this.reconnectTimer = setTimeout(() => this.connect(), 2000);
    };
  }

  dispose() {
    this.disposed = true;
    clearTimeout(this.reconnectTimer);
    clearTimeout(this._fitTimeout);
    clearInterval(this.pingTimer);
    this.resizeObserver?.disconnect();
    this.intersectionObserver?.disconnect();
    if (this.ws) {
      try {
        this.ws.onclose = null;
        this.ws.close();
      } catch {}
      this.ws = null;
    }
    try {
      this.term?.dispose();
    } catch {}
    this.term = null;
  }
}

let activeController = null;
let mountedEl = null;

app.registerExtension({
  name: "ComfyUI.WebTerminal",
  bottomPanelTabs: [
    {
      id: "command-terminal",
      title: "Terminal",
      type: "custom",
      targetPanel: "terminal",
      render: (el) => {
        if (!el) return;
        if (mountedEl === el && activeController && !activeController.disposed) {
          activeController.scheduleFit();
          return;
        }
        if (activeController) {
          activeController.dispose();
        }
        mountedEl = el;
        activeController = new EmbeddedTerminalController(el);
        activeController.mount();
      },
      destroy: () => {
        if (activeController) {
          activeController.dispose();
          activeController = null;
        }
        mountedEl = null;
      },
    },
  ],
});
