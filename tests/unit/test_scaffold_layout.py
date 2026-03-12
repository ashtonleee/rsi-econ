from pathlib import Path

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

    bridge_volumes = bridge.get("volumes", [])
    agent_volumes = agent.get("volumes", [])
    litellm_volumes = litellm.get("volumes", [])

    assert any("./runtime/trusted_state:" in entry for entry in bridge_volumes)
    assert any("./untrusted/agent_workspace:" in entry for entry in agent_volumes)
    assert all("runtime/trusted_state" not in entry for entry in agent_volumes)
    assert not litellm_volumes

    assert not agent.get("ports"), agent.get("ports")
    assert "healthcheck" in bridge
    assert "healthcheck" in agent
    assert "healthcheck" in litellm


def test_compose_uses_two_internal_networks():
    compose = load_compose()
    agent_net = compose["networks"]["agent_net"]
    trusted_net = compose["networks"]["trusted_net"]

    assert agent_net["internal"] is True
    assert trusted_net["internal"] is True
    assert compose["services"]["agent"]["networks"] == ["agent_net"]
    assert compose["services"]["bridge"]["networks"] == ["agent_net", "trusted_net"]
    assert compose["services"]["litellm"]["networks"] == ["trusted_net"]


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

    assert "trusted_net" not in agent_networks
    assert agent_networks.isdisjoint(litellm_networks)
