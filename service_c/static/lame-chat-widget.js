/**
 * LAME Service C — Embeddable Compact Chat Box Widget for Service A's Frontend
 *
 * Usage in Service A's HTML frontend:
 *   <div id="lame-ai-chatbox" data-api-url="http://localhost:8080" data-symbol="BTC-USDT"></div>
 *   <script src="http://localhost:8080/static/lame-chat-widget.js"></script>
 *
 * Or programmatically:
 *   LameAnalystWidget.mount("#my-panel", { apiBaseUrl: "http://localhost:8080", symbol: "BTC-USDT" });
 */
(function (global) {
  "use strict";

  const DEFAULT_API_BASE =
    global.location && global.location.protocol.startsWith("http")
      ? global.location.origin
      : "http://localhost:8080";

  function escapeHtml(str) {
    return String(str || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function mount(target, options = {}) {
    const container =
      typeof target === "string" ? document.querySelector(target) : target;
    if (!container) {
      console.warn("[LameAnalystWidget] Target container not found:", target);
      return null;
    }

    const apiBaseUrl = (
      options.apiBaseUrl ||
      container.dataset.apiUrl ||
      DEFAULT_API_BASE
    ).replace(/\/+$/, "");
    let currentSymbol =
      options.symbol || container.dataset.symbol || "BTC-USDT";

    container.innerHTML = `
      <div class="lame-c-widget-root">
        <style>
          .lame-c-widget-root {
            font-family: 'Google Sans', 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background: #0d131f;
            color: #e2e8f0;
            border: 1px solid #1e293b;
            border-radius: 10px;
            display: flex;
            flex-direction: column;
            height: 100%;
            min-height: 360px;
            max-height: 520px;
            overflow: hidden;
            box-sizing: border-box;
            box-shadow: 0 8px 24px rgba(0,0,0,0.35);
          }
          .lame-c-w-header {
            padding: 10px 12px;
            background: #111827;
            border-bottom: 1px solid #1e293b;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 8px;
          }
          .lame-c-w-title {
            display: flex;
            align-items: center;
            gap: 7px;
            font-size: 13px;
            font-weight: 700;
            color: #f8fafc;
          }
          .lame-c-w-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: #10b981;
            box-shadow: 0 0 8px #10b981;
          }
          .lame-c-w-controls {
            display: flex;
            align-items: center;
            gap: 6px;
          }
          .lame-c-w-select {
            background: #0b0f19;
            color: #93c5fd;
            border: 1px solid #334155;
            border-radius: 5px;
            padding: 3px 6px;
            font-size: 11px;
            font-weight: 600;
            outline: none;
          }
          .lame-c-w-badge {
            font-size: 10px;
            font-family: 'JetBrains Mono', monospace;
            padding: 2px 6px;
            border-radius: 4px;
            background: rgba(59, 130, 246, 0.15);
            color: #60a5fa;
            border: 1px solid rgba(59, 130, 246, 0.3);
          }
          .lame-c-w-ticker {
            padding: 6px 12px;
            background: rgba(15, 23, 42, 0.75);
            border-bottom: 1px solid #1e293b;
            font-size: 11px;
            color: #94a3b8;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            display: flex;
            align-items: center;
            gap: 6px;
          }
          .lame-c-w-ticker strong {
            color: #38bdf8;
            font-family: 'JetBrains Mono', monospace;
            font-size: 10px;
          }
          .lame-c-w-messages {
            flex: 1;
            overflow-y: auto;
            padding: 10px 12px;
            display: flex;
            flex-direction: column;
            gap: 8px;
          }
          .lame-c-w-msg {
            padding: 8px 10px;
            border-radius: 8px;
            font-size: 12px;
            line-height: 1.45;
            max-width: 92%;
            word-wrap: break-word;
          }
          .lame-c-w-msg.user {
            align-self: flex-end;
            background: #2563eb;
            color: #ffffff;
            border-bottom-right-radius: 2px;
          }
          .lame-c-w-msg.bot {
            align-self: flex-start;
            background: #161f30;
            border: 1px solid #26334d;
            color: #e2e8f0;
            border-bottom-left-radius: 2px;
          }
          .lame-c-w-status {
            display: inline-block;
            font-size: 9px;
            font-weight: 700;
            font-family: 'JetBrains Mono', monospace;
            padding: 1px 5px;
            border-radius: 3px;
            margin-bottom: 4px;
            text-transform: uppercase;
          }
          .lame-c-w-status.ANSWERED {
            background: rgba(16, 185, 129, 0.16);
            color: #34d399;
            border: 1px solid rgba(16, 185, 129, 0.35);
          }
          .lame-c-w-status.INSUFFICIENT_EVIDENCE {
            background: rgba(245, 158, 11, 0.16);
            color: #fbbf24;
            border: 1px solid rgba(245, 158, 11, 0.35);
          }
          .lame-c-w-status.REJECTED_UNSAFE {
            background: rgba(239, 68, 68, 0.16);
            color: #f87171;
            border: 1px solid rgba(239, 68, 68, 0.35);
          }
          .lame-c-w-meta {
            margin-top: 6px;
            padding-top: 5px;
            border-top: 1px dashed #26334d;
            font-size: 10px;
            color: #94a3b8;
            font-family: 'JetBrains Mono', monospace;
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
          }
          .lame-c-w-cite {
            background: rgba(56, 189, 248, 0.12);
            color: #38bdf8;
            border: 1px solid rgba(56, 189, 248, 0.28);
            border-radius: 3px;
            padding: 1px 4px;
            font-size: 10px;
          }
          .lame-c-w-quick {
            padding: 5px 10px;
            display: flex;
            gap: 5px;
            overflow-x: auto;
            border-top: 1px solid #1e293b;
            background: #0b0f19;
          }
          .lame-c-w-chip {
            background: #161f30;
            color: #cbd5e1;
            border: 1px solid #26334d;
            border-radius: 999px;
            padding: 3px 8px;
            font-size: 10px;
            cursor: pointer;
            white-space: nowrap;
          }
          .lame-c-w-chip:hover {
            border-color: #3b82f6;
            color: #ffffff;
          }
          .lame-c-w-form {
            display: flex;
            padding: 8px 10px;
            gap: 6px;
            background: #111827;
            border-top: 1px solid #1e293b;
          }
          .lame-c-w-input {
            flex: 1;
            background: #090d16;
            color: #f8fafc;
            border: 1px solid #334155;
            border-radius: 6px;
            padding: 6px 9px;
            font-size: 12px;
            outline: none;
          }
          .lame-c-w-input:focus {
            border-color: #3b82f6;
          }
          .lame-c-w-btn {
            background: #2563eb;
            color: #ffffff;
            border: none;
            border-radius: 6px;
            padding: 6px 12px;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
          }
          .lame-c-w-btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
          }
        </style>

        <div class="lame-c-w-header">
          <div class="lame-c-w-title">
            <span class="lame-c-w-dot" id="lame-w-dot"></span>
            <span>AI Market Analyst (Service C)</span>
          </div>
          <div class="lame-c-w-controls">
            <select class="lame-c-w-select" id="lame-w-symbol">
              <option value="BTC-USDT" ${currentSymbol === "BTC-USDT" ? "selected" : ""}>BTC-USDT</option>
              <option value="ETH-USDT" ${currentSymbol === "ETH-USDT" ? "selected" : ""}>ETH-USDT</option>
            </select>
            <span class="lame-c-w-badge" id="lame-w-idx">idx v-</span>
          </div>
        </div>

        <div class="lame-c-w-ticker" id="lame-w-ticker">
          <strong>LATEST NEWS</strong>
          <span id="lame-w-headline">Connecting to Service C news stream...</span>
        </div>

        <div class="lame-c-w-messages" id="lame-w-messages">
          <div class="lame-c-w-msg bot">
            <div class="lame-c-w-status ANSWERED">GROUNDED RAG READY</div>
            <div>Ask why a pair moved (e.g. <em>"Why did BTC-USDT drop?"</em>). Every claim cites verified news IDs and Service B snapshot freshness.</div>
          </div>
        </div>

        <div class="lame-c-w-quick">
          <button type="button" class="lame-c-w-chip" data-q="Why did BTC-USDT drop over the last 15 minutes?" data-sym="BTC-USDT">Why did BTC drop?</button>
          <button type="button" class="lame-c-w-chip" data-q="What is driving ETH-USDT Layer-2 demand?" data-sym="ETH-USDT">ETH L2 move</button>
          <button type="button" class="lame-c-w-chip" data-q="Are there wash-trading volume anomalies?" data-sym="BTC-USDT">Wash-trade alert</button>
          <button type="button" class="lame-c-w-chip" data-q="Why did SOL-USDT crash after the eclipse?" data-sym="BTC-USDT">Test Refusal</button>
        </div>

        <form class="lame-c-w-form" id="lame-w-form">
          <input type="text" class="lame-c-w-input" id="lame-w-input" placeholder="Ask Service C about market moves..." autocomplete="off" />
          <button type="submit" class="lame-c-w-btn" id="lame-w-send">Ask</button>
        </form>
      </div>
    `;

    const msgList = container.querySelector("#lame-w-messages");
    const form = container.querySelector("#lame-w-form");
    const input = container.querySelector("#lame-w-input");
    const sendBtn = container.querySelector("#lame-w-send");
    const symSelect = container.querySelector("#lame-w-symbol");
    const idxBadge = container.querySelector("#lame-w-idx");
    const headlineEl = container.querySelector("#lame-w-headline");
    const dotEl = container.querySelector("#lame-w-dot");

    symSelect.addEventListener("change", () => {
      currentSymbol = symSelect.value;
      refreshNewsTicker();
    });

    container.querySelectorAll(".lame-c-w-chip").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (btn.dataset.sym) {
          currentSymbol = btn.dataset.sym;
          symSelect.value = currentSymbol;
        }
        askQuestion(btn.dataset.q);
      });
    });

    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const q = input.value.trim();
      if (!q) return;
      input.value = "";
      askQuestion(q);
    });

    async function refreshNewsTicker() {
      try {
        const res = await fetch(
          `${apiBaseUrl}/api/news?symbol=${encodeURIComponent(currentSymbol)}&limit=3`
        );
        if (!res.ok) throw new Error("HTTP " + res.status);
        const items = await res.json();
        dotEl.style.background = "#10b981";
        if (items.length > 0) {
          idxBadge.textContent = `idx v${items[0].index_version}`;
          headlineEl.textContent = `[${items[0].news_id}] ${items[0].headline}`;
        }
      } catch (err) {
        dotEl.style.background = "#f59e0b";
        headlineEl.textContent = `Service C API offline (${apiBaseUrl}) — trading unaffected`;
      }
    }

    async function askQuestion(queryText) {
      const userDiv = document.createElement("div");
      userDiv.className = "lame-c-w-msg user";
      userDiv.textContent = queryText;
      msgList.appendChild(userDiv);
      msgList.scrollTop = msgList.scrollHeight;

      sendBtn.disabled = true;
      try {
        const res = await fetch(`${apiBaseUrl}/api/chat`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query: queryText, symbol: currentSymbol }),
        });
        const data = await res.json();
        idxBadge.textContent = `idx v${data.index_version ?? "-"}`;

        const botDiv = document.createElement("div");
        botDiv.className = "lame-c-w-msg bot";
        const citesHtml = (data.citations || [])
          .map(
            (c) =>
              `<span class="lame-c-w-cite" title="${escapeHtml(c.headline)}">[${escapeHtml(c.news_id)}]</span>`
          )
          .join(" ");
        const priceInfo = data.market_context
          ? `${escapeHtml(data.market_context.symbol)}: ${data.market_context.last_price} USDT (age: ${data.market_context.data_freshness_seconds}s)`
          : "";
        const latMs = data.telemetry ? `${data.telemetry.latency_ms}ms` : "";

        botDiv.innerHTML = `
          <div class="lame-c-w-status ${escapeHtml(data.status)}">${escapeHtml(data.status)}</div>
          <div>${escapeHtml(data.answer)}</div>
          <div class="lame-c-w-meta">
            ${citesHtml ? `<span>Sources: ${citesHtml}</span>` : ""}
            ${priceInfo ? `<span>${priceInfo}</span>` : ""}
            ${latMs ? `<span>${latMs}</span>` : ""}
          </div>
        `;
        msgList.appendChild(botDiv);
        msgList.scrollTop = msgList.scrollHeight;
      } catch (err) {
        const errDiv = document.createElement("div");
        errDiv.className = "lame-c-w-msg bot";
        errDiv.innerHTML = `
          <div class="lame-c-w-status REJECTED_UNSAFE">SERVICE C OFFLINE</div>
          <div>Could not reach Service C at <code>${escapeHtml(apiBaseUrl)}</code>. Core trading on Services A, B, and D continues unaffected.</div>
        `;
        msgList.appendChild(errDiv);
        msgList.scrollTop = msgList.scrollHeight;
      } finally {
        sendBtn.disabled = false;
      }
    }

    refreshNewsTicker();
    return { askQuestion, refreshNewsTicker };
  }

  // Auto-mount if #lame-ai-chatbox exists on DOMContentLoaded
  if (typeof document !== "undefined") {
    const autoInit = () => {
      const el = document.getElementById("lame-ai-chatbox");
      if (el && !el.dataset.mounted) {
        el.dataset.mounted = "true";
        mount(el);
      }
    };
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", autoInit);
    } else {
      autoInit();
    }
  }

  global.LameAnalystWidget = { mount };
})(typeof window !== "undefined" ? window : this);
