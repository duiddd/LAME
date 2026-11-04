# Service C — News & AI Market Analyst

**Owner:** Rushabh Mutha  
**Question Answered:** *What is happening in the market, and why?*

Service C is the off-path generative AI market analyst and scripted news ingestion pipeline for the LAME exchange.

## Quick Start

```bash
# Install dependencies
pip install -r service_c/requirements.txt

# Start Service C on port 8080 (from repo root)
uvicorn service_c.main:app --host 0.0.0.0 --port 8080 --reload

# Run acceptance & unit tests
pytest service_c/tests/test_service_c.py -v
```

## Key URLs

- **Dedicated Chatbot & News Terminal:** `http://localhost:8080/`
- **Compact Embeddable Widget (for Service A):** `http://localhost:8080/widget`
- **Drop-in JS Script (for Service A):** `http://localhost:8080/static/lame-chat-widget.js`
- **Prometheus Metrics:** `http://localhost:8080/metrics`
- **Health Check:** `http://localhost:8080/healthz`

## Documentation

- Architecture: [`../docs/service_c_architecture.md`](../docs/service_c_architecture.md)
- Technical Decisions: [`../docs/service_c_tech_decisions.md`](../docs/service_c_tech_decisions.md)
- Service A Frontend Integration Guide: [`../docs/service_c_frontend_integration.md`](../docs/service_c_frontend_integration.md)
