from contextlib import asynccontextmanager
import base64
import hashlib
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException

from shared.config import fetcher_settings
from shared.schemas import (
    EgressFetchRequest,
    EgressFetchResponse,
    FetcherFetchResponse,
    HealthReport,
    WebFetchRequest,
)
from trusted.web.policy import (
    NormalizedWebTarget,
    WebPolicyError,
    WebPolicy,
    normalize_web_redirect_target,
    normalize_web_target,
    web_policy_status_code,
)


def build_policy() -> WebPolicy:
    settings = fetcher_settings()
    return WebPolicy(
        allowlist_hosts=settings.allowlist_hosts,
        private_test_hosts=settings.private_test_hosts,
        max_redirects=settings.max_redirects,
        timeout_seconds=settings.timeout_seconds,
        allowed_content_types=settings.allowed_content_types,
        max_response_bytes=settings.max_response_bytes,
        max_preview_chars=settings.max_preview_chars,
        user_agent=settings.user_agent,
        enable_private_test_hosts=settings.enable_private_test_hosts,
    )


def _content_type(raw_content_type: str) -> str:
    return raw_content_type.split(";", 1)[0].strip().lower()


def _decode_text(raw: bytes, *, content_type: str, max_preview_chars: int) -> str:
    charset = "utf-8"
    if "charset=" in content_type.lower():
        charset = content_type.lower().split("charset=", 1)[1].split(";", 1)[0].strip()
    text = raw.decode(charset or "utf-8", errors="replace")
    return text[:max_preview_chars]


def _content_type_allowed(content_type: str, *, allowed_content_types: tuple[str, ...]) -> bool:
    value = content_type.split(";", 1)[0].strip().lower()
    return value in allowed_content_types


async def _read_limited_body(response: httpx.Response, limit: int) -> tuple[bytes, bool]:
    data = bytearray()
    truncated = False
    async for chunk in response.aiter_bytes():
        remaining = limit + 1 - len(data)
        if remaining <= 0:
            truncated = True
            break
        data.extend(chunk[:remaining])
        if len(data) > limit:
            truncated = True
            break
    return bytes(data[:limit]), truncated or len(data) > limit


def _mediation_hop(
    target: NormalizedWebTarget,
    *,
    channel: str,
    approved_ips: list[str] | tuple[str, ...],
    disposition: str,
    reason: str,
    actual_peer_ip: str | None,
    dialed_ip: str | None,
    http_status: int | None,
    enforcement_stage: str = "unknown",
    request_forwarded: bool = False,
) -> dict[str, Any]:
    return {
        "channel": channel,
        "requested_url": target.original_url,
        "normalized_url": target.normalized_url,
        "host": target.host,
        "approved_ips": list(approved_ips),
        "actual_peer_ip": actual_peer_ip,
        "dialed_ip": dialed_ip,
        "disposition": disposition,
        "reason": reason,
        "http_status": http_status,
        "enforcement_stage": enforcement_stage,
        "request_forwarded": request_forwarded,
    }


async def execute_fetch(url: str) -> FetcherFetchResponse:
    policy = app.state.policy
    settings = app.state.settings
    try:
        current_target = normalize_web_target(url, policy)
    except WebPolicyError as exc:
        raise HTTPException(
            status_code=web_policy_status_code(exc.reason),
            detail={
                "reason": exc.reason,
                "detail": exc.detail,
                "normalized_url": url.strip(),
                "scheme": "",
                "host": "",
                "port": 0,
                "approved_ips": [],
                "actual_peer_ip": None,
                "used_ip": None,
                "mediation_hops": [],
                "redirect_chain": [],
            },
        ) from exc
    redirect_chain: list[str] = []
    mediation_hops: list[dict[str, Any]] = []
    current_channel = "top_level_navigation"

    async with httpx.AsyncClient(
        base_url=settings.egress_url,
        timeout=policy.timeout_seconds + 1.0,
        trust_env=False,
    ) as client:
        for _ in range(policy.max_redirects + 1):
            headers = {
                "User-Agent": settings.user_agent,
                "Accept": ", ".join(settings.allowed_content_types),
            }
            try:
                response = await client.post(
                    "/internal/fetch",
                    json=EgressFetchRequest(
                        url=current_target.normalized_url,
                        channel=current_channel,
                        headers=headers,
                        max_body_bytes=settings.max_response_bytes + 1,
                    ).model_dump(),
                )
                response.raise_for_status()
                egress = EgressFetchResponse.model_validate(response.json())
                actual_peer_ip = egress.actual_peer_ip
                status = egress.http_status
                raw_content_type = egress.headers.get("content-type", "")
                body = base64.b64decode(egress.body_base64)
                if status in {301, 302, 303, 307, 308}:
                    location = egress.headers.get("location", "").strip()
                    if not location:
                        raise WebPolicyError(
                            "redirect_missing_location",
                            current.target.normalized_url,
                        )
                    mediation_hops.append(
                        _mediation_hop(
                            current_target,
                            channel=current_channel,
                            approved_ips=egress.approved_ips,
                            disposition="allowed",
                            reason="redirect_hop_allowed",
                            actual_peer_ip=actual_peer_ip,
                            dialed_ip=egress.dialed_ip,
                            http_status=status,
                            enforcement_stage=egress.enforcement_stage,
                            request_forwarded=egress.request_forwarded,
                        )
                    )
                    current_target = normalize_web_redirect_target(
                        location,
                        current_url=current_target.normalized_url,
                        policy=policy,
                    )
                    current_channel = "redirect"
                    redirect_chain.append(current_target.normalized_url)
                    continue
                mediation_hops.append(
                    _mediation_hop(
                        current_target,
                        channel=current_channel,
                        approved_ips=egress.approved_ips,
                        disposition="allowed",
                        reason="pre_connect_pinned",
                        actual_peer_ip=actual_peer_ip,
                        dialed_ip=egress.dialed_ip,
                        http_status=status,
                        enforcement_stage=egress.enforcement_stage,
                        request_forwarded=egress.request_forwarded,
                    )
                )
                if status < 200 or status >= 300:
                    raise HTTPException(
                        status_code=502,
                        detail={
                            "reason": "upstream_http_error",
                            "http_status": status,
                            "normalized_url": current_target.normalized_url,
                            "scheme": current_target.scheme,
                            "host": current_target.host,
                            "port": current_target.port,
                            "resolved_ips": list(egress.approved_ips),
                            "approved_ips": list(egress.approved_ips),
                            "actual_peer_ip": actual_peer_ip,
                            "used_ip": egress.dialed_ip,
                            "request_forwarded": egress.request_forwarded,
                            "enforcement_stage": egress.enforcement_stage,
                            "mediation_hops": list(mediation_hops),
                            "redirect_chain": redirect_chain,
                        },
                    )
                if not _content_type_allowed(
                    raw_content_type,
                    allowed_content_types=settings.allowed_content_types,
                ):
                    raise HTTPException(
                        status_code=415,
                        detail={
                            "reason": "content_type_not_allowed",
                            "content_type": _content_type(raw_content_type),
                            "normalized_url": current_target.normalized_url,
                            "scheme": current_target.scheme,
                            "host": current_target.host,
                            "port": current_target.port,
                            "resolved_ips": list(egress.approved_ips),
                            "approved_ips": list(egress.approved_ips),
                            "actual_peer_ip": actual_peer_ip,
                            "used_ip": egress.dialed_ip,
                            "request_forwarded": egress.request_forwarded,
                            "enforcement_stage": egress.enforcement_stage,
                            "mediation_hops": list(mediation_hops),
                            "redirect_chain": redirect_chain,
                        },
                    )
                truncated = len(body) > settings.max_response_bytes
                body = body[: settings.max_response_bytes]
                decoded = _decode_text(
                    body,
                    content_type=raw_content_type,
                    max_preview_chars=settings.max_preview_chars,
                )
                return FetcherFetchResponse(
                    normalized_url=current_target.normalized_url,
                    final_url=current_target.normalized_url,
                    scheme=current_target.scheme,
                    host=current_target.host,
                    port=current_target.port,
                    http_status=status,
                    content_type=_content_type(raw_content_type),
                    byte_count=len(body),
                    truncated=truncated,
                    redirect_chain=list(redirect_chain),
                    resolved_ips=list(egress.approved_ips),
                    approved_ips=list(egress.approved_ips),
                    actual_peer_ip=actual_peer_ip,
                    used_ip=egress.dialed_ip,
                    content_sha256=hashlib.sha256(body).hexdigest(),
                    text=decoded,
                    mediation_hops=list(mediation_hops),
                )
            except httpx.HTTPStatusError as exc:
                detail = exc.response.json()["detail"]
                mediation_hops.append(
                    _mediation_hop(
                        current_target,
                        channel=current_channel,
                        approved_ips=detail.get("approved_ips", []),
                        disposition="denied",
                        reason=detail.get("reason", "egress_denied"),
                        actual_peer_ip=detail.get("actual_peer_ip"),
                        dialed_ip=detail.get("dialed_ip"),
                        http_status=detail.get("http_status"),
                        enforcement_stage=detail.get("enforcement_stage", "unknown"),
                        request_forwarded=bool(detail.get("request_forwarded", False)),
                    )
                )
                raise HTTPException(
                    status_code=exc.response.status_code,
                    detail={
                        "reason": detail.get("reason", "egress_denied"),
                        "detail": detail.get("detail", ""),
                        "normalized_url": detail.get("normalized_url", current_target.normalized_url),
                        "scheme": detail.get("scheme", current_target.scheme),
                        "host": detail.get("host", current_target.host),
                        "port": detail.get("port", current_target.port),
                        "resolved_ips": list(detail.get("approved_ips", [])),
                        "approved_ips": list(detail.get("approved_ips", [])),
                        "actual_peer_ip": detail.get("actual_peer_ip"),
                        "used_ip": detail.get("dialed_ip"),
                        "request_forwarded": bool(detail.get("request_forwarded", False)),
                        "enforcement_stage": detail.get("enforcement_stage", "unknown"),
                        "mediation_hops": list(mediation_hops),
                        "redirect_chain": redirect_chain,
                    },
                ) from exc
            except WebPolicyError as exc:
                mediation_hops.append(
                    _mediation_hop(
                        current_target,
                        channel=current_channel,
                        approved_ips=[],
                        disposition="denied",
                        reason=exc.reason,
                        actual_peer_ip=None,
                        dialed_ip=None,
                        http_status=None,
                        enforcement_stage="pre_connect",
                        request_forwarded=False,
                    )
                )
                raise HTTPException(
                    status_code=web_policy_status_code(exc.reason),
                    detail={
                        "reason": exc.reason,
                        "detail": exc.detail,
                        "normalized_url": current_target.normalized_url,
                        "scheme": current_target.scheme,
                        "host": current_target.host,
                        "port": current_target.port,
                        "approved_ips": [],
                        "actual_peer_ip": None,
                        "used_ip": None,
                        "request_forwarded": False,
                        "enforcement_stage": "pre_connect",
                        "mediation_hops": list(mediation_hops),
                        "redirect_chain": redirect_chain,
                    },
                ) from exc
            except httpx.TimeoutException as exc:
                raise HTTPException(
                    status_code=504,
                    detail={"reason": "timeout", "detail": str(exc)},
                ) from exc
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status_code=502,
                    detail={"reason": type(exc).__name__, "detail": str(exc)},
                ) from exc

    raise HTTPException(
        status_code=508,
        detail={"reason": "too_many_redirects", "redirect_chain": redirect_chain},
    )


def startup_checks(app: FastAPI):
    settings = fetcher_settings()
    app.state.settings = settings
    app.state.policy = build_policy()


@asynccontextmanager
async def lifespan(app: FastAPI):
    startup_checks(app)
    yield


app = FastAPI(title="trusted-fetcher", lifespan=lifespan)


@app.get("/healthz", response_model=HealthReport)
async def healthz() -> HealthReport:
    settings = app.state.settings
    return HealthReport(
        service=settings.service_name,
        status="ok",
        stage=settings.stage,
        details={
            "egress_url": settings.egress_url,
            "allowlist_hosts": list(settings.allowlist_hosts),
            "allowed_content_types": list(settings.allowed_content_types),
            "max_response_bytes": settings.max_response_bytes,
            "max_preview_chars": settings.max_preview_chars,
            "max_redirects": settings.max_redirects,
            "timeout_seconds": settings.timeout_seconds,
            "private_test_hosts": list(settings.private_test_hosts),
        },
    )


@app.post("/internal/fetch", response_model=FetcherFetchResponse)
async def fetch(payload: WebFetchRequest) -> FetcherFetchResponse:
    return await execute_fetch(payload.url)
