from fastapi import FastAPI
from fastapi.responses import PlainTextResponse, RedirectResponse, Response


app = FastAPI(title="stage5-web-fixture")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/allowed")
async def allowed():
    return PlainTextResponse("Stage 5 fixture page.\nThis content is safe to preview.\n")


@app.get("/redirect-blocked")
async def redirect_blocked():
    return RedirectResponse("http://blocked.test/blocked", status_code=302)


@app.get("/blocked")
async def blocked():
    return PlainTextResponse("blocked host body\n")


@app.get("/binary")
async def binary():
    return Response(content=b"\x00\x01\x02\x03", media_type="application/octet-stream")


@app.get("/large")
async def large():
    return PlainTextResponse("x" * 20000)
