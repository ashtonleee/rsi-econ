from collections import defaultdict
import time
from urllib.parse import parse_qs

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response


app = FastAPI(title="stage5-web-fixture")
REQUEST_COUNTS: dict[str, int] = defaultdict(int)
FIXTURE_PROVIDER_RETURNED_MODEL = "openai/gpt-4.1-mini-fixture-2026-03-16"


@app.middleware("http")
async def count_requests(request: Request, call_next):
    if not request.url.path.startswith("/debug/") and request.url.path != "/healthz":
        REQUEST_COUNTS[request.url.path] += 1
    return await call_next(request)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/debug/reset-counters")
async def reset_counters():
    REQUEST_COUNTS.clear()
    return {"status": "ok"}


@app.get("/debug/counters")
async def counters():
    return {"counts": dict(REQUEST_COUNTS)}


@app.get("/allowed")
async def allowed():
    return PlainTextResponse("Stage 5 fixture page.\nThis content is safe to preview.\n")


@app.get("/redirect-blocked")
async def redirect_blocked():
    return RedirectResponse("http://blocked.test/blocked", status_code=302)


@app.get("/redirect-allowed-two")
async def redirect_allowed_two():
    return RedirectResponse("http://allowed-two.test/allowed", status_code=302)


@app.get("/blocked")
async def blocked():
    return PlainTextResponse("blocked host body\n")


@app.get("/provider/v1/models")
async def provider_models():
    return {
        "object": "list",
        "data": [{"id": FIXTURE_PROVIDER_RETURNED_MODEL, "object": "model"}],
    }


@app.post("/provider/v1/chat/completions")
async def provider_chat_completions(request: Request):
    payload = await request.json()
    messages = payload.get("messages", [])
    last_message = ""
    if messages:
        last_message = str(messages[-1].get("content", ""))
    return {
        "id": "chatcmpl-fixture-provider",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": FIXTURE_PROVIDER_RETURNED_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": f"Fixture provider reply: {last_message[:120]}",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 23,
            "completion_tokens": 11,
            "total_tokens": 34,
        },
    }


@app.get("/binary")
async def binary():
    return Response(content=b"\x00\x01\x02\x03", media_type="application/octet-stream")


@app.get("/large")
async def large():
    return PlainTextResponse("x" * 20000)


@app.get("/browser/rendered")
async def browser_rendered():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="description" content="Stage 6 browser fixture description" />
    <title>Loading...</title>
    <style>
      body {
        background: #f6f2e8;
        color: #1f2933;
        font-family: Georgia, serif;
        margin: 0;
      }
      main {
        margin: 48px auto;
        max-width: 720px;
        padding: 32px;
        background: #fffdf8;
        border: 2px solid #d7c2a0;
      }
      .eyebrow {
        letter-spacing: 0.08em;
        text-transform: uppercase;
        font-size: 12px;
      }
      h1 {
        margin-top: 12px;
      }
    </style>
  </head>
  <body>
    <main>
      <div class="eyebrow">Stage 6 Fixture</div>
      <h1 id="headline">Booting browser fixture</h1>
      <p id="body">Waiting for trusted browser rendering...</p>
    </main>
    <script>
      setTimeout(() => {
        document.title = "Stage 6 Fixture Title";
        document.getElementById("headline").textContent = "Stage 6 fixture rendered body";
        document.getElementById("body").textContent =
          "This rendered text comes from a deterministic JS fixture.";
      }, 50);
    </script>
  </body>
</html>
""".strip()
    )


@app.get("/browser/follow-source")
async def browser_follow_source():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="description" content="Stage 6B source fixture description" />
    <title>Stage 6B Source</title>
  </head>
  <body>
    <main>
      <h1>Stage 6B source page</h1>
      <p>This page exposes a deterministic set of safe href targets.</p>
      <ul>
        <li><a href="/browser/follow-target">Follow same origin target</a></li>
        <li><a href="http://allowed-two.test/browser/cross-origin-target">Follow cross origin target</a></li>
        <li><a href="/browser/follow-blocked-subresource">Follow blocked subresource target</a></li>
        <li><a href="/browser/follow-popup-target">Follow popup target</a></li>
        <li><a href="/browser/follow-download-target">Follow download target</a></li>
        <li><a href="/browser/follow-meta-refresh-target">Follow meta refresh target</a></li>
        <li><a href="/browser/follow-redirect-blocked-target">Follow blocked redirect target</a></li>
        <li><a href="mailto:hello@example.com">Ignored mailto link</a></li>
      </ul>
    </main>
  </body>
</html>
""".strip()
    )


@app.get("/browser/follow-target")
async def browser_follow_target():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="description" content="Stage 6B same origin target description" />
    <title>Stage 6B Same Origin Target</title>
  </head>
  <body>
    <main>
      <h1>Stage 6B same origin target</h1>
      <p>This target page is safe to follow.</p>
    </main>
    <script>
      setTimeout(() => {
        document.title = "Stage 6B Same Origin Target";
      }, 50);
    </script>
  </body>
</html>
""".strip()
    )


@app.get("/browser/cross-origin-target")
async def browser_cross_origin_target():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="description" content="Stage 6B cross origin target description" />
    <title>Stage 6B Cross Origin Target</title>
  </head>
  <body>
    <main>
      <h1>Stage 6B cross origin target</h1>
      <p>This cross origin target remains allowlisted.</p>
    </main>
  </body>
</html>
""".strip()
    )


@app.get("/browser/follow-blocked-subresource")
async def browser_follow_blocked_subresource():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>Stage 6B blocked subresource target</title>
  </head>
  <body>
    <p>Blocked subresource target.</p>
    <img src="http://blocked.test/browser/blocked-image.png" alt="blocked" />
  </body>
</html>
""".strip()
    )


@app.get("/browser/follow-popup-target")
async def browser_follow_popup_target():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>Stage 6B popup target</title>
  </head>
  <body>
    <p>Popup follow target.</p>
    <script>
      setTimeout(() => {
        window.open("http://allowed.test/browser/popup-target", "_blank");
      }, 50);
    </script>
  </body>
</html>
""".strip()
    )


@app.get("/browser/follow-download-target")
async def browser_follow_download_target():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>Stage 6B download target</title>
  </head>
  <body>
    <p>Download follow target.</p>
    <script>
      setTimeout(() => {
        const link = document.createElement("a");
        link.href = "http://allowed.test/browser/download.bin";
        link.download = "fixture.bin";
        document.body.appendChild(link);
        link.click();
      }, 50);
    </script>
  </body>
</html>
""".strip()
    )


@app.get("/browser/follow-meta-refresh-target")
async def browser_follow_meta_refresh_target():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>Stage 6B meta refresh target</title>
    <meta http-equiv="refresh" content="0.1; url=http://blocked.test/browser/rendered" />
  </head>
  <body>
    <p>Meta refresh follow target.</p>
  </body>
</html>
""".strip()
    )


@app.get("/browser/follow-redirect-blocked-target")
async def browser_follow_redirect_blocked_target():
    return RedirectResponse("http://blocked.test/browser/rendered", status_code=302)


@app.get("/browser/blocked-subresource")
async def browser_blocked_subresource():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>Blocked subresource</title>
  </head>
  <body>
    <p>Attempting blocked subresource.</p>
    <img src="http://blocked.test/browser/blocked-image.png" alt="blocked" />
  </body>
</html>
""".strip()
    )


@app.get("/browser/popup")
async def browser_popup():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>Popup fixture</title>
  </head>
  <body>
    <p>Popup attempt fixture.</p>
    <script>
      setTimeout(() => {
        window.open("http://allowed.test/browser/popup-target", "_blank");
      }, 50);
    </script>
  </body>
</html>
""".strip()
    )


@app.get("/browser/popup-target")
async def browser_popup_target():
    return HTMLResponse("<html><body><p>popup target</p></body></html>")


@app.get("/browser/download-page")
async def browser_download_page():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>Download fixture</title>
  </head>
  <body>
    <p>Download attempt fixture.</p>
    <script>
      setTimeout(() => {
        const link = document.createElement("a");
        link.href = "http://allowed.test/browser/download.bin";
        link.download = "fixture.bin";
        document.body.appendChild(link);
        link.click();
      }, 50);
    </script>
  </body>
</html>
""".strip()
    )


@app.get("/browser/download.bin")
async def browser_download_bin():
    return Response(
        content=b"fixture-download",
        media_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="fixture.bin"'},
    )


@app.get("/browser/redirect-blocked")
async def browser_redirect_blocked():
    return RedirectResponse("http://blocked.test/browser/rendered", status_code=302)


@app.get("/browser/redirect-allowed-two")
async def browser_redirect_allowed_two():
    return RedirectResponse(
        "http://allowed-two.test/browser/cross-origin-target",
        status_code=302,
    )


@app.get("/browser/blocked-image.png")
async def browser_blocked_image():
    return Response(content=b"\x89PNG\r\n\x1a\n", media_type="image/png")


@app.get("/browser/render-meta-refresh")
async def browser_render_meta_refresh():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>Render meta refresh fixture</title>
    <meta http-equiv="refresh" content="0.1; url=http://blocked.test/browser/rendered" />
  </head>
  <body>
    <main>
      <h1>Render meta refresh fixture</h1>
      <p>This page attempts an additional top-level navigation.</p>
    </main>
  </body>
</html>
""".strip()
    )


@app.get("/browser/render-js-redirect")
async def browser_render_js_redirect():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>Render JS redirect fixture</title>
  </head>
  <body>
    <main>
      <h1>Render JS redirect fixture</h1>
      <p>This page attempts an additional top-level navigation via JS.</p>
    </main>
    <script>
      setTimeout(() => {
        window.location = "http://blocked.test/browser/rendered";
      }, 50);
    </script>
  </body>
</html>
""".strip()
    )


@app.get("/browser/channel-iframe-blocked")
async def browser_channel_iframe_blocked():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>Iframe channel fixture</title>
  </head>
  <body>
    <main>
      <h1>Iframe channel fixture</h1>
      <iframe src="http://blocked.test/browser/frame-target" title="blocked frame"></iframe>
    </main>
  </body>
</html>
""".strip()
    )


@app.get("/browser/channel-fetch-xhr")
async def browser_channel_fetch_xhr():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>Fetch XHR channel fixture</title>
  </head>
  <body>
    <main>
      <h1>Fetch/XHR channel fixture</h1>
    </main>
    <script>
      setTimeout(() => {
        fetch("http://blocked.test/browser/xhr-target").catch(() => {});
        const xhr = new XMLHttpRequest();
        xhr.open("GET", "http://blocked.test/browser/xhr-target");
        xhr.send();
      }, 25);
    </script>
  </body>
</html>
""".strip()
    )


@app.get("/browser/channel-form-submit")
async def browser_channel_form_submit():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>Form submission fixture</title>
  </head>
  <body>
    <main>
      <h1>Form submission fixture</h1>
      <form id="blocked-form" action="http://blocked.test/browser/form-target" method="post">
        <input type="text" name="payload" value="fixture" />
      </form>
    </main>
    <script>
      setTimeout(() => {
        document.getElementById("blocked-form").submit();
      }, 25);
    </script>
  </body>
</html>
""".strip()
    )


@app.get("/browser/channel-websocket")
async def browser_channel_websocket():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>WebSocket fixture</title>
  </head>
  <body>
    <main>
      <h1>WebSocket fixture</h1>
    </main>
    <script>
      setTimeout(() => {
        try {
          new WebSocket("ws://blocked.test/browser/socket");
        } catch (err) {}
      }, 25);
    </script>
  </body>
</html>
""".strip()
    )


@app.get("/browser/channel-eventsource")
async def browser_channel_eventsource():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>EventSource fixture</title>
  </head>
  <body>
    <main>
      <h1>EventSource fixture</h1>
    </main>
    <script>
      setTimeout(() => {
        try {
          new EventSource("http://blocked.test/browser/events");
        } catch (err) {}
      }, 25);
    </script>
  </body>
</html>
""".strip()
    )


@app.get("/browser/channel-beacon")
async def browser_channel_beacon():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>Beacon fixture</title>
  </head>
  <body>
    <main>
      <h1>Beacon fixture</h1>
    </main>
    <script>
      setTimeout(() => {
        navigator.sendBeacon("http://blocked.test/browser/beacon", "fixture");
      }, 25);
    </script>
  </body>
</html>
""".strip()
    )


@app.get("/browser/channel-popup")
async def browser_channel_popup():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>Popup channel fixture</title>
  </head>
  <body>
    <main>
      <h1>Popup channel fixture</h1>
    </main>
    <script>
      setTimeout(() => {
        window.open("http://allowed.test/browser/popup-target", "_blank");
      }, 25);
    </script>
  </body>
</html>
""".strip()
    )


@app.get("/browser/channel-download")
async def browser_channel_download():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>Download channel fixture</title>
  </head>
  <body>
    <main>
      <h1>Download channel fixture</h1>
    </main>
    <script>
      setTimeout(() => {
        const link = document.createElement("a");
        link.href = "http://allowed.test/browser/download.bin";
        link.download = "fixture.bin";
        document.body.appendChild(link);
        link.click();
      }, 25);
    </script>
  </body>
</html>
""".strip()
    )


@app.get("/browser/channel-upload")
async def browser_channel_upload():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>Upload channel fixture</title>
  </head>
  <body>
    <main>
      <h1>Upload channel fixture</h1>
    </main>
    <script>
      setTimeout(() => {
        const input = document.createElement("input");
        input.type = "file";
        document.body.appendChild(input);
        input.click();
      }, 25);
    </script>
  </body>
</html>
""".strip()
    )


@app.get("/browser/channel-prefetch")
async def browser_channel_prefetch():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>Prefetch fixture</title>
  </head>
  <body>
    <main>
      <h1>Prefetch fixture</h1>
    </main>
    <script>
      setTimeout(() => {
        const prefetch = document.createElement("link");
        prefetch.rel = "prefetch";
        prefetch.href = "http://blocked.test/browser/prefetch-target";
        document.head.appendChild(prefetch);

        const preconnect = document.createElement("link");
        preconnect.rel = "preconnect";
        preconnect.href = "http://blocked.test";
        document.head.appendChild(preconnect);
      }, 25);
    </script>
  </body>
</html>
""".strip()
    )


@app.get("/browser/channel-external-protocol")
async def browser_channel_external_protocol():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>External protocol fixture</title>
  </head>
  <body>
    <main>
      <h1>External protocol fixture</h1>
    </main>
    <script>
      setTimeout(() => {
        const link = document.createElement("a");
        link.href = "mailto:hello@example.com";
        document.body.appendChild(link);
        link.click();
      }, 25);
    </script>
  </body>
</html>
""".strip()
    )


@app.get("/browser/channel-worker")
async def browser_channel_worker():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>Worker fixture</title>
  </head>
  <body>
    <main>
      <h1>Worker fixture</h1>
    </main>
    <script>
      setTimeout(() => {
        try {
          new Worker("http://blocked.test/browser/worker.js");
        } catch (err) {}
      }, 25);
    </script>
  </body>
</html>
""".strip()
    )


@app.get("/browser/frame-target")
async def browser_frame_target():
    return HTMLResponse("<html><body><p>blocked frame target</p></body></html>")


@app.get("/browser/xhr-target")
async def browser_xhr_target():
    return PlainTextResponse("xhr target\n")


@app.post("/browser/form-target")
async def browser_form_target():
    return PlainTextResponse("form target\n")


@app.get("/browser/interactive-form")
async def browser_interactive_form():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>Interactive form fixture</title>
  </head>
  <body>
    <main>
      <h1>Interactive form fixture</h1>
      <p>Use the controls below to prepare a gated submit.</p>
      <form action="http://allowed.test/browser/interactive-result" method="post">
        <label>
          Name
          <input type="text" name="name" value="fixture" />
        </label>
        <label>
          Plan
          <select name="plan">
            <option value="basic">Basic</option>
            <option value="pro">Pro</option>
          </select>
        </label>
        <label>
          <input type="checkbox" name="agree" value="yes" />
          I agree
        </label>
        <button type="submit">Claim reward</button>
      </form>
      <p><a href="http://allowed.test/browser/interactive-help">Read more</a></p>
    </main>
  </body>
</html>
""".strip()
    )


@app.get("/browser/interactive-form-disallowed")
async def browser_interactive_form_disallowed():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>Interactive disallowed form fixture</title>
  </head>
  <body>
    <main>
      <h1>Interactive disallowed form fixture</h1>
      <p>This form is readable, but its submit target is outside the action allowlist.</p>
      <form action="http://allowed-two.test/browser/interactive-result" method="post">
        <label>
          Name
          <input type="text" name="name" value="fixture" />
        </label>
        <button type="submit">Try blocked submit</button>
      </form>
    </main>
  </body>
</html>
""".strip()
    )


@app.get("/browser/interactive-help")
async def browser_interactive_help():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>Interactive help fixture</title>
  </head>
  <body>
    <main>
      <h1>Interactive help fixture</h1>
      <p>This page stays allowlisted and can be reached through an interactive click.</p>
    </main>
  </body>
</html>
""".strip()
    )


@app.get("/browser/public-workflow-start")
async def browser_public_workflow_start():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>Public workflow start fixture</title>
  </head>
  <body>
    <main>
      <h1>Public workflow start fixture</h1>
      <p>This page lives on a host outside the bounded browser allowlist.</p>
      <p>Use workflow_browser_public mode to continue the mediated workflow.</p>
    </main>
  </body>
</html>
""".strip()
    )


@app.get("/browser/public-workflow-step")
async def browser_public_workflow_step():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>Public workflow step fixture</title>
  </head>
  <body>
    <main>
      <h1>Public workflow step fixture</h1>
      <p>Fill the email field, then send a real browser request that pauses for approval.</p>
      <label>
        Email
        <input type="email" name="email" value="" placeholder="operator@example.com" />
      </label>
      <button id="send-request" type="button">Send public workflow request</button>
      <p id="status">No result yet.</p>
    </main>
    <script>
      const button = document.getElementById("send-request");
      const status = document.getElementById("status");
      button.addEventListener("click", async () => {
        const email = String(document.querySelector('input[name="email"]').value || "");
        const response = await fetch("http://blocked.test/browser/public-workflow-result", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            email,
            source: window.location.pathname,
          }),
        });
        const payload = await response.json();
        status.textContent = payload.message;
      });
    </script>
  </body>
</html>
""".strip()
    )


@app.post("/browser/public-workflow-result")
async def browser_public_workflow_result(request: Request):
    payload = await request.json()
    email = str(payload.get("email", ""))
    source = str(payload.get("source", ""))
    return {
        "ok": True,
        "message": f"Workflow submitted for {email} from {source}.",
    }


@app.post("/browser/interactive-result")
async def browser_interactive_result(request: Request):
    raw = (await request.body()).decode("utf-8", errors="ignore")
    form = parse_qs(raw, keep_blank_values=True)
    name = str((form.get("name") or [""])[0])
    plan = str((form.get("plan") or [""])[0])
    agree = str((form.get("agree") or [""])[0])
    return HTMLResponse(
        f"""
<!doctype html>
<html>
  <head>
    <title>Interactive result fixture</title>
  </head>
  <body>
    <main>
      <h1>Interactive result fixture</h1>
      <p>Submitted name={name} plan={plan} agree={agree or "no"}.</p>
    </main>
  </body>
</html>
""".strip()
    )


@app.get("/browser/events")
async def browser_events():
    return Response(
        content="event: message\ndata: fixture\n\n",
        media_type="text/event-stream",
    )


@app.post("/browser/beacon")
async def browser_beacon():
    return PlainTextResponse("beacon target\n")


@app.get("/browser/prefetch-target")
async def browser_prefetch_target():
    return PlainTextResponse("prefetch target\n")


@app.get("/browser/worker.js")
async def browser_worker_script():
    return Response(
        content='self.postMessage("worker ready");',
        media_type="application/javascript",
    )


@app.websocket("/browser/socket")
async def browser_socket(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text("fixture")
    await websocket.close()


# --- Stage 8: Consequential action fixtures ---


@app.post("/action/echo-post")
async def action_echo_post(request: Request):
    """Echo the POST body back as JSON. Used for Stage 8 consequential action tests."""
    body = await request.json()
    return {"echoed": body, "method": "POST", "path": "/action/echo-post"}
