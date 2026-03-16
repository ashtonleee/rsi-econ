from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]


def load_compose():
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="ascii"))


def test_stage_docs_exist():
    required = [
        "README.md",
        "REPO_LAYOUT.md",
        "TASK_GRAPH.md",
        "ACCEPTANCE_TEST_MATRIX.md",
    ]

    for name in required:
        assert (ROOT / name).exists(), name


def test_compose_preserves_trusted_untrusted_boundary():
    compose = load_compose()
    services = compose["services"]

    bridge = services["bridge"]
    agent = services["agent"]
    litellm = services["litellm"]
    fetcher = services["fetcher"]
    browser = services["browser"]
    egress = services["egress"]

    bridge_volumes = bridge.get("volumes", [])
    agent_volumes = agent.get("volumes", [])
    litellm_volumes = litellm.get("volumes", [])
    fetcher_volumes = fetcher.get("volumes", [])
    browser_volumes = browser.get("volumes", [])
    egress_volumes = egress.get("volumes", [])

    assert any(
        "./runtime/trusted_state:" in entry
        or "${RSI_TRUSTED_STATE_HOST_DIR:-./runtime/trusted_state}:" in entry
        for entry in bridge_volumes
    )
    assert any(
        "./untrusted/agent_workspace:" in entry
        or "${RSI_AGENT_WORKSPACE_HOST_DIR:-./untrusted/agent_workspace}:" in entry
        for entry in agent_volumes
    )
    assert all("runtime/trusted_state" not in entry for entry in agent_volumes)
    assert not litellm_volumes
    assert not fetcher_volumes
    assert not browser_volumes
    assert not egress_volumes

    assert not agent.get("ports"), agent.get("ports")
    assert agent.get("read_only") is True
    assert "/tmp" in agent.get("tmpfs", [])
    assert "healthcheck" in bridge
    assert "healthcheck" in agent
    assert "healthcheck" in litellm
    assert "healthcheck" in fetcher
    assert "healthcheck" in browser
    assert "healthcheck" in egress
    assert "/tmp" in browser.get("tmpfs", [])
    browser_security = browser.get("security_opt", [])
    assert "no-new-privileges:true" in browser_security
    assert any(item.startswith("seccomp=") for item in browser_security)


def test_compose_uses_expected_network_topology():
    compose = load_compose()
    agent_net = compose["networks"]["agent_net"]
    trusted_net = compose["networks"]["trusted_net"]
    egress_net = compose["networks"]["egress_net"]

    assert agent_net["internal"] is True
    assert trusted_net["internal"] is True
    assert egress_net.get("internal") is not True
    assert compose["services"]["agent"]["networks"] == ["agent_net"]
    assert compose["services"]["bridge"]["networks"] == ["agent_net", "trusted_net"]
    assert compose["services"]["litellm"]["networks"] == ["trusted_net", "egress_net"]
    assert compose["services"]["fetcher"]["networks"] == ["trusted_net"]
    assert compose["services"]["browser"]["networks"] == ["trusted_net"]
    assert compose["services"]["egress"]["networks"] == ["trusted_net", "egress_net"]


def test_agent_service_has_no_obvious_secret_env_entries():
    compose = load_compose()
    environment = compose["services"]["agent"].get("environment", {})

    if isinstance(environment, list):
        keys = [item.split("=", 1)[0] for item in environment]
    else:
        keys = list(environment.keys())

    banned_fragments = ("KEY", "SECRET", "TOKEN")
    assert all(fragment not in key.upper() for key in keys for fragment in banned_fragments)


def test_litellm_is_not_reachable_by_agent_topology():
    compose = load_compose()
    agent_networks = set(compose["services"]["agent"]["networks"])
    litellm_networks = set(compose["services"]["litellm"]["networks"])
    fetcher_networks = set(compose["services"]["fetcher"]["networks"])
    browser_networks = set(compose["services"]["browser"]["networks"])
    egress_networks = set(compose["services"]["egress"]["networks"])

    assert "trusted_net" not in agent_networks
    assert agent_networks.isdisjoint(litellm_networks)
    assert agent_networks.isdisjoint(fetcher_networks)
    assert agent_networks.isdisjoint(browser_networks)
    assert agent_networks.isdisjoint(egress_networks)


@pytest.mark.fast
def test_agent_service_static_hardening_guards_are_present():
    compose = load_compose()
    agent = compose["services"]["agent"]

    assert agent.get("privileged") is not True
    assert agent.get("network_mode") != "host"
    assert not agent.get("cap_add")
    assert not agent.get("devices")
    assert not agent.get("extra_hosts")

    volumes = agent.get("volumes", [])
    assert len(volumes) == 1
    assert any(
        "./untrusted/agent_workspace:" in entry
        or "${RSI_AGENT_WORKSPACE_HOST_DIR:-./untrusted/agent_workspace}:" in entry
        for entry in volumes
    )
    assert all("docker.sock" not in entry.lower() for entry in volumes)
