import pytest

from trusted.web.policy import WebPolicy, WebPolicyError


def make_policy(**overrides) -> WebPolicy:
    return WebPolicy(
        allowlist_hosts=tuple(overrides.pop("allowlist_hosts", ("allowed.test", "allowed-two.test"))),
        private_test_hosts=tuple(overrides.pop("private_test_hosts", ("allowed.test", "allowed-two.test"))),
        max_redirects=overrides.pop("max_redirects", 3),
        timeout_seconds=overrides.pop("timeout_seconds", 5.0),
        allowed_content_types=tuple(overrides.pop("allowed_content_types", ("text/plain", "text/html"))),
        max_response_bytes=overrides.pop("max_response_bytes", 8192),
        max_preview_chars=overrides.pop("max_preview_chars", 1024),
        user_agent=overrides.pop("user_agent", "rsi-h2-test/1.0"),
        enable_private_test_hosts=overrides.pop("enable_private_test_hosts", True),
    )


def test_shared_mediation_binds_live_peer_to_approved_target():
    from trusted.web.mediation import approve_egress_target, enforce_connected_peer

    approved = approve_egress_target(
        "http://allowed.test/browser/rendered",
        policy=make_policy(),
        channel="top_level_navigation",
        resolved_ips=("172.18.0.10",),
    )

    assert approved.target.normalized_url == "http://allowed.test/browser/rendered"
    assert tuple(approved.approved_ips) == ("172.18.0.10",)
    assert enforce_connected_peer(approved, "172.18.0.10") == "172.18.0.10"


def test_shared_mediation_rejects_peer_mismatch_and_missing_peer():
    from trusted.web.mediation import approve_egress_target, enforce_connected_peer

    approved = approve_egress_target(
        "http://allowed.test/browser/rendered",
        policy=make_policy(),
        channel="top_level_navigation",
        resolved_ips=("172.18.0.10",),
    )

    with pytest.raises(WebPolicyError, match="peer_binding_missing"):
        enforce_connected_peer(approved, None)

    with pytest.raises(WebPolicyError, match="peer_binding_mismatch"):
        enforce_connected_peer(approved, "172.18.0.22")


def test_shared_mediation_revalidates_redirect_hops():
    from trusted.web.mediation import approve_egress_redirect

    policy = make_policy()
    hop = approve_egress_redirect(
        "http://allowed-two.test/browser/cross-origin-target",
        current_url="http://allowed.test/browser/redirect-allowed-two",
        policy=policy,
        channel="redirect",
        resolved_ips=("172.18.0.11",),
    )
    assert hop.target.normalized_url == "http://allowed-two.test/browser/cross-origin-target"

    with pytest.raises(WebPolicyError, match="host_not_allowlisted"):
        approve_egress_redirect(
            "http://blocked.test/browser/rendered",
            current_url="http://allowed.test/browser/redirect-allowed-two",
            policy=policy,
            channel="redirect",
            resolved_ips=("172.18.0.99",),
        )


def test_browser_channel_policy_denies_frozen_h2_non_read_only_channels():
    from trusted.web.mediation import channel_disposition

    deny_only_channels = {
        "form_submission",
        "websocket",
        "eventsource",
        "send_beacon",
        "popup",
        "download",
        "upload",
        "prefetch_preconnect",
        "external_protocol",
        "worker",
        "subresource",
    }
    both_channels = {
        "top_level_navigation",
        "redirect",
        "frame_navigation",
        "fetch_xhr",
    }

    for channel in sorted(deny_only_channels):
        assert channel_disposition(channel) == "deny_and_log"
    for channel in sorted(both_channels):
        assert channel_disposition(channel) == "classify_and_deny_when_unapproved"
