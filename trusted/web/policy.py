from dataclasses import dataclass
import ipaddress
import socket
from typing import Iterable
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit


DEFAULT_BLOCKED_HOSTS = {
    "localhost",
    "127.0.0.1",
    "::1",
    "bridge",
    "agent",
    "litellm",
    "fetcher",
    "browser",
    "host.docker.internal",
    "metadata.google.internal",
}
BLOCKED_HOST_SUFFIXES = (".internal", ".local", ".localhost")


class WebPolicyError(ValueError):
    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class WebPolicy:
    allowlist_hosts: tuple[str, ...]
    private_test_hosts: tuple[str, ...]
    max_redirects: int
    timeout_seconds: float
    allow_public_hosts: bool = False
    allowed_content_types: tuple[str, ...] = ()
    max_response_bytes: int = 0
    max_preview_chars: int = 0
    user_agent: str = ""
    enable_private_test_hosts: bool = False


@dataclass(frozen=True)
class NormalizedWebTarget:
    original_url: str
    normalized_url: str
    scheme: str
    host: str
    port: int
    path_and_query: str


def _default_port_for(scheme: str) -> int:
    if scheme == "http":
        return 80
    if scheme == "https":
        return 443
    raise WebPolicyError("unsupported_scheme", scheme)


def _is_blocked_hostname(host: str) -> bool:
    host = host.lower()
    if host in DEFAULT_BLOCKED_HOSTS:
        return True
    return host.endswith(BLOCKED_HOST_SUFFIXES)


def web_policy_status_code(reason: str) -> int:
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


def normalize_web_target(raw_url: str, policy: WebPolicy) -> NormalizedWebTarget:
    raw_url = raw_url.strip()
    if not raw_url:
        raise WebPolicyError("empty_url", "URL must not be empty")

    parts = urlsplit(raw_url)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise WebPolicyError("unsupported_scheme", raw_url)
    if parts.username or parts.password:
        raise WebPolicyError("userinfo_not_allowed", raw_url)
    if parts.fragment:
        raise WebPolicyError("fragment_not_allowed", raw_url)
    if not parts.hostname:
        raise WebPolicyError("missing_hostname", raw_url)

    host = parts.hostname.lower()
    if _is_blocked_hostname(host):
        raise WebPolicyError("blocked_hostname", host)
    if not policy.allow_public_hosts and host not in policy.allowlist_hosts:
        raise WebPolicyError("host_not_allowlisted", host)

    default_port = _default_port_for(scheme)
    if parts.port is not None and parts.port != default_port:
        raise WebPolicyError("port_not_allowed", f"{host}:{parts.port}")
    port = parts.port or default_port
    path = parts.path or "/"
    path_and_query = path
    if parts.query:
        path_and_query = f"{path}?{parts.query}"

    normalized = urlunsplit(
        SplitResult(
            scheme=scheme,
            netloc=host,
            path=path,
            query=parts.query,
            fragment="",
        )
    )
    return NormalizedWebTarget(
        original_url=raw_url,
        normalized_url=normalized,
        scheme=scheme,
        host=host,
        port=port,
        path_and_query=path_and_query,
    )


def resolve_target_ips(target: NormalizedWebTarget) -> list[str]:
    try:
        records = socket.getaddrinfo(
            target.host,
            target.port,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise WebPolicyError("dns_resolution_failed", f"{target.host}: {exc}") from exc

    ips: list[str] = []
    for family, _, _, _, sockaddr in records:
        if family not in {socket.AF_INET, socket.AF_INET6}:
            continue
        ip = sockaddr[0]
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise WebPolicyError("dns_resolution_failed", f"{target.host}: no_ip_records")
    return ips


def validate_resolved_ips(
    target: NormalizedWebTarget,
    resolved_ips: Iterable[str],
    policy: WebPolicy,
) -> list[str]:
    ips = [ip for ip in resolved_ips if ip]
    if not ips:
        raise WebPolicyError("dns_resolution_failed", f"{target.host}: no_ip_records")

    allow_private = (
        policy.enable_private_test_hosts and target.host in policy.private_test_hosts
    )
    validated: list[str] = []
    for raw_ip in ips:
        ip = ipaddress.ip_address(raw_ip)
        if not allow_private and (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise WebPolicyError("blocked_ip", f"{target.host} -> {raw_ip}")
        validated.append(raw_ip)
    return validated


def normalize_web_redirect_target(
    location: str,
    *,
    current_url: str,
    policy: WebPolicy,
) -> NormalizedWebTarget:
    return normalize_web_target(urljoin(current_url, location), policy)
