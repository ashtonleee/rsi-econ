from __future__ import annotations

import base64
import ipaddress
import socket
import ssl
from typing import Any

import aiohttp
from fastapi import FastAPI, HTTPException

from shared.config import egress_settings
from shared.schemas import EgressFetchRequest, EgressFetchResponse, HealthReport
from trusted.web.mediation import ApprovedEgressTarget, approve_egress_target
from trusted.web.policy import WebPolicy, WebPolicyError, web_policy_status_code


def build_policy() -> WebPolicy:
    settings = egress_settings()
    return WebPolicy(
        allowlist_hosts=settings.allowlist_hosts,
        private_test_hosts=settings.private_test_hosts,
        max_redirects=settings.max_redirects,
        timeout_seconds=settings.timeout_seconds,
        allowed_content_types=settings.allowed_content_types,
        user_agent=settings.user_agent,
        enable_private_test_hosts=settings.enable_private_test_hosts,
    )


class PinnedResolver(aiohttp.abc.AbstractResolver):
    def __init__(self, *, host: str, ips: tuple[str, ...], port: int):
        self.host = host
        self.ips = ips
        self.port = port

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_UNSPEC):
        if host != self.host:
            raise OSError(f"unexpected resolve target: {host}")
        records = []
        for raw_ip in self.ips:
            ip = ipaddress.ip_address(raw_ip)
            records.append(
                {
                    "hostname": host,
                    "host": raw_ip,
                    "port": port or self.port,
                    "family": socket.AF_INET6 if ip.version == 6 else socket.AF_INET,
                    "proto": 0,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
        return records

    async def close(self) -> None:
        return None


def _peer_ip(response: aiohttp.ClientResponse) -> str | None:
    connection = response.connection
    if connection is None or connection.transport is None:
        return None
    peer = connection.transport.get_extra_info("peername")
    if not peer:
        return None
    if isinstance(peer, tuple):
        return str(peer[0])
    return str(peer)


async def _read_limited_body(response: aiohttp.ClientResponse, limit: int) -> bytes:
    if limit <= 0:
        return await response.read()
    chunks = bytearray()
    async for chunk in response.content.iter_chunked(65536):
        remaining = limit + 1 - len(chunks)
        if remaining <= 0:
            break
        chunks.extend(chunk[:remaining])
        if len(chunks) > limit:
            break
    return bytes(chunks[:limit])


def _with_test_overrides(
    approved: ApprovedEgressTarget,
    *,
    overrides: dict[str, tuple[str, ...]],
) -> ApprovedEgressTarget:
    ips = overrides.get(approved.target.host)
    if not ips:
        return approved
    return ApprovedEgressTarget(
        channel=approved.channel,
        target=approved.target,
        approved_ips=tuple(ips),
    )


def _error_detail(
    *,
    approved: ApprovedEgressTarget | None,
    reason: str,
    detail: str,
    actual_peer_ip: str | None,
    dialed_ip: str | None,
    request_forwarded: bool,
    enforcement_stage: str,
    http_status: int | None = None,
) -> dict[str, Any]:
    if approved is None:
        return {
            "reason": reason,
            "detail": detail,
            "normalized_url": "",
            "scheme": "",
            "host": "",
            "port": 0,
            "approved_ips": [],
            "actual_peer_ip": actual_peer_ip,
            "dialed_ip": dialed_ip,
            "request_forwarded": request_forwarded,
            "enforcement_stage": enforcement_stage,
            "http_status": http_status,
        }
    return {
        "reason": reason,
        "detail": detail,
        "normalized_url": approved.target.normalized_url,
        "scheme": approved.target.scheme,
        "host": approved.target.host,
        "port": approved.target.port,
        "approved_ips": list(approved.approved_ips),
        "actual_peer_ip": actual_peer_ip,
        "dialed_ip": dialed_ip,
        "request_forwarded": request_forwarded,
        "enforcement_stage": enforcement_stage,
        "http_status": http_status,
    }


CHANNELS_ALLOWING_POST = {"consequential_action"}
ALLOWED_METHODS = {"GET", "POST"}


async def execute_fetch(payload: EgressFetchRequest) -> EgressFetchResponse:
    method = payload.method.upper()
    if method not in ALLOWED_METHODS:
        raise HTTPException(
            status_code=405,
            detail={"reason": "method_not_allowed", "detail": f"unsupported method: {method}"},
        )
    if method != "GET" and payload.channel not in CHANNELS_ALLOWING_POST:
        raise HTTPException(
            status_code=405,
            detail={
                "reason": "method_not_allowed",
                "detail": f"{method} not allowed on channel {payload.channel}",
            },
        )

    policy = app.state.policy
    settings = app.state.settings
    approved: ApprovedEgressTarget | None = None
    try:
        approved = approve_egress_target(
            payload.url,
            policy=policy,
            channel=payload.channel,
        )
        approved = _with_test_overrides(approved, overrides=settings.test_ip_overrides)
    except WebPolicyError as exc:
        raise HTTPException(
            status_code=web_policy_status_code(exc.reason),
            detail=_error_detail(
                approved=None,
                reason=exc.reason,
                detail=exc.detail,
                actual_peer_ip=None,
                dialed_ip=None,
                request_forwarded=False,
                enforcement_stage="pre_connect",
            ),
        ) from exc

    headers = {
        key: value
        for key, value in payload.headers.items()
        if not key.lower().startswith("x-rsi-egress-")
    }
    headers.setdefault("User-Agent", settings.user_agent)
    headers.setdefault("Accept-Encoding", "identity")

    # Prepare request body for POST
    request_body: bytes | None = None
    if method == "POST":
        if payload.request_body_base64:
            request_body = base64.b64decode(payload.request_body_base64)
        else:
            request_body = b""
        if payload.request_content_type:
            headers["Content-Type"] = payload.request_content_type

    try:
        connect_errors: list[str] = []
        for dialed_ip in approved.approved_ips:
            ssl_context = ssl.create_default_context() if approved.target.scheme == "https" else None
            resolver = PinnedResolver(
                host=approved.target.host,
                ips=(dialed_ip,),
                port=approved.target.port,
            )
            connector = aiohttp.TCPConnector(
                resolver=resolver,
                use_dns_cache=False,
                ttl_dns_cache=0,
                ssl=ssl_context,
            )
            try:
                async with aiohttp.ClientSession(
                    connector=connector,
                    auto_decompress=False,
                    trust_env=False,
                ) as session:
                    request_kwargs: dict = {
                        "headers": headers,
                        "allow_redirects": False,
                        "timeout": aiohttp.ClientTimeout(total=policy.timeout_seconds),
                    }
                    if method == "POST" and request_body is not None:
                        request_kwargs["data"] = request_body
                    async with session.request(
                        method,
                        approved.target.normalized_url,
                        **request_kwargs,
                    ) as response:
                        actual_peer_ip = _peer_ip(response) or dialed_ip
                        if actual_peer_ip != dialed_ip:
                            raise HTTPException(
                                status_code=403,
                                detail=_error_detail(
                                    approved=approved,
                                    reason="peer_binding_mismatch",
                                    detail=f"{actual_peer_ip} != {dialed_ip}",
                                    actual_peer_ip=actual_peer_ip,
                                    dialed_ip=dialed_ip,
                                    request_forwarded=True,
                                    enforcement_stage="post_connect",
                                    http_status=response.status,
                                ),
                            )
                        body = await _read_limited_body(response, payload.max_body_bytes)
                        return EgressFetchResponse(
                            normalized_url=approved.target.normalized_url,
                            scheme=approved.target.scheme,
                            host=approved.target.host,
                            port=approved.target.port,
                            channel=payload.channel,
                            approved_ips=list(approved.approved_ips),
                            actual_peer_ip=actual_peer_ip,
                            dialed_ip=dialed_ip,
                            request_forwarded=True,
                            enforcement_stage="pre_connect",
                            http_status=response.status,
                            headers={key.lower(): value for key, value in response.headers.items()},
                            body_base64=base64.b64encode(body).decode("ascii"),
                        )
            except HTTPException:
                raise
            except (aiohttp.ClientConnectorError, aiohttp.ClientSSLError, aiohttp.ClientConnectionError, TimeoutError) as exc:
                connect_errors.append(f"{dialed_ip}: {exc}")
            finally:
                await connector.close()
        raise HTTPException(
            status_code=502,
            detail=_error_detail(
                approved=approved,
                reason="connect_failed",
                detail="; ".join(connect_errors) if connect_errors else "no_approved_peer_reachable",
                actual_peer_ip=None,
                dialed_ip=None,
                request_forwarded=False,
                enforcement_stage="pre_connect",
            ),
        )
    except HTTPException:
        raise


def startup_checks(app: FastAPI):
    app.state.settings = egress_settings()
    app.state.policy = build_policy()


app = FastAPI(title="trusted-egress")


@app.on_event("startup")
async def startup_event():
    startup_checks(app)


@app.get("/healthz", response_model=HealthReport)
async def healthz() -> HealthReport:
    settings = app.state.settings
    return HealthReport(
        service=settings.service_name,
        status="ok",
        stage=settings.stage,
        details={
            "allowlist_hosts": list(settings.allowlist_hosts),
            "private_test_hosts": list(settings.private_test_hosts),
            "timeout_seconds": settings.timeout_seconds,
            "max_redirects": settings.max_redirects,
            "test_ip_overrides": {key: list(value) for key, value in settings.test_ip_overrides.items()},
        },
    )


@app.post("/internal/fetch", response_model=EgressFetchResponse)
async def fetch(payload: EgressFetchRequest) -> EgressFetchResponse:
    return await execute_fetch(payload)
