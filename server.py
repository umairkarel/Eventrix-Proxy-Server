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


def get_real_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host


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


@app.get("/healthz")
def health():
    return {"status": "ok"}
