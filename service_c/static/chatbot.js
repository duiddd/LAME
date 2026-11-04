/**
 * Controller for LAME Service C Full-Page AI Market Analyst & News Terminal
 */
(function () {
  "use strict";

  const API_BASE =
    window.location.protocol.startsWith("http")
      ? window.location.origin
      : "http://localhost:8080";

  const newsListEl = document.getElementById("news-list");
  const dlqListEl = document.getElementById("dlq-list");
  const chatStreamEl = document.getElementById("chat-stream");
  const chatForm = document.getElementById("chat-form");
  const chatInput = document.getElementById("chat-input");
  const chatSymbol = document.getElementById("chat-symbol");
  const chatSubmit = document.getElementById("chat-submit");

  function esc(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function refreshAll() {
    await Promise.all([
      loadMarket(),
      loadNews(),
      loadDLQ(),
      loadMetrics(),
    ]);
  }

  async function loadMarket() {
    try {
      const res = await fetch(`${API_BASE}/api/market`);
      if (!res.ok) return;
      const data = await res.json();
      const btc = data.symbols && data.symbols["BTC-USDT"];
      const eth = data.symbols && data.symbols["ETH-USDT"];
      if (btc) {
        document.getElementById("btc-price").textContent = Number(btc.last_price).toLocaleString();
        document.getElementById("btc-fresh").textContent = `${btc.data_freshness_seconds}s`;
        document.getElementById("btc-bbo").textContent = `Bid ${Number(btc.best_bid).toLocaleString()} · Ask ${Number(btc.best_ask).toLocaleString()}`;
      }
      if (eth) {
        document.getElementById("eth-price").textContent = Number(eth.last_price).toLocaleString();
        document.getElementById("eth-fresh").textContent = `${eth.data_freshness_seconds}s`;
        document.getElementById("eth-bbo").textContent = `Bid ${Number(eth.best_bid).toLocaleString()} · Ask ${Number(eth.best_ask).toLocaleString()}`;
      }
    } catch (_) {}
  }

  async function loadNews() {
    try {
      const res = await fetch(`${API_BASE}/api/news?limit=20`);
      if (!res.ok) return;
      const items = await res.json();
      newsListEl.innerHTML = items
        .map((item) => {
          const syms = (item.symbols || [])
            .map((s) => `<span class="tag tag-sym">${esc(s)}</span>`)
            .join(" ");
          const san = item.sanitized
            ? `<span class="tag tag-sanitized" title="Prompt injection inside news body was neutralized">SANITIZED</span>`
            : "";
          return `
            <article class="news-item" id="news-card-${esc(item.news_id)}">
              <div class="news-meta">
                <div><span class="tag tag-id">${esc(item.news_id)}</span> ${syms} ${san}</div>
                <span>idx v${item.index_version}</span>
              </div>
              <div class="news-headline">${esc(item.headline)}</div>
              <div class="news-body">${esc(item.body)}</div>
            </article>
          `;
        })
        .join("");
    } catch (_) {}
  }

  async function loadDLQ() {
    try {
      const res = await fetch(`${API_BASE}/api/dlq`);
      if (!res.ok) return;
      const items = await res.json();
      if (!items.length) {
        dlqListEl.innerHTML = `<div style="font-size:11px;color:#64748b;">No dead-lettered messages.</div>`;
        return;
      }
      dlqListEl.innerHTML = items
        .map(
          (d) => `
          <div class="dlq-box" style="margin-bottom:6px;">
            <div style="color:#f87171;font-weight:700;">DLQ #${d.dlq_id} (${esc(d.routing_key)})</div>
            <div style="color:#fca5a5;margin:2px 0;">${esc(d.error_reason)}</div>
            <div style="color:#94a3b8;word-break:break-all;">${esc(d.raw_payload)}</div>
          </div>
        `
        )
        .join("");
    } catch (_) {}
  }

  async function loadMetrics() {
    try {
      const res = await fetch(`${API_BASE}/api/metrics/summary`);
      if (!res.ok) return;
      const m = await res.json();
      document.getElementById("m-idx").textContent = `v${m.index_version}`;
      document.getElementById("m-lat").textContent = `${m.llm_inference_ms.p50} / ${m.llm_inference_ms.p95} ms`;
      document.getElementById("m-tok").textContent = `${m.tokens_total.prompt} / ${m.tokens_total.eval}`;
      document.getElementById("m-ref").textContent = `${(m.refusal_rate * 100).toFixed(1)}%`;
      document.getElementById("m-dlq").textContent = `${m.dlq_messages_count}`;
    } catch (_) {}
  }

  window.highlightNewsCard = function (newsId) {
    document.querySelectorAll(".news-item.highlight").forEach((el) => el.classList.remove("highlight"));
    const target = document.getElementById(`news-card-${newsId}`);
    if (target) {
      target.classList.add("highlight");
      target.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  };

  async function sendChat(query, symbol) {
    const userBubble = document.createElement("div");
    userBubble.className = "chat-bubble user";
    userBubble.textContent = query;
    chatStreamEl.appendChild(userBubble);
    chatStreamEl.scrollTop = chatStreamEl.scrollHeight;

    chatSubmit.disabled = true;
    try {
      const res = await fetch(`${API_BASE}/api/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, symbol: symbol || null }),
      });
      const data = await res.json();

      const botBubble = document.createElement("div");
      botBubble.className = "chat-bubble bot";

      const citationsHtml =
        data.citations && data.citations.length
          ? `<div class="citations-box">
              <div style="color:#94a3b8;margin-bottom:4px;font-weight:600;">Verified FTS5 Citations (click to inspect in feed):</div>
              ${data.citations
                .map(
                  (c) =>
                    `<span class="cite-pill" onclick="highlightNewsCard('${esc(c.news_id)}')">[${esc(c.news_id)}] ${esc(c.headline)} (BM25: ${c.bm25_score})</span>`
                )
                .join("")}
            </div>`
          : "";

      const mkt = data.market_context;
      const mktText = mkt
        ? `Price (${esc(mkt.symbol)}): ${Number(mkt.last_price).toLocaleString()} USDT (freshness: ${mkt.data_freshness_seconds}s)`
        : "";
      const tel = data.telemetry || {};

      botBubble.innerHTML = `
        <div class="status-badge ${esc(data.status)}">${esc(data.status)}${data.refusal_reason ? " · " + esc(data.refusal_reason) : ""}</div>
        <div>${esc(data.answer)}</div>
        ${citationsHtml}
        <div class="freshness-footer">
          ${mktText ? `<span>${mktText}</span>` : ""}
          <span>KB Index: v${data.index_version}</span>
          <span>Total Latency: ${tel.latency_ms ?? 0}ms (LLM: ${tel.llm_inference_ms ?? 0}ms)</span>
          <span>Tokens: ${tel.prompt_tokens ?? 0}p / ${tel.eval_tokens ?? 0}e</span>
          <span>Engine: ${esc(tel.engine || "rag")}</span>
        </div>
      `;
      chatStreamEl.appendChild(botBubble);
      chatStreamEl.scrollTop = chatStreamEl.scrollHeight;

      if (data.citations && data.citations.length > 0) {
        window.highlightNewsCard(data.citations[0].news_id);
      }
      await loadMetrics();
    } catch (err) {
      const errBubble = document.createElement("div");
      errBubble.className = "chat-bubble bot";
      errBubble.innerHTML = `<div class="status-badge REJECTED_UNSAFE">CONNECTION ERROR</div><div>Could not reach Service C API (${esc(err.message)}).</div>`;
      chatStreamEl.appendChild(errBubble);
    } finally {
      chatSubmit.disabled = false;
    }
  }

  chatForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = chatInput.value.trim();
    if (!q) return;
    chatInput.value = "";
    sendChat(q, chatSymbol.value);
  });

  document.querySelectorAll(".scenario-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      if (chip.dataset.sym) chatSymbol.value = chip.dataset.sym;
      sendChat(chip.dataset.q, chip.dataset.sym);
    });
  });

  document.getElementById("btn-replay").addEventListener("click", async () => {
    await fetch(`${API_BASE}/api/replay`, { method: "POST" });
    await refreshAll();
  });

  document.getElementById("btn-poison").addEventListener("click", async () => {
    const poison = `{"news_id": "poison_${Date.now()}", "timestamp_ms": "CORRUPTED_NOT_INT", "symbols": []}`;
    await fetch(`${API_BASE}/api/news/inject`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ raw_payload: poison }),
    });
    await refreshAll();
  });

  refreshAll();
  setInterval(loadMetrics, 5000);
})();
