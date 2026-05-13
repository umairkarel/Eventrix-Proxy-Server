import os
import re
import time
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

GA4_ENDPOINT = "https://www.google-analytics.com/g/collect"
BASE_DOMAIN = os.getenv("BASE_DOMAIN", "")
GTM_PORT = int(os.getenv("GTM_PORT", "8080"))


def get_real_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host


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


@app.get("/g/collect")
@app.post("/g/collect")
async def proxy_collect(request: Request):
    client_ip = get_real_ip(request)
    user_agent = request.headers.get("user-agent", "")
    referrer = request.headers.get("referer", "")
    body = await request.body()

    print(
        f"ip={client_ip} ua={user_agent!r} ref={referrer!r} "
        f"content_length={len(body)}"
    )

    params = dict(request.query_params)
    headers = {
        "User-Agent": user_agent,
        "X-Forwarded-For": client_ip,
    }

    start = time.perf_counter()
    async with httpx.AsyncClient() as client:
        response = await client.request(
            method=request.method,
            url=GA4_ENDPOINT,
            params=params,
            content=body,
            headers=headers,
        )
    latency_ms = (time.perf_counter() - start) * 1000
    print(f"ga4_status={response.status_code} latency_ms={latency_ms:.1f}")

    return Response(
        content=response.content,
        status_code=response.status_code,
        headers=dict(response.headers),
    )


@app.get("/gtm.js")
async def proxy_gtm_js(request: Request):
    host = get_host(request)
    upstream = get_gtm_upstream(host)
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{upstream}/gtm.js",
            params=dict(request.query_params),
            headers={"Accept-Encoding": "identity"},
            follow_redirects=True,
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
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{upstream}/gtag/js",
            params=dict(request.query_params),
            headers={"Accept-Encoding": "identity"},
            follow_redirects=True,
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
