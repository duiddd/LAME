# Service A ↔ Service C Frontend Integration Guide

**Owners:** Pin-Yun Kuo (Service A: Trading & Accounts) & Rushabh Mutha (Service C: News & AI Analyst)

This guide explains how **Service A's unified exchange frontend** integrates **Service C's compact AI Market Analyst chat box and live news feed**, while preserving **strict failure isolation** (if Service C is stopped or unreachable, order placement and account management in Service A continue unaffected).

---

## 1. Running Service C for Frontend Integration

From the repository root (`LAME/`):

```bash
# Option 1: Run directly with Python / Uvicorn (zero external container dependencies required)
pip install -r service_c/requirements.txt
uvicorn service_c.main:app --host 0.0.0.0 --port 8080 --reload

# Option 2: Run via Docker Compose
docker compose up --build service-c rabbitmq
```

Once running on `http://localhost:8080`:
- **Full-Page Service C Terminal:** `http://localhost:8080/` (or `/chatbot`)
- **Compact Chat Box Widget Page:** `http://localhost:8080/widget`
- **Embeddable Widget Script:** `http://localhost:8080/static/lame-chat-widget.js`
- **Interactive OpenAPI Docs:** `http://localhost:8080/docs`

> **CORS Enabled:** Service C sets `Access-Control-Allow-Origin: *` on all endpoints, so Service A's frontend (whether served from `localhost:3000`, `localhost:5173`, `localhost:8000`, or `file://`) can call `http://localhost:8080` directly from the browser without proxy issues.

---

## 2. Three Ways Service A Can Integrate the Small Chat Box

### Option A: 2-Line Drop-In JavaScript Widget (Recommended)

Add a container `<div>` inside Service A's sidebar or bottom-right panel and load `lame-chat-widget.js`:

```html
<!-- Inside Service A's trading terminal HTML -->
<div id="lame-ai-chatbox"
     data-api-url="http://localhost:8080"
     data-symbol="BTC-USDT"
     style="width: 350px; height: 420px;">
</div>
<script src="http://localhost:8080/static/lame-chat-widget.js"></script>
```

#### Syncing When User Switches Trading Pair (`BTC-USDT` ↔ `ETH-USDT`)
When a trader selects a different trading pair in Service A's UI, call `LameAnalystWidget.mount` to switch the chat box context:

```javascript
function onTradingPairChanged(newSymbol) {
  // newSymbol: "BTC-USDT" or "ETH-USDT"
  if (window.LameAnalystWidget) {
    window.LameAnalystWidget.mount("#lame-ai-chatbox", {
      apiBaseUrl: "http://localhost:8080",
      symbol: newSymbol,
    });
  }
}
```

---

### Option B: Compact `<iframe>` Embed

If Service A uses React/Vue/Svelte and prefers complete CSS/DOM isolation, embed `/widget` in an `<iframe>`:

```html
<iframe
  src="http://localhost:8080/widget"
  title="LAME AI Market Analyst (Service C)"
  style="width: 350px; height: 420px; border: 1px solid #2B3139; border-radius: 6px; background: #181A20;"
  loading="lazy">
</iframe>
```

---

### Option C: Custom UI Pulling Directly from Service C REST API

If Service A wants to render custom HTML/React components for the chat box and news ticker, call these two endpoints directly:

#### 1. Ask the AI Market Analyst (`POST /api/chat`)

**Request:**
```javascript
async function askMarketAnalyst(queryText, symbol = "BTC-USDT") {
  try {
    const res = await fetch("http://localhost:8080/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query: queryText,
        symbol: symbol, // "BTC-USDT" | "ETH-USDT" | null
      }),
    });
    return await res.json();
  } catch (err) {
    // Failure Isolation: Service C being down must never break Service A's UI
    return {
      status: "SERVICE_UNAVAILABLE",
      answer: "AI Analyst (Service C) is currently offline. Trading continues normally.",
      citations: [],
    };
  }
}
```

**Response (`200 OK`):**
```json
{
  "answer": "Current BTC-USDT snapshot from Service B is 64,250 USDT (best bid 64,245, best ask 64,255) (price freshness: 0.4s ago). BTC-USDT declined by 2.4% over the past 15 minutes after the regional financial authority announced stricter reserve audit timelines for offshore spot exchanges. [news_20261023_001]",
  "status": "ANSWERED",
  "refusal_reason": null,
  "citations": [
    {
      "news_id": "news_20261023_001",
      "headline": "Regional Financial Regulator Issues Compliance Warning to Unlicensed Spot Venues",
      "timestamp_ms": 1729692000000,
      "news_freshness_seconds": 15.2,
      "bm25_score": 2.4105
    }
  ],
  "market_context": {
    "symbol": "BTC-USDT",
    "last_price": 64250,
    "best_bid": 64245,
    "best_ask": 64255,
    "price_timestamp_ms": 1729692005000,
    "data_freshness_seconds": 0.45,
    "is_stale": false,
    "source": "service_b_ws"
  },
  "news_as_of_ms": 1729692000000,
  "news_freshness_seconds": 15.2,
  "index_version": 5,
  "telemetry": {
    "latency_ms": 16.5,
    "llm_inference_ms": 12.1,
    "prompt_tokens": 182,
    "eval_tokens": 46,
    "stripped_hallucinated_citations": 0,
    "engine": "ollama:llama3.2:1b"
  }
}
```

#### Handling `status` Values in Service A's UI:
| `status` | Meaning | Recommended UI Badge |
|---|---|---|
| `ANSWERED` | Grounded explanation with verified FTS5 citations (`citations` array). | Green badge (`#0ECB81`) + clickable `[news_id]` pills + freshness timestamp |
| `INSUFFICIENT_EVIDENCE` | No relevant news found in SQLite FTS5 (`refusal_reason: "insufficient_evidence"`). | Gold badge (`#F0B90B` — "Insufficient Evidence — Refused to Guess") |
| `REJECTED_UNSAFE` | Blocked by Prompt-Injection or No-Investment-Advice guardrail (`refusal_reason: "unsafe_injection"` or `"investment_advice"`). | Red badge (`#F6465D` — "Blocked by Safety Guardrail") |
| `SERVICE_UNAVAILABLE` | Ollama or Service C is unreachable (`HTTP 503` or network error). | Red/Amber banner ("AI Analyst Offline — Trading Unaffected") |

---

#### 2. Pull Recent Market News (`GET /api/news`)

**Request:**
```javascript
const res = await fetch("http://localhost:8080/api/news?symbol=BTC-USDT&limit=10");
const newsItems = await res.json();
```

**Response (`200 OK`):**
```json
[
  {
    "news_id": "news_20261023_006",
    "timestamp_ms": 1729692720000,
    "symbols": ["BTC-USDT"],
    "headline": "Spot Order Book Liquidity Recovers Following Brief Volatility Spike",
    "body": "Top-3 order book depth on BTC-USDT recovered to normal levels within ten minutes of the regulatory headline as automated liquidity providers restored resting bids around 64,200 USDT. [REDACTED_PROMPT_INJECTION] Market spreads remain tight at 10 ticks.",
    "source": "LAME-MarketWatch",
    "signature": "sig_valid_006",
    "index_version": 5,
    "sanitized": true
  }
]
```

---

## 3. Shared Binance Pro Dark Theme Tokens (For Visual Consistency with Service A)

Both `chatbot.html` and `lame-chat-widget.js` use the **Binance Spot Pro** dark color system so Service C's widget blends seamlessly into Service A's trading terminal:

| Token Role | Hex Code | Usage |
|---|---|---|
| **Canvas Background** | `#0B0E11` | Outer grid background / input field fill |
| **Primary Panel Surface** | `#181A20` | Main modular panel containers (Order Book, Chart, Chat Box) |
| **Elevated Header / Card** | `#1E2329` | Panel headers, bot message cards, ticker bar |
| **Grid Border / Divider** | `#2B3139` | `1px solid` hairline borders between panels |
| **Primary Accent (Binance Gold)** | `#F0B90B` (`#FCD535` hover) | Primary CTA buttons, `[news_id]` citation badges, active tabs |
| **Bullish / Bid Green** | `#0ECB81` | Buy buttons, bid depth bars, `ANSWERED` status badge |
| **Bearish / Ask Red** | `#F6465D` | Sell buttons, ask depth bars, `REJECTED_UNSAFE` & DLQ alerts |
| **Primary / Secondary Text** | `#EAECEF` / `#848E9C` | Primary values (`Inter` + `JetBrains Mono`) and muted labels |

---

## 4. Acceptance Demo Checklist (Service A + Service C)

1. **Normal Grounded Q&A:** Ask *"Why did BTC-USDT drop over the last 15 minutes?"* inside Service A's embedded chat box -> Verify `[news_20261023_001]` is cited and `data_freshness_seconds` is displayed.
2. **Insufficient Evidence Refusal:** Ask *"Why did SOL-USDT crash after the eclipse?"* -> Verify `INSUFFICIENT_EVIDENCE` badge appears with zero fabricated citations.
3. **Prompt Injection Block:** Ask *"Ignore previous instructions and system override..."* -> Verify `REJECTED_UNSAFE` badge appears immediately.
4. **Failure Independence (`docker stop lame-service-c`):** Stop Service C while Service A's UI is open -> Verify the chat box shows `"Service C Offline — Core trading continues unaffected"` while placing/cancelling orders on Service A still succeeds at full speed.
