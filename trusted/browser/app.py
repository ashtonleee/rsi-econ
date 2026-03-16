from contextlib import asynccontextmanager
import asyncio
import base64
import hashlib
import httpx
import os
from typing import Any
from urllib.parse import urljoin, urlsplit

from fastapi import FastAPI, HTTPException

from shared.config import browser_settings
from shared.schemas import (
    BrowserFollowHrefInternalResponse,
    BrowserFollowHrefRequest,
    BrowserFollowLink,
    BrowserRenderInternalResponse,
    BrowserRenderRequest,
    EgressFetchRequest,
    EgressFetchResponse,
    HealthReport,
)
from trusted.browser.policy import (
    browser_channel_violation,
    classify_browser_channel,
    download_violation,
    filechooser_violation,
    popup_violation,
    select_followable_link,
    top_level_navigation_violation,
    validate_browser_target,
)
from trusted.web.mediation import (
    channel_disposition,
    channel_record,
)
from trusted.web.policy import (
    WebPolicy,
    WebPolicyError,
    web_policy_status_code,
)


def build_policy() -> WebPolicy:
    settings = browser_settings()
    return WebPolicy(
        allowlist_hosts=settings.allowlist_hosts,
        private_test_hosts=settings.private_test_hosts,
        max_redirects=settings.max_redirects,
        timeout_seconds=settings.timeout_seconds,
        enable_private_test_hosts=settings.enable_private_test_hosts,
    )


def browser_launch_args() -> list[str]:
    return [
        "--disable-dev-shm-usage",
    ]


def browser_launch_kwargs() -> dict[str, Any]:
    return {
        "headless": True,
        "chromium_sandbox": True,
        "args": browser_launch_args(),
    }


def _browser_status_code(reason: str) -> int:
    if reason in {"screenshot_too_large"}:
        return 413
    return web_policy_status_code(reason)


def _truncate_utf8(text: str, limit_bytes: int) -> tuple[str, int, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= limit_bytes:
        return text, len(raw), False
    truncated = raw[:limit_bytes]
    while True:
        try:
            return truncated.decode("utf-8"), len(raw), True
        except UnicodeDecodeError:
            truncated = truncated[:-1]


def _limited_text(value: str, limit_chars: int) -> str:
    value = (value or "").strip()
    return value[:limit_chars]


def _fulfill_headers(headers: dict[str, str]) -> dict[str, str]:
    blocked = {
        "connection",
        "content-length",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in blocked and not key.lower().startswith("x-rsi-")
    }


def _violation_detail(
    *,
    exc: WebPolicyError,
    normalized_url: str,
    final_url: str,
    host: str,
    redirect_chain: list[str],
    observed_hosts: list[str],
    resolved_ips: list[str],
    http_status: int | None,
    page_title: str,
    text_bytes: int,
    text_truncated: bool,
    channel_records: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "reason": exc.reason,
        "detail": exc.detail,
        "normalized_url": normalized_url,
        "final_url": final_url,
        "host": host,
        "allowlist_decision": "denied",
        "redirect_chain": redirect_chain,
        "observed_hosts": observed_hosts,
        "resolved_ips": resolved_ips,
        "http_status": http_status,
        "page_title": page_title,
        "text_bytes": text_bytes,
        "text_truncated": text_truncated,
        "screenshot_bytes": 0,
        "screenshot_sha256": "",
        "channel_records": channel_records,
    }


def _error_detail(
    *,
    reason: str,
    detail: str,
    normalized_url: str,
    final_url: str,
    host: str,
    redirect_chain: list[str],
    observed_hosts: list[str],
    resolved_ips: list[str],
    http_status: int | None,
    channel_records: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "reason": reason,
        "detail": detail,
        "normalized_url": normalized_url,
        "final_url": final_url,
        "host": host,
        "allowlist_decision": "unknown",
        "redirect_chain": redirect_chain,
        "observed_hosts": observed_hosts,
        "resolved_ips": resolved_ips,
        "http_status": http_status,
        "page_title": "",
        "text_bytes": 0,
        "text_truncated": False,
        "screenshot_bytes": 0,
        "screenshot_sha256": "",
        "channel_records": channel_records,
    }


def _browser_channel_guards_script() -> str:
    return """
(() => {
  const root = window;
  root.__RSI_BLOCKED_CHANNELS = [];
  const record = (channel, requestedUrl, reason) => {
    root.__RSI_BLOCKED_CHANNELS.push({
      channel,
      requested_url: String(requestedUrl || ""),
      reason: String(reason || channel),
    });
  };
  const reject = (channel, requestedUrl, reason) => {
    record(channel, requestedUrl, reason);
    throw new Error(reason || channel);
  };

  const originalFetch = root.fetch ? root.fetch.bind(root) : null;
  if (originalFetch) {
    root.fetch = (...args) => {
      const target = args[0] && typeof args[0] === "object" && "url" in args[0]
        ? args[0].url
        : args[0];
      record("fetch_xhr", target, "fetch_xhr_not_allowed");
      return Promise.reject(new Error("fetch_xhr_not_allowed"));
    };
  }

  const xhrOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url) {
    this.__rsiUrl = url;
    return xhrOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function() {
    reject("fetch_xhr", this.__rsiUrl || "", "fetch_xhr_not_allowed");
  };

  const formSubmit = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function() {
    reject("form_submission", this.action || "", "form_submission_not_allowed");
  };
  if (HTMLFormElement.prototype.requestSubmit) {
    HTMLFormElement.prototype.requestSubmit = function() {
      reject("form_submission", this.action || "", "form_submission_not_allowed");
    };
  }

  if (root.WebSocket) {
    const OriginalWebSocket = root.WebSocket;
    root.WebSocket = function(url) {
      reject("websocket", url, "websocket_not_allowed");
    };
    root.WebSocket.prototype = OriginalWebSocket.prototype;
  }

  if (root.EventSource) {
    const OriginalEventSource = root.EventSource;
    root.EventSource = function(url) {
      reject("eventsource", url, "eventsource_not_allowed");
    };
    root.EventSource.prototype = OriginalEventSource.prototype;
  }

  if (navigator.sendBeacon) {
    const originalSendBeacon = navigator.sendBeacon.bind(navigator);
    navigator.sendBeacon = function(url) {
      record("send_beacon", url, "send_beacon_not_allowed");
      return false;
    };
  }

  const originalWindowOpen = root.open ? root.open.bind(root) : null;
  if (originalWindowOpen) {
    root.open = function(url) {
      record("popup", url, "popup_not_allowed");
      return null;
    };
  }

  if (root.location && root.location.assign) {
    const originalAssign = root.location.assign.bind(root.location);
    root.location.assign = function(url) {
      reject("external_protocol", url, "external_protocol_not_allowed");
    };
  }
  if (root.location && root.location.replace) {
    const originalReplace = root.location.replace.bind(root.location);
    root.location.replace = function(url) {
      reject("external_protocol", url, "external_protocol_not_allowed");
    };
  }

  const appendChild = Element.prototype.appendChild;
  Element.prototype.appendChild = function(node) {
    if (node && node.tagName === "LINK") {
      const rel = String(node.rel || "").toLowerCase();
      if (rel === "prefetch" || rel === "preconnect") {
        record("prefetch_preconnect", node.href || "", "prefetch_preconnect_not_allowed");
        return node;
      }
    }
    return appendChild.call(this, node);
  };

  const click = HTMLElement.prototype.click;
  HTMLElement.prototype.click = function() {
    if (this && this.tagName === "A") {
      const href = this.href || this.getAttribute("href") || "";
      if (href && !href.startsWith("http://") && !href.startsWith("https://")) {
        reject("external_protocol", href, "external_protocol_not_allowed");
      }
    }
    if (this && this.tagName === "INPUT" && String(this.type || "").toLowerCase() === "file") {
      reject("upload", "", "upload_not_allowed");
    }
    return click.call(this);
  };
  if (root.HTMLInputElement && HTMLInputElement.prototype.showPicker) {
    const showPicker = HTMLInputElement.prototype.showPicker;
    HTMLInputElement.prototype.showPicker = function() {
      if (this && String(this.type || "").toLowerCase() === "file") {
        reject("upload", "", "upload_not_allowed");
      }
      return showPicker.call(this);
    };
  }

  if (root.Worker) {
    const OriginalWorker = root.Worker;
    root.Worker = function(url) {
      reject("worker", url, "worker_not_allowed");
    };
    root.Worker.prototype = OriginalWorker.prototype;
  }
  if (root.SharedWorker) {
    const OriginalSharedWorker = root.SharedWorker;
    root.SharedWorker = function(url) {
      reject("worker", url, "worker_not_allowed");
    };
    root.SharedWorker.prototype = OriginalSharedWorker.prototype;
  }
  if (navigator.serviceWorker && navigator.serviceWorker.register) {
    const originalRegister = navigator.serviceWorker.register.bind(navigator.serviceWorker);
    navigator.serviceWorker.register = function(url) {
      reject("worker", url, "worker_not_allowed");
    };
  }
})();
""".strip()


async def _extract_js_channel_events(page) -> list[dict[str, Any]]:
    return await page.evaluate(
        "() => Array.isArray(window.__RSI_BLOCKED_CHANNELS) ? window.__RSI_BLOCKED_CHANNELS : []"
    )


def _plain_channel_records(records: list[Any]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for record in records:
        if hasattr(record, "model_dump"):
            payload.append(record.model_dump())
        else:
            payload.append(dict(record))
    return payload


async def _extract_meta_description(page) -> str:
    return _limited_text(
        await page.evaluate(
            "() => {"
            "  const el = document.querySelector('meta[name=\"description\"]');"
            "  return el ? (el.getAttribute('content') || '') : '';"
            "}"
        ),
        512,
    )


async def _extract_rendered_text(page, *, limit_bytes: int) -> tuple[str, str, int, bool]:
    raw_text = await page.evaluate("() => document.body ? document.body.innerText || '' : ''")
    text, text_bytes, truncated = _truncate_utf8(raw_text, limit_bytes)
    return text, hashlib.sha256(raw_text.encode("utf-8")).hexdigest(), text_bytes, truncated


async def _extract_followable_links(
    page,
    *,
    base_url: str,
    policy: WebPolicy,
    max_links: int,
) -> list[BrowserFollowLink]:
    base_target = validate_browser_target(base_url, policy)
    raw_links = await page.evaluate(
        "() => Array.from(document.querySelectorAll('a[href]')).map((anchor) => ({"
        "  href: anchor.getAttribute('href') || '',"
        "  text: (anchor.innerText || anchor.textContent || '').trim(),"
        "}));"
    )
    seen: set[str] = set()
    followable_links: list[BrowserFollowLink] = []
    for entry in raw_links:
        href = str(entry.get("href", "")).strip()
        if not href:
            continue
        try:
            target = validate_browser_target(urljoin(base_url, href), policy)
        except WebPolicyError:
            continue
        if target.normalized_url in seen:
            continue
        seen.add(target.normalized_url)
        label = _limited_text(str(entry.get("text", "")).strip(), 120) or target.normalized_url
        followable_links.append(
            BrowserFollowLink(
                text=label,
                target_url=target.normalized_url,
                same_origin=(
                    target.scheme == base_target.scheme
                    and target.host == base_target.host
                    and target.port == base_target.port
                ),
            )
        )
        if len(followable_links) >= max_links:
            break
    return followable_links


async def _preflight_navigation(
    url: str,
    *,
    policy: WebPolicy,
) -> tuple[Any, list[str], set[str], set[str], list[dict[str, Any]]]:
    current_target = validate_browser_target(url, policy)
    current_channel = "top_level_navigation"
    redirect_chain: list[str] = []
    observed_hosts = {current_target.host}
    resolved_ips: set[str] = set()
    channel_records: list[dict[str, Any]] = []

    for _ in range(policy.max_redirects + 1):
        try:
            response = await app.state.egress_client.post(
                "/internal/fetch",
                json=EgressFetchRequest(
                    url=current_target.normalized_url,
                    channel=current_channel,
                    headers={},
                    max_body_bytes=1,
                ).model_dump(),
            )
            response.raise_for_status()
            egress = EgressFetchResponse.model_validate(response.json())
        except httpx.HTTPStatusError as exc:
            detail = exc.response.json()["detail"]
            observed_hosts.add(detail.get("host", current_target.host))
            resolved_ips.update(detail.get("approved_ips", []))
            denied = channel_record(
                channel=current_channel,
                requested_url=current_target.normalized_url,
                disposition="denied",
                reason=detail.get("reason", "egress_denied"),
                top_level=current_channel in {"top_level_navigation", "redirect"},
                navigation=True,
            )
            denied.update(
                {
                    "normalized_url": detail.get("normalized_url", current_target.normalized_url),
                    "host": detail.get("host", current_target.host),
                    "approved_ips": list(detail.get("approved_ips", [])),
                    "actual_peer_ip": detail.get("actual_peer_ip"),
                    "dialed_ip": detail.get("dialed_ip"),
                    "enforcement_stage": detail.get("enforcement_stage", "unknown"),
                    "request_forwarded": bool(detail.get("request_forwarded", False)),
                }
            )
            channel_records.append(denied)
            status_code = (
                403
                if detail.get("reason") in {"connect_failed", "peer_binding_mismatch", "peer_binding_missing"}
                else exc.response.status_code
            )
            raise HTTPException(
                status_code=status_code,
                detail=_error_detail(
                    reason=detail.get("reason", "egress_denied"),
                    detail=detail.get("detail", detail.get("reason", "egress_denied")),
                    normalized_url=current_target.normalized_url,
                    final_url=current_target.normalized_url,
                    host=current_target.host,
                    redirect_chain=list(redirect_chain),
                    observed_hosts=sorted(observed_hosts),
                    resolved_ips=sorted(resolved_ips),
                    http_status=detail.get("http_status"),
                    channel_records=list(channel_records),
                ),
            ) from exc

        allowed = channel_record(
            channel=current_channel,
            requested_url=current_target.normalized_url,
            disposition="allowed",
            reason="pre_connect_pinned",
            top_level=current_channel in {"top_level_navigation", "redirect"},
            navigation=True,
        )
        allowed.update(
            {
                "normalized_url": egress.normalized_url,
                "host": egress.host,
                "approved_ips": list(egress.approved_ips),
                "actual_peer_ip": egress.actual_peer_ip,
                "dialed_ip": egress.dialed_ip,
                "enforcement_stage": egress.enforcement_stage,
                "request_forwarded": egress.request_forwarded,
                "reason": "redirect_hop_allowed"
                if egress.http_status in {301, 302, 303, 307, 308}
                else "pre_connect_pinned",
            }
        )
        channel_records.append(allowed)
        observed_hosts.add(egress.host)
        resolved_ips.update(egress.approved_ips)

        if egress.http_status not in {301, 302, 303, 307, 308}:
            return current_target, redirect_chain, observed_hosts, resolved_ips, channel_records

        location = egress.headers.get("location", "").strip()
        if not location:
            raise HTTPException(
                status_code=400,
                detail=_error_detail(
                    reason="redirect_missing_location",
                    detail=current_target.normalized_url,
                    normalized_url=current_target.normalized_url,
                    final_url=current_target.normalized_url,
                    host=current_target.host,
                    redirect_chain=list(redirect_chain),
                    observed_hosts=sorted(observed_hosts),
                    resolved_ips=sorted(resolved_ips),
                    http_status=egress.http_status,
                    channel_records=list(channel_records),
                ),
            )

        try:
            next_target = validate_browser_target(urljoin(current_target.normalized_url, location), policy)
        except WebPolicyError as exc:
            denied = channel_record(
                channel="redirect",
                requested_url=urljoin(current_target.normalized_url, location),
                disposition="denied",
                reason=exc.reason,
                top_level=True,
                navigation=True,
            )
            channel_records.append(denied)
            raise HTTPException(
                status_code=_browser_status_code(exc.reason),
                detail=_violation_detail(
                    exc=exc,
                    normalized_url=current_target.normalized_url,
                    final_url=current_target.normalized_url,
                    host=current_target.host,
                    redirect_chain=list(redirect_chain),
                    observed_hosts=sorted(observed_hosts),
                    resolved_ips=sorted(resolved_ips),
                    http_status=egress.http_status,
                    page_title="",
                    text_bytes=0,
                    text_truncated=False,
                    channel_records=list(channel_records),
                ),
            ) from exc

        redirect_chain.append(next_target.normalized_url)
        observed_hosts.add(next_target.host)
        current_target = next_target
        current_channel = "redirect"

    raise HTTPException(
        status_code=403,
        detail=_error_detail(
            reason="too_many_redirects",
            detail=url.strip(),
            normalized_url=url.strip(),
            final_url=url.strip(),
            host=urlsplit(url).hostname or "",
            redirect_chain=list(redirect_chain),
            observed_hosts=sorted(observed_hosts),
            resolved_ips=sorted(resolved_ips),
            http_status=None,
            channel_records=list(channel_records),
        ),
    )


async def _render_page(
    url: str,
    *,
    strict_top_level_after_load: bool = False,
    include_followable_links: bool = True,
) -> BrowserRenderInternalResponse:
    settings = app.state.settings
    policy = app.state.policy
    try:
        target = validate_browser_target(url, policy)
    except WebPolicyError as exc:
        raise HTTPException(
            status_code=_browser_status_code(exc.reason),
            detail={
                "reason": exc.reason,
                "detail": exc.detail,
                "normalized_url": url.strip(),
                "final_url": url.strip(),
                "host": "",
                "allowlist_decision": "denied",
                "redirect_chain": [],
                "observed_hosts": [],
                "resolved_ips": [],
                "http_status": None,
                "page_title": "",
                "meta_description": "",
                "rendered_text_sha256": "",
                "text_bytes": 0,
                "text_truncated": False,
                "screenshot_sha256": "",
                "screenshot_bytes": 0,
                "channel_records": [],
            },
        ) from exc

    target, redirect_chain, observed_hosts, resolved_ips, channel_records = await _preflight_navigation(
        target.normalized_url,
        policy=policy,
    )
    violation: WebPolicyError | None = None
    http_status: int | None = None
    page_title = ""
    text_bytes = 0
    text_truncated = False
    event_tasks: list[asyncio.Task] = []
    locked_main_url: str | None = None
    top_level_started = False

    browser = app.state.browser
    context = await browser.new_context(
        viewport={
            "width": settings.viewport_width,
            "height": settings.viewport_height,
        },
        accept_downloads=False,
        service_workers="block",
    )
    page = await context.new_page()
    await page.add_init_script(_browser_channel_guards_script())

    async def record_violation(exc: WebPolicyError):
        nonlocal violation
        if violation is None:
            violation = exc

    async def handle_route(route):
        nonlocal locked_main_url, top_level_started
        request = route.request
        request_url = request.url
        channel = classify_browser_channel(
            resource_type=request.resource_type,
            is_navigation_request=request.is_navigation_request(),
            is_main_frame=request.frame == page.main_frame,
            headers=dict(request.headers),
            top_level_started=top_level_started,
        )
        is_top_level = request.is_navigation_request() and request.frame == page.main_frame
        is_navigation = request.is_navigation_request()

        if is_top_level and locked_main_url is not None:
            exc = top_level_navigation_violation(request_url)
            channel_records.append(
                channel_record(
                    channel="top_level_navigation",
                    requested_url=request_url,
                    disposition="denied",
                    reason=exc.reason,
                    top_level=True,
                    navigation=True,
                )
            )
            await record_violation(exc)
            await route.abort("blockedbyclient")
            return

        try:
            normalized = validate_browser_target(request_url, policy)
        except WebPolicyError as exc:
            channel_records.append(
                channel_record(
                    channel=channel,
                    requested_url=request_url,
                    disposition="denied",
                    reason=exc.reason,
                    top_level=is_top_level,
                    navigation=is_navigation,
                )
            )
            await record_violation(exc)
            await route.abort("blockedbyclient")
            return

        if channel not in {"top_level_navigation", "redirect"} and channel_disposition(channel):
            exc = browser_channel_violation(channel, request_url)
            channel_records.append(
                channel_record(
                    channel=channel,
                    requested_url=request_url,
                    disposition="denied",
                    reason=exc.reason,
                    top_level=is_top_level,
                    navigation=is_navigation,
                )
            )
            await record_violation(exc)
            await route.abort("blockedbyclient")
            return

        observed_hosts.add(normalized.host)
        if channel == "redirect":
            if len(redirect_chain) >= policy.max_redirects:
                exc = WebPolicyError("too_many_redirects", normalized.normalized_url)
                channel_records.append(
                    channel_record(
                        channel=channel,
                        requested_url=request_url,
                        disposition="denied",
                        reason=exc.reason,
                        top_level=is_top_level,
                        navigation=is_navigation,
                    )
                )
                await record_violation(exc)
                await route.abort("blockedbyclient")
                return
            if normalized.normalized_url not in redirect_chain:
                redirect_chain.append(normalized.normalized_url)
        if is_top_level:
            top_level_started = True

        record = channel_record(
            channel=channel,
            requested_url=request_url,
            disposition="allowed",
            reason="pre_connect_pending",
            top_level=is_top_level,
            navigation=is_navigation,
        )
        try:
            response = await app.state.egress_client.post(
                "/internal/fetch",
                json=EgressFetchRequest(
                    url=normalized.normalized_url,
                    channel=channel,
                    headers=dict(request.headers),
                    max_body_bytes=2 * 1024 * 1024,
                ).model_dump(),
            )
            response.raise_for_status()
            egress = EgressFetchResponse.model_validate(response.json())
        except httpx.HTTPStatusError as exc:
            detail = exc.response.json()["detail"]
            record.update(
                {
                    "normalized_url": detail.get("normalized_url", normalized.normalized_url),
                    "host": detail.get("host", normalized.host),
                    "approved_ips": list(detail.get("approved_ips", [])),
                    "actual_peer_ip": detail.get("actual_peer_ip"),
                    "dialed_ip": detail.get("dialed_ip"),
                    "disposition": "denied",
                    "reason": detail.get("reason", "egress_denied"),
                    "enforcement_stage": detail.get("enforcement_stage", "unknown"),
                    "request_forwarded": bool(detail.get("request_forwarded", False)),
                }
            )
            observed_hosts.add(detail.get("host", normalized.host))
            resolved_ips.update(detail.get("approved_ips", []))
            channel_records.append(record)
            await record_violation(
                WebPolicyError(
                    detail.get("reason", "egress_denied"),
                    detail.get("detail", detail.get("reason", "egress_denied")),
                )
            )
            await route.fulfill(
                status=exc.response.status_code,
                headers={"content-type": "text/plain; charset=utf-8"},
                body=detail.get("reason", "egress_denied"),
            )
            return

        record.update(
            {
                "normalized_url": egress.normalized_url,
                "host": egress.host,
                "approved_ips": list(egress.approved_ips),
                "actual_peer_ip": egress.actual_peer_ip,
                "dialed_ip": egress.dialed_ip,
                "reason": "pre_connect_pinned",
                "enforcement_stage": egress.enforcement_stage,
                "request_forwarded": egress.request_forwarded,
            }
        )
        if egress.http_status in {301, 302, 303, 307, 308}:
            location = egress.headers.get("location", "").strip()
            if location:
                redirect_chain.append(urljoin(normalized.normalized_url, location))
            exc = top_level_navigation_violation(location or normalized.normalized_url)
            record.update(
                {
                    "disposition": "denied",
                    "reason": exc.reason,
                }
            )
            channel_records.append(record)
            await record_violation(exc)
            await route.fulfill(
                status=403,
                headers={"content-type": "text/plain; charset=utf-8"},
                body=exc.reason,
            )
            return
        observed_hosts.add(egress.host)
        resolved_ips.update(egress.approved_ips)
        channel_records.append(record)
        await route.fulfill(
            status=egress.http_status,
            headers=_fulfill_headers(egress.headers),
            body=base64.b64decode(egress.body_base64),
        )

    async def handle_popup(popup):
        exc = popup_violation(popup.url or page.url)
        channel_records.append(
            channel_record(
                channel="popup",
                requested_url=popup.url or page.url,
                disposition="denied",
                reason=exc.reason,
            )
        )
        await record_violation(exc)
        try:
            await popup.close()
        except Exception:
            pass

    async def handle_download(download):
        exc = download_violation(
            page.url or target.normalized_url,
            suggested_filename=getattr(download, "suggested_filename", None),
        )
        channel_records.append(
            channel_record(
                channel="download",
                requested_url=page.url or target.normalized_url,
                disposition="denied",
                reason=exc.reason,
            )
        )
        await record_violation(exc)
        try:
            await download.cancel()
        except Exception:
            pass

    async def handle_filechooser(file_chooser):
        exc = filechooser_violation(file_chooser.page.url or page.url)
        channel_records.append(
            channel_record(
                channel="upload",
                requested_url=file_chooser.page.url or page.url,
                disposition="denied",
                reason=exc.reason,
            )
        )
        await record_violation(exc)

    page.on(
        "popup",
        lambda popup: event_tasks.append(asyncio.create_task(handle_popup(popup))),
    )
    page.on(
        "download",
        lambda download: event_tasks.append(asyncio.create_task(handle_download(download))),
    )
    page.on(
        "filechooser",
        lambda chooser: event_tasks.append(asyncio.create_task(handle_filechooser(chooser))),
    )
    await page.route("**/*", handle_route)

    try:
        response = await page.goto(
            target.normalized_url,
            wait_until="domcontentloaded",
            timeout=int(settings.timeout_seconds * 1000),
        )
        if response is not None:
            http_status = response.status
        if strict_top_level_after_load:
            locked_target = validate_browser_target(
                page.url or target.normalized_url,
                policy=policy,
            )
            locked_main_url = locked_target.normalized_url
            observed_hosts.add(locked_target.host)
        await page.wait_for_timeout(settings.settle_time_ms)
        if event_tasks:
            await asyncio.gather(*event_tasks, return_exceptions=True)

        for js_event in await _extract_js_channel_events(page):
            requested_url = str(js_event.get("requested_url", "")).strip()
            reason = str(js_event.get("reason", "browser_channel_not_allowed"))
            channel = str(js_event.get("channel", "subresource"))
            channel_records.append(
                channel_record(
                    channel=channel,
                    requested_url=requested_url,
                    disposition="denied",
                    reason=reason,
                )
            )
            if violation is None:
                await record_violation(browser_channel_violation(channel, requested_url or reason))

        if violation is not None:
            raise violation

        final_url = page.url or target.normalized_url
        final_target = validate_browser_target(
            final_url,
            policy=policy,
        )
        observed_hosts.add(final_target.host)

        page_title = _limited_text(await page.title(), 256)
        meta_description = await _extract_meta_description(page)
        followable_links: list[BrowserFollowLink] = []
        if include_followable_links:
            followable_links = await _extract_followable_links(
                page,
                base_url=final_target.normalized_url,
                policy=policy,
                max_links=settings.max_followable_links,
            )
        rendered_text, rendered_text_sha256, text_bytes, text_truncated = await _extract_rendered_text(
            page,
            limit_bytes=settings.max_rendered_text_bytes,
        )
        screenshot = await page.screenshot(type="png")
        if len(screenshot) > settings.max_screenshot_bytes:
            raise WebPolicyError(
                "screenshot_too_large",
                f"{len(screenshot)} > {settings.max_screenshot_bytes}",
            )
        screenshot_sha256 = hashlib.sha256(screenshot).hexdigest()
        return BrowserRenderInternalResponse(
            normalized_url=target.normalized_url,
            final_url=final_target.normalized_url,
            http_status=http_status,
            page_title=page_title,
            meta_description=meta_description,
            rendered_text=rendered_text,
            rendered_text_sha256=rendered_text_sha256,
            text_bytes=text_bytes,
            text_truncated=text_truncated,
            screenshot_png_base64=base64.b64encode(screenshot).decode("ascii"),
            screenshot_sha256=screenshot_sha256,
            screenshot_bytes=len(screenshot),
            redirect_chain=list(redirect_chain),
            observed_hosts=sorted(observed_hosts),
            resolved_ips=sorted(resolved_ips),
            channel_records=list(channel_records),
            followable_links=followable_links,
        )
    except WebPolicyError as exc:
        raise HTTPException(
            status_code=_browser_status_code(exc.reason),
            detail=_violation_detail(
                exc=exc,
                normalized_url=target.normalized_url,
                final_url=page.url or target.normalized_url,
                host=target.host,
                redirect_chain=list(redirect_chain),
                observed_hosts=sorted(observed_hosts),
                resolved_ips=sorted(resolved_ips),
                http_status=http_status,
                page_title=page_title,
                text_bytes=text_bytes,
                text_truncated=text_truncated,
                channel_records=list(channel_records),
            ),
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        if violation is not None:
            raise HTTPException(
                status_code=_browser_status_code(violation.reason),
                detail=_violation_detail(
                    exc=violation,
                    normalized_url=target.normalized_url,
                    final_url=page.url or target.normalized_url,
                    host=target.host,
                    redirect_chain=list(redirect_chain),
                    observed_hosts=sorted(observed_hosts),
                    resolved_ips=sorted(resolved_ips),
                    http_status=http_status,
                    page_title=page_title,
                    text_bytes=text_bytes,
                    text_truncated=text_truncated,
                    channel_records=list(channel_records),
                ),
            ) from exc
        raise HTTPException(
            status_code=502,
            detail=_error_detail(
                reason=type(exc).__name__,
                detail=str(exc),
                normalized_url=target.normalized_url,
                final_url=page.url or target.normalized_url,
                host=target.host,
                redirect_chain=list(redirect_chain),
                observed_hosts=sorted(observed_hosts),
                resolved_ips=sorted(resolved_ips),
                http_status=http_status,
                channel_records=list(channel_records),
            ),
        ) from exc
    finally:
        await context.close()


def _follow_detail(
    *,
    source_render: BrowserRenderInternalResponse,
    requested_target_url: str,
    matched_link_text: str,
    detail: dict[str, Any],
) -> dict[str, Any]:
    normalized_target = detail.get("normalized_url", requested_target_url)
    final_url = detail.get("final_url", normalized_target)
    navigation_history = [source_render.final_url, requested_target_url]
    if final_url and final_url not in navigation_history:
        navigation_history.append(final_url)
    source_channel_records = _plain_channel_records(list(source_render.channel_records))
    target_channel_records = _plain_channel_records(list(detail.get("channel_records", [])))
    return {
        "source_url": source_render.normalized_url,
        "source_final_url": source_render.final_url,
        "requested_target_url": requested_target_url,
        "matched_link_text": matched_link_text,
        "follow_hop_count": 1,
        "navigation_history": navigation_history,
        "normalized_url": normalized_target,
        "final_url": final_url,
        "host": detail.get("host", urlsplit(normalized_target).hostname or ""),
        "allowlist_decision": detail.get("allowlist_decision", "denied"),
        "redirect_chain": list(detail.get("redirect_chain", [])),
        "observed_hosts": list(detail.get("observed_hosts", [])),
        "resolved_ips": list(detail.get("resolved_ips", [])),
        "http_status": detail.get("http_status"),
        "page_title": detail.get("page_title", ""),
        "meta_description": detail.get("meta_description", ""),
        "rendered_text_sha256": detail.get("rendered_text_sha256", ""),
        "text_bytes": int(detail.get("text_bytes", 0)),
        "text_truncated": bool(detail.get("text_truncated", False)),
        "screenshot_sha256": detail.get("screenshot_sha256", ""),
        "screenshot_bytes": int(detail.get("screenshot_bytes", 0)),
        "channel_records": source_channel_records + target_channel_records,
        "reason": detail.get("reason", detail.get("detail", "browser_follow_href_failed")),
    }


async def execute_render(url: str) -> BrowserRenderInternalResponse:
    return await _render_page(url, strict_top_level_after_load=True, include_followable_links=True)


async def execute_follow_href(
    source_url: str,
    target_url: str,
) -> BrowserFollowHrefInternalResponse:
    policy = app.state.policy
    try:
        requested_target = validate_browser_target(target_url, policy)
    except WebPolicyError as exc:
        raise HTTPException(
            status_code=_browser_status_code(exc.reason),
            detail={
                "source_url": source_url,
                "source_final_url": source_url,
                "requested_target_url": target_url,
                "matched_link_text": "",
                "follow_hop_count": 1,
                "navigation_history": [source_url],
                "normalized_url": target_url,
                "final_url": target_url,
                "host": "",
                "allowlist_decision": "denied",
                "redirect_chain": [],
                "observed_hosts": [],
                "resolved_ips": [],
                "http_status": None,
                "page_title": "",
                "meta_description": "",
                "rendered_text_sha256": "",
                "text_bytes": 0,
                "text_truncated": False,
                "screenshot_sha256": "",
                "screenshot_bytes": 0,
                "channel_records": [],
                "reason": exc.reason,
                "detail": exc.detail,
            },
        ) from exc
    source_render = await execute_render(source_url)
    try:
        matched_link = select_followable_link(
            requested_target.normalized_url,
            source_render.followable_links,
        )
    except WebPolicyError as exc:
        raise HTTPException(
            status_code=_browser_status_code(exc.reason),
            detail=_follow_detail(
                source_render=source_render,
                requested_target_url=requested_target.normalized_url,
                matched_link_text="",
                detail={
                    "normalized_url": requested_target.normalized_url,
                    "final_url": requested_target.normalized_url,
                    "host": requested_target.host,
                    "allowlist_decision": "denied",
                    "redirect_chain": [],
                    "observed_hosts": list(source_render.observed_hosts),
                    "resolved_ips": list(source_render.resolved_ips),
                    "http_status": None,
                    "page_title": "",
                    "meta_description": "",
                    "rendered_text_sha256": "",
                    "text_bytes": 0,
                    "text_truncated": False,
                    "screenshot_sha256": "",
                    "screenshot_bytes": 0,
                    "channel_records": _plain_channel_records(list(source_render.channel_records)),
                    "reason": exc.reason,
                    "detail": exc.detail,
                },
            ),
        ) from exc

    try:
        target_render = await _render_page(
            matched_link.target_url,
            strict_top_level_after_load=True,
            include_followable_links=False,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"reason": str(exc.detail)}
        raise HTTPException(
            status_code=exc.status_code,
            detail=_follow_detail(
                source_render=source_render,
                requested_target_url=matched_link.target_url,
                matched_link_text=matched_link.text,
                detail=detail,
            ),
        ) from exc

    navigation_history = [source_render.final_url, matched_link.target_url]
    if target_render.final_url not in navigation_history:
        navigation_history.append(target_render.final_url)

    return BrowserFollowHrefInternalResponse(
        source_url=source_render.normalized_url,
        source_final_url=source_render.final_url,
        requested_target_url=matched_link.target_url,
        matched_link_text=matched_link.text,
        follow_hop_count=1,
        navigation_history=navigation_history,
        normalized_url=target_render.normalized_url,
        final_url=target_render.final_url,
        http_status=target_render.http_status,
        page_title=target_render.page_title,
        meta_description=target_render.meta_description,
        rendered_text=target_render.rendered_text,
        rendered_text_sha256=target_render.rendered_text_sha256,
        text_bytes=target_render.text_bytes,
        text_truncated=target_render.text_truncated,
        screenshot_png_base64=target_render.screenshot_png_base64,
        screenshot_sha256=target_render.screenshot_sha256,
        screenshot_bytes=target_render.screenshot_bytes,
        redirect_chain=list(target_render.redirect_chain),
        observed_hosts=sorted(
            set(source_render.observed_hosts) | set(target_render.observed_hosts)
        ),
        resolved_ips=sorted(set(source_render.resolved_ips) | set(target_render.resolved_ips)),
        channel_records=_plain_channel_records(list(source_render.channel_records))
        + _plain_channel_records(list(target_render.channel_records)),
    )


def startup_checks(app: FastAPI):
    settings = browser_settings()
    app.state.settings = settings
    app.state.policy = build_policy()


async def _launch_browser_runtime():
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(**browser_launch_kwargs())
    return playwright, browser


@asynccontextmanager
async def lifespan(app: FastAPI):
    startup_checks(app)
    playwright, browser = await _launch_browser_runtime()
    egress_client = httpx.AsyncClient(
        base_url=app.state.settings.egress_url,
        timeout=app.state.settings.timeout_seconds + 1.0,
        trust_env=False,
    )
    app.state.playwright = playwright
    app.state.browser = browser
    app.state.egress_client = egress_client
    try:
        yield
    finally:
        await egress_client.aclose()
        await browser.close()
        await playwright.stop()


app = FastAPI(title="trusted-browser", lifespan=lifespan)


@app.get("/healthz", response_model=HealthReport)
async def healthz() -> HealthReport:
    settings = app.state.settings
    launch_kwargs = browser_launch_kwargs()
    return HealthReport(
        service=settings.service_name,
        status="ok",
        stage=settings.stage,
        details={
            "allowlist_hosts": list(settings.allowlist_hosts),
            "private_test_hosts": list(settings.private_test_hosts),
            "max_redirects": settings.max_redirects,
            "timeout_seconds": settings.timeout_seconds,
            "viewport_width": settings.viewport_width,
            "viewport_height": settings.viewport_height,
            "settle_time_ms": settings.settle_time_ms,
            "max_rendered_text_bytes": settings.max_rendered_text_bytes,
            "max_screenshot_bytes": settings.max_screenshot_bytes,
            "max_followable_links": settings.max_followable_links,
            "max_follow_hops": settings.max_follow_hops,
            "egress_url": settings.egress_url,
            "running_as_root": os.geteuid() == 0,
            "chromium_sandbox": bool(launch_kwargs["chromium_sandbox"]),
            "launch_args": list(launch_kwargs["args"]),
        },
    )


@app.post("/internal/render", response_model=BrowserRenderInternalResponse)
async def render(payload: BrowserRenderRequest) -> BrowserRenderInternalResponse:
    return await execute_render(payload.url)


@app.post("/internal/follow-href", response_model=BrowserFollowHrefInternalResponse)
async def follow_href(payload: BrowserFollowHrefRequest) -> BrowserFollowHrefInternalResponse:
    return await execute_follow_href(payload.source_url, payload.target_url)
