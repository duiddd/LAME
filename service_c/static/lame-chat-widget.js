/**
 * LAME Service C — Binance-Pro Styled Compact Chat Box Widget for Service A
 *
 * Palette (Binance Pro Dark Terminal):
 *   Canvas: #0B0E11 | Panel: #181A20 | Card/Input: #1E2329 | Border: #2B3139
 *   Primary Gold: #F0B90B | Bullish Green: #0ECB81 | Bearish Red: #F6465D
 *   Text: #EAECEF | Secondary: #848E9C
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
    const urlSymbol =
      global.location && global.location.search
        ? new URLSearchParams(global.location.search).get("symbol")
        : null;
    let currentSymbol =
      options.symbol || urlSymbol || container.dataset.symbol || "BTC-USDT";

    container.innerHTML = `
      <div class="lame-c-widget-root">
        <style>
          .lame-c-widget-root {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background: #181A20;
            color: #EAECEF;
            border: 1px solid #2B3139;
            border-radius: 6px;
            display: flex;
            flex-direction: column;
            height: 100%;
            min-height: 360px;
            max-height: 520px;
            overflow: hidden;
            box-sizing: border-box;
          }
          .lame-c-w-header {
            padding: 9px 12px;
            background: #1E2329;
            border-bottom: 1px solid #2B3139;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 8px;
          }
          .lame-c-w-title {
            display: flex;
            align-items: center;
            gap: 7px;
            font-size: 12.5px;
            font-weight: 700;
            color: #EAECEF;
            letter-spacing: 0.2px;
          }
          .lame-c-w-logo {
            width: 16px;
            height: 16px;
            background: #F0B90B;
            color: #0B0E11;
            font-weight: 900;
            font-size: 10px;
            border-radius: 3px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
          }
          .lame-c-w-dot {
            width: 7px;
            height: 7px;
            border-radius: 50%;
            background: #0ECB81;
            box-shadow: 0 0 6px #0ECB81;
          }
          .lame-c-w-controls {
            display: flex;
            align-items: center;
            gap: 6px;
          }
          .lame-c-w-select {
            background: #0B0E11;
            color: #F0B90B;
            border: 1px solid #2B3139;
            border-radius: 4px;
            padding: 3px 6px;
            font-size: 11px;
            font-weight: 600;
            font-family: 'JetBrains Mono', monospace;
            outline: none;
            cursor: pointer;
          }
          .lame-c-w-select:hover {
            border-color: #F0B90B;
          }
          .lame-c-w-badge {
            font-size: 10px;
            font-family: 'JetBrains Mono', monospace;
            padding: 2px 6px;
            border-radius: 3px;
            background: rgba(240, 185, 11, 0.12);
            color: #F0B90B;
            border: 1px solid rgba(240, 185, 11, 0.3);
          }
          .lame-c-w-ticker {
            padding: 6px 12px;
            background: #0B0E11;
            border-bottom: 1px solid #2B3139;
            font-size: 11px;
            color: #848E9C;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            display: flex;
            align-items: center;
            gap: 6px;
          }
          .lame-c-w-ticker strong {
            color: #F0B90B;
            font-family: 'JetBrains Mono', monospace;
            font-size: 9.5px;
            background: rgba(240, 185, 11, 0.12);
            padding: 1px 5px;
            border-radius: 2px;
          }
          .lame-c-w-messages {
            flex: 1;
            overflow-y: auto;
            padding: 10px 12px;
            display: flex;
            flex-direction: column;
            gap: 8px;
            background: #181A20;
          }
          .lame-c-w-msg {
            padding: 8px 10px;
            border-radius: 6px;
            font-size: 12px;
            line-height: 1.45;
            max-width: 93%;
            word-wrap: break-word;
          }
          .lame-c-w-msg.user {
            align-self: flex-end;
            background: #F0B90B;
            color: #0B0E11;
            font-weight: 600;
            border-bottom-right-radius: 2px;
          }
          .lame-c-w-msg.bot {
            align-self: flex-start;
            background: #1E2329;
            border: 1px solid #2B3139;
            color: #EAECEF;
            border-bottom-left-radius: 2px;
          }
          .lame-c-w-status {
            display: inline-block;
            font-size: 9px;
            font-weight: 700;
            font-family: 'JetBrains Mono', monospace;
            padding: 1px 5px;
            border-radius: 2px;
            margin-bottom: 4px;
            text-transform: uppercase;
          }
          .lame-c-w-status.ANSWERED {
            background: rgba(14, 203, 129, 0.14);
            color: #0ECB81;
            border: 1px solid rgba(14, 203, 129, 0.35);
          }
          .lame-c-w-status.INSUFFICIENT_EVIDENCE {
            background: rgba(240, 185, 11, 0.14);
            color: #F0B90B;
            border: 1px solid rgba(240, 185, 11, 0.35);
          }
          .lame-c-w-status.REJECTED_UNSAFE {
            background: rgba(246, 70, 93, 0.14);
            color: #F6465D;
            border: 1px solid rgba(246, 70, 93, 0.35);
          }
          .lame-c-w-meta {
            margin-top: 6px;
            padding-top: 5px;
            border-top: 1px solid #2B3139;
            font-size: 10px;
            color: #848E9C;
            font-family: 'JetBrains Mono', monospace;
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
          }
          .lame-c-w-cite {
            background: rgba(240, 185, 11, 0.12);
            color: #F0B90B;
            border: 1px solid rgba(240, 185, 11, 0.3);
            border-radius: 2px;
            padding: 0px 4px;
            font-size: 9.5px;
          }
          .lame-c-w-quick {
            padding: 6px 10px;
            display: flex;
            gap: 5px;
            overflow-x: auto;
            border-top: 1px solid #2B3139;
            background: #0B0E11;
          }
          .lame-c-w-chip {
            background: #1E2329;
            color: #848E9C;
            border: 1px solid #2B3139;
            border-radius: 3px;
            padding: 3px 8px;
            font-size: 10px;
            font-weight: 600;
            cursor: pointer;
            white-space: nowrap;
            transition: all 0.15s;
          }
          .lame-c-w-chip:hover {
            border-color: #F0B90B;
            color: #F0B90B;
          }
          .lame-c-w-form {
            display: flex;
            padding: 8px 10px;
            gap: 6px;
            background: #1E2329;
            border-top: 1px solid #2B3139;
          }
          .lame-c-w-input {
            flex: 1;
            background: #0B0E11;
            color: #EAECEF;
            border: 1px solid #2B3139;
            border-radius: 4px;
            padding: 6px 9px;
            font-size: 12px;
            outline: none;
          }
          .lame-c-w-input:focus {
            border-color: #F0B90B;
          }
          .lame-c-w-btn {
            background: #F0B90B;
            color: #0B0E11;
            border: none;
            border-radius: 4px;
            padding: 6px 12px;
            font-size: 12px;
            font-weight: 700;
            cursor: pointer;
          }
          .lame-c-w-btn:hover {
            background: #FCD535;
          }
          .lame-c-w-btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
          }
        </style>

        <div class="lame-c-w-header">
          <div class="lame-c-w-title">
            <span class="lame-c-w-logo">AI</span>
            <span>LAME Analyst</span>
            <span class="lame-c-w-dot" id="lame-w-dot"></span>
          </div>
          <div class="lame-c-w-controls">
            <select class="lame-c-w-select" id="lame-w-symbol">
              <option value="BTC-USDT" ${currentSymbol === "BTC-USDT" ? "selected" : ""}>BTC/USDT</option>
              <option value="ETH-USDT" ${currentSymbol === "ETH-USDT" ? "selected" : ""}>ETH/USDT</option>
            </select>
            <span class="lame-c-w-badge" id="lame-w-idx">KB v-</span>
          </div>
        </div>

        <div class="lame-c-w-ticker" id="lame-w-ticker">
          <strong>FLASH WIRE</strong>
          <span id="lame-w-headline">Syncing scripted news feed...</span>
        </div>

        <div class="lame-c-w-messages" id="lame-w-messages">
          <div class="lame-c-w-msg bot">
            <div class="lame-c-w-status ANSWERED">GROUNDED RAG READY</div>
            <div>Ask why a spot pair moved (e.g. <em>"Why did BTC-USDT drop?"</em>). Cites verified FTS5 news IDs and Service B price snapshots.</div>
          </div>
        </div>

        <div class="lame-c-w-quick">
          <button type="button" class="lame-c-w-chip" data-q="Why did BTC-USDT drop over the last 15 minutes?" data-sym="BTC-USDT">BTC Drop</button>
          <button type="button" class="lame-c-w-chip" data-q="What is driving ETH-USDT Layer-2 demand?" data-sym="ETH-USDT">ETH L2</button>
          <button type="button" class="lame-c-w-chip" data-q="Are there wash-trading volume anomalies?" data-sym="BTC-USDT">Wash Trade</button>
          <button type="button" class="lame-c-w-chip" data-q="Why did SOL-USDT crash after the eclipse?" data-sym="BTC-USDT">Refusal</button>
        </div>

        <form class="lame-c-w-form" id="lame-w-form">
          <input type="text" class="lame-c-w-input" id="lame-w-input" placeholder="Ask LAME AI about market moves..." autocomplete="off" />
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
        dotEl.style.background = "#0ECB81";
        if (items.length > 0) {
          idxBadge.textContent = `KB v${items[0].index_version}`;
          headlineEl.textContent = `[${items[0].news_id}] ${items[0].headline}`;
        }
      } catch (err) {
        dotEl.style.background = "#F6465D";
        headlineEl.textContent = `Service C offline — spot trading unaffected`;
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
        idxBadge.textContent = `KB v${data.index_version ?? "-"}`;

        const botDiv = document.createElement("div");
        botDiv.className = "lame-c-w-msg bot";
        const citesHtml = (data.citations || [])
          .map(
            (c) =>
              `<span class="lame-c-w-cite" title="${escapeHtml(c.headline)}">[${escapeHtml(c.news_id)}]</span>`
          )
          .join(" ");
        const priceInfo = data.market_context
          ? `${escapeHtml(data.market_context.symbol)}: ${Number(data.market_context.last_price).toLocaleString()} USDT (${data.market_context.data_freshness_seconds}s)`
          : "";
        const latMs = data.telemetry ? `${data.telemetry.latency_ms}ms` : "";

        botDiv.innerHTML = `
          <div class="lame-c-w-status ${escapeHtml(data.status)}">${escapeHtml(data.status)}</div>
          <div>${escapeHtml(data.answer)}</div>
          <div class="lame-c-w-meta">
            ${citesHtml ? `<span>Cite: ${citesHtml}</span>` : ""}
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
          <div>Service C unreachable at <code>${escapeHtml(apiBaseUrl)}</code>. Core trading on A, B, D continues unaffected.</div>
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
