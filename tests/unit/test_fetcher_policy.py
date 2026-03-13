import pytest

from trusted.fetcher.policy import (
    FetchPolicy,
    FetchPolicyError,
    normalize_fetch_target,
    validate_resolved_ips,
)


def make_policy(**overrides) -> FetchPolicy:
    return FetchPolicy(
        allowlist_hosts=("example.com",),
        private_test_hosts=tuple(overrides.pop("private_test_hosts", ())),
        allowed_content_types=("text/plain", "text/html"),
        max_response_bytes=32768,
        max_preview_chars=1024,
        max_redirects=3,
        timeout_seconds=5.0,
        user_agent="rsi-fetcher-test/1.0",
        enable_private_test_hosts=overrides.pop("enable_private_test_hosts", False),
    )


@pytest.mark.parametrize(
    ("raw_url", "reason_fragment"),
    [
        ("ftp://example.com/data", "unsupported_scheme"),
        ("https://user@example.com/data", "userinfo_not_allowed"),
        ("https://example.com/data#frag", "fragment_not_allowed"),
        ("https://localhost/data", "blocked_hostname"),
        ("https://bridge/data", "blocked_hostname"),
        ("https://not-allowed.test/data", "host_not_allowlisted"),
        ("https://example.com:444/data", "port_not_allowed"),
    ],
)
def test_normalize_fetch_target_rejects_unsafe_urls(raw_url, reason_fragment):
    with pytest.raises(FetchPolicyError) as exc:
        normalize_fetch_target(raw_url, make_policy())

    assert reason_fragment in str(exc.value)


def test_normalize_fetch_target_accepts_allowlisted_default_port_url():
    target = normalize_fetch_target("https://Example.com/path?q=1", make_policy())

    assert target.normalized_url == "https://example.com/path?q=1"
    assert target.host == "example.com"
    assert target.port == 443
    assert target.scheme == "https"


def test_validate_resolved_ips_rejects_private_ranges_without_test_override():
    target = normalize_fetch_target("http://example.com/ok", make_policy())

    with pytest.raises(FetchPolicyError) as exc:
        validate_resolved_ips(target, ["172.18.0.5"], make_policy())

    assert "blocked_ip" in str(exc.value)


def test_validate_resolved_ips_allows_private_fixture_host_only_in_test_mode():
    policy = FetchPolicy(
        allowlist_hosts=("allowed.test",),
        private_test_hosts=("allowed.test",),
        allowed_content_types=("text/plain", "text/html"),
        max_response_bytes=32768,
        max_preview_chars=1024,
        max_redirects=3,
        timeout_seconds=5.0,
        user_agent="rsi-fetcher-test/1.0",
        enable_private_test_hosts=True,
    )
    target = normalize_fetch_target("http://allowed.test/ok", policy)

    resolved = validate_resolved_ips(target, ["172.19.0.12"], policy)

    assert resolved == ["172.19.0.12"]
