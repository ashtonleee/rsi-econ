from dataclasses import dataclass
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TRUSTED_STATE_DIR = ROOT / "runtime" / "trusted_state"
DEFAULT_LOG_DIR = DEFAULT_TRUSTED_STATE_DIR / "logs"
DEFAULT_STATE_DIR = DEFAULT_TRUSTED_STATE_DIR / "state"
DEFAULT_CHECKPOINT_DIR = DEFAULT_TRUSTED_STATE_DIR / "checkpoints"
DEFAULT_BRIDGE_URL = "http://bridge:8000"
DEFAULT_LITELLM_URL = "http://litellm:4000"
DEFAULT_FETCHER_URL = "http://fetcher:8082"
DEFAULT_AGENT_URL = "http://agent:8001"
DEFAULT_AGENT_WORKSPACE_DIR = ROOT / "untrusted" / "agent_workspace"
DEFAULT_AGENT_RUNTIME_CODE_DIR = ROOT / "untrusted"
DEFAULT_SEED_BASELINE_DIR = ROOT / "trusted" / "recovery" / "seed_workspace_baseline"
DEFAULT_PUBLIC_PROBE_URL = "http://1.1.1.1"
DEFAULT_PROVIDER_PROBE_URL = "https://api.openai.com/v1/models"
DEFAULT_LLM_BUDGET_TOKEN_CAP = 100
DEFAULT_BUDGET_UNIT = "mock_tokens"
DEFAULT_WEB_ALLOWLIST_HOSTS = ("example.com",)
DEFAULT_FETCH_ALLOWED_CONTENT_TYPES = ("text/plain", "text/html")
DEFAULT_WEB_MAX_RESPONSE_BYTES = 8192
DEFAULT_WEB_MAX_PREVIEW_CHARS = 1024
DEFAULT_WEB_MAX_REDIRECTS = 3
DEFAULT_WEB_TIMEOUT_SECONDS = 5.0
DEFAULT_FETCH_USER_AGENT = "rsi-fetcher/0.1"


def _resolve_path(raw: str | None, default: Path) -> Path:
    if raw is None or raw.strip() == "":
        return default

    path = Path(raw)
    if path.is_absolute():
        return path
    return ROOT / path


def _split_csv(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None or raw.strip() == "":
        return default
    values = [item.strip().lower() for item in raw.split(",") if item.strip()]
    return tuple(values)


def _env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class BridgeSettings:
    service_name: str
    stage: str
    trusted_state_dir: Path
    log_dir: Path
    state_dir: Path
    checkpoint_dir: Path
    seed_baseline_dir: Path
    litellm_url: str
    fetcher_url: str
    agent_url: str
    llm_budget_token_cap: int
    budget_unit: str
    web_allowlist_hosts: tuple[str, ...]
    web_private_test_hosts: tuple[str, ...]
    web_allowed_content_types: tuple[str, ...]
    web_max_response_bytes: int
    web_max_preview_chars: int
    web_max_redirects: int
    web_timeout_seconds: float
    fetch_user_agent: str
    enable_debug_probes: bool


@dataclass(frozen=True)
class FetcherSettings:
    service_name: str
    stage: str
    fetcher_url: str
    allowlist_hosts: tuple[str, ...]
    private_test_hosts: tuple[str, ...]
    allowed_content_types: tuple[str, ...]
    max_response_bytes: int
    max_preview_chars: int
    max_redirects: int
    timeout_seconds: float
    user_agent: str
    enable_private_test_hosts: bool


@dataclass(frozen=True)
class AgentSettings:
    service_name: str
    stage: str
    bridge_url: str
    workspace_dir: Path
    runtime_code_dir: Path
    public_probe_url: str
    provider_probe_url: str


def bridge_settings() -> BridgeSettings:
    trusted_state_dir = _resolve_path(
        os.environ.get("RSI_TRUSTED_STATE_DIR"),
        DEFAULT_TRUSTED_STATE_DIR,
    )
    llm_budget_token_cap = int(
        os.environ.get(
            "RSI_LLM_BUDGET_TOKEN_CAP",
            str(DEFAULT_LLM_BUDGET_TOKEN_CAP),
        ).strip()
    )
    assert llm_budget_token_cap > 0, "RSI_LLM_BUDGET_TOKEN_CAP must be positive"
    web_max_response_bytes = int(
        os.environ.get(
            "RSI_WEB_MAX_RESPONSE_BYTES",
            str(DEFAULT_WEB_MAX_RESPONSE_BYTES),
        ).strip()
    )
    web_max_preview_chars = int(
        os.environ.get(
            "RSI_WEB_MAX_PREVIEW_CHARS",
            str(DEFAULT_WEB_MAX_PREVIEW_CHARS),
        ).strip()
    )
    web_max_redirects = int(
        os.environ.get(
            "RSI_WEB_MAX_REDIRECTS",
            str(DEFAULT_WEB_MAX_REDIRECTS),
        ).strip()
    )
    web_timeout_seconds = float(
        os.environ.get(
            "RSI_WEB_TIMEOUT_SECONDS",
            str(DEFAULT_WEB_TIMEOUT_SECONDS),
        ).strip()
    )
    assert web_max_response_bytes > 0
    assert web_max_preview_chars > 0
    assert web_max_redirects >= 0
    assert web_timeout_seconds > 0
    return BridgeSettings(
        service_name="bridge",
        stage="stage5_read_only_web",
        trusted_state_dir=trusted_state_dir,
        log_dir=_resolve_path(
            os.environ.get("RSI_BRIDGE_LOG_DIR"),
            trusted_state_dir / "logs",
        ),
        state_dir=_resolve_path(
            os.environ.get("RSI_BRIDGE_STATE_DIR"),
            trusted_state_dir / "state",
        ),
        checkpoint_dir=_resolve_path(
            os.environ.get("RSI_CHECKPOINT_DIR"),
            trusted_state_dir / "checkpoints",
        ),
        seed_baseline_dir=_resolve_path(
            os.environ.get("RSI_SEED_BASELINE_DIR"),
            DEFAULT_SEED_BASELINE_DIR,
        ),
        litellm_url=os.environ.get("RSI_LITELLM_URL", DEFAULT_LITELLM_URL).strip(),
        fetcher_url=os.environ.get("RSI_FETCHER_URL", DEFAULT_FETCHER_URL).strip(),
        agent_url=os.environ.get("RSI_AGENT_URL", DEFAULT_AGENT_URL).strip(),
        llm_budget_token_cap=llm_budget_token_cap,
        budget_unit=os.environ.get("RSI_BUDGET_UNIT", DEFAULT_BUDGET_UNIT).strip(),
        web_allowlist_hosts=_split_csv(
            os.environ.get("RSI_WEB_ALLOWLIST_HOSTS"),
            DEFAULT_WEB_ALLOWLIST_HOSTS,
        ),
        web_private_test_hosts=_split_csv(
            os.environ.get("RSI_FETCH_ALLOW_PRIVATE_TEST_HOSTS"),
            (),
        ),
        web_allowed_content_types=_split_csv(
            os.environ.get("RSI_FETCH_ALLOWED_CONTENT_TYPES"),
            DEFAULT_FETCH_ALLOWED_CONTENT_TYPES,
        ),
        web_max_response_bytes=web_max_response_bytes,
        web_max_preview_chars=web_max_preview_chars,
        web_max_redirects=web_max_redirects,
        web_timeout_seconds=web_timeout_seconds,
        fetch_user_agent=os.environ.get(
            "RSI_FETCH_USER_AGENT",
            DEFAULT_FETCH_USER_AGENT,
        ).strip(),
        enable_debug_probes=_env_flag("RSI_ENABLE_DEBUG_PROBES"),
    )


def fetcher_settings() -> FetcherSettings:
    web_max_response_bytes = int(
        os.environ.get(
            "RSI_WEB_MAX_RESPONSE_BYTES",
            str(DEFAULT_WEB_MAX_RESPONSE_BYTES),
        ).strip()
    )
    web_max_preview_chars = int(
        os.environ.get(
            "RSI_WEB_MAX_PREVIEW_CHARS",
            str(DEFAULT_WEB_MAX_PREVIEW_CHARS),
        ).strip()
    )
    web_max_redirects = int(
        os.environ.get(
            "RSI_WEB_MAX_REDIRECTS",
            str(DEFAULT_WEB_MAX_REDIRECTS),
        ).strip()
    )
    web_timeout_seconds = float(
        os.environ.get(
            "RSI_WEB_TIMEOUT_SECONDS",
            str(DEFAULT_WEB_TIMEOUT_SECONDS),
        ).strip()
    )
    return FetcherSettings(
        service_name="fetcher",
        stage="stage5_read_only_web",
        fetcher_url=os.environ.get("RSI_FETCHER_URL", DEFAULT_FETCHER_URL).strip(),
        allowlist_hosts=_split_csv(
            os.environ.get("RSI_WEB_ALLOWLIST_HOSTS"),
            DEFAULT_WEB_ALLOWLIST_HOSTS,
        ),
        private_test_hosts=_split_csv(
            os.environ.get("RSI_FETCH_ALLOW_PRIVATE_TEST_HOSTS"),
            (),
        ),
        allowed_content_types=_split_csv(
            os.environ.get("RSI_FETCH_ALLOWED_CONTENT_TYPES"),
            DEFAULT_FETCH_ALLOWED_CONTENT_TYPES,
        ),
        max_response_bytes=web_max_response_bytes,
        max_preview_chars=web_max_preview_chars,
        max_redirects=web_max_redirects,
        timeout_seconds=web_timeout_seconds,
        user_agent=os.environ.get(
            "RSI_FETCH_USER_AGENT",
            DEFAULT_FETCH_USER_AGENT,
        ).strip(),
        enable_private_test_hosts=_env_flag("RSI_FETCH_ENABLE_PRIVATE_TEST_HOSTS", default=True),
    )


def agent_settings() -> AgentSettings:
    bridge_url = os.environ.get("RSI_BRIDGE_URL", DEFAULT_BRIDGE_URL).strip()
    assert bridge_url, "RSI_BRIDGE_URL must not be empty"

    return AgentSettings(
        service_name="agent",
        stage="stage5_read_only_web",
        bridge_url=bridge_url,
        workspace_dir=_resolve_path(
            os.environ.get("RSI_AGENT_WORKSPACE_DIR"),
            DEFAULT_AGENT_WORKSPACE_DIR,
        ),
        runtime_code_dir=_resolve_path(
            os.environ.get("RSI_AGENT_RUNTIME_CODE_DIR"),
            DEFAULT_AGENT_RUNTIME_CODE_DIR,
        ),
        public_probe_url=os.environ.get(
            "RSI_PUBLIC_PROBE_URL",
            DEFAULT_PUBLIC_PROBE_URL,
        ).strip(),
        provider_probe_url=os.environ.get(
            "RSI_PROVIDER_PROBE_URL",
            DEFAULT_PROVIDER_PROBE_URL,
        ).strip(),
    )
