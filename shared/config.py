from dataclasses import dataclass
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TRUSTED_STATE_DIR = ROOT / "runtime" / "trusted_state"
DEFAULT_LOG_DIR = DEFAULT_TRUSTED_STATE_DIR / "logs"
DEFAULT_BRIDGE_URL = "http://bridge:8000"
DEFAULT_LITELLM_URL = "http://litellm:4000"
DEFAULT_AGENT_URL = "http://agent:8001"
DEFAULT_PUBLIC_PROBE_URL = "http://1.1.1.1"
DEFAULT_PROVIDER_PROBE_URL = "https://api.openai.com/v1/models"


def _resolve_path(raw: str | None, default: Path) -> Path:
    if raw is None or raw.strip() == "":
        return default

    path = Path(raw)
    if path.is_absolute():
        return path
    return ROOT / path


@dataclass(frozen=True)
class BridgeSettings:
    service_name: str
    stage: str
    trusted_state_dir: Path
    log_dir: Path
    litellm_url: str
    agent_url: str


@dataclass(frozen=True)
class AgentSettings:
    service_name: str
    stage: str
    bridge_url: str
    public_probe_url: str
    provider_probe_url: str


def bridge_settings() -> BridgeSettings:
    trusted_state_dir = _resolve_path(
        os.environ.get("RSI_TRUSTED_STATE_DIR"),
        DEFAULT_TRUSTED_STATE_DIR,
    )
    return BridgeSettings(
        service_name="bridge",
        stage="stage1_hard_boundary",
        trusted_state_dir=trusted_state_dir,
        log_dir=_resolve_path(
            os.environ.get("RSI_BRIDGE_LOG_DIR"),
            trusted_state_dir / "logs",
        ),
        litellm_url=os.environ.get("RSI_LITELLM_URL", DEFAULT_LITELLM_URL).strip(),
        agent_url=os.environ.get("RSI_AGENT_URL", DEFAULT_AGENT_URL).strip(),
    )


def agent_settings() -> AgentSettings:
    bridge_url = os.environ.get("RSI_BRIDGE_URL", DEFAULT_BRIDGE_URL).strip()
    assert bridge_url, "RSI_BRIDGE_URL must not be empty"

    return AgentSettings(
        service_name="agent",
        stage="stage1_hard_boundary",
        bridge_url=bridge_url,
        public_probe_url=os.environ.get(
            "RSI_PUBLIC_PROBE_URL",
            DEFAULT_PUBLIC_PROBE_URL,
        ).strip(),
        provider_probe_url=os.environ.get(
            "RSI_PROVIDER_PROBE_URL",
            DEFAULT_PROVIDER_PROBE_URL,
        ).strip(),
    )
