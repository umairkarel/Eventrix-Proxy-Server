import logging
import os
import re
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_http: httpx.AsyncClient


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _http
    _http = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, read=None),
        limits=httpx.Limits(max_keepalive_connections=50, max_connections=100),
        follow_redirects=True,
    )
    yield
    await _http.aclose()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

BASE_DOMAIN = os.getenv("BASE_DOMAIN", "")
GTM_PORT = int(os.getenv("GTM_PORT", "8080"))
GTM_ORIGIN = "https://www.googletagmanager.com"

_SKIP_REQUEST_HEADERS = frozenset({
    "host", "content-length", "transfer-encoding",
    "connection", "accept-encoding", "te", "upgrade",
})
GTM_CONTAINER_PREFIXES = tuple(
    p.strip() for p in os.getenv("GTM_CONTAINER_PREFIXES", "/gtm/,/_/service_worker/").split(",")
)


def get_host(request: Request) -> str:
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return host.split(":")[0]


def get_gtm_upstream(host: str) -> str:
    if BASE_DOMAIN and host.endswith(f".{BASE_DOMAIN}"):
        subdomain = host[: -(len(BASE_DOMAIN) + 1)]
    else:
        subdomain = host.split(".")[0]
    return f"http://gtm-server-{subdomain}:{GTM_PORT}"


def rewrite_script(text: str, host: str) -> str:
    return re.sub(
        r'"(21|3)":"www\.googletagmanager\.com"',
        lambda m: f'"{m.group(1)}":"{host}"',
        text,
    )


@app.get("/gtm.js")
async def proxy_gtm_js(request: Request):
    host = get_host(request)
    upstream = get_gtm_upstream(host)
    response = await _http.get(
        f"{upstream}/gtm.js",
        params=dict(request.query_params),
        headers={"Accept-Encoding": "identity"},
    )
    return Response(
        content=rewrite_script(response.text, host).encode("utf-8"),
        status_code=response.status_code,
        media_type="application/javascript; charset=UTF-8",
        headers={"Cache-Control": response.headers.get("cache-control", "private, max-age=900")},
    )


ATTRIBUTION_REPORTING_WRAPPER = (
    "if(XMLHttpRequest.prototype.setAttributionReporting){"
    "XMLHttpRequest.prototype.setAttributionReporting = "
    "(function(setAttributionReporting) {"
    "return function() {"
    "try {setAttributionReporting.apply(this, arguments);} "
    "catch(e) {console.error(e);}}"
    "})(XMLHttpRequest.prototype.setAttributionReporting);}\n"
)


@app.get("/gtag/js")
async def proxy_gtag_js(request: Request):
    host = get_host(request)
    upstream = get_gtm_upstream(host)
    response = await _http.get(
        f"{upstream}/gtag/js",
        params=dict(request.query_params),
        headers={"Accept-Encoding": "identity"},
    )
    script = ATTRIBUTION_REPORTING_WRAPPER + response.text
    return Response(
        content=script.encode("utf-8"),
        status_code=response.status_code,
        media_type="application/javascript; charset=UTF-8",
        headers={"Cache-Control": response.headers.get("cache-control", "private, max-age=900")},
    )


@app.get("/healthz")
def health():
    return {"status": "ok"}


def _routes_to_gtm_container(req_path: str) -> bool:
    return any(req_path.startswith(p) for p in GTM_CONTAINER_PREFIXES)


_EXCLUDED_RESPONSE_HEADERS = frozenset({
    "content-encoding", "transfer-encoding", "content-length",
    "access-control-allow-origin", "access-control-allow-credentials",
    "access-control-allow-methods", "access-control-allow-headers",
    "access-control-expose-headers", "access-control-max-age",
})


@app.api_route("/{path:path}", methods=["GET", "POST", "OPTIONS", "HEAD"])
async def proxy_all(path: str, request: Request):
    host = get_host(request)
    req_path = f"/{path}"
    to_gtm = _routes_to_gtm_container(req_path)

    if to_gtm:
        target = f"{get_gtm_upstream(host)}{req_path}"
    else:
        # Browser page navigations to Google-hosted pages can't be transparently
        # proxied (they need browser cookies/session). Redirect instead.
        if (
            "text/html" in request.headers.get("accept", "")
            and request.headers.get("sec-fetch-mode") == "navigate"
        ):
            qs = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(url=f"{GTM_ORIGIN}{req_path}{qs}")
        target = f"{GTM_ORIGIN}{req_path}"

    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _SKIP_REQUEST_HEADERS
    }
    fwd_headers["Accept-Encoding"] = "identity"

    upstream_req = _http.build_request(
        method=request.method,
        url=target,
        params=dict(request.query_params),
        headers=fwd_headers,
        content=await request.body(),
    )
    response = await _http.send(upstream_req, stream=True)

    # Collect all response headers, keeping duplicate Set-Cookie entries separate.
    # response.headers.multi_items() preserves all (key, value) pairs including
    # multiple Set-Cookie headers that a dict comprehension would silently drop.
    set_cookies: list[str] = []
    resp_headers: dict[str, str] = {}
    for k, v in response.headers.multi_items():
        if k.lower() in _EXCLUDED_RESPONSE_HEADERS:
            continue
        if k.lower() == "set-cookie":
            set_cookies.append(v)
        else:
            resp_headers[k.lower()] = v

    # On /gtm/debug: synthesize x-gtm-server-preview from the gtm_auth + gtm_preview
    # Set-Cookie headers and add it as a browser cookie. This lets /g/collect requests
    # that bypass proxy-server still carry the preview token directly to sGTM.
    if req_path.startswith("/gtm/debug"):
        sc_map: dict[str, str] = {}
        for sc in set_cookies:
            name, _, rest = sc.partition("=")
            sc_map[name.strip()] = rest.partition(";")[0].strip()
        gtm_auth_raw = sc_map.get("gtm_auth", "")
        gtm_preview_raw = sc_map.get("gtm_preview", "")
        if gtm_auth_raw and gtm_preview_raw:
            container_id, _, auth_token = gtm_auth_raw.partition("=")
            _, _, env_part = gtm_preview_raw.partition("=")
            if env_part.startswith("env-"):
                env_num = env_part[len("env-"):]
                preview_value = f"id={container_id}&env={env_num}&auth={auth_token}"
                set_cookies.append(
                    f"x-gtm-server-preview={preview_value}; Max-Age=300; Path=/; SameSite=None; Secure"
                )
                logger.info("GTM[%s] synthesized x-gtm-server-preview cookie for direct collect bypass", req_path)

    content_type = response.headers.get("content-type", "")

    if "text/event-stream" in content_type:
        async def stream_sse():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()

        sr = StreamingResponse(
            stream_sse(),
            status_code=response.status_code,
            media_type=content_type,
            headers=resp_headers,
        )
        for sc in set_cookies:
            sr.raw_headers.append((b"set-cookie", sc.encode("latin-1")))
        return sr

    content = await response.aread()
    await response.aclose()

    r = Response(
        content=content,
        status_code=response.status_code,
        media_type=content_type or None,
        headers=resp_headers,
    )
    for sc in set_cookies:
        r.raw_headers.append((b"set-cookie", sc.encode("latin-1")))
    return r
