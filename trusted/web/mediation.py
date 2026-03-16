from dataclasses import dataclass
from typing import Literal

from trusted.web.policy import (
    NormalizedWebTarget,
    WebPolicy,
    WebPolicyError,
    normalize_web_redirect_target,
    normalize_web_target,
    resolve_target_ips,
    validate_resolved_ips,
)


ChannelDisposition = Literal["deny_and_log", "classify_and_deny_when_unapproved"]


CHANNEL_POLICIES: dict[str, ChannelDisposition] = {
    "top_level_navigation": "classify_and_deny_when_unapproved",
    "redirect": "classify_and_deny_when_unapproved",
    "frame_navigation": "classify_and_deny_when_unapproved",
    "fetch_xhr": "classify_and_deny_when_unapproved",
    "form_submission": "deny_and_log",
    "websocket": "deny_and_log",
    "eventsource": "deny_and_log",
    "send_beacon": "deny_and_log",
    "popup": "deny_and_log",
    "download": "deny_and_log",
    "upload": "deny_and_log",
    "prefetch_preconnect": "deny_and_log",
    "external_protocol": "deny_and_log",
    "worker": "deny_and_log",
    "subresource": "deny_and_log",
}


@dataclass(frozen=True)
class ApprovedEgressTarget:
    channel: str
    target: NormalizedWebTarget
    approved_ips: tuple[str, ...]


def channel_disposition(channel: str) -> ChannelDisposition:
    try:
        return CHANNEL_POLICIES[channel]
    except KeyError as exc:
        raise ValueError(f"unsupported mediation channel: {channel}") from exc


def approve_egress_target(
    raw_url: str,
    *,
    policy: WebPolicy,
    channel: str,
    resolved_ips: tuple[str, ...] | list[str] | None = None,
) -> ApprovedEgressTarget:
    channel_disposition(channel)
    target = normalize_web_target(raw_url, policy)
    approved_ips = validate_resolved_ips(
        target,
        resolved_ips if resolved_ips is not None else resolve_target_ips(target),
        policy,
    )
    return ApprovedEgressTarget(
        channel=channel,
        target=target,
        approved_ips=tuple(approved_ips),
    )


def approve_egress_redirect(
    location: str,
    *,
    current_url: str,
    policy: WebPolicy,
    channel: str,
    resolved_ips: tuple[str, ...] | list[str] | None = None,
) -> ApprovedEgressTarget:
    channel_disposition(channel)
    target = normalize_web_redirect_target(location, current_url=current_url, policy=policy)
    approved_ips = validate_resolved_ips(
        target,
        resolved_ips if resolved_ips is not None else resolve_target_ips(target),
        policy,
    )
    return ApprovedEgressTarget(
        channel=channel,
        target=target,
        approved_ips=tuple(approved_ips),
    )


def enforce_connected_peer(approved: ApprovedEgressTarget, actual_peer_ip: str | None) -> str:
    if actual_peer_ip is None or not actual_peer_ip.strip():
        raise WebPolicyError(
            "peer_binding_missing",
            f"{approved.target.host}: peer_ip_missing",
        )
    peer = actual_peer_ip.strip()
    if peer not in approved.approved_ips:
        raise WebPolicyError(
            "peer_binding_mismatch",
            f"{approved.target.host}: {peer} not in {list(approved.approved_ips)}",
        )
    return peer


def channel_record(
    *,
    channel: str,
    requested_url: str,
    disposition: Literal["allowed", "denied", "classified"],
    reason: str,
    approved: ApprovedEgressTarget | None = None,
    actual_peer_ip: str | None = None,
    top_level: bool = False,
    navigation: bool = False,
    dialed_ip: str | None = None,
    enforcement_stage: Literal["unknown", "pre_connect", "post_connect"] = "unknown",
    request_forwarded: bool = False,
) -> dict[str, object]:
    if approved is None:
        normalized_url = requested_url
        host = ""
        approved_ips: list[str] = []
    else:
        normalized_url = approved.target.normalized_url
        host = approved.target.host
        approved_ips = list(approved.approved_ips)
    return {
        "channel": channel,
        "requested_url": requested_url,
        "normalized_url": normalized_url,
        "host": host,
        "approved_ips": approved_ips,
        "actual_peer_ip": actual_peer_ip,
        "dialed_ip": dialed_ip,
        "disposition": disposition,
        "reason": reason,
        "top_level": top_level,
        "navigation": navigation,
        "enforcement_stage": enforcement_stage,
        "request_forwarded": request_forwarded,
    }
