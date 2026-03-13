from contextlib import asynccontextmanager
import hashlib
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException

from shared.config import fetcher_settings
from shared.schemas import FetcherFetchResponse, HealthReport, WebFetchRequest
from trusted.fetcher.policy import (
    FetchPolicy,
    FetchPolicyError,
    content_type_allowed,
    normalize_fetch_target,
    normalize_redirect_target,
    resolve_target_ips,
    validate_resolved_ips,
)


def build_policy() -> FetchPolicy:
    settings = fetcher_settings()
    return FetchPolicy(
        allowlist_hosts=settings.allowlist_hosts,
        private_test_hosts=settings.private_test_hosts,
        allowed_content_types=settings.allowed_content_types,
        max_response_bytes=settings.max_response_bytes,
        max_preview_chars=settings.max_preview_chars,
        max_redirects=settings.max_redirects,
        timeout_seconds=settings.timeout_seconds,
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


def _used_ip(response: httpx.Response) -> str | None:
    stream = response.extensions.get("network_stream")
    if stream is None:
        return None
    server_addr = stream.get_extra_info("server_addr")
    if server_addr is None:
        return None
    if isinstance(server_addr, tuple):
        return str(server_addr[0])
    return str(server_addr)


def _policy_status_code(reason: str) -> int:
    if reason in {
        "unsupported_scheme",
        "userinfo_not_allowed",
        "fragment_not_allowed",
        "missing_hostname",
        "port_not_allowed",
        "empty_url",
    }:
        return 400
    return 403


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


async def execute_fetch(url: str) -> FetcherFetchResponse:
    policy = app.state.policy
    try:
        current = normalize_fetch_target(url, policy)
    except FetchPolicyError as exc:
        raise HTTPException(
            status_code=_policy_status_code(exc.reason),
            detail={
                "reason": exc.reason,
                "detail": exc.detail,
                "normalized_url": url.strip(),
                "scheme": "",
                "host": "",
                "port": 0,
                "redirect_chain": [],
            },
        ) from exc
    redirect_chain: list[str] = []

    async with httpx.AsyncClient(
        timeout=policy.timeout_seconds,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        for _ in range(policy.max_redirects + 1):
            resolved_ips = validate_resolved_ips(current, resolve_target_ips(current), policy)
            headers = {
                "User-Agent": policy.user_agent,
                "Accept": ", ".join(policy.allowed_content_types),
            }
            try:
                async with client.stream("GET", current.normalized_url, headers=headers) as response:
                    used_ip = _used_ip(response)
                    status = response.status_code
                    raw_content_type = response.headers.get("content-type", "")
                    if status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location", "").strip()
                        if not location:
                            raise FetchPolicyError("redirect_missing_location", current.normalized_url)
                        current = normalize_redirect_target(
                            location,
                            current_url=current.normalized_url,
                            policy=policy,
                        )
                        redirect_chain.append(current.normalized_url)
                        continue
                    if status < 200 or status >= 300:
                        raise HTTPException(
                            status_code=502,
                            detail={
                                "reason": "upstream_http_error",
                                "http_status": status,
                                "normalized_url": current.normalized_url,
                                "resolved_ips": resolved_ips,
                                "used_ip": used_ip,
                                "redirect_chain": redirect_chain,
                            },
                        )
                    if not content_type_allowed(raw_content_type, policy):
                        raise HTTPException(
                            status_code=415,
                            detail={
                                "reason": "content_type_not_allowed",
                                "content_type": _content_type(raw_content_type),
                                "normalized_url": current.normalized_url,
                                "resolved_ips": resolved_ips,
                                "used_ip": used_ip,
                                "redirect_chain": redirect_chain,
                            },
                        )
                    body, truncated = await _read_limited_body(response, policy.max_response_bytes)
                    decoded = _decode_text(
                        body,
                        content_type=raw_content_type,
                        max_preview_chars=policy.max_preview_chars,
                    )
                    return FetcherFetchResponse(
                        normalized_url=current.normalized_url,
                        final_url=str(response.url),
                        scheme=current.scheme,
                        host=current.host,
                        port=current.port,
                        http_status=status,
                        content_type=_content_type(raw_content_type),
                        byte_count=len(body),
                        truncated=truncated,
                        redirect_chain=list(redirect_chain),
                        resolved_ips=resolved_ips,
                        used_ip=used_ip,
                        content_sha256=hashlib.sha256(body).hexdigest(),
                        text=decoded,
                    )
            except FetchPolicyError as exc:
                raise HTTPException(
                    status_code=_policy_status_code(exc.reason),
                    detail={
                        "reason": exc.reason,
                        "detail": exc.detail,
                        "normalized_url": current.normalized_url,
                        "scheme": current.scheme,
                        "host": current.host,
                        "port": current.port,
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
