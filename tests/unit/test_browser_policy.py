import pytest

from shared.schemas import BrowserFollowLink
from trusted.fetcher.app import build_policy as build_fetch_policy
from trusted.web.policy import (
    WebPolicy,
    WebPolicyError,
    normalize_web_target,
    normalize_web_redirect_target,
)


def test_fetcher_and_browser_reuse_same_shared_policy(monkeypatch):
    monkeypatch.setenv("RSI_WEB_ALLOWLIST_HOSTS", "allowed.test")
    monkeypatch.setenv("RSI_FETCH_ALLOW_PRIVATE_TEST_HOSTS", "allowed.test")

    from trusted.browser.app import build_policy as build_browser_policy

    fetch_policy = build_fetch_policy()
    browser_policy = build_browser_policy()

    assert type(fetch_policy) is type(browser_policy)
    assert fetch_policy.allowlist_hosts == ("allowed.test",)
    assert browser_policy.allowlist_hosts == ("allowed.test",)
    assert fetch_policy.max_redirects == browser_policy.max_redirects


def test_shared_policy_blocks_hosts_redirects_and_private_ips():
    policy = build_fetch_policy()

    with pytest.raises(WebPolicyError, match="host_not_allowlisted"):
        normalize_web_target("http://blocked.test/", policy)

    start = normalize_web_target("http://example.com/", policy)
    with pytest.raises(WebPolicyError, match="host_not_allowlisted"):
        normalize_web_redirect_target(
            "http://blocked.test/redirected",
            current_url=start.normalized_url,
            policy=policy,
        )

    with pytest.raises(WebPolicyError, match="blocked_hostname"):
        normalize_web_target("http://localhost/secret", policy)


def test_public_workflow_policy_allows_public_hosts_but_still_blocks_private_targets():
    policy = WebPolicy(
        allowlist_hosts=("allowed.test",),
        private_test_hosts=(),
        max_redirects=3,
        timeout_seconds=5.0,
        allow_public_hosts=True,
    )

    target = normalize_web_target("https://public.example/workflow", policy)
    assert target.host == "public.example"

    with pytest.raises(WebPolicyError, match="blocked_hostname"):
        normalize_web_target("http://localhost/internal", policy)

    with pytest.raises(WebPolicyError, match="unsupported_scheme"):
        normalize_web_target("javascript:alert(1)", policy)


def test_browser_policy_denies_popup_download_and_local_file():
    from trusted.browser.policy import (
        browser_channel_violation,
        classify_browser_channel,
        download_violation,
        popup_violation,
        select_followable_link,
        top_level_navigation_violation,
        validate_browser_target,
    )

    policy = build_fetch_policy()

    with pytest.raises(WebPolicyError, match="unsupported_scheme"):
        validate_browser_target("file:///etc/passwd", policy)

    popup_error = popup_violation("http://allowed.test/popup")
    assert popup_error.reason == "popup_not_allowed"

    download_error = download_violation(
        "http://allowed.test/download.bin",
        suggested_filename="download.bin",
    )
    assert download_error.reason == "download_not_allowed"

    top_level_error = top_level_navigation_violation("http://allowed.test/other")
    assert top_level_error.reason == "top_level_navigation_not_allowed"

    assert (
        classify_browser_channel(
            resource_type="document",
            is_navigation_request=True,
            is_main_frame=True,
            headers={},
            top_level_started=False,
        )
        == "top_level_navigation"
    )
    assert (
        classify_browser_channel(
            resource_type="document",
            is_navigation_request=True,
            is_main_frame=True,
            headers={},
            top_level_started=True,
        )
        == "redirect"
    )
    assert (
        classify_browser_channel(
            resource_type="document",
            is_navigation_request=True,
            is_main_frame=False,
            headers={},
            top_level_started=True,
        )
        == "frame_navigation"
    )
    assert (
        classify_browser_channel(
            resource_type="fetch",
            is_navigation_request=False,
            is_main_frame=False,
            headers={},
            top_level_started=True,
        )
        == "fetch_xhr"
    )
    assert (
        classify_browser_channel(
            resource_type="other",
            is_navigation_request=False,
            is_main_frame=False,
            headers={"Purpose": "prefetch"},
            top_level_started=True,
        )
        == "prefetch_preconnect"
    )

    channel_error = browser_channel_violation("websocket", "ws://blocked.test/socket")
    assert channel_error.reason == "websocket_not_allowed"

    matched = select_followable_link(
        "http://allowed.test/browser/follow-target",
        [
            BrowserFollowLink(
                text="same origin",
                target_url="http://allowed.test/browser/follow-target",
                same_origin=True,
            )
        ],
    )
    assert matched.target_url == "http://allowed.test/browser/follow-target"

    with pytest.raises(WebPolicyError, match="requested_target_not_present"):
        select_followable_link(
            "http://allowed.test/browser/not-linked",
            [
                BrowserFollowLink(
                    text="same origin",
                    target_url="http://allowed.test/browser/follow-target",
                    same_origin=True,
                )
            ],
        )
