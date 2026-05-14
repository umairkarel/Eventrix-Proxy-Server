# Proxy Server

Lightweight FastAPI service that proxies GTM script files from the correct upstream GTM container and rewrites domain references inside the scripts so the browser routes follow-up requests through your first-party domain.

Part of the [Server-Side GTM Tracking Platform](../../README.md).

---

## Overview

When a browser requests GTM scripts from `{subdomain}.{BASE_DOMAIN}`, Traefik routes those requests here. The proxy:

1. Extracts the subdomain from the `Host` header
2. Determines the upstream GTM container: `http://gtm-server-{subdomain}:8080`
3. Fetches the GTM JS from that container
4. Rewrites `"www.googletagmanager.com"` domain references inside the script with the request's own hostname

This keeps the full tracking flow first-party — subsequent browser requests for tags and event collection stay on your domain instead of Google's.

```
Browser
  │
  │  GET https://{subdomain}.{BASE_DOMAIN}/gtm.js
  ▼
Traefik  ──── routes to proxy-server
  │
  ▼
Proxy Server  ──── determines upstream from subdomain
  │
  │  GET http://gtm-server-{subdomain}:8080/gtm.js
  ▼
GTM Container  ──── returns GTM loader JS
  │
  ▼
Proxy Server  ──── rewrites domain references → returns to browser
```

---

## Endpoints

### `GET /gtm.js`

Fetches the GTM loader script from the upstream GTM container (`gtm-server-{subdomain}:8080/gtm.js`). Rewrites `"www.googletagmanager.com"` domain references in the script with the request hostname so the browser sends subsequent requests to your domain. Passes through all query parameters.

### `GET /gtag/js`

Fetches `gtag.js` from the upstream GTM container. Prepends an Attribution Reporting API safety wrapper that silently swallows `setAttributionReporting` errors on browsers where the API is not supported.

### `GET /healthz`

```json
{"status": "ok"}
```

---

## Upstream Routing

The `get_gtm_upstream` function determines the GTM container to proxy to based on the incoming hostname:

- If `BASE_DOMAIN` is set and the host ends with `.{BASE_DOMAIN}`, the subdomain is extracted from the prefix
- Otherwise, the first segment of the hostname is used as the subdomain
- Returns `http://gtm-server-{subdomain}:{GTM_PORT}` (default port 8080)

**Example:** A request to `acme.example.com` with `BASE_DOMAIN=example.com` routes to `http://gtm-server-acme:8080`.

---

## Domain Rewriting

The `rewrite_script` function applies a regex to the fetched GTM bundle:

```
"21":"www.googletagmanager.com"  →  "21":"{request-host}"
 "3":"www.googletagmanager.com"  →   "3":"{request-host}"
```

These keys correspond to domain configuration entries in the compiled GTM bundle. Replacing them causes the GTM loader to send all subsequent requests (tag libraries, event collection) to your domain rather than `www.googletagmanager.com`, keeping the entire tracking flow first-party.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `BASE_DOMAIN` | `""` | Root domain used for subdomain extraction from the Host header |
| `GTM_PORT` | `8080` | Port GTM containers listen on |

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

The server listens on `http://localhost:8000`.

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

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| `fastapi` | ≥0.110.0 | HTTP framework |
| `httpx` | ≥0.27.0 | Async HTTP client for fetching GTM scripts from upstream containers |
| `uvicorn[standard]` | ≥0.29.0 | ASGI server |

Python ≥3.11 required.
