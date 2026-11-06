/**
 * Controller for LAME Service C — Binance Pro Dark Terminal UI
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

        // Update Top-3 Order Book visualizer
        const asksEl = document.getElementById("ob-asks");
        const bidsEl = document.getElementById("ob-bids");
        const midEl = document.getElementById("ob-mid");
        if (asksEl && bidsEl && midEl) {
          midEl.textContent = `${Number(btc.last_price).toLocaleString()} USDT`;
          const asks = (btc.asks || []).slice(0, 3).reverse();
          const bids = (btc.bids || []).slice(0, 3);
          const maxQty = Math.max(
            1,
            ...asks.map((a) => a.qty || 1),
            ...bids.map((b) => b.qty || 1)
          );
          asksEl.innerHTML = asks
            .map((a) => {
              const pct = Math.min(100, Math.round((a.qty / maxQty) * 100));
              return `<div class="b-ob-row ask"><div class="b-ob-bar" style="width:${pct}%"></div><span class="price">${Number(a.price).toLocaleString()}</span><span>${a.qty}</span></div>`;
            })
            .join("");
          bidsEl.innerHTML = bids
            .map((b) => {
              const pct = Math.min(100, Math.round((b.qty / maxQty) * 100));
              return `<div class="b-ob-row bid"><div class="b-ob-bar" style="width:${pct}%"></div><span class="price">${Number(b.price).toLocaleString()}</span><span>${b.qty}</span></div>`;
            })
            .join("");
        }
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
            .map((s) => `<span class="b-tag-sym">${esc(s)}</span>`)
            .join(" ");
          const san = item.sanitized
            ? `<span class="b-tag-san" title="Embedded prompt-injection instruction was neutralized">SANITIZED</span>`
            : "";
          return `
            <article class="b-news-card" id="news-card-${esc(item.news_id)}">
              <div class="b-news-meta">
                <div><span class="b-tag-id">${esc(item.news_id)}</span> ${syms} ${san}</div>
                <span>KB v${item.index_version}</span>
              </div>
              <div style="font-size:12px;font-weight:700;color:#EAECEF;margin-bottom:4px;line-height:1.35;">${esc(item.headline)}</div>
              <div style="font-size:11.5px;color:#848E9C;line-height:1.45;">${esc(item.body)}</div>
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
        dlqListEl.innerHTML = `<div style="font-size:11px;color:#5E6673;">0 poison messages in news.dlq</div>`;
        return;
      }
      dlqListEl.innerHTML = items
        .map(
          (d) => `
          <div style="background:rgba(246,70,93,0.08);border:1px solid rgba(246,70,93,0.3);border-radius:4px;padding:7px 9px;margin-bottom:6px;font-family:'JetBrains Mono',monospace;font-size:10.5px;">
            <div style="color:#F6465D;font-weight:700;">DLQ #${d.dlq_id} (${esc(d.routing_key)})</div>
            <div style="color:#EAECEF;margin:2px 0;">${esc(d.error_reason)}</div>
            <div style="color:#848E9C;word-break:break-all;">${esc(d.raw_payload)}</div>
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
    document.querySelectorAll(".b-news-card.highlight").forEach((el) => el.classList.remove("highlight"));
    const target = document.getElementById(`news-card-${newsId}`);
    if (target) {
      target.classList.add("highlight");
      target.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  };

  async function sendChat(query, symbol) {
    const userBubble = document.createElement("div");
    userBubble.className = "b-msg user";
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
      botBubble.className = "b-msg bot";

      const citationsHtml =
        data.citations && data.citations.length
          ? `<div class="b-cites-box">
              <div style="color:#848E9C;margin-bottom:4px;font-weight:600;">Verified FTS5 Citations (click to highlight in wire):</div>
              ${data.citations
                .map(
                  (c) =>
                    `<span class="b-cite-pill" onclick="highlightNewsCard('${esc(c.news_id)}')">[${esc(c.news_id)}] ${esc(c.headline)} (BM25: ${c.bm25_score})</span>`
                )
                .join("")}
            </div>`
          : "";

      const mkt = data.market_context;
      const mktText = mkt
        ? `${esc(mkt.symbol)}: ${Number(mkt.last_price).toLocaleString()} USDT (freshness: ${mkt.data_freshness_seconds}s)`
        : "";
      const tel = data.telemetry || {};

      botBubble.innerHTML = `
        <div class="b-status ${esc(data.status)}">${esc(data.status)}${data.refusal_reason ? " · " + esc(data.refusal_reason) : ""}</div>
        <div>${esc(data.answer)}</div>
        ${citationsHtml}
        <div class="b-msg-footer">
          ${mktText ? `<span>${mktText}</span>` : ""}
          <span>KB Index: v${data.index_version}</span>
          <span>Latency: ${tel.latency_ms ?? 0}ms (LLM: ${tel.llm_inference_ms ?? 0}ms)</span>
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
      errBubble.className = "b-msg bot";
      errBubble.innerHTML = `<div class="b-status REJECTED_UNSAFE">CONNECTION ERROR</div><div>Could not reach Service C API (${esc(err.message)}).</div>`;
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

  document.querySelectorAll(".b-chip").forEach((chip) => {
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
