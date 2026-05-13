# Proxy Server

Lightweight FastAPI service that proxies GA4 analytics events through your own domain to Google Analytics. Bypasses ad blockers by serving `/g/collect` from a first-party origin, and injects the real client IP so GA4 receives accurate geolocation data.

Part of the [Server-Side GTM Tracking Platform](../../README.md).

---

## Overview

Most ad blockers target requests to `www.google-analytics.com`. This proxy sits on your domain and forwards requests transparently, so the browser sends analytics to `yourdomain.com/g/collect` instead.

```
Browser
  │
  │  POST https://yoursite.com/g/collect?...
  ▼
Proxy Server  ──── extracts real IP, logs request
  │
  │  POST https://www.google-analytics.com/g/collect
  │       X-Forwarded-For: <real client IP>
  ▼
Google Analytics
```

The proxy is intentionally minimal — it handles the forwarding and IP injection. For tag management, routing logic, and server-side tag execution, see the [GTM Server](../gtm-server/README.md) instead.

---

## Endpoints

### `GET /POST /g/collect`

Proxies GA4 measurement events to Google Analytics.

- Preserves all query parameters from the original request
- Preserves the request body (POST)
- Injects `X-Forwarded-For` with the real client IP (extracted from the incoming `X-Forwarded-For` header when behind a reverse proxy)
- Streams the GA4 response back to the client

**Example request from the browser:**
```
POST /g/collect?v=2&tid=G-XXXXXXXXXX&...
```

**Forwarded to GA4 as:**
```
POST https://www.google-analytics.com/g/collect?v=2&tid=G-XXXXXXXXXX&...
X-Forwarded-For: 203.0.113.42
User-Agent: Mozilla/5.0 ...
```

### `GET /healthz`

```json
{"status": "ok"}
```

---

## Running Locally

```bash
# Install dependencies (uv recommended)
uv sync

# Start the server
uv run uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

Or with pip:

```bash
pip install fastapi httpx "uvicorn[standard]"
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

The server listens on `http://localhost:8000`. Test it:

```bash
curl "http://localhost:8000/g/collect?v=2&tid=G-XXXXXXXXXX&cid=555&en=page_view"
```

---

## Running with Docker

Create a `Dockerfile`:

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml .
RUN uv sync --frozen
COPY server.py .
CMD ["uv", "run", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
```

```bash
docker build -t proxy-server .
docker run -p 8000:8000 proxy-server
```

---

## Deploying Behind Traefik

Add to your Traefik routing config or Docker labels so requests to `yourdomain.com/g/collect` are routed here:

```yaml
# traefik/conf.d/proxy.yml
http:
  routers:
    ga4-proxy:
      rule: "PathPrefix(`/g/collect`)"
      entryPoints: [websecure]
      tls: {}
      service: ga4-proxy-svc

  services:
    ga4-proxy-svc:
      loadBalancer:
        servers:
          - url: "http://proxy-server:8000"
```

Or with Docker labels:

```yaml
# docker-compose.yml
services:
  proxy-server:
    image: proxy-server
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.ga4.rule=PathPrefix(`/g/collect`)"
      - "traefik.http.routers.ga4.entrypoints=websecure"
      - "traefik.http.routers.ga4.tls.certresolver=cloudflare"
      - "traefik.http.services.ga4.loadbalancer.server.port=8000"
    networks:
      - traefik_public
```

---

## GA4 Tag Configuration

In Google Analytics / Google Tag Manager, update the GA4 Configuration tag to point at your proxy:

**In GTM (server-side or web):**
1. Open your GA4 Configuration tag
2. Set **Transport URL** to `https://yourdomain.com`

GA4 will then send events to `https://yourdomain.com/g/collect` instead of `https://www.google-analytics.com/g/collect`.

**Direct gtag.js configuration:**

```html
<script async src="https://www.googletagmanager.com/gtag/js?id=G-XXXXXXXXXX"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){dataLayer.push(arguments);}
  gtag('js', new Date());
  gtag('config', 'G-XXXXXXXXXX', {
    'transport_url': 'https://yourdomain.com'
  });
</script>
```

---

## IP Extraction

The proxy reads the real client IP in this priority order:

1. **`X-Forwarded-For` header** — first IP in the comma-separated list (set by Traefik or your load balancer)
2. **Direct connection IP** — falls back to `request.client.host` if no forwarded header is present

The extracted IP is forwarded to GA4 via `X-Forwarded-For`, which GA4 uses for geolocation. Without this, GA4 would see your server's IP for all events.

---

## Logging

Each request logs two lines to stdout:

```
ip=203.0.113.42 ua='Mozilla/5.0 ...' ref='https://yoursite.com/page' content_length=128
ga4_status=204 latency_ms=42.3
```

| Field | Description |
|---|---|
| `ip` | Real client IP (after X-Forwarded-For extraction) |
| `ua` | User-Agent string |
| `ref` | Referer header |
| `content_length` | Request body size in bytes |
| `ga4_status` | HTTP status returned by Google Analytics (204 = success) |
| `latency_ms` | Round-trip time to GA4 in milliseconds |

---

## CORS

CORS is configured to allow all origins, methods (`GET`, `POST`, `OPTIONS`), and headers. This is intentional — the proxy needs to accept requests from any website using your GA4 property.

If you want to restrict to specific origins, edit `server.py`:

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://yoursite.com", "https://www.yoursite.com"],
    ...
)
```

---

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| `fastapi` | ≥0.110.0 | HTTP framework |
| `httpx` | ≥0.27.0 | Async HTTP client for forwarding requests to GA4 |
| `uvicorn[standard]` | ≥0.29.0 | ASGI server |

Python ≥3.11 required.

---

## Proxy Server vs GTM Server

| | Proxy Server | GTM Server |
|---|---|---|
| **Purpose** | Forward GA4 events only | Full server-side tag execution |
| **Tag management** | None — pass-through only | Full GTM workspace (tags, triggers, variables) |
| **Setup complexity** | Minimal | Requires GTM workspace config |
| **Resource usage** | Very low (~50 MB) | ~512 MB per client |
| **Custom enrichment** | IP injection only | Full server-side enrichment, consent, etc. |
| **Use when** | You only need GA4 and want minimal infrastructure | You need server-side GTM capabilities |
